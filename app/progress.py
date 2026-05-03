from threading import Lock

from pydantic import BaseModel


class ProgressInfo(BaseModel):
    stage: str = "queued"
    percent: int = 0
    current_file: str | None = None
    files_processed: int = 0
    total_files: int = 0
    message: str = ""
    error: str | None = None


INDEXING_PROGRESS: dict[str, ProgressInfo] = {}
PROGRESS_LOCK = Lock()


def set_progress(repo_id: str, **kwargs) -> None:
    with PROGRESS_LOCK:
        cur = INDEXING_PROGRESS.get(repo_id)
        data = cur.model_dump() if cur else {}
        data.update(kwargs)
        INDEXING_PROGRESS[repo_id] = ProgressInfo(**data)


def get_progress(repo_id: str) -> ProgressInfo | None:
    with PROGRESS_LOCK:
        return INDEXING_PROGRESS.get(repo_id)


def clear_progress(repo_id: str) -> None:
    with PROGRESS_LOCK:
        INDEXING_PROGRESS.pop(repo_id, None)
