"""GET /files/{repo_id} and GET /file/{repo_id} endpoints.

File content is reconstructed from the chunks already stored in Chroma at
ingestion time — we do not keep the clone on disk.
"""
from __future__ import annotations

import json
import os

from fastapi import APIRouter, HTTPException

from db import collection_exists, get_or_create_collection

router = APIRouter()


@router.get("/files/{repo_id}")
def list_files(repo_id: str):
    if not collection_exists(repo_id):
        raise HTTPException(status_code=404, detail="Repo not indexed")

    list_path = f"deps/{repo_id}_files.json"
    if not os.path.exists(list_path):
        raise HTTPException(status_code=404, detail="File list not found — re-index the repo")

    with open(list_path, "r", encoding="utf-8") as f:
        return {"repo_id": repo_id, "files": json.load(f)}


@router.get("/file/{repo_id}")
def get_file_content(repo_id: str, path: str):
    """Reconstruct file content from sorted Chroma chunks.

    `path` is a query parameter, e.g. /file/{repo_id}?path=auth/login.py
    """
    if not collection_exists(repo_id):
        raise HTTPException(status_code=404, detail="Repo not indexed")

    collection = get_or_create_collection(repo_id)

    # collection.get(where=...) does not require an embedding, unlike .query
    results = collection.get(
        where={"file_path": path},
        limit=10000,
        include=["documents", "metadatas"],
    )
    docs = results.get("documents") or []
    metas = results.get("metadatas") or []

    if not docs:
        raise HTTPException(status_code=404, detail="File not found")

    # Chunks overlap (chunk_lines_overlap=5), so naive concat duplicates lines.
    # Stitch by line number using each chunk's start_line metadata.
    lines_by_num: dict[int, str] = {}
    for doc, meta in zip(docs, metas):
        start = int(meta.get("start_line", 1))
        for i, line in enumerate(doc.splitlines()):
            line_num = start + i
            lines_by_num.setdefault(line_num, line)

    content = "\n".join(lines_by_num[n] for n in sorted(lines_by_num))
    language = metas[0].get("language", "unknown") if metas else "unknown"
    return {"file_path": path, "language": language, "content": content}
