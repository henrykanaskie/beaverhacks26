"""GET /files/{repo_id}, GET /file/{repo_id}, and GET /architecture/{repo_id} endpoints.

File content is reconstructed from the chunks already stored in Chroma at
ingestion time — we do not keep the clone on disk.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

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


_EXT_TO_LANGUAGE = {
    ".py": "python", ".js": "javascript", ".ts": "typescript",
    ".tsx": "tsx", ".jsx": "javascript", ".go": "go",
    ".rs": "rust", ".java": "java", ".cpp": "cpp", ".cs": "c_sharp",
}


def _count_folders(node: dict) -> int:
    """Recursively count folder nodes (including the given node if it's a folder)."""
    if node["type"] == "file":
        return 0
    return 1 + sum(_count_folders(c) for c in node.get("children", []))


@router.get("/architecture/{repo_id}")
def get_architecture(repo_id: str):
    """
    Return the physical folder/file tree of the repo.
    Edges represent containment: a folder contains files and subfolders.
    Built from the flat file list saved at ingestion time — no AST parsing.
    """
    if not collection_exists(repo_id):
        raise HTTPException(status_code=404, detail="Repo not indexed")

    files_path = f"deps/{repo_id}_files.json"
    if not os.path.exists(files_path):
        raise HTTPException(status_code=404, detail="File list not found — re-index the repo")

    with open(files_path, "r", encoding="utf-8") as f:
        all_files = json.load(f)

    root = {"id": "/", "name": "/", "type": "folder", "children": []}

    def get_or_create_folder(start: dict, folder_path: str) -> dict:
        """Walk down the tree, creating folder nodes as needed. Returns the target folder node."""
        parts = folder_path.split("/")
        current = start
        accumulated = ""
        for part in parts:
            accumulated = f"{accumulated}/{part}".lstrip("/")
            existing = next(
                (c for c in current["children"]
                 if c["type"] == "folder" and c["name"] == part),
                None,
            )
            if existing is None:
                existing = {"id": accumulated, "name": part, "type": "folder", "children": []}
                current["children"].append(existing)
            current = existing
        return current

    for file_path in sorted(all_files):
        parts = file_path.split("/")
        filename = parts[-1]
        ext = Path(filename).suffix
        language = _EXT_TO_LANGUAGE.get(ext, "unknown")
        file_node = {"id": file_path, "name": filename, "type": "file", "language": language}

        if len(parts) == 1:
            root["children"].append(file_node)
        else:
            folder_path = "/".join(parts[:-1])
            folder_node = get_or_create_folder(root, folder_path)
            folder_node["children"].append(file_node)

    def sort_children(node: dict) -> None:
        if node["type"] != "folder":
            return
        node["children"].sort(key=lambda c: (0 if c["type"] == "folder" else 1, c["name"]))
        for child in node["children"]:
            sort_children(child)

    sort_children(root)

    total_files = len(all_files)
    total_folders = _count_folders(root) - 1  # exclude the root itself

    return {
        "repo_id": repo_id,
        "tree": root,
        "total_files": total_files,
        "total_folders": total_folders,
    }
