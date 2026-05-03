import json
import os
from datetime import datetime, timezone
from threading import Lock
from urllib.parse import urlparse

from fastapi import APIRouter
from db import get_client

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


def _sync_missing_from_chroma() -> None:
    """Backfill projects registry from existing non-empty Chroma collections.

    Some repos may exist in Chroma from older runs but not in projects.json.
    Those should still show up in Recent projects.
    """
    with _LOCK:
        entries = _read()
        known = {e.get("repo_id") for e in entries}
        now = datetime.now(timezone.utc).isoformat()
        changed = False

        try:
            collections = get_client().list_collections()
        except Exception:
            return

        for c in collections:
            name = c if isinstance(c, str) else getattr(c, "name", None)
            if not name or name in known:
                continue
            try:
                col = get_client().get_collection(name=name)
                n = int(col.count() or 0)
            except Exception:
                continue
            if n <= 0:
                continue
            if not os.path.exists(f"deps/{name}_files.json"):
                continue
            # URL is unknown for legacy entries; UI falls back to repo_id label.
            entries.insert(0, {
                "repo_id": name,
                "repo_url": "",
                "indexed_at": now,
                "chunk_count": n,
            })
            known.add(name)
            changed = True

        if changed:
            _write_atomic(entries)


def list_projects() -> list[dict]:
    _sync_missing_from_chroma()
    return sorted(_read(), key=lambda e: e.get("indexed_at", ""), reverse=True)


def _project_display_name(entry: dict) -> str:
    repo_url = str(entry.get("repo_url") or "").strip()
    if repo_url:
        try:
            p = urlparse(repo_url)
            path = (p.path or "").strip("/")
            if path:
                return path.removesuffix(".git")
        except Exception:
            pass
        return repo_url
    return "Unlabeled indexed repo"


@router.delete("/projects/{repo_id}")
def delete_project(repo_id: str):
    with _LOCK:
        entries = [e for e in _read() if e.get("repo_id") != repo_id]
        _write_atomic(entries)
    # Remove Chroma collection and deps files if present
    try:
        get_client().delete_collection(name=repo_id)
    except Exception:
        pass
    for suffix in ("_files.json", "_deps.json", "_system.json"):
        path = f"deps/{repo_id}{suffix}"
        try:
            os.remove(path)
        except OSError:
            pass
    return {"status": "deleted", "repo_id": repo_id}


@router.get("/projects")
def get_projects():
    projects = list_projects()
    enriched: list[dict] = []
    for p in projects:
        item = dict(p)
        item["display_name"] = _project_display_name(item)
        item["has_repo_url"] = bool((item.get("repo_url") or "").strip())
        enriched.append(item)
    return {"projects": enriched}
