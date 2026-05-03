"""Shared LLM factories, models, and helpers used by both /query and /agent."""
from __future__ import annotations

import json
import os

from llama_index.llms.nvidia import NVIDIA
from pydantic import BaseModel

MODEL = "nvidia/llama-3.3-nemotron-super-49b-v1.5"

# ── Pydantic models shared across endpoints ──────────────────────────────────

class Turn(BaseModel):
    role: str
    content: str


# ── LLM singletons ──────────────────────────────────────────────────────────

_llm = None
_agent_llm = None


def _init_nvidia_llm(max_tokens: int, temperature: float = 0.1):
    llm = NVIDIA(
        model=MODEL,
        api_key=os.getenv("NVIDIA_API_KEY"),
        temperature=temperature,
        max_tokens=max_tokens,
    )
    if llm.is_chat_model is None:
        llm.is_chat_model = True
    if llm.is_function_calling_model is None:
        llm.is_function_calling_model = False
    return llm


def get_llm():
    """Full LLM for generating final answers (higher max_tokens)."""
    global _llm
    if _llm is None:
        _llm = _init_nvidia_llm(max_tokens=2048)
    return _llm


def get_agent_llm():
    """LLM tuned for agent tool-call iterations: lower max_tokens for speed."""
    global _agent_llm
    if _agent_llm is None:
        _agent_llm = _init_nvidia_llm(max_tokens=768, temperature=0.05)
    return _agent_llm


# ── Embedding adapter (singleton, matches ingestion model) ───────────────────

_embed_adapter = None
_embed_provider = None


def get_embed_adapter():
    """Return a singleton embedding adapter matching the ingestion model.

    Tries CodeT5+ first, falls back to all-MiniLM-L6-v2 — same order as
    ingest.py so query-time embeddings stay aligned with stored vectors.
    """
    global _embed_adapter, _embed_provider
    if _embed_adapter is not None:
        return _embed_adapter, _embed_provider

    try:
        import torch
        from transformers import AutoModel, AutoTokenizer

        tokenizer = AutoTokenizer.from_pretrained(
            "Salesforce/codet5p-110m-embedding", trust_remote_code=True
        )
        model = AutoModel.from_pretrained(
            "Salesforce/codet5p-110m-embedding", trust_remote_code=True
        )
        model.eval()

        class _CodeT5Adapter:
            def embed(self, text: str) -> list[float]:
                inputs = tokenizer(
                    [text],
                    padding=True,
                    truncation=True,
                    max_length=512,
                    return_tensors="pt",
                )
                with torch.no_grad():
                    outputs = model(**inputs)
                return outputs[0].tolist()

        print("Query embed: using Salesforce/codet5p-110m-embedding")
        _embed_adapter = _CodeT5Adapter()
        _embed_provider = "codet5p"
        return _embed_adapter, _embed_provider
    except Exception as e:
        print(
            f"Warning: CodeT5+ failed to load for queries ({e!r}); "
            "falling back to all-MiniLM-L6-v2"
        )

    from chromadb.utils.embedding_functions import SentenceTransformerEmbeddingFunction

    st_fn = SentenceTransformerEmbeddingFunction(model_name="all-MiniLM-L6-v2")

    class _FallbackAdapter:
        def embed(self, text: str) -> list[float]:
            return st_fn([text])[0]

    print("Query embed: using fallback all-MiniLM-L6-v2")
    _embed_adapter = _FallbackAdapter()
    _embed_provider = "local"
    return _embed_adapter, _embed_provider


# ── Small helpers ────────────────────────────────────────────────────────────

def extract_citations(answer: str, chunks: list[dict] | None = None) -> list[dict]:
    """Extract [file_path:line] citations the model actually wrote."""
    import re as _re

    pattern = r'\[([^\]]+):(\d+)\]'
    found = _re.findall(pattern, answer)
    citations = []
    seen: set[tuple[str, str]] = set()
    for file_path, line_str in found:
        key = (file_path, line_str)
        if key not in seen:
            seen.add(key)
            citations.append({"file_path": file_path, "start_line": int(line_str)})
    return citations


def ndjson_event(obj: dict) -> str:
    """One NDJSON line."""
    return json.dumps(obj) + "\n"


def iter_deltas(stream):
    """Yield string deltas from a llama_index streaming response.

    Some wrappers expose ``.delta`` per chunk; others only ``.text``
    (cumulative). Handle both.
    """
    last = ""
    for chunk in stream:
        delta = getattr(chunk, "delta", None)
        if delta is None:
            text = getattr(chunk, "text", "") or ""
            delta = text[len(last):]
            last = text
        if delta:
            yield delta
