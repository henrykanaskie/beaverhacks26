import hashlib
import os
import shutil
import stat
import subprocess
from pathlib import Path

from fastapi import APIRouter, HTTPException
from llama_index.core import Document
from llama_index.core.node_parser import CodeSplitter
from pydantic import BaseModel

from constants import EXCLUDE_DIRS, INCLUDE_EXTENSIONS, LANGUAGE_MAP
from db import collection_exists, get_or_create_collection


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


@router.post("/index")
async def index_repo(request: IndexRequest):
    validate_url(request.repo_url)
    repo_id = get_repo_id(request.repo_url)

    # Already indexed — return immediately without re-processing
    if collection_exists(repo_id):
        collection = get_or_create_collection(repo_id)
        return {"repo_id": repo_id, "status": "already_indexed", "chunk_count": collection.count()}

    # Remaining steps implemented by ING-02 through ING-07
    raise HTTPException(status_code=501, detail="Indexing not yet implemented")


@router.get("/status/{repo_id}")
def get_status(repo_id: str):
    if not collection_exists(repo_id):
        raise HTTPException(status_code=404, detail="Repo not found")
    return {"repo_id": repo_id, "indexed": True}
