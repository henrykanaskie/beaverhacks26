"""Reverse dependency graph builder.

Runs at ingestion time so /trace can be O(1) JSON lookup.
"""
from __future__ import annotations

import ast
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

_JS_LIKE = {"javascript", "typescript", "tsx"}
_RESOLUTION_EXTS = (".py", ".js", ".ts", ".tsx", ".jsx")


def build_dependency_graph(
    files: list[dict], clone_path: str, repo_id: str, max_workers: int | None = None
) -> dict:
    """Build reverse dependency graph and persist to deps/{repo_id}_deps.json.

    Returns: { file_rel_path: [files_that_import_it] }

    Per-file `_extract_imports` runs concurrently across a small thread pool —
    file I/O dominates and AST parsing is small. Resolution and reverse-graph
    construction stay in the main thread.
    """
    file_set = {f["rel_path"] for f in files}
    workers = max_workers or min(8, (os.cpu_count() or 4))

    def _extract_for(file_info: dict) -> tuple[str, list[str]]:
        return file_info["rel_path"], _extract_imports(file_info["path"], file_info["language"])

    forward: dict[str, list[str]] = {}
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="deps") as ex:
        for rel_path, imports in ex.map(_extract_for, files):
            forward[rel_path] = _resolve_imports(imports, rel_path, file_set)

    reverse: dict[str, list[str]] = {f: [] for f in file_set}
    for src, targets in forward.items():
        for target in targets:
            if target in reverse and src not in reverse[target]:
                reverse[target].append(src)

    os.makedirs("deps", exist_ok=True)
    with open(f"deps/{repo_id}_deps.json", "w", encoding="utf-8") as fh:
        json.dump(reverse, fh)
    return reverse


def _extract_imports(file_path: str, language: str) -> list[str]:
    """Return raw import strings from a single source file."""
    try:
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
    except Exception:
        return []

    imports: list[str] = []

    if language == "python":
        try:
            tree = ast.parse(content)
        except SyntaxError:
            return []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                # level > 0 means relative import: "from . import x" or "from ..pkg import y"
                if node.level and node.level > 0:
                    prefix = "." * node.level
                    imports.append(prefix + (node.module or ""))
                elif node.module:
                    imports.append(node.module)
    elif language in _JS_LIKE:
        imports.extend(re.findall(r"""from\s+['"]([^'"]+)['"]""", content))
        imports.extend(re.findall(r"""require\(\s*['"]([^'"]+)['"]\s*\)""", content))
        imports.extend(re.findall(r"""import\s*\(\s*['"]([^'"]+)['"]\s*\)""", content))

    return imports


def _resolve_imports(
    imports: list[str], source_rel_path: str, file_set: set[str]
) -> list[str]:
    """Resolve raw import strings to repo-relative file paths.

    Drops imports that don't resolve to a file inside the repo (third-party packages).
    """
    resolved: list[str] = []
    seen: set[str] = set()
    source_dir = Path(source_rel_path).parent

    for imp in imports:
        for candidate in _generate_candidates(imp, source_dir):
            if candidate in file_set and candidate != source_rel_path:
                if candidate not in seen:
                    seen.add(candidate)
                    resolved.append(candidate)
                break
    return resolved


def _generate_candidates(imp: str, source_dir: Path) -> list[str]:
    """Generate possible repo-relative file paths a raw import could refer to."""
    candidates: list[str] = []

    if imp.startswith("."):
        # Python relative ("from ..pkg import x") OR JS relative ("./foo", "../bar/baz")
        leading_dots = len(imp) - len(imp.lstrip("."))
        remainder = imp[leading_dots:].lstrip("/")

        if "/" in imp or imp.startswith("./") or imp.startswith("../"):
            # JS/TS style — leading dots are path components
            base = source_dir
            up = leading_dots - 1 if leading_dots >= 1 else 0
            for _ in range(up):
                base = base.parent
            target = base / remainder if remainder else base
        else:
            # Python style — each dot is one package level up; level=1 means same dir
            base = source_dir
            for _ in range(leading_dots - 1):
                base = base.parent
            target = base / remainder.replace(".", "/") if remainder else base

        candidates.extend(_with_extensions(target))
    else:
        # Absolute import — try as path from repo root
        as_path = Path(imp.replace(".", "/"))
        candidates.extend(_with_extensions(as_path))

    return candidates


def _with_extensions(target: Path) -> list[str]:
    """Expand a path stem into all candidate file names: foo.py, foo/__init__.py, etc."""
    base = target.as_posix().lstrip("/")
    if not base or base == ".":
        return []
    out: list[str] = []
    for ext in _RESOLUTION_EXTS:
        out.append(base + ext)
        out.append(f"{base}/__init__{ext}")
        out.append(f"{base}/index{ext}")  # JS convention
    return out
