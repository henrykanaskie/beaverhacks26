"""Path helpers and repo-wide literals (GLOBAL CONTEXT)."""
from __future__ import annotations

import hashlib

INCLUDE_EXTENSIONS = {
    ".py",
    ".js",
    ".ts",
    ".tsx",
    ".jsx",
    ".go",
    ".rs",
    ".java",
    ".cpp",
    ".cs",
}
EXCLUDE_DIRS = {
    "node_modules",
    ".venv",
    "venv",
    "dist",
    "build",
    "__pycache__",
    ".git",
    "vendor",
    "site-packages",
    ".next",
    "coverage",
    ".mypy_cache",
}
LANGUAGE_MAP = {
    ".py": "python",
    ".js": "javascript",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".jsx": "javascript",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".cpp": "cpp",
    ".cs": "c_sharp",
}


def get_repo_id(repo_url: str) -> str:
    return hashlib.md5(repo_url.encode()).hexdigest()


def dep_graph_path(repo_id: str) -> str:
    return f"deps/{repo_id}_deps.json"


def files_list_path(repo_id: str) -> str:
    return f"deps/{repo_id}_files.json"


def clone_dir(repo_id: str) -> str:
    return f"clones/{repo_id}"
