import json
import os

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from db import collection_exists

router = APIRouter()


class TraceRequest(BaseModel):
    repo_id: str
    file_path: str


@router.post("/trace")
def trace_file(request: TraceRequest):
    if not collection_exists(request.repo_id):
        raise HTTPException(status_code=404, detail="Repo not indexed")

    dep_path = f"deps/{request.repo_id}_deps.json"
    if not os.path.exists(dep_path):
        raise HTTPException(
            status_code=404, detail="Dependency graph not found for this repo"
        )

    with open(dep_path, "r", encoding="utf-8") as f:
        graph = json.load(f)

    file_path = request.file_path.lstrip("/")

    if file_path not in graph:
        raise HTTPException(
            status_code=404,
            detail=f"File '{file_path}' not found in dependency graph",
        )

    return {
        "file_path": file_path,
        "affected_files": graph[file_path],
    }
