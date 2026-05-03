"""System architecture diagram (cached per repo) + narrative step generation.

GET /system/{repo_id}
    Returns a logical component diagram of the indexed repo — components
    (frontend/backend/db/external/etc.) and directed edges with labels.
    Generated once by Nemotron from a sample of code chunks, cached to
    deps/{repo_id}_system.json for all subsequent calls.

POST /narrative/{repo_id}  body: {question: str}
    Returns an ordered list of narrative "steps" that step-through-animate
    the cached system diagram to answer the user's question. Each step has:
        narration   — 1–3 sentences
        highlight   — component ids to pulse
        animate     — optional {edge, label} data-flow animation
        citations   — file_path + start_line list
    Uses the existing RAG retrieval pipeline (embed -> Chroma -> rerank)
    plus the cached system diagram as additional context.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from db import collection_exists, get_or_create_collection
from llm import get_llm

router = APIRouter()

_DEPS_DIR = Path("deps")
_SYSTEM_SAMPLE_CAP = 30  # max chunks sampled from Chroma for system analysis
_ZOOM_FILE_CHUNK_CAP = 4  # per-file chunk cap for zoom context
_ZOOM_FILES_CAP = 6       # how many files from a component we pull chunks from
_zoom_cache: dict[tuple, dict] = {}  # in-memory cache: (repo_id, component_id, q_hash) -> response


# ── System diagram ───────────────────────────────────────────────────────────


def _sample_chunks_for_system(repo_id: str) -> list[dict]:
    """Pick up to _SYSTEM_SAMPLE_CAP chunks that cover the repo's top-level structure.

    One chunk per top-level directory (first path segment). This gives Nemotron
    a representative slice without blowing out the prompt context.
    """
    collection = get_or_create_collection(repo_id)
    results = collection.get(
        limit=2000,
        include=["documents", "metadatas"],
    )
    docs = results.get("documents") or []
    metas = results.get("metadatas") or []

    by_bucket: dict[str, dict] = {}
    for doc, meta in zip(docs, metas):
        file_path = meta.get("file_path", "")
        bucket = file_path.split("/")[0] if "/" in file_path else file_path
        if bucket in by_bucket:
            continue
        by_bucket[bucket] = {"text": doc, "metadata": meta}
        if len(by_bucket) >= _SYSTEM_SAMPLE_CAP:
            break

    return list(by_bucket.values())


def _build_system_prompt(files: list[str], chunks: list[dict]) -> str:
    chunks_block = ""
    for chunk in chunks:
        meta = chunk["metadata"]
        chunks_block += f"[{meta.get('file_path', '')}:{meta.get('start_line', 1)}]\n"
        chunks_block += f"```{meta.get('language','')}\n{chunk['text']}\n```\n\n"

    files_block = "\n".join(files[:200])

    return f"""You are a software architect. From the repository below, enumerate the
high-level LOGICAL components of the system (NOT a file tree). Components are the
distinct moving parts — e.g. frontend UI, backend service, vector database,
external LLM API, worker, cache. Then list directed edges between components
with a short label describing the interaction.

Respond with ONLY a single JSON object (no prose, no code fences) matching:

{{
  "components": [
    {{
      "id": "<short_snake_case_id>",
      "label": "<human label>",
      "kind": "ui" | "service" | "database" | "external" | "queue" | "worker" | "cache",
      "files": ["<relative file path>", ...]
    }}
  ],
  "edges": [
    {{
      "id": "<short_edge_id>",
      "from": "<component id>",
      "to": "<component id>",
      "label": "<short interaction label>"
    }}
  ]
}}

Guidelines:
- At most 8 components and 12 edges.
- All edge from/to values MUST reference an id in components.
- id values are unique, lowercase, snake_case.
- Include external services (LLM APIs, auth providers) as kind "external".
- List 1–3 source-of-truth files per component in its files array.

REPO FILE LIST (first 200):
{files_block}

REPRESENTATIVE CODE SAMPLES:
{chunks_block}

JSON:"""


_JSON_BLOCK_RE = re.compile(r"\{.*\}", re.DOTALL)


def _parse_json_from_llm(text: str) -> dict | None:
    """json.loads the raw text, then fall back to extracting the first {...} block."""
    text = (text or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        pass
    m = _JSON_BLOCK_RE.search(text)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None


def _generate_system_doc(repo_id: str) -> dict:
    """Call Nemotron to build the system diagram. Raises HTTPException on bad JSON."""
    files_path = _DEPS_DIR / f"{repo_id}_files.json"
    if not files_path.exists():
        raise HTTPException(status_code=404, detail="File list missing — re-index repo")

    with open(files_path, "r", encoding="utf-8") as f:
        all_files = json.load(f)

    chunks = _sample_chunks_for_system(repo_id)
    prompt = _build_system_prompt(all_files, chunks)

    llm = get_llm("system_diagram")
    raw = str(llm.complete(prompt))

    parsed = _parse_json_from_llm(raw)
    if not parsed or not isinstance(parsed.get("components"), list) or not isinstance(parsed.get("edges"), list):
        raise HTTPException(
            status_code=502,
            detail={
                "message": "Nemotron returned invalid system JSON.",
                "raw": raw[:800],
            },
        )

    comp_ids = {c.get("id") for c in parsed["components"] if c.get("id")}
    parsed["edges"] = [
        e for e in parsed["edges"]
        if e.get("from") in comp_ids and e.get("to") in comp_ids
    ]

    result = {"repo_id": repo_id, **parsed}
    _DEPS_DIR.mkdir(exist_ok=True)
    cache_path = _DEPS_DIR / f"{repo_id}_system.json"
    with open(cache_path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2)
    return result


@router.get("/system/{repo_id}")
def get_system(repo_id: str):
    if not collection_exists(repo_id):
        raise HTTPException(status_code=404, detail="Repo not indexed")

    cache_path = _DEPS_DIR / f"{repo_id}_system.json"
    if cache_path.exists():
        with open(cache_path, "r", encoding="utf-8") as f:
            return json.load(f)

    return _generate_system_doc(repo_id)


@router.post("/system/{repo_id}/regenerate")
def regenerate_system(repo_id: str):
    """Force-rebuild the system diagram, bypassing cache."""
    if not collection_exists(repo_id):
        raise HTTPException(status_code=404, detail="Repo not indexed")
    cache_path = _DEPS_DIR / f"{repo_id}_system.json"
    if cache_path.exists():
        cache_path.unlink()
    return _generate_system_doc(repo_id)


# ── Narrative steps ──────────────────────────────────────────────────────────


class NarrativeRequest(BaseModel):
    question: str


def _retrieve_rag_chunks(repo_id: str, question: str, k: int = 5) -> list[dict]:
    """Mirror the embed -> Chroma -> rerank pipeline from query.py."""
    from llm import get_embed_adapter
    from query import _reranker  # lazy import avoids circular load

    adapter, _ = get_embed_adapter()
    query_embedding = adapter.embed(question)

    collection = get_or_create_collection(repo_id)
    count = collection.count()
    if count == 0:
        return []

    results = collection.query(
        query_embeddings=[query_embedding],
        n_results=min(20, count),
        include=["documents", "metadatas"],
    )
    docs = results["documents"][0]
    metas = results["metadatas"][0]
    if not docs:
        return []

    pairs = [(question, doc) for doc in docs]
    scores = _reranker.predict(pairs)
    ranked = sorted(zip(scores, docs, metas), key=lambda x: float(x[0]), reverse=True)
    top = ranked[:k]
    return [{"text": doc, "metadata": meta, "score": float(s)} for s, doc, meta in top]


def _build_narrative_prompt(question: str, system_doc: dict, chunks: list[dict]) -> str:
    chunks_block = ""
    for c in chunks:
        m = c["metadata"]
        chunks_block += f"[{m.get('file_path', '')}:{m.get('start_line', 1)}]\n"
        chunks_block += f"```{m.get('language', '')}\n{c['text']}\n```\n\n"

    slim_components = [
        {"id": c["id"], "label": c["label"], "kind": c["kind"]}
        for c in system_doc.get("components", [])
    ]
    slim_edges = [
        {"id": e["id"], "from": e["from"], "to": e["to"], "label": e["label"]}
        for e in system_doc.get("edges", [])
    ]
    system_block = json.dumps({"components": slim_components, "edges": slim_edges}, indent=2)

    return f"""You are explaining how the system works as a step-by-step animated narrative.
Using the given component diagram and code context, break the answer into ordered
steps that will drive a UI animation. Each step highlights components and may
animate data flow along exactly one edge.

Respond with ONLY a single JSON object (no prose, no code fences):

{{
  "steps": [
    {{
      "narration": "<1-3 short sentences; may include `inline code`>",
      "highlight": ["<component id>", ...],
      "animate": {{ "edge": "<edge id>", "label": "<short>", "direction": "forward" | "reverse" }} | null,
      "citations": [{{ "file_path": "<path>", "start_line": <int> }}]
    }}
  ]
}}

Guidelines:
- 4 to 8 steps.
- All highlight entries MUST be component ids present in the diagram.
- If animate is not null, animate.edge MUST reference an edge id in the diagram.
- animate.direction: "forward" if data flows from the edge's `from` component to its `to`
  component (e.g., request, outbound call, write). Use "reverse" when data flows the other
  way along the same edge (e.g., API response, DB query result, LLM returning tokens,
  cache hit). Default to "forward" if unsure.
- Include at least one citation per step when the step is about specific code; otherwise [].
- Do NOT invent components, edges, or files outside the provided diagram/context.

SYSTEM DIAGRAM:
{system_block}

CODE CONTEXT:
{chunks_block}

QUESTION: {question}

JSON:"""


@router.post("/narrative/{repo_id}")
def post_narrative(repo_id: str, request: NarrativeRequest):
    if not collection_exists(repo_id):
        raise HTTPException(status_code=404, detail="Repo not indexed")

    cache_path = _DEPS_DIR / f"{repo_id}_system.json"
    if cache_path.exists():
        with open(cache_path, "r", encoding="utf-8") as f:
            system_doc = json.load(f)
    else:
        system_doc = _generate_system_doc(repo_id)

    chunks = _retrieve_rag_chunks(repo_id, request.question)
    prompt = _build_narrative_prompt(request.question, system_doc, chunks)

    llm = get_llm("narrative")
    raw = str(llm.complete(prompt))

    parsed = _parse_json_from_llm(raw)
    if not parsed or not isinstance(parsed.get("steps"), list):
        raise HTTPException(
            status_code=502,
            detail={
                "message": "Nemotron returned invalid narrative JSON.",
                "raw": raw[:800],
            },
        )

    comp_ids = {c["id"] for c in system_doc.get("components", [])}
    edge_ids = {e["id"] for e in system_doc.get("edges", [])}

    cleaned_steps = []
    for s in parsed["steps"]:
        highlight = [h for h in (s.get("highlight") or []) if h in comp_ids]
        animate = s.get("animate") or None
        if animate and (not isinstance(animate, dict) or animate.get("edge") not in edge_ids):
            animate = None
        if animate:
            direction = animate.get("direction")
            if direction not in ("forward", "reverse"):
                direction = "forward"
            animate = {
                "edge": animate.get("edge"),
                "label": animate.get("label", "") or "",
                "direction": direction,
            }
        cleaned_steps.append({
            "narration": s.get("narration", ""),
            "highlight": highlight,
            "animate": animate,
            "citations": s.get("citations", []) or [],
        })

    return {
        "repo_id": repo_id,
        "question": request.question,
        "steps": cleaned_steps,
    }


# ── Zoom (per-component deep dive) ───────────────────────────────────────────


class ZoomRequest(BaseModel):
    component_id: str
    question: str = ""
    narration: str = ""


def _chunks_for_component(repo_id: str, component: dict, fallback_query: str) -> list[dict]:
    """Get code chunks relevant to a component.

    Primary strategy: pull chunks from the files the component lists.
    Fallback: semantic retrieval using the fallback_query when the component
    has no files or returned no chunks (e.g. external services).
    """
    collection = get_or_create_collection(repo_id)
    files = (component.get("files") or [])[:_ZOOM_FILES_CAP]
    out: list[dict] = []

    for fp in files:
        try:
            results = collection.get(
                where={"file_path": fp},
                limit=_ZOOM_FILE_CHUNK_CAP,
                include=["documents", "metadatas"],
            )
            docs = results.get("documents") or []
            metas = results.get("metadatas") or []
            for doc, meta in zip(docs, metas):
                out.append({"text": doc, "metadata": meta})
        except Exception:
            continue

    if out:
        return out

    # Fallback — semantic retrieval against the question + component label
    query = f"{component.get('label', '')} {fallback_query}".strip() or component.get("label", "")
    return _retrieve_rag_chunks(repo_id, query, k=5) if query else []


def _build_zoom_prompt(component: dict, question: str, narration: str, chunks: list[dict]) -> str:
    chunks_block = ""
    for c in chunks:
        m = c["metadata"]
        chunks_block += f"[{m.get('file_path', '')}:{m.get('start_line', 1)}]\n"
        chunks_block += f"```{m.get('language', '')}\n{c['text']}\n```\n\n"

    files_list = "\n".join(f"  - {fp}" for fp in (component.get("files") or [])) or "  (none listed)"

    return f"""You are explaining ONE specific component of a system in the context of a user's
question. Focus only on this component — do NOT re-explain the full system or
describe how other components work. Where relevant, reference the current step
of the narrative so your explanation fits the bigger flow.

COMPONENT:
  id:    {component.get("id", "")}
  label: {component.get("label", "")}
  kind:  {component.get("kind", "")}
  files listed for this component:
{files_list}

USER QUESTION: {question or "(general overview)"}
CURRENT STEP NARRATION: {narration or "(no active step)"}

CODE CONTEXT:
{chunks_block or "(no chunks available)"}

Respond with ONLY a single JSON object, no prose, no code fences:

{{
  "deep_dive": "<2-4 short paragraphs explaining this component's internals and role in the current flow. You may use `inline code`. Target ~120-220 words.>",
  "involved_files": [
    {{ "file_path": "<relative path>", "role": "<short role>", "start_line": <int> }}
  ],
  "tools": ["<external lib or framework name>", ...],
  "citations": [
    {{ "file_path": "<path>", "start_line": <int> }}
  ]
}}

Guidelines:
- involved_files: 1-5 files from the provided context that are core to THIS component. Each needs a short role description.
- tools: REQUIRED. 2-8 external libraries, frameworks, languages, or services that make up this component's tech stack (e.g. "FastAPI", "ChromaDB", "sentence-transformers", "Python", "D3.js"). Infer from imports, file extensions, and code patterns in the CODE CONTEXT. Only return [] if there is genuinely no code context at all.
- citations: 2-4 (file_path, start_line) pairs from the CODE CONTEXT that support the deep_dive.
- Do NOT invent files that are not in the provided context.
- Do NOT reference other components except in passing to orient the reader.

JSON:"""


@router.post("/zoom/{repo_id}")
def post_zoom(repo_id: str, request: ZoomRequest):
    if not collection_exists(repo_id):
        raise HTTPException(status_code=404, detail="Repo not indexed")

    # Load the cached system doc so we can look up the component
    cache_path = _DEPS_DIR / f"{repo_id}_system.json"
    if cache_path.exists():
        with open(cache_path, "r", encoding="utf-8") as f:
            system_doc = json.load(f)
    else:
        system_doc = _generate_system_doc(repo_id)

    component = next(
        (c for c in system_doc.get("components", []) if c.get("id") == request.component_id),
        None,
    )
    if component is None:
        raise HTTPException(
            status_code=404,
            detail=f"Unknown component_id {request.component_id!r} for this repo",
        )

    # In-memory cache key — same question + component returns instantly on replay
    q_key = hashlib.md5((request.question + "||" + request.narration).encode("utf-8")).hexdigest()
    cache_key = (repo_id, request.component_id, q_key)
    if cache_key in _zoom_cache:
        return _zoom_cache[cache_key]

    chunks = _chunks_for_component(repo_id, component, fallback_query=request.question)
    prompt = _build_zoom_prompt(component, request.question, request.narration, chunks)

    llm = get_llm("zoom")
    raw = str(llm.complete(prompt))

    parsed = _parse_json_from_llm(raw)
    if not parsed or "deep_dive" not in parsed:
        raise HTTPException(
            status_code=502,
            detail={
                "message": "Nemotron returned invalid zoom JSON.",
                "raw": raw[:800],
            },
        )

    # Normalize & prune fields to known shape
    def _clean_file_ref(obj):
        if not isinstance(obj, dict):
            return None
        fp = obj.get("file_path")
        if not fp:
            return None
        ref = {"file_path": str(fp)}
        if "role" in obj:
            ref["role"] = str(obj.get("role") or "")
        sl = obj.get("start_line")
        try:
            ref["start_line"] = int(sl) if sl is not None else 1
        except Exception:
            ref["start_line"] = 1
        return ref

    result = {
        "repo_id": repo_id,
        "component_id": component.get("id"),
        "component_label": component.get("label"),
        "component_kind": component.get("kind"),
        "deep_dive": str(parsed.get("deep_dive", "")),
        "involved_files": [f for f in (_clean_file_ref(x) for x in (parsed.get("involved_files") or [])) if f],
        "tools": [str(t) for t in (parsed.get("tools") or []) if t],
        "citations": [f for f in (_clean_file_ref(x) for x in (parsed.get("citations") or [])) if f],
    }

    _zoom_cache[cache_key] = result
    return result


# ── Trace impact (single-file change consequence analysis) ───────────────────


_TRACE_FILE_CHUNK_CAP = 6
_TRACE_DEP_CHUNK_CAP = 2
_TRACE_DEP_FILES_CAP = 12


class TraceImpactRequest(BaseModel):
    question: str
    origin_file: str | None = None


def _load_dep_graph(repo_id: str) -> dict[str, list[str]]:
    dep_path = _DEPS_DIR / f"{repo_id}_deps.json"
    if not dep_path.exists():
        return {}
    try:
        with open(dep_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return {k: list(v) if isinstance(v, list) else [] for k, v in data.items()}
    except Exception:
        pass
    return {}


def _guess_origin_file(question: str, all_files: list[str]) -> str | None:
    q = question or ""
    if not q or not all_files:
        return None

    candidates: list[tuple[int, str]] = []
    q_lower = q.lower()
    for fp in all_files:
        if not fp:
            continue
        if fp in q:
            candidates.append((len(fp), fp))
            continue
        name = fp.split("/")[-1]
        if name and len(name) >= 3 and name in q:
            candidates.append((len(name), fp))
            continue
        if fp.lower() in q_lower:
            candidates.append((len(fp), fp))

    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[0][1]


def _chunks_for_file(repo_id: str, file_path: str, cap: int) -> list[dict]:
    if not file_path:
        return []
    try:
        collection = get_or_create_collection(repo_id)
        results = collection.get(
            where={"file_path": file_path},
            limit=cap,
            include=["documents", "metadatas"],
        )
        docs = results.get("documents") or []
        metas = results.get("metadatas") or []
        return [{"text": d, "metadata": m} for d, m in zip(docs, metas)]
    except Exception:
        return []


def _build_trace_prompt(
    question: str,
    origin_file: str | None,
    all_files: list[str],
    direct_dependents: list[str],
    system_doc: dict,
    origin_chunks: list[dict],
    dep_chunks: list[dict],
) -> str:
    def _fmt_chunks(chunks: list[dict]) -> str:
        out = ""
        for c in chunks:
            m = c["metadata"]
            out += f"[{m.get('file_path', '')}:{m.get('start_line', 1)}]\n"
            out += f"```{m.get('language', '')}\n{c['text']}\n```\n\n"
        return out or "(none)"

    slim_components = [
        {"id": c["id"], "label": c["label"], "kind": c["kind"], "files": c.get("files", [])[:4]}
        for c in system_doc.get("components", [])
    ]
    slim_edges = [
        {"id": e["id"], "from": e["from"], "to": e["to"], "label": e["label"]}
        for e in system_doc.get("edges", [])
    ]
    system_block = json.dumps({"components": slim_components, "edges": slim_edges}, indent=2)

    files_block = "\n".join(all_files[:300])
    direct_block = "\n".join(f"  - {fp}" for fp in direct_dependents[:40]) or "  (none — file has no static reverse deps)"

    origin_line = origin_file or "(unknown — infer the most likely target file from the question and file list)"

    return f"""You are a senior engineer performing CHANGE IMPACT ANALYSIS on a single file in
this codebase. The user wants to know how a change/action in ONE specific file
will ripple through the rest of the system. Decide which other files are
SIGNIFICANTLY affected, infer the most likely causal path from the origin file
to the most impacted target file(s), and explain the consequences.

You are the source of truth for SIGNIFICANCE. Static reverse-dependencies are
provided as a hint, but you should weigh them against the question's intent
and include files that are conceptually impacted even if the static graph
misses them. Likewise, prune static dependents that are NOT meaningfully
impacted by the user's change.

Respond with ONLY a single JSON object — no prose, no code fences:

{{
  "origin_file": "<repo-relative path of the file the user is asking about>",
  "primary_path": ["<origin_file>", "<intermediate file>", ..., "<target file>"],
  "related_files": [
    {{
      "file_path": "<repo-relative path>",
      "significance": "high" | "medium" | "low",
      "reason": "<1 short sentence describing why this file is affected>"
    }}
  ],
  "connections": [
    {{
      "from": "<repo-relative path>",
      "to": "<repo-relative path>",
      "kind": "imports" | "calls" | "data" | "event" | "config" | "schema" | "ui",
      "detail": "<short explanation of how the change in `from` propagates to `to`>"
    }}
  ],
  "summary": "<2-5 short paragraphs explaining the consequences of changing the origin file. You may use `inline code` and inline citations like [path:line]. Target ~200-350 words.>",
  "citations": [
    {{ "file_path": "<path>", "start_line": <int> }}
  ]
}}

Guidelines:
- All file paths MUST appear in the REPO FILE LIST below. Do NOT invent files.
- `primary_path` is the single most important causal chain: start at `origin_file`,
  end at the most impacted target file. 2-6 entries.
- `related_files` should list 3-12 OTHER files (excluding files already in
  `primary_path`) that the origin change touches. Rank by significance.
- `connections` describes at least every edge in `primary_path` plus the most
  important origin -> related_file links. 4-15 entries total.
- Inline citations in `summary` use the form [path:line] and MUST refer to
  files in the provided context.

USER QUESTION: {question}

CANDIDATE ORIGIN FILE (heuristic guess — confirm or override): {origin_line}

REPO FILE LIST (truncated to 300):
{files_block}

STATIC REVERSE DEPENDENTS OF ORIGIN (files that import the origin file):
{direct_block}

SYSTEM ARCHITECTURE (logical components & edges):
{system_block}

ORIGIN FILE CODE CONTEXT:
{_fmt_chunks(origin_chunks)}

DEPENDENT FILES CODE CONTEXT:
{_fmt_chunks(dep_chunks)}

JSON:"""


@router.post("/trace_impact/{repo_id}")
def post_trace_impact(repo_id: str, request: TraceImpactRequest):
    if not collection_exists(repo_id):
        raise HTTPException(status_code=404, detail="Repo not indexed")

    files_path = _DEPS_DIR / f"{repo_id}_files.json"
    if not files_path.exists():
        raise HTTPException(status_code=404, detail="File list missing — re-index repo")
    with open(files_path, "r", encoding="utf-8") as f:
        all_files = json.load(f)
    file_set = set(all_files)

    system_cache = _DEPS_DIR / f"{repo_id}_system.json"
    if system_cache.exists():
        with open(system_cache, "r", encoding="utf-8") as f:
            system_doc = json.load(f)
    else:
        system_doc = _generate_system_doc(repo_id)

    dep_graph = _load_dep_graph(repo_id)

    origin_file = (request.origin_file or "").strip().lstrip("/") or None
    if origin_file and origin_file not in file_set:
        origin_file = None
    if not origin_file:
        origin_file = _guess_origin_file(request.question, all_files)

    direct_dependents: list[str] = []
    if origin_file and origin_file in dep_graph:
        direct_dependents = [d for d in dep_graph[origin_file] if d in file_set]

    origin_chunks = _chunks_for_file(repo_id, origin_file, _TRACE_FILE_CHUNK_CAP) if origin_file else []
    dep_chunks: list[dict] = []
    for fp in direct_dependents[:_TRACE_DEP_FILES_CAP]:
        dep_chunks.extend(_chunks_for_file(repo_id, fp, _TRACE_DEP_CHUNK_CAP))

    if not origin_chunks and not dep_chunks:
        origin_chunks = _retrieve_rag_chunks(repo_id, request.question, k=6)

    prompt = _build_trace_prompt(
        question=request.question,
        origin_file=origin_file,
        all_files=all_files,
        direct_dependents=direct_dependents,
        system_doc=system_doc,
        origin_chunks=origin_chunks,
        dep_chunks=dep_chunks,
    )

    llm = get_llm("trace")
    raw = str(llm.complete(prompt))

    parsed = _parse_json_from_llm(raw)
    if not parsed or not isinstance(parsed, dict):
        raise HTTPException(
            status_code=502,
            detail={
                "message": "Nemotron returned invalid trace JSON.",
                "raw": raw[:800],
            },
        )

    def _norm_path(p) -> str | None:
        if not isinstance(p, str):
            return None
        p = p.strip().lstrip("/")
        return p if p in file_set else None

    out_origin = _norm_path(parsed.get("origin_file")) or origin_file

    primary_in = parsed.get("primary_path") or []
    primary_path: list[str] = []
    seen_p: set[str] = set()
    for entry in primary_in:
        fp = _norm_path(entry)
        if fp and fp not in seen_p:
            seen_p.add(fp)
            primary_path.append(fp)
    if out_origin and (not primary_path or primary_path[0] != out_origin):
        primary_path = [out_origin] + [p for p in primary_path if p != out_origin]

    primary_set = set(primary_path)

    related_in = parsed.get("related_files") or []
    related_files: list[dict] = []
    seen_r: set[str] = set()
    for r in related_in:
        if not isinstance(r, dict):
            continue
        fp = _norm_path(r.get("file_path"))
        if not fp or fp in primary_set or fp in seen_r:
            continue
        sig = str(r.get("significance") or "medium").lower()
        if sig not in ("high", "medium", "low"):
            sig = "medium"
        related_files.append({
            "file_path": fp,
            "significance": sig,
            "reason": str(r.get("reason") or "")[:400],
        })
        seen_r.add(fp)

    sig_order = {"high": 0, "medium": 1, "low": 2}
    related_files.sort(key=lambda x: sig_order.get(x["significance"], 1))

    connections_in = parsed.get("connections") or []
    connections: list[dict] = []
    valid_kinds = {"imports", "calls", "data", "event", "config", "schema", "ui"}
    for c in connections_in:
        if not isinstance(c, dict):
            continue
        a = _norm_path(c.get("from"))
        b = _norm_path(c.get("to"))
        if not a or not b or a == b:
            continue
        kind = str(c.get("kind") or "calls").lower()
        if kind not in valid_kinds:
            kind = "calls"
        connections.append({
            "from": a,
            "to": b,
            "kind": kind,
            "detail": str(c.get("detail") or "")[:400],
        })

    def _clean_cite(obj):
        if not isinstance(obj, dict):
            return None
        fp = obj.get("file_path")
        if not isinstance(fp, str) or not fp.strip():
            return None
        ref = {"file_path": fp.strip().lstrip("/")}
        sl = obj.get("start_line")
        try:
            ref["start_line"] = int(sl) if sl is not None else 1
        except Exception:
            ref["start_line"] = 1
        return ref

    citations = [c for c in (_clean_cite(x) for x in (parsed.get("citations") or [])) if c]

    return {
        "repo_id": repo_id,
        "question": request.question,
        "origin_file": out_origin,
        "primary_path": primary_path,
        "related_files": related_files,
        "connections": connections,
        "summary": str(parsed.get("summary") or ""),
        "citations": citations,
    }
