"""Path helpers and repo-wide literals (GLOBAL CONTEXT)."""
from __future__ import annotations

import hashlib

INCLUDE_EXTENSIONS = {
    # Code
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
    # Docs & config
    ".md",
    ".txt",
    ".rst",
    ".yaml",
    ".yml",
    ".toml",
    ".ini",
    ".cfg",
    # Data (sampled, not fully indexed)
    ".json",
    ".csv",
    # Web
    ".html",
    ".css",
    # Shell / CI
    ".sh",
    ".bash",
    ".dockerfile",
}
EXCLUDE_EXTENSIONS = {
    # images
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".webp", ".bmp", ".tiff",
    # fonts
    ".ttf", ".woff", ".woff2", ".eot", ".otf",
    # compiled / binary
    ".pyc", ".pyo", ".pyd", ".so", ".dll", ".exe", ".bin", ".obj", ".o",
    ".class", ".jar", ".war", ".ear",
    # archives
    ".zip", ".tar", ".gz", ".bz2", ".xz", ".rar", ".7z",
    # media
    ".mp3", ".mp4", ".wav", ".ogg", ".avi", ".mov", ".mkv",
    # data / lock files unlikely to help LLM understand code
    ".lock", ".sum", ".snap",
    # misc binary
    ".pdf", ".doc", ".docx", ".xls", ".xlsx",
    ".db", ".sqlite", ".sqlite3",
    ".map",  # source maps are huge and useless as context
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
    ".md": "text",
    ".txt": "text",
    ".rst": "text",
    ".yaml": "text",
    ".yml": "text",
    ".toml": "text",
    ".ini": "text",
    ".cfg": "text",
    ".json": "text",
    ".csv": "text",
    ".html": "html",
    ".css": "css",
    ".sh": "bash",
    ".bash": "bash",
    ".dockerfile": "text",
}

# Extensions that support AST-aware chunking via CodeSplitter.
# Everything else uses raw text chunking.
AST_LANGUAGES = {
    "python", "javascript", "typescript", "tsx", "go",
    "rust", "java", "cpp", "c_sharp",
}

# Large data files get sampled rather than fully chunked
DATA_EXTENSIONS = {".csv", ".json"}
MAX_DATA_SAMPLE_CHARS = 3000


def get_repo_id(repo_url: str) -> str:
    return hashlib.md5(repo_url.encode()).hexdigest()


def dep_graph_path(repo_id: str) -> str:
    return f"deps/{repo_id}_deps.json"


def files_list_path(repo_id: str) -> str:
    return f"deps/{repo_id}_files.json"


def clone_dir(repo_id: str) -> str:
    return f"clones/{repo_id}"
