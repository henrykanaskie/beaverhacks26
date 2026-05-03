import hashlib
import json
import os
import shutil
import stat
import subprocess
import time
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, HTTPException
from llama_index.core import Document
from llama_index.core.node_parser import CodeSplitter
from pydantic import BaseModel

from constants import EXCLUDE_DIRS, INCLUDE_EXTENSIONS, LANGUAGE_MAP
from db import collection_exists, get_client, get_or_create_collection
from deps_builder import build_dependency_graph
from progress import (
    INDEXING_PROGRESS,
    PROGRESS_LOCK,
    get_progress,
    set_progress,
)
from projects import upsert_project


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
    timeout = int(os.getenv("CLONE_TIMEOUT_SECONDS", "600"))

    if os.path.exists(clone_path):
        _rmtree(clone_path)

    # GIT_TERMINAL_PROMPT=0 ensures private repos fail fast on auth instead of
    # blocking on a credential prompt until the timeout fires.
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_LFS_SKIP_SMUDGE": "1"}

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


def _get_embed_model():
    """Return the code embedding model (Salesforce CodeT5+, runs locally).

    Falls back to all-MiniLM-L6-v2 if CodeT5+ fails to load.
    """
    from transformers import AutoModel, AutoTokenizer
    import torch

    try:
        _tokenizer = AutoTokenizer.from_pretrained("Salesforce/codet5p-110m-embedding", trust_remote_code=True)
        _model = AutoModel.from_pretrained("Salesforce/codet5p-110m-embedding", trust_remote_code=True)
        _model.eval()

        class _CodeT5Adapter:
            def get_text_embedding_batch(self, texts: list[str]) -> list[list[float]]:
                inputs = _tokenizer(texts, padding=True, truncation=True, max_length=512, return_tensors="pt")
                with torch.no_grad():
                    outputs = _model(**inputs)
                return outputs.tolist()

        print("Embed: using Salesforce/codet5p-110m-embedding")
        return _CodeT5Adapter(), "codet5p"
    except Exception as e:
        print(f"Warning: CodeT5+ failed to load ({e!r}), falling back to all-MiniLM-L6-v2")

    from chromadb.utils.embedding_functions import SentenceTransformerEmbeddingFunction
    _st_fn = SentenceTransformerEmbeddingFunction(model_name="all-MiniLM-L6-v2")

    class _FallbackAdapter:
        def get_text_embedding_batch(self, texts: list[str]) -> list[list[float]]:
            return _st_fn(texts)

    print("Embed: using fallback all-MiniLM-L6-v2")
    return _FallbackAdapter(), "local"


def embed_and_store(chunks: list[dict], repo_id: str, _progress_repo: str | None = None) -> int:
    """Embed chunks in batches and write to the repo's Chroma collection. Returns total stored."""
    if not chunks:
        return 0

    # CodeSplitter's overlap windows can emit byte-identical chunks for the same
    # file, which collide on _chunk_id and trigger Chroma's unique-id constraint.
    # Identical chunks carry no extra signal, so drop the dupes.
    seen_ids: set[str] = set()
    deduped: list[dict] = []
    for c in chunks:
        cid = _chunk_id(repo_id, c)
        if cid in seen_ids:
            continue
        seen_ids.add(cid)
        deduped.append(c)
    chunks = deduped

    embed_model, provider = _get_embed_model()
    collection = get_or_create_collection(repo_id)
    total_stored = 0

    for i in range(0, len(chunks), _EMBED_BATCH_SIZE):
        batch = chunks[i:i + _EMBED_BATCH_SIZE]
        texts = [c["text"] for c in batch]

        embeddings = None
        max_attempts = 6 if provider == "nomic" else 2
        for attempt in range(max_attempts):
            try:
                embeddings = embed_model.get_text_embedding_batch(texts)
                break
            except Exception as e:
                if attempt == max_attempts - 1:
                    raise
                wait = min(90.0, 3.0 * (2 ** attempt))
                err_s = str(e).lower()
                if "503" in err_s or "429" in err_s or "retry" in err_s:
                    wait = min(120.0, wait * 1.5)
                print(
                    f"Warning: embed batch retry {attempt + 1}/{max_attempts} "
                    f"after {e!r}; sleeping {wait:.0f}s"
                )
                time.sleep(wait)

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

        if _progress_repo:
            pct = 40 + int(55 * (i + len(batch)) / max(1, len(chunks)))
            set_progress(
                _progress_repo,
                percent=min(95, pct),
                current_file=batch[0]["file_path"],
                files_processed=total_stored,
                total_files=len(chunks),
            )

    return total_stored


def _run_pipeline(repo_id: str, repo_url: str) -> None:
    clone_path: str | None = None
    try:
        set_progress(repo_id, stage="cloning", percent=5, message="Cloning repository...", error=None)
        clone_path = clone_repo(repo_url, repo_id)

        set_progress(repo_id, stage="collecting", percent=18, message="Scanning files...")
        files = collect_files(clone_path)
        set_progress(repo_id, total_files=len(files), percent=25)

        set_progress(repo_id, stage="chunking", percent=30, message="Chunking source...")
        chunks = chunk_files(files)

        set_progress(repo_id, stage="embedding", percent=40, message="Embedding chunks...")
        chunk_count = embed_and_store(chunks, repo_id, _progress_repo=repo_id)

        set_progress(repo_id, stage="finalizing", percent=96, message="Building dep graph...")
        build_dependency_graph(files, clone_path, repo_id)
        os.makedirs("deps", exist_ok=True)
        with open(f"deps/{repo_id}_files.json", "w", encoding="utf-8") as fh:
            json.dump([f["rel_path"] for f in files], fh)

        upsert_project(repo_id, repo_url, chunk_count)
        set_progress(repo_id, stage="done", percent=100, message=f"{chunk_count} chunks")
    except HTTPException as e:
        try:
            get_client().delete_collection(repo_id)
        except Exception:
            pass
        set_progress(repo_id, stage="error", error=str(e.detail))
    except Exception as e:
        try:
            get_client().delete_collection(repo_id)
        except Exception:
            pass
        set_progress(repo_id, stage="error", error=str(e))
    finally:
        if clone_path:
            cleanup_clone(repo_id)


@router.post("/index")
async def index_repo(request: IndexRequest, background_tasks: BackgroundTasks):
    validate_url(request.repo_url)
    repo_id = get_repo_id(request.repo_url)

    # Already indexed — return immediately unless collection is empty (failed/interrupted run).
    if collection_exists(repo_id):
        collection = get_or_create_collection(repo_id)
        n = collection.count()
        if n > 0:
            return {
                "repo_id": repo_id,
                "status": "already_indexed",
                "chunk_count": n,
            }
        try:
            get_client().delete_collection(repo_id)
        except Exception:
            pass

    # Concurrency guard: if a run is already in flight for this repo, just say so.
    with PROGRESS_LOCK:
        cur = INDEXING_PROGRESS.get(repo_id)
        if cur and cur.stage not in ("done", "error"):
            return {"repo_id": repo_id, "status": "indexing"}

    set_progress(repo_id, stage="queued", percent=0, error=None, message="Queued",
                 current_file=None, files_processed=0, total_files=0)
    background_tasks.add_task(_run_pipeline, repo_id, request.repo_url)
    return {"repo_id": repo_id, "status": "indexing"}


@router.get("/index/progress/{repo_id}")
def get_index_progress(repo_id: str):
    p = get_progress(repo_id)
    if p:
        return p.model_dump()
    if collection_exists(repo_id) and get_or_create_collection(repo_id).count() > 0:
        return {
            "stage": "done",
            "percent": 100,
            "message": "already indexed",
            "current_file": None,
            "files_processed": 0,
            "total_files": 0,
            "error": None,
        }
    raise HTTPException(status_code=404, detail="Unknown repo_id")


@router.get("/status/{repo_id}")
def get_status(repo_id: str):
    if not collection_exists(repo_id):
        raise HTTPException(status_code=404, detail="Repo not found")
    return {"repo_id": repo_id, "indexed": True}
