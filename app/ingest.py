import hashlib
import json
import os
import shutil
import stat
import subprocess
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException
from llama_index.core import Document
from llama_index.core.node_parser import CodeSplitter
from llama_index.embeddings.nomic import NomicEmbedding
from pydantic import BaseModel

from constants import EXCLUDE_DIRS, INCLUDE_EXTENSIONS, LANGUAGE_MAP
from db import collection_exists, get_client, get_or_create_collection
from deps_builder import build_dependency_graph


def _force_remove_readonly(func, path, _exc_info):
    """rmtree onerror handler — clears the read-only bit and retries.

    Required on Windows where git marks files inside .git/objects/pack/ as
    read-only, causing PermissionError on shutil.rmtree. Harmless on POSIX.
    """
    try:
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except Exception:
        pass


def _rmtree(path: str) -> None:
    shutil.rmtree(path, onerror=_force_remove_readonly)

router = APIRouter()


class IndexRequest(BaseModel):
    repo_url: str


def validate_url(repo_url: str) -> None:
    """Raise HTTPException if URL is not a valid public GitHub/GitLab URL."""
    if "@" in repo_url:
        raise HTTPException(status_code=400, detail="Auth required — only public repos supported")
    if not (repo_url.startswith("https://github.com/") or repo_url.startswith("https://gitlab.com/")):
        raise HTTPException(status_code=400, detail="Invalid URL — only public GitHub/GitLab URLs accepted")


def get_repo_id(repo_url: str) -> str:
    return hashlib.md5(repo_url.encode()).hexdigest()


def clone_repo(repo_url: str, repo_id: str) -> str:
    """
    Clone repo to clones/{repo_id}/ with depth=1.
    Returns the clone path. Raises HTTPException on failure.
    Always call cleanup_clone(repo_id) in a finally block after calling this.
    """
    clone_path = f"clones/{repo_id}"
    timeout = int(os.getenv("CLONE_TIMEOUT_SECONDS", "60"))

    if os.path.exists(clone_path):
        _rmtree(clone_path)

    # GIT_TERMINAL_PROMPT=0 ensures private repos fail fast on auth instead of
    # blocking on a credential prompt until the timeout fires.
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}

    try:
        result = subprocess.run(
            ["git", "clone", "--depth", "1", repo_url, clone_path],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
    except subprocess.TimeoutExpired:
        _rmtree(clone_path)
        raise HTTPException(status_code=408, detail=f"Clone timed out after {timeout}s")
    except FileNotFoundError:
        raise HTTPException(status_code=500, detail="git executable not found on PATH")

    if result.returncode != 0:
        _rmtree(clone_path)
        stderr = (result.stderr or "").strip()
        auth_markers = (
            "Authentication",
            "could not read Username",
            "terminal prompts disabled",
            "Repository not found",
        )
        if any(marker in stderr for marker in auth_markers):
            raise HTTPException(status_code=400, detail="Auth required — only public repos supported")
        raise HTTPException(status_code=500, detail=f"Clone failed: {stderr}")

    return clone_path


def cleanup_clone(repo_id: str) -> None:
    """Delete the clone directory. Always call this in a finally block."""
    clone_path = f"clones/{repo_id}"
    if os.path.exists(clone_path):
        _rmtree(clone_path)


def collect_files(clone_path: str) -> list[dict]:
    """
    Walk clone_path and return list of { path, rel_path, language }.
    rel_path is relative to clone_path with no leading slash, forward-slash separated.
    Raises HTTPException 400 if file count exceeds MAX_FILES_PER_REPO.
    """
    max_files = int(os.getenv("MAX_FILES_PER_REPO", "2000"))
    root = Path(clone_path)
    collected: list[dict] = []

    for file_path in root.rglob("*"):
        if not file_path.is_file():
            continue
        if any(part in EXCLUDE_DIRS for part in file_path.parts):
            continue
        if file_path.suffix not in INCLUDE_EXTENSIONS:
            continue
        rel_path = file_path.relative_to(root).as_posix()
        collected.append({
            "path": str(file_path),
            "rel_path": rel_path,
            "language": LANGUAGE_MAP[file_path.suffix],
        })

    if len(collected) > max_files:
        raise HTTPException(
            status_code=400,
            detail=f"Repo too large — {len(collected)} files exceeds limit of {max_files}",
        )
    return collected


def _compute_line_range(content: str, chunk_text: str, fallback_end: int) -> tuple[int, int]:
    """Locate chunk_text inside content and return (start_line, end_line), 1-indexed inclusive.

    Falls back to (1, fallback_end) if the chunk cannot be located (e.g. whitespace
    drift). Beats trusting CodeSplitter metadata, which doesn't populate line numbers.
    """
    offset = content.find(chunk_text)
    if offset < 0:
        return 1, fallback_end
    start_line = content.count("\n", 0, offset) + 1
    end_line = start_line + chunk_text.count("\n")
    return start_line, end_line


def chunk_files(files: list[dict]) -> list[dict]:
    """
    Chunk all collected files using AST-aware splitting.
    Returns list of { text, file_path, language, start_line, end_line }.
    Files that fail to parse are skipped with a warning, not crashed on.
    """
    chunks: list[dict] = []
    for file_info in files:
        try:
            with open(file_info["path"], "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
            if not content.strip():
                continue

            splitter = CodeSplitter(
                language=file_info["language"],
                chunk_lines=40,
                chunk_lines_overlap=5,
                max_chars=1500,
            )
            doc = Document(text=content)
            nodes = splitter.get_nodes_from_documents([doc])

            total_lines = len(content.splitlines())
            for node in nodes:
                start_line, end_line = _compute_line_range(content, node.text, total_lines)
                chunks.append({
                    "text": node.text,
                    "file_path": file_info["rel_path"],
                    "language": file_info["language"],
                    "start_line": int(start_line),
                    "end_line": int(end_line),
                })
        except Exception as e:
            print(f"Warning: skipping {file_info['rel_path']}: {e}")
            continue
    return chunks


_EMBED_BATCH_SIZE = 128


def _chunk_id(repo_id: str, chunk: dict) -> str:
    """Deterministic, collision-resistant chunk ID.

    Spec format is `{repo_id}_{file_path}_{start_line}`, but multiple chunks can
    legitimately share (file_path, start_line) — overlap windows, or fallback line
    numbers. We append an 8-char hash of the chunk body so re-runs produce the
    same ID while collisions don't break Chroma's unique-id constraint.
    """
    body = f"{chunk['file_path']}|{chunk['start_line']}|{chunk['end_line']}|{chunk['text']}"
    suffix = hashlib.md5(body.encode()).hexdigest()[:8]
    return f"{repo_id}_{chunk['file_path']}_{chunk['start_line']}_{suffix}"


def embed_and_store(chunks: list[dict], repo_id: str) -> int:
    """Embed chunks in batches via Nomic and write to the repo's Chroma collection.

    Uses nomic-embed-code-v1 with task_type=search_document. Returns total stored.
    """
    if not chunks:
        return 0

    embed_model = NomicEmbedding(
        api_key=os.getenv("NOMIC_API_KEY"),
        model_name="nomic-embed-code-v1",
        task_type="search_document",
    )
    collection = get_or_create_collection(repo_id)
    total_stored = 0

    for i in range(0, len(chunks), _EMBED_BATCH_SIZE):
        batch = chunks[i:i + _EMBED_BATCH_SIZE]
        texts = [c["text"] for c in batch]

        embeddings = None
        for attempt in range(3):
            try:
                embeddings = embed_model.get_text_embedding_batch(texts)
                break
            except Exception:
                if attempt == 2:
                    raise
                time.sleep(2 ** attempt)

        ids = [_chunk_id(repo_id, c) for c in batch]
        metadatas = [{
            "file_path": c["file_path"],
            "language": c["language"],
            "start_line": int(c["start_line"]),
            "end_line": int(c["end_line"]),
            "content_type": "code",
        } for c in batch]

        collection.add(
            ids=ids,
            embeddings=embeddings,
            documents=texts,
            metadatas=metadatas,
        )
        total_stored += len(batch)

    return total_stored


@router.post("/index")
async def index_repo(request: IndexRequest):
    validate_url(request.repo_url)
    repo_id = get_repo_id(request.repo_url)

    # Already indexed — return immediately without re-processing
    if collection_exists(repo_id):
        collection = get_or_create_collection(repo_id)
        return {"repo_id": repo_id, "status": "already_indexed", "chunk_count": collection.count()}

    clone_path: str | None = None
    try:
        clone_path = clone_repo(request.repo_url, repo_id)
        files = collect_files(clone_path)
        chunks = chunk_files(files)
        build_dependency_graph(files, clone_path, repo_id)
        chunk_count = embed_and_store(chunks, repo_id)

        os.makedirs("deps", exist_ok=True)
        with open(f"deps/{repo_id}_files.json", "w", encoding="utf-8") as fh:
            json.dump([f["rel_path"] for f in files], fh)

        return {"repo_id": repo_id, "status": "indexed", "chunk_count": chunk_count}
    except HTTPException:
        # Roll back partial Chroma writes so the repo isn't half-indexed
        try:
            get_client().delete_collection(repo_id)
        except Exception:
            pass
        raise
    except Exception as e:
        try:
            get_client().delete_collection(repo_id)
        except Exception:
            pass
        raise HTTPException(status_code=500, detail=f"Indexing failed: {e}")
    finally:
        if clone_path:
            cleanup_clone(repo_id)


@router.get("/status/{repo_id}")
def get_status(repo_id: str):
    if not collection_exists(repo_id):
        raise HTTPException(status_code=404, detail="Repo not found")
    return {"repo_id": repo_id, "indexed": True}
