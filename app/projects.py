import json
import os
from datetime import datetime, timezone
from threading import Lock

from fastapi import APIRouter

REGISTRY_PATH = "deps/projects.json"
_LOCK = Lock()
router = APIRouter()


def _read() -> list[dict]:
    if not os.path.exists(REGISTRY_PATH):
        return []
    try:
        with open(REGISTRY_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def _write_atomic(data: list[dict]) -> None:
    os.makedirs("deps", exist_ok=True)
    tmp = REGISTRY_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, REGISTRY_PATH)


def upsert_project(repo_id: str, repo_url: str, chunk_count: int) -> None:
    with _LOCK:
        entries = [e for e in _read() if e.get("repo_id") != repo_id]
        entries.insert(0, {
            "repo_id": repo_id,
            "repo_url": repo_url,
            "indexed_at": datetime.now(timezone.utc).isoformat(),
            "chunk_count": chunk_count,
        })
        _write_atomic(entries)


def list_projects() -> list[dict]:
    return sorted(_read(), key=lambda e: e.get("indexed_at", ""), reverse=True)


@router.get("/projects")
def get_projects():
    return {"projects": list_projects()}
