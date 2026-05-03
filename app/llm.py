"""Shared LLM factories, models, and helpers used by both /query and /agent."""
from __future__ import annotations

import json
import os
from functools import lru_cache

from llama_index.llms.nvidia import NVIDIA
from pydantic import BaseModel

ROLE_CONFIG: dict[str, dict] = {
    "qa": {
        "model": "nvidia/llama-3.3-nemotron-super-49b-v1.5",
        "temperature": 0.1,
        "max_tokens": 2048,
        "api_key_env": "NVIDIA_API_KEY",
    },
    "system_diagram": {
        "model": "mistralai/mistral-nemotron",
        "temperature": 0.1,
        "max_tokens": 2048,
        "api_key_env": "NANO_API_KEY",
    },
    "narrative": {
        "model": "mistralai/mistral-nemotron",
        "temperature": 0.2,
        "max_tokens": 4096,
        "api_key_env": "NANO_API_KEY",
    },
    "zoom": {
        "model": "mistralai/mistral-nemotron",
        "temperature": 0.1,
        "max_tokens": 1536,
        "api_key_env": "NANO_API_KEY",
    },
    "trace": {
        "model": "mistralai/mistral-nemotron",
        "temperature": 0.15,
        "max_tokens": 3072,
        "api_key_env": "NANO_API_KEY",
    },
}

# ── Pydantic models shared across endpoints ──────────────────────────────────

class Turn(BaseModel):
    role: str
    content: str


def _init_nvidia_llm(model: str, max_tokens: int, temperature: float, api_key: str):
    llm = NVIDIA(
        model=model,
        api_key=api_key,
        temperature=temperature,
        max_tokens=max_tokens,
    )
    if llm.is_chat_model is None:
        llm.is_chat_model = True
    if llm.is_function_calling_model is None:
        llm.is_function_calling_model = False
    return llm


def _resolve_api_key(role: str, cfg: dict) -> str | None:
    direct = os.getenv(f"LLM_API_KEY_{role.upper()}")
    if direct:
        return direct
    env_name = cfg.get("api_key_env", "NVIDIA_API_KEY")
    return os.getenv(env_name) or os.getenv("NVIDIA_API_KEY")


@lru_cache(maxsize=None)
def get_llm(role: str = "qa"):
    """Role-based LLM getter, backwards compatible with get_llm() calls."""
    if role not in ROLE_CONFIG:
        raise ValueError(f"Unknown LLM role {role!r}. Known roles: {list(ROLE_CONFIG)}")
    cfg = ROLE_CONFIG[role]
    api_key = _resolve_api_key(role, cfg)
    if not api_key:
        raise RuntimeError(
            f"No API key available for role {role!r}. Set "
            f"{cfg.get('api_key_env', 'NVIDIA_API_KEY')} or LLM_API_KEY_{role.upper()}."
        )
    return _init_nvidia_llm(
        model=cfg["model"],
        max_tokens=int(cfg["max_tokens"]),
        temperature=float(cfg["temperature"]),
        api_key=api_key,
    )


def get_agent_llm():
    """LLM tuned for agent tool-call iterations: lower max_tokens for speed."""
    role = "qa"
    cfg = ROLE_CONFIG[role]
    api_key = _resolve_api_key(role, cfg)
    if not api_key:
        raise RuntimeError("No API key available for agent LLM (qa role).")
    return _init_nvidia_llm(
        model=cfg["model"],
        max_tokens=768,
        temperature=0.05,
        api_key=api_key,
    )


@lru_cache(maxsize=1)
def get_smalltalk_llm():
    """LLM tuned for greetings / meta / general-knowledge replies.

    Tight max_tokens so the model can't ramble — greetings should land in a
    sentence or two, not a paragraph.
    """
    role = "qa"
    cfg = ROLE_CONFIG[role]
    api_key = _resolve_api_key(role, cfg)
    if not api_key:
        raise RuntimeError("No API key available for smalltalk LLM (qa role).")
    return _init_nvidia_llm(
        model=cfg["model"],
        max_tokens=200,
        temperature=0.4,
        api_key=api_key,
    )


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
