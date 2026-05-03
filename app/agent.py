"""Agentic codebase search — /agent endpoint.

The LLM calls tools (semantic_search, read_file, grep, list_files,
find_references, directory_tree) to explore the indexed repo. Multiple
tools in one step may run in parallel (ThreadPoolExecutor). Batched calls
use <tool_calls>[...]</tool_calls> JSON; single calls use XML tool_call tags.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Callable

log = logging.getLogger("agent")

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from db import collection_exists, get_or_create_collection
from files import build_file_tree, reconstruct_file
from llm import (
    Turn,
    extract_citations,
    get_agent_llm,
    get_embed_adapter,
    get_llm,
    get_smalltalk_llm,
    iter_deltas,
    ndjson_event,
)

router = APIRouter()

_NUDGE_USE_TOOLS_MESSAGE = (
    "You answered about THIS repo without calling tools first. "
    "You MUST explore before answering. Start with a <tool_calls> batch: "
    "directory_tree to see the structure, read_file for README.md, and read_file for the main entry point. "
    "Then read the key modules those files reference. Never invent filenames; "
    "only cite paths and lines shown in <observation> blocks after tools run."
)

MAX_ITERATIONS = 5
MAX_PARALLEL_TOOLS = 8
OUTPUT_CAP_CHARS = 3000

_cache_lock = threading.Lock()


def _files_json_mtime(repo_id: str) -> float:
    p = os.path.join("deps", f"{repo_id}_files.json")
    try:
        return os.path.getmtime(p)
    except OSError:
        return 0.0


_path_index_cache: dict[tuple[str, float], frozenset[str]] = {}
_path_index_lock = threading.Lock()
_CIT_LINE_BRACKET_RE = re.compile(r"\[([^\[\]]+):(\d+)\]")


def _indexed_path_aliases(path: str) -> set[str]:
    p = path.strip()
    out = {p, p.lstrip("./")}
    nop = os.path.normpath(p).replace("\\", "/")
    out.add(nop)
    out.discard("")
    return out


def _indexed_path_set(repo_id: str) -> frozenset[str]:
    mt = _files_json_mtime(repo_id)
    key = (repo_id, mt)
    with _path_index_lock:
        hit = _path_index_cache.get(key)
    if hit is not None:
        return hit
    fp = os.path.join("deps", f"{repo_id}_files.json")
    acc: set[str] = set()
    if os.path.exists(fp):
        with open(fp, "r", encoding="utf-8") as fh:
            paths = json.load(fh)
        for entry in paths:
            if isinstance(entry, str):
                acc.update(_indexed_path_aliases(entry))
    frozen = frozenset(acc)
    with _path_index_lock:
        while len(_path_index_cache) > 64:
            _path_index_cache.pop(next(iter(_path_index_cache)), None)
        _path_index_cache[key] = frozen
    return frozen


def _citation_path_is_indexed(path_ref: str, indexed: frozenset[str]) -> bool:
    return any(alias in indexed for alias in _indexed_path_aliases(path_ref))


def _sanitize_bracket_citations(answer: str, repo_id: str) -> str:
    """Strip [path:line] citations that reference paths not in deps/*_files.json."""
    indexed = _indexed_path_set(repo_id)
    if not indexed:
        return answer

    def drop_bad(m: re.Match[str]) -> str:
        fp = m.group(1).strip()
        if _citation_path_is_indexed(fp, indexed):
            return m.group(0)
        log.warning("[citation-drop] non-indexed path in model answer: %r", fp)
        return ""

    return _CIT_LINE_BRACKET_RE.sub(drop_bad, answer)


def _finalize_agent_answer(answer: str, repo_id: str) -> tuple[str, list[dict[str, Any]]]:
    tex = answer.strip()
    tex = _sanitize_bracket_citations(tex, repo_id)
    tex = re.sub(r"[ \t]{2,}", " ", tex)
    return tex, extract_citations(tex, [])


# ---------------------------------------------------------------------------
# Tool registry
# ---------------------------------------------------------------------------

@dataclass
class ToolDef:
    name: str
    description: str
    parameters: dict[str, Any]
    fn: Callable[..., str] = field(repr=False)


def _cap(text: str, limit: int = OUTPUT_CAP_CHARS) -> str:
    if len(text) <= limit:
        return text
    lines = text.splitlines()
    kept: list[str] = []
    total = 0
    for line in lines:
        if total + len(line) + 1 > limit:
            break
        kept.append(line)
        total += len(line) + 1
    remaining = len(lines) - len(kept)
    return "\n".join(kept) + f"\n… [truncated, {remaining} more lines]"


# -- Tool result cache -----------------------------------------------------
# Deterministic tools (directory_tree, list_files, read_file, find_references)
# always return the same result for the same args within a session.
# semantic_search and grep are also stable for the same indexed data.

_tool_cache: dict[str, str] = {}
_CACHEABLE_TOOLS = {"directory_tree", "list_files", "read_file", "find_references", "semantic_search", "grep"}


def _cache_key(name: str, args: dict[str, Any]) -> str:
    raw = json.dumps({"name": name, **args}, sort_keys=True)
    return hashlib.md5(raw.encode()).hexdigest()


def _run_tool_cached(name: str, args: dict[str, Any], tools: list[ToolDef]) -> str:
    """Execute a tool, returning a cached result if available."""
    if name in _CACHEABLE_TOOLS:
        key = _cache_key(name, args)
        with _cache_lock:
            cached = _tool_cache.get(key)
        if cached is not None:
            log.info("[tool-cache HIT] %s(%s)", name, args)
            return cached

    tool = next((t for t in tools if t.name == name), None)
    if tool is None:
        log.warning("[tool] unknown tool: %s", name)
        return f"ERROR: unknown tool '{name}'"
    try:
        result = tool.fn(**args)
    except Exception as e:
        log.error("[tool] %s(%s) raised: %s", name, args, e)
        return f"ERROR: {e}"

    log.info("[tool] %s(%s) → %d chars, preview: %s", name, args, len(result), result[:120])

    if name in _CACHEABLE_TOOLS:
        with _cache_lock:
            _tool_cache[_cache_key(name, args)] = result

    return result


def _run_tools_parallel(
    calls: list[tuple[str, dict[str, Any]]],
    tools: list[ToolDef],
) -> list[tuple[str, dict[str, Any], str]]:
    """Run independent tool calls in parallel; return [(name, args, result), ...] in original order."""

    if not calls:
        return []
    if len(calls) == 1:
        n, a = calls[0]
        return [(n, a, _run_tool_cached(n, a, tools))]

    n_workers = min(MAX_PARALLEL_TOOLS, len(calls))
    pending: dict[Future[Any], int] = {}
    with ThreadPoolExecutor(max_workers=n_workers) as pool:
        for i, (name, args) in enumerate(calls):
            fut = pool.submit(_run_tool_cached, name, args, tools)
            pending[fut] = i
        idx_to_result: dict[int, str] = {}
        for fut in as_completed(pending):
            idx_to_result[pending[fut]] = fut.result()
    return [(calls[i][0], calls[i][1], idx_to_result[i]) for i in range(len(calls))]


# -- Tool implementations --------------------------------------------------

def _retrieve_fast(query: str, repo_id: str, k: int = 5) -> list[dict]:
    """Vector search only — no reranker. ~10x faster than _retrieve."""
    adapter, _ = get_embed_adapter()
    query_embedding = adapter.embed(query)

    collection = get_or_create_collection(repo_id)
    count = collection.count()
    if count == 0:
        return []

    results = collection.query(
        query_embeddings=[query_embedding],
        n_results=min(k, count),
        include=["documents", "metadatas", "distances"],
    )
    docs = results["documents"][0]
    metas = results["metadatas"][0]
    distances = results["distances"][0]

    return [
        {"text": doc, "metadata": meta, "distance": dist}
        for doc, meta, dist in zip(docs, metas, distances)
    ]


def _tool_semantic_search(repo_id: str, query: str, k: int = 5) -> str:
    chunks = _retrieve_fast(query, repo_id, k)
    if not chunks:
        return "(no matches)"
    parts = []
    for c in chunks:
        m = c["metadata"]
        parts.append(f"[{m['file_path']}:{m['start_line']}]\n{c['text']}")
    return _cap("\n\n".join(parts))


def _tool_read_file(repo_id: str, path: str, start_line: int = 1, end_line: int | None = None) -> str:
    path = path.strip().lstrip("./")
    result = reconstruct_file(repo_id, path)
    if result is None:
        return f"ERROR: file not found: {path}"
    lines = result["content"].splitlines()
    sl = max(1, start_line) - 1
    el = end_line if end_line else len(lines)
    selected = lines[sl:el]
    numbered = [f"{sl + i + 1:>4}| {line}" for i, line in enumerate(selected)]
    return _cap("\n".join(numbered))


def _normalize_dir(directory: str) -> str:
    """Sanitize directory arguments the model may pass."""
    d = directory.strip().strip("/").strip()
    if d in (".", "./", "", "root", "/"):
        return ""
    return d


def _tool_list_files(repo_id: str, directory: str = "") -> str:
    files_path = f"deps/{repo_id}_files.json"
    if not os.path.exists(files_path):
        return "ERROR: file list not available — repo may need re-indexing"
    with open(files_path, "r", encoding="utf-8") as f:
        all_files: list[str] = json.load(f)
    prefix = _normalize_dir(directory)
    if prefix:
        filtered = [fp for fp in all_files if fp.startswith(prefix + "/") or fp == prefix]
    else:
        filtered = all_files
    if not filtered:
        return f"(no files under '{directory}')"
    return _cap("\n".join(sorted(filtered)))


def _glob_to_regex(pattern: str) -> str:
    pattern = pattern.replace(".", r"\.")
    pattern = pattern.replace("**", "__DOUBLESTAR__")
    pattern = pattern.replace("*", "[^/]*")
    pattern = pattern.replace("__DOUBLESTAR__", ".*")
    pattern = pattern.replace("?", "[^/]")
    return pattern


def _tool_grep(repo_id: str, pattern: str, path_glob: str | None = None) -> str:
    collection = get_or_create_collection(repo_id)

    results = collection.get(
        limit=50000,
        include=["documents", "metadatas"],
    )
    docs = results.get("documents") or []
    metas = results.get("metadatas") or []

    try:
        regex = re.compile(pattern, re.IGNORECASE)
    except re.error as e:
        return f"ERROR: invalid regex: {e}"

    glob_regex = re.compile(_glob_to_regex(path_glob)) if path_glob else None

    seen: set[tuple[str, int]] = set()
    matches: list[str] = []
    for doc, meta in zip(docs, metas):
        fp = meta.get("file_path", "")
        if glob_regex and not glob_regex.search(fp):
            continue
        start = int(meta.get("start_line", 1))
        for offset, line in enumerate(doc.splitlines()):
            if regex.search(line):
                abs_line = start + offset
                key = (fp, abs_line)
                if key in seen:
                    continue
                seen.add(key)
                matches.append(f"{fp}:{abs_line}: {line.strip()}")
                if len(matches) >= 30:
                    return _cap("\n".join(matches) + "\n… [truncated]")

    return _cap("\n".join(matches)) if matches else "(no matches)"


def _tool_find_references(repo_id: str, file_path: str) -> str:
    dep_path = f"deps/{repo_id}_deps.json"
    if not os.path.exists(dep_path):
        return "ERROR: dependency graph not available"
    with open(dep_path, "r", encoding="utf-8") as f:
        graph: dict[str, list[str]] = json.load(f)

    fp = file_path.strip().lstrip("./")
    if fp not in graph:
        return f"(file '{fp}' not found in dependency graph)"

    direct = set(graph.get(fp, []))
    visited: set[str] = set(direct)
    queue: deque[str] = deque(direct)
    transitive: set[str] = set()
    while queue:
        node = queue.popleft()
        for dep in graph.get(node, []):
            if dep not in visited:
                visited.add(dep)
                transitive.add(dep)
                queue.append(dep)

    lines: list[str] = []
    for d in sorted(direct):
        lines.append(f"{d}  (direct)")
    for t in sorted(transitive - direct):
        lines.append(f"{t}  (transitive)")

    if not lines:
        return f"(no files import {fp})"
    if len(lines) > 20:
        lines = lines[:20]
        lines.append("… and more")
    return "\n".join(lines)


def _render_tree_text(node: dict, prefix: str = "", max_depth: int = 3, depth: int = 0) -> list[str]:
    lines: list[str] = []
    name = node.get("name", "")
    ntype = node.get("type", "file")

    if depth > 0:
        icon = "📁 " if ntype == "folder" else "📄 "
        lines.append(f"{prefix}{icon}{name}")

    if ntype == "folder" and depth < max_depth:
        children = node.get("children", [])
        for i, child in enumerate(children):
            is_last = i == len(children) - 1
            child_prefix = prefix + ("    " if is_last else "│   ") if depth > 0 else ""
            lines.extend(_render_tree_text(child, child_prefix, max_depth, depth + 1))
    elif ntype == "folder" and depth >= max_depth:
        child_count = len(node.get("children", []))
        if child_count:
            lines.append(f"{prefix}    … ({child_count} items)")

    return lines


def _tool_directory_tree(repo_id: str, directory: str = "", max_depth: int = 3) -> str:
    files_path = f"deps/{repo_id}_files.json"
    if not os.path.exists(files_path):
        return "ERROR: file list not available"
    with open(files_path, "r", encoding="utf-8") as f:
        all_files: list[str] = json.load(f)

    prefix = _normalize_dir(directory)
    if prefix:
        filtered = [fp for fp in all_files if fp.startswith(prefix + "/")]
        filtered = [fp[len(prefix) + 1:] for fp in filtered]
    else:
        filtered = all_files

    if not filtered:
        return f"(no files under '{directory}')"

    tree = build_file_tree(filtered)
    tree["name"] = prefix or "/"
    lines = _render_tree_text(tree, max_depth=max_depth)
    return _cap("\n".join(lines))


# -- Build tool registry ---------------------------------------------------

def _build_tools(repo_id: str) -> list[ToolDef]:
    return [
        ToolDef(
            name="semantic_search",
            description="Search code by meaning. Returns top-k relevant chunks.",
            parameters={
                "query": {"type": "string", "description": "Search query", "required": True},
                "k": {"type": "integer", "description": "Number of results (default 5)", "required": False},
            },
            fn=lambda query, k=5: _tool_semantic_search(repo_id, query, int(k)),
        ),
        ToolDef(
            name="read_file",
            description="Read full or partial file contents from the index. Always use this before claiming what a specific file does.",
            parameters={
                "path": {"type": "string", "description": "File path", "required": True},
                "start_line": {"type": "integer", "description": "First line (default 1)", "required": False},
                "end_line": {"type": "integer", "description": "Last line", "required": False},
            },
            fn=lambda path, start_line=1, end_line=None: _tool_read_file(
                repo_id, path, int(start_line), int(end_line) if end_line is not None else None
            ),
        ),
        ToolDef(
            name="list_files",
            description="List indexed files, optionally filtered by directory.",
            parameters={
                "directory": {"type": "string", "description": "Directory prefix", "required": False},
            },
            fn=lambda directory="": _tool_list_files(repo_id, directory),
        ),
        ToolDef(
            name="grep",
            description="Regex search across code. Returns up to 30 matches.",
            parameters={
                "pattern": {"type": "string", "description": "Regex pattern", "required": True},
                "path_glob": {"type": "string", "description": "Glob filter (e.g. '*.py')", "required": False},
            },
            fn=lambda pattern, path_glob=None: _tool_grep(repo_id, pattern, path_glob if path_glob else None),
        ),
        ToolDef(
            name="find_references",
            description="Find files that import a given file.",
            parameters={
                "file_path": {"type": "string", "description": "File to find importers of", "required": True},
            },
            fn=lambda file_path: _tool_find_references(repo_id, file_path),
        ),
        ToolDef(
            name="directory_tree",
            description="Show folder/file tree structure. Good first step; pair with read_file on README and main modules for substantive answers.",
            parameters={
                "directory": {"type": "string", "description": "Subdirectory (default: root)", "required": False},
                "max_depth": {"type": "integer", "description": "Max depth (default 3)", "required": False},
            },
            fn=lambda directory="", max_depth=3: _tool_directory_tree(repo_id, directory, int(max_depth)),
        ),
    ]


def _tools_schema_text(tools: list[ToolDef]) -> str:
    parts: list[str] = []
    for t in tools:
        params = ", ".join(
            f'{name}: {info["type"]}' + (" (required)" if info.get("required") else "")
            for name, info in t.parameters.items()
        )
        parts.append(f"- {t.name}({params}): {t.description}")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# System prompt — kept lean to reduce per-iteration token cost
# ---------------------------------------------------------------------------

_AGENT_SYSTEM_PROMPT = """You are a friendly senior software engineer. You can explore an indexed codebase using tools, or answer directly from general knowledge.

Available tools:
{tools}

Single tool — reply with ONLY this (no preamble):
<tool_call name="tool_name">{{"arg": "value"}}</tool_call>

Multiple independent tools at once (faster — the server runs them in parallel when possible). Use ONE block, valid JSON array:
<tool_calls>[
  {{"name": "read_file", "args": {{"path": "README.md"}}}},
  {{"name": "read_file", "args": {{"path": "app/main.py"}}}}
]</tool_calls>
Each element must have "name" (string) and "args" (object). Prefer this whenever you would issue several reads/searches in the same step.

To give your final answer after exploring with tools:
<answer>Your answer here. Cite code as [file_path:line_number].</answer>

CRITICAL — When to use tools vs. answer directly:
- Greetings, thanks, small talk → wrap your reply in <answer> tags, no tools.
- General programming/CS questions (NOT about this repo) → <answer> from your own knowledge.

THIS INDEXED CODEBASE — tools are mandatory before <answer>:
- Before any <answer> about repo structure, symbols, APIs, configs, etc., run at least one batch of actual tools (<tool_calls> encouraged): directory_tree, list_files, read_file, grep, semantic_search as needed — then cite from tool <observation> outputs.
- Do NOT invent paths, filenames, folders, URLs, configs, APIs, imports, env vars, or line numbers. If unsure, emit another tool_call / tool_calls batch instead of guessing.
- EVERY [some/path.ext:line] citation in your final answer must match a path that existed in tool outputs for this conversation (or be dropped). Never cite files you never read or listed.

Rules:
- Focus ONLY on the current question. Prior conversation is only for pronoun resolution.
- NEVER say "I can't access" or "the context doesn't include" — you have tools.
- Never list or describe directories/files/commands that tools did not return.
- When calling tools: output ONLY a <tool_call>…</tool_call> OR a single <tool_calls>[…]</tool_calls> JSON block — no preamble or explanation outside those tags.

Depth & evidence (critical):
- NEVER describe what a file or module does based on its name alone. You MUST read_file it first.
- Seeing an import like "from app.llm import …" tells you the file exists, NOT what it does. Read it.
- For broad/overview questions ("what does this do", "where do I start", "how does this work", "walk me through"):
  1. First batch: directory_tree + read_file for README.md (or similar) + read_file for the main entry point (e.g. main.py, app.py, index.ts).
  2. After reading the entry point, identify the key modules it imports or delegates to. Issue a SECOND <tool_calls> batch to read_file those modules.
  3. Keep reading until you understand the actual business logic, not just the framework/boilerplate. A good overview answer explains WHAT the application does for the user, not just that it "uses FastAPI" or "has a database."
  4. Only then write <answer> — it should describe the project's purpose, its core workflow, and how the pieces fit together, all grounded in code you actually read.
- For narrow/specific questions: at least one tool observation containing the cited paths.
- NEVER stop after reading only main.py / the entry point. That file is usually just boilerplate (imports, route registration, server startup). The real logic lives in the modules it imports.
- For overview answers, structure your <answer> in this order:
  1) Product purpose in plain language (what the repo is for, who uses it),
  2) Core end-to-end workflow,
  3) Key modules and responsibilities with citations.

Tone when tools have run:
- Write with grounded confidence from tool observations only. Avoid hedging for facts grounded in observations.
- If tools did not return something, say so — do not fill gaps from imagination.
- Explain what the code ACTUALLY does (its purpose, its logic, its data flow), not just the tech stack it uses.
- Final answers: substantive, [path:line] citations referencing paths that appear in observations (prefer read_file / grep excerpts).
"""


# ---------------------------------------------------------------------------
# LLM output parser
# ---------------------------------------------------------------------------

@dataclass
class ParsedOutput:
    kind: str  # "tool_calls" | "answer" | "answer_truncated" | "text"
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    text: str = ""

    # Back-compat accessors for logs / single-call paths
    @property
    def name(self) -> str:
        return self.calls[0][0] if self.calls else ""

    @property
    def args(self) -> dict[str, Any]:
        return dict(self.calls[0][1]) if self.calls else {}


_TOOL_CALL_BODY_RE = r'<tool_call\s+name=["\'](\w+)["\']>\s*(.*?)\s*</tool_call>'
_TOOL_CALL_SPLIT_RE = re.compile(_TOOL_CALL_BODY_RE, re.DOTALL)


def _extract_tool_calls_from_batch_json(text: str) -> list[tuple[str, dict[str, Any]]] | None:
    batch = re.search(r"<tool_calls>\s*(.*?)\s*</tool_calls>", text, re.DOTALL)
    if not batch:
        return None
    raw = batch.group(1).strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        log.warning("[parse] invalid JSON inside <tool_calls>")
        return None
    if not isinstance(data, list) or len(data) == 0:
        return None
    out: list[tuple[str, dict[str, Any]]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        nm = item.get("name")
        if not isinstance(nm, str) or not nm:
            continue
        ar = item.get("args") or {}
        out.append((nm, ar if isinstance(ar, dict) else {}))
    return out or None


def _extract_tool_calls_from_xml_tags(text: str) -> list[tuple[str, dict[str, Any]]]:
    found: list[tuple[str, dict[str, Any]]] = []
    for m in _TOOL_CALL_SPLIT_RE.finditer(text):
        name = m.group(1)
        args_raw = m.group(2).strip()
        try:
            args = json.loads(args_raw) if args_raw else {}
        except json.JSONDecodeError:
            args = {}
        found.append((name, args))
    return found


_BARE_JSON_TOOL_RE = re.compile(
    r'\{\s*"name"\s*:\s*"(\w+)"\s*,\s*"args"\s*:\s*(\{[^}]*\})\s*\}',
)


def _extract_tool_calls_from_bare_json(text: str) -> list[tuple[str, dict[str, Any]]] | None:
    """Parse bare JSON objects like {"name":"read_file","args":{"path":"x.py"}}
    that the model sometimes emits without <tool_calls> wrapper."""
    matches = _BARE_JSON_TOOL_RE.findall(text)
    if not matches:
        return None
    out: list[tuple[str, dict[str, Any]]] = []
    for name, args_raw in matches:
        try:
            args = json.loads(args_raw)
        except json.JSONDecodeError:
            args = {}
        out.append((name, args if isinstance(args, dict) else {}))
    return out or None


def _parse_llm_output(text: str) -> ParsedOutput:
    calls = _extract_tool_calls_from_batch_json(text)
    if calls:
        return ParsedOutput(kind="tool_calls", calls=calls)

    xml_calls = _extract_tool_calls_from_xml_tags(text)
    if xml_calls:
        return ParsedOutput(kind="tool_calls", calls=xml_calls)

    # Closed <answer>...</answer>
    ans_match = re.search(r'<answer>(.*?)</answer>', text, re.DOTALL)
    if ans_match:
        return ParsedOutput(kind="answer", text=ans_match.group(1).strip())

    # Unclosed <answer> (model hit max_tokens before closing tag)
    ans_open = re.search(r'<answer>(.*)', text, re.DOTALL)
    if ans_open:
        return ParsedOutput(kind="answer_truncated", text=ans_open.group(1).strip())

    # Bare JSON tool calls without XML wrapper — common model failure mode
    bare_calls = _extract_tool_calls_from_bare_json(text)
    if bare_calls:
        log.info("[parse] recovered %d bare JSON tool calls", len(bare_calls))
        return ParsedOutput(kind="tool_calls", calls=bare_calls)

    # Plain text — strip any stray XML-like artifacts the model may have emitted
    clean = text.strip()
    clean = re.sub(r'</?(?:tool_call|answer|observation)[^>]*>', '', clean).strip()
    return ParsedOutput(kind="text", text=clean)


# ---------------------------------------------------------------------------
# Tool dispatcher
# ---------------------------------------------------------------------------

def _run_tool(name: str, args: dict[str, Any], tools: list[ToolDef]) -> str:
    tool = next((t for t in tools if t.name == name), None)
    if tool is None:
        return f"ERROR: unknown tool '{name}'"
    try:
        return tool.fn(**args)
    except Exception as e:
        return f"ERROR: {e}"


def _tool_label(name: str, args: dict[str, Any]) -> str:
    if name == "semantic_search":
        q = args.get("query", "")[:40]
        return f'Searching for "{q}"'
    if name == "read_file":
        p = args.get("path", "?")
        sl = args.get("start_line")
        if sl:
            return f"Reading {p}:{sl}"
        return f"Reading {p}"
    if name == "grep":
        return f'Grepping for /{args.get("pattern", "")[:30]}/'
    if name == "list_files":
        d = args.get("directory", "")
        return f"Listing files in {d}/" if d else "Listing all files"
    if name == "find_references":
        return f'Tracing imports of {args.get("file_path", "?")}'
    if name == "directory_tree":
        d = args.get("directory", "")
        return f"Mapping out {d}/" if d else "Mapping the project structure"
    return f"Running {name}"


def _summarize_trace(tool_trace: list[dict[str, Any]]) -> dict[str, Any]:
    counts: dict[str, int] = {}
    files_read: list[str] = []
    last_search: str = ""
    for step in tool_trace:
        nm = step.get("name", "")
        counts[nm] = counts.get(nm, 0) + 1
        args = step.get("args") or {}
        if nm == "read_file":
            p = args.get("path")
            if p and p not in files_read:
                files_read.append(p)
        elif nm in ("semantic_search", "grep"):
            last_search = str(args.get("query") or args.get("pattern") or "")
    return {
        "counts": counts,
        "files_read": files_read,
        "last_search": last_search,
        "total": len(tool_trace),
    }


def _thinking_status(iteration: int, tool_trace: list[dict[str, Any]] | None = None) -> str:
    """State-derived status reflecting what the agent has actually done so far."""
    trace = tool_trace or []
    if not trace:
        return "Sizing up the question…" if iteration == 0 else "Picking where to look first…"

    s = _summarize_trace(trace)
    counts = s["counts"]
    files = s["files_read"]
    last_search = s["last_search"]

    if counts.get("directory_tree") and not files:
        return "Got the project layout — picking files to read next…"
    if files:
        if len(files) == 1:
            return f"Read {files[0]} — tracing what it pulls in next…"
        recent = files[-1]
        return f"Read {len(files)} files (last: {recent}) — following the imports…"
    if last_search:
        snippet = last_search[:40] + ("…" if len(last_search) > 40 else "")
        return f'Sifting through results for "{snippet}" — narrowing down…'
    return f"Reviewed {s['total']} tool result{'s' if s['total'] != 1 else ''} — planning the next step…"


def _composing_status(iteration: int, tool_trace: list[dict[str, Any]] | None = None) -> str:
    """State-derived composing status."""
    trace = tool_trace or []
    if not trace:
        return "Writing up the answer…"
    s = _summarize_trace(trace)
    n_files = len(s["files_read"])
    if n_files:
        return f"Composing the answer from {n_files} file{'s' if n_files != 1 else ''} I read…"
    return f"Composing the answer from {s['total']} observation{'s' if s['total'] != 1 else ''}…"


# ---------------------------------------------------------------------------
# Streaming LLM helpers
# ---------------------------------------------------------------------------

def _stream_llm_text(llm, prompt: str) -> tuple[str, bool]:
    """Stream tokens, accumulating full text. Returns (text, stopped_early).

    Stops early on </answer>, on </tool_calls> (parallel batch), or on the
    first </tool_call> when not using <tool_calls> (single-call fast path).
    """
    accumulated = ""
    try:
        for delta in iter_deltas(llm.stream_complete(prompt)):
            accumulated += delta
            if "</answer>" in accumulated:
                return accumulated, True
            if "</tool_calls>" in accumulated:
                return accumulated, True
            if "</tool_call>" in accumulated and "<tool_calls>" not in accumulated:
                return accumulated, True
    except Exception:
        if accumulated:
            return accumulated, False
        raise
    return accumulated, False


def _stream_answer_tokens(llm, prompt: str):
    """Stream the final answer, yielding clean token events.

    Strips <answer>/</ answer> XML tags from the stream so the frontend
    only sees clean prose. Yields (event_dict, ...) until done.
    Returns the full clean answer text.
    """
    raw = ""
    emitted_len = 0
    answer_started = False

    for delta in iter_deltas(llm.stream_complete(prompt)):
        raw += delta

        if "</answer>" in raw:
            # Emit any remaining clean text before the close tag
            clean = re.sub(r'^.*?<answer>', '', raw, count=1, flags=re.DOTALL)
            clean = re.sub(r'</answer>.*$', '', clean, flags=re.DOTALL)
            remaining = clean[emitted_len:]
            if remaining:
                yield {"type": "token", "text": remaining}
            return

        if not answer_started:
            if "<answer>" in raw:
                answer_started = True
            else:
                # No <answer> tag yet — the model might be writing plain text.
                # If we've accumulated a decent chunk without any XML, start
                # emitting it as tokens (for direct/untagged answers).
                if len(raw) > 20 and "<" not in raw:
                    answer_started = True

        if answer_started:
            clean = re.sub(r'^.*?<answer>', '', raw, count=1, flags=re.DOTALL)
            new_text = clean[emitted_len:]
            if new_text:
                yield {"type": "token", "text": new_text}
                emitted_len = len(clean)


# ---------------------------------------------------------------------------
# Agent loop
# ---------------------------------------------------------------------------

def _yield_chunked(text: str, chunk_size: int = 12):
    """Yield a pre-built answer in small chunks to simulate streaming.

    Splits on word boundaries so partial words never flash on screen.
    """
    words = text.split(" ")
    buf: list[str] = []
    for word in words:
        buf.append(word)
        if len(buf) >= chunk_size:
            yield ndjson_event({"type": "token", "text": " ".join(buf) + " "})
            buf = []
    if buf:
        yield ndjson_event({"type": "token", "text": " ".join(buf)})


def _stream_final_answer(messages, llm, tool_trace, repo_id: str):
    """Stream a final answer from the LLM, stripping XML tags in real-time."""
    prompt = _render_messages(messages)
    yield ndjson_event({"type": "status", "text": _composing_status(0, tool_trace)})

    full_answer = ""
    try:
        for evt in _stream_answer_tokens(llm, prompt):
            yield ndjson_event(evt)
            # Accumulate clean text for the done event
            if evt["type"] == "token":
                full_answer += evt["text"]
    except Exception as e:
        yield ndjson_event({"type": "error", "message": f"LLM call failed: {e!r}"})
        return

    if not full_answer.strip():
        full_answer = "(I wasn't able to generate a response. Please try again.)"
        yield ndjson_event({"type": "token", "text": full_answer})

    clean, citations = _finalize_agent_answer(full_answer, repo_id)
    yield ndjson_event({
        "type": "done",
        "citations": citations,
        "answer": clean,
        "tool_trace": tool_trace,
    })


class AgentRequest(BaseModel):
    repo_id: str
    question: str
    scope: str | None = None
    history: list[Turn] = []


_SMALLTALK_PATTERNS = re.compile(
    r"^("
    r"h(i|ello|ey|owdy|ow are you)|"
    r"thanks?|thank you|thx|ty|"
    r"(good )?(bye|night|morning|evening)|"
    r"cool|nice|ok(ay)?|got it|sure|"
    r"yo|sup|what'?s up|"
    r"you'?re welcome|no problem|np"
    r")[\s!?.]*$",
    re.IGNORECASE,
)

_META_PATTERNS = re.compile(
    r"(who are you|what are you|what can you do|how do you work|"
    r"what('?s| is) your (name|purpose|context|model)|"
    r"tell me about yourself|introduce yourself|"
    r"what makes you|how are you)",
    re.IGNORECASE,
)

_OVERVIEW_PATTERNS = re.compile(
    r"("
    r"what does (this|the|it) (do|project|app|repo|codebase)|"
    r"what is this|what('s| is) (this|the) (project|app|codebase|repo)|"
    r"how does (this|the|it) (all )?(work|fit together)|"
    r"walk me through|give me (a |an )?(overview|summary|tour|rundown)|"
    r"where (do|should) (i|we) (start|begin)|"
    r"(help|explain|tell) me (understand|about|get started)|"
    r"i('m| am) new|don't (really )?know where to start|"
    r"what('s| is) going on (here|in this)|"
    r"(can you )?(explain|describe) (the |this )?(project|codebase|code|repo|app)|"
    r"first thing.*(know|understand|learn)|"
    r"(high[ -]?level|big picture|bird'?s?[ -]?eye)"
    r")",
    re.IGNORECASE,
)

_OVERVIEW_HINT = (
    "\n\n[SYSTEM HINT: This is a broad overview question. You MUST deeply explore before answering. "
    "Start with a <tool_calls> batch: directory_tree, read_file README.md, read_file for the main entry point. "
    "After reading those, issue MORE read_file calls for the key modules/files they reference. "
    "Your answer must explain what this project actually DOES (its purpose, workflow, and core logic), "
    "not just list the tech stack or file names. Read until you understand the business logic.]"
)


def _build_rag_seed_observation(repo_id: str, question: str, k: int = 4) -> str:
    """Inject small RAG-like context to guide initial tool exploration.

    The seed is advisory context from semantic retrieval. The agent must still
    run tools and verify details before finalizing its answer.
    """
    seed_query = question.strip() or "project purpose overview"
    chunks = _retrieve_fast(seed_query, repo_id, k=max(1, int(k)))
    if not chunks:
        return ""
    parts: list[str] = []
    paths: list[str] = []
    for c in chunks:
        m = c.get("metadata") or {}
        fp = m.get("file_path", "")
        sl = int(m.get("start_line", 1) or 1)
        txt = (c.get("text") or "").strip()
        if not txt:
            continue
        if fp and fp not in paths:
            paths.append(fp)
        if len(txt) > 420:
            txt = txt[:420] + "…"
        parts.append(f"[{fp}:{sl}]\n{txt}")
    if not parts:
        return ""
    path_suggestions = ""
    if paths:
        hints = ", ".join(paths[:4])
        path_suggestions = (
            f"\n\nSuggested next tool step: start with read_file on these likely relevant files: {hints}. "
            "Then branch into imports/dependencies you discover."
        )
    return (
        "RAG OVERVIEW SEED (initial high-level context from semantic retrieval):\n"
        + "\n\n".join(parts)
        + "\n\nUse this as orientation only; still run tools to verify and expand."
        + path_suggestions
    )


def _is_direct_answer_ok(question: str, answer_text: str) -> bool:
    """Decide if the model's ungrounded answer should be accepted.

    Strict: only obvious small talk and meta questions. Anything ambiguous
    falls through to the full agent, whose system prompt already permits
    direct general-knowledge answers when a question isn't about the repo.
    """
    q = question.strip()
    if _SMALLTALK_PATTERNS.match(q):
        return True
    if _META_PATTERNS.search(q):
        return True
    return False


_AGENT_MAX_HISTORY_TURNS = 4
_AGENT_MAX_CHARS_PER_TURN = 300


def _format_agent_history(history: list[Turn]) -> str:
    """Compact history for the agent — just enough for pronoun resolution.

    Keeps only the last few turns, heavily truncated, so the model doesn't
    anchor on prior topics. User messages are kept more intact than assistant
    messages (the model's own prior answers are the main source of anchoring).
    """
    if not history:
        return ""
    recent = [t for t in history if t.role in ("user", "assistant")][-_AGENT_MAX_HISTORY_TURNS:]
    if not recent:
        return ""
    lines = []
    for turn in recent:
        content = turn.content.strip()
        if turn.role == "assistant":
            # Heavily truncate prior answers — the model doesn't need them
            limit = 150
        else:
            limit = _AGENT_MAX_CHARS_PER_TURN
        if len(content) > limit:
            content = content[:limit] + "…"
        speaker = "User" if turn.role == "user" else "Assistant"
        lines.append(f"{speaker}: {content}")
    return "\n".join(lines)


def _force_bootstrap(tools, tool_trace, messages, question):
    """Force-call directory_tree + README + entry point when the model refuses tools.

    Provides enough context for the model to give a substantive answer about
    the actual project, not just a generic tech-stack description.
    """
    log.info("[force-bootstrap] model refused tools, auto-calling directory_tree + key files")
    yield ndjson_event({"type": "status", "text": "Scanning project structure…"})

    bootstrap_calls = [("directory_tree", {})]
    readme_candidates = ["README.md", "readme.md", "README.rst", "README"]
    entry_candidates = ["app/main.py", "main.py", "app.py", "index.ts", "src/index.ts", "src/main.py"]

    for rc in readme_candidates:
        bootstrap_calls.append(("read_file", {"path": rc}))
        break
    for ec in entry_candidates:
        bootstrap_calls.append(("read_file", {"path": ec}))
        break

    for idx, (name, args) in enumerate(bootstrap_calls):
        yield ndjson_event({"type": "tool_call", "name": name, "args": args, "idx": idx})

    trio_results = _run_tools_parallel(bootstrap_calls, tools)

    compact_parts: list[str] = []
    obs_parts: list[str] = []
    for idx, (name, args, observation) in enumerate(trio_results):
        truncated_result = observation[:500]
        yield ndjson_event({"type": "tool_result", "name": name, "args": args, "result": truncated_result, "idx": idx})
        tool_trace.append({"name": name, "args": args, "result": truncated_result})

        compact_parts.append(f'<tool_call name="{name}">{json.dumps(args)}</tool_call>')

        budget = 2000 if name == "read_file" else 1500
        obs_for_hist = observation[:budget]
        if len(observation) > budget:
            obs_for_hist += f"\n… [truncated, {len(observation)} chars total]"
        obs_parts.append(f"--- [{idx + 1}/{len(bootstrap_calls)}] {name} ---\n<observation>\n{obs_for_hist}\n</observation>")

    messages.append({"role": "assistant", "content": "".join(compact_parts)})
    messages.append({
        "role": "user",
        "content": "\n\n".join(obs_parts) + (
            "\n\nYou now have the project structure and key files. "
            "Read more files if needed to understand the actual business logic, "
            "then write your <answer> explaining what this project does and how it works. "
            f"User's question: {question}"
        ),
    })


_DIRECT_SYSTEM = (
    "You are a friendly senior software engineer assistant. "
    "Answer the user naturally and concisely. No tool calls, no XML tags."
)


def _agent_loop(
    question: str,
    history: list[Turn],
    repo_id: str,
    scope: str | None,
):
    """Generator yielding NDJSON events as the agent explores and answers."""

    # ── Fast path: small talk / meta. Skip the full tool prompt.
    # Pure greetings get the constrained smalltalk LLM (short replies);
    # meta questions ("who are you, what can you do") need more room, so
    # they use the regular agent LLM.
    if _is_direct_answer_ok(question, ""):
        yield ndjson_event({"type": "status", "text": "Thinking…"})
        is_smalltalk = bool(_SMALLTALK_PATTERNS.match(question.strip()))
        agent_llm = get_smalltalk_llm() if is_smalltalk else get_agent_llm()
        history_block = _format_agent_history(history)
        if history_block:
            user_content = f"{history_block}\n\nUser: {question}"
        else:
            user_content = question
        prompt = f"SYSTEM:\n{_DIRECT_SYSTEM}\n\nUSER:\n{user_content}\n\nASSISTANT:\n"
        try:
            answer = ""
            for delta in iter_deltas(agent_llm.stream_complete(prompt)):
                answer += delta
                yield ndjson_event({"type": "token", "text": delta})
            answer = answer.strip()
            if not answer:
                answer = "Hey! Ask me anything about the codebase."
            ans, cites = _finalize_agent_answer(answer, repo_id)
            yield ndjson_event({"type": "done", "citations": cites, "answer": ans, "tool_trace": []})
        except Exception as e:
            yield ndjson_event({"type": "error", "message": f"LLM call failed: {e!r}"})
        return

    # ── Full agent loop: codebase questions ───────────────────────────
    tools = _build_tools(repo_id)

    system = _AGENT_SYSTEM_PROMPT.format(tools=_tools_schema_text(tools))

    history_block = _format_agent_history(history)

    is_overview = bool(_OVERVIEW_PATTERNS.search(question))
    overview_suffix = _OVERVIEW_HINT if is_overview else ""

    messages: list[dict[str, str]] = []
    messages.append({"role": "system", "content": system})

    if history_block:
        messages.append({
            "role": "user",
            "content": (
                f"[Prior conversation for context — do NOT repeat or continue these topics unless asked]\n"
                f"{history_block}\n\n---\nCurrent question: {question}{overview_suffix}"
            ),
        })
    else:
        messages.append({"role": "user", "content": question + overview_suffix})

    # Hybrid bootstrap: use lightweight RAG as an orientation layer for broad
    # overview questions. Narrow questions skip the seed — the model's own
    # tool calls are cheaper than paying for a redundant retrieval round.
    if is_overview:
        seed_obs = _build_rag_seed_observation(repo_id, question, k=6)
        if seed_obs:
            tail = (
                "\n\nFor the final answer, lead with plain-language product purpose, "
                "then explain the technical workflow and key modules."
            )
            messages.append({"role": "user", "content": seed_obs + tail})

    tool_trace: list[dict[str, Any]] = []
    nudge_count = 0

    agent_llm = get_agent_llm()
    answer_llm = get_llm()

    for iteration in range(MAX_ITERATIONS):
        prompt = _render_messages(messages)

        yield ndjson_event({"type": "status", "text": _thinking_status(iteration, tool_trace)})

        # Use the fast LLM for tool-call iterations; only the final answer
        # streaming path uses answer_llm.
        llm = agent_llm

        try:
            response_text, _ = _stream_llm_text(llm, prompt)
        except Exception as e:
            yield ndjson_event({"type": "error", "message": f"LLM call failed: {e!r}"})
            return

        parsed = _parse_llm_output(response_text)
        log.info("[iter %d] kind=%s name=%s text_preview=%s", iteration, parsed.kind, parsed.name, (parsed.text or response_text)[:150])

        if parsed.kind == "tool_calls" and parsed.calls:
            calls = parsed.calls[:MAX_PARALLEL_TOOLS]
            if len(parsed.calls) > MAX_PARALLEL_TOOLS:
                log.warning("[tool_batch] truncated %d calls to %d", len(parsed.calls), MAX_PARALLEL_TOOLS)

            yield ndjson_event({
                "type": "status",
                "text": (
                    f"Running {len(calls)} tools in parallel…"
                    if len(calls) > 1
                    else f"{_tool_label(calls[0][0], calls[0][1])}…"
                ),
            })
            compact_parts: list[str] = []
            chunk_budget = max(1200, 4500 // max(len(calls), 1))

            for idx, (name, args) in enumerate(calls):
                yield ndjson_event({
                    "type": "tool_call",
                    "name": name,
                    "args": args,
                    "idx": idx,
                })

            trio_results = _run_tools_parallel(calls, tools)
            obs_parts_user: list[str] = []
            for idx, (name, args, observation) in enumerate(trio_results):
                truncated_result = observation[:500]
                yield ndjson_event({
                    "type": "tool_result",
                    "name": name,
                    "args": args,
                    "result": truncated_result,
                    "idx": idx,
                })
                tool_trace.append({
                    "name": name,
                    "args": args,
                    "result": truncated_result,
                })
                compact_parts.append(f'<tool_call name="{name}">{json.dumps(args)}</tool_call>')
                obs_for_hist = observation[:chunk_budget]
                if len(observation) > chunk_budget:
                    obs_for_hist += f"\n… [truncated, {len(observation)} chars total]"
                obs_parts_user.append(
                    f"--- [{idx + 1}/{len(calls)}] {name} ---\n"
                    f"<observation>\n{obs_for_hist}\n</observation>"
                )

            messages.append({"role": "assistant", "content": "".join(compact_parts)})
            messages.append({"role": "user", "content": "\n\n".join(obs_parts_user)})
            continue

        if parsed.kind in ("answer", "answer_truncated") and parsed.text:
            if not tool_trace and not _is_direct_answer_ok(question, parsed.text):
                nudge_count += 1
                if nudge_count <= 1:
                    messages.append({"role": "assistant", "content": response_text})
                    messages.append({"role": "user", "content": _NUDGE_USE_TOOLS_MESSAGE})
                    continue
                yield from _force_bootstrap(tools, tool_trace, messages, question)
                continue

            read_files = {t["args"].get("path") for t in tool_trace if t["name"] == "read_file" and t["args"].get("path")}
            too_shallow = is_overview and len(read_files) < 2 and iteration < MAX_ITERATIONS - 2

            if too_shallow:
                log.info("[shallow] overview question but only read %d files (%s), pushing for more", len(read_files), read_files)
                messages.append({"role": "assistant", "content": response_text})
                messages.append({
                    "role": "user",
                    "content": (
                        "STOP — you've only read boilerplate files so far. "
                        "Your answer just describes the tech stack, not what the project actually does. "
                        "Use <tool_calls> to read the core business-logic modules "
                        "(e.g. the files imported by main.py like query.py, ingest.py, llm.py, etc.). "
                        "Then write a substantive <answer> explaining the project's PURPOSE and WORKFLOW."
                    ),
                })
                continue

            if parsed.kind == "answer_truncated":
                log.info("[truncated] answer cut off, re-streaming with answer_llm")
                messages.append({"role": "assistant", "content": response_text})
                messages.append({"role": "user", "content": "Your answer was cut off. Please write a complete <answer>."})
                break

            clean_answer, cites = _finalize_agent_answer(parsed.text, repo_id)
            yield from _yield_chunked(clean_answer)
            yield ndjson_event({
                "type": "done",
                "citations": cites,
                "answer": clean_answer,
                "tool_trace": tool_trace,
            })
            return

        # Plain text or empty <answer> tags.
        answer_text = parsed.text or response_text.strip()
        answer_text = re.sub(r'</?(?:tool_call|answer|observation)[^>]*>', '', answer_text).strip()

        if not answer_text:
            messages.append({"role": "assistant", "content": response_text})
            messages.append({"role": "user", "content": "Your response was empty. Please answer the question."})
            continue

        if not tool_trace:
            if _is_direct_answer_ok(question, answer_text):
                ans, cites = _finalize_agent_answer(answer_text, repo_id)
                yield from _yield_chunked(ans)
                yield ndjson_event({
                    "type": "done",
                    "citations": cites,
                    "answer": ans,
                    "tool_trace": [],
                })
                return
            nudge_count += 1
            if nudge_count <= 1:
                messages.append({"role": "assistant", "content": response_text})
                messages.append({"role": "user", "content": _NUDGE_USE_TOOLS_MESSAGE})
                continue
            yield from _force_bootstrap(tools, tool_trace, messages, question)
            continue

        # Tools were used but model forgot tags — fall through to streaming answer
        yield ndjson_event({"type": "status", "text": _composing_status(iteration, tool_trace)})
        messages.append({"role": "assistant", "content": response_text})
        messages.append({"role": "user", "content": "Now write your final <answer> citing what you found."})
        break

    # Exhausted iterations or fell through — stream a proper final answer
    if len(tool_trace) >= MAX_ITERATIONS:
        messages.append({
            "role": "user",
            "content": "Write your <answer> now based on what you found.",
        })

    yield from _stream_final_answer(messages, answer_llm, tool_trace, repo_id)


def _render_messages(messages: list[dict[str, str]]) -> str:
    """Flatten message list into a single prompt string."""
    parts: list[str] = []
    for msg in messages:
        role = msg["role"].upper()
        parts.append(f"{role}:\n{msg['content']}")
    parts.append("ASSISTANT:\n")
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------

_STREAM_MEDIA_TYPE = "application/x-ndjson"


@router.post("/agent")
async def agent_query(request: AgentRequest):
    if not collection_exists(request.repo_id):
        raise HTTPException(status_code=404, detail="Repo not indexed")
    return StreamingResponse(
        _agent_loop(
            request.question,
            request.history,
            request.repo_id,
            request.scope,
        ),
        media_type=_STREAM_MEDIA_TYPE,
    )
