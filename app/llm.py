"""Centralized NVIDIA LLM client selection by role.

Callers request an LLM by role (e.g. "qa", "system_diagram", "narrative"),
not by raw parameters. This keeps route handlers readable and lets us retune
models/tokens/temps in one place — ROLE_CONFIG — or override any field per
role via environment variables.

API key sourcing
    Each role specifies an `api_key_env` — the NAME of the env var to read
    the key from (default: "NVIDIA_API_KEY"). To use a different API key for
    a role, either:
      - edit ROLE_CONFIG[role]["api_key_env"] to point at a different env var,
        e.g. "NANO_API_KEY", and define that var in your .env, OR
      - set the direct per-role override LLM_API_KEY_<ROLE>=nvapi-...

Environment override pattern (optional): LLM_<FIELD>_<ROLE>, upper-cased.
Examples:
    LLM_MODEL_NARRATIVE=nvidia/llama-3.1-nemotron-70b-instruct
    LLM_MAX_TOKENS_QA=8192
    LLM_TEMPERATURE_SYSTEM_DIAGRAM=0.0
    LLM_API_KEY_NARRATIVE=nvapi-xxxxx
"""
from __future__ import annotations

import os
from functools import lru_cache
from typing import Literal

from llama_index.llms.nvidia import NVIDIA

Role = Literal["qa", "system_diagram", "narrative", "zoom"]


# Single source of truth. Adding a role means appending here; call sites stay stable.
#
# Model choice rationale:
#   - "qa" keeps the Nemotron-Super reasoning model — good for free-form code Q&A.
#   - "system_diagram" and "narrative" produce structured JSON; mistral-nemotron
#     is fine-tuned for agentic workflows and function calling, so it emits valid
#     JSON reliably and avoids the silent thinking pass that caused APITimeouts
#     on the reasoning model.
ROLE_CONFIG: dict[str, dict] = {
    "qa": {
        "model": "nvidia/llama-3.3-nemotron-super-49b-v1.5",
        "temperature": 0.1,
        "max_tokens": 4096,
        "timeout": 300.0,
        "api_key_env": "NVIDIA_API_KEY",
    },
    "system_diagram": {
        "model": "mistralai/mistral-nemotron",
        "temperature": 0.1,
        "max_tokens": 2048,
        "timeout": 300.0,
        "api_key_env": "NANO_API_KEY",
    },
    "narrative": {
        "model": "mistralai/mistral-nemotron",
        "temperature": 0.2,
        "max_tokens": 4096,
        "timeout": 300.0,
        "api_key_env": "NANO_API_KEY",
    },
    # Per-component deep-dive for the narrative "zoom in" feature. Smaller output
    # than narrative because it describes a single component, not a full flow.
    "zoom": {
        "model": "mistralai/mistral-nemotron",
        "temperature": 0.1,
        "max_tokens": 1536,
        "timeout": 300.0,
        "api_key_env": "NANO_API_KEY",
    },
}


def _override(role: str, field: str, default):
    """Return env override if set, else the default from ROLE_CONFIG."""
    env_key = f"LLM_{field.upper()}_{role.upper()}"
    return os.getenv(env_key, default)


def _resolve_api_key(role: str, cfg: dict) -> str | None:
    """Resolve the API key for a role.

    Precedence:
      1. Direct per-role env override:  LLM_API_KEY_<ROLE>
      2. ROLE_CONFIG api_key_env        (reads that env var, e.g. NANO_API_KEY)
      3. NVIDIA_API_KEY                 (global fallback)
    """
    direct = os.getenv(f"LLM_API_KEY_{role.upper()}")
    if direct:
        return direct
    env_name = cfg.get("api_key_env", "NVIDIA_API_KEY")
    return os.getenv(env_name) or os.getenv("NVIDIA_API_KEY")


@lru_cache(maxsize=None)
def get_llm(role: Role = "qa") -> NVIDIA:
    """Return the NVIDIA LLM configured for the given role (cached singleton per role)."""
    if role not in ROLE_CONFIG:
        raise ValueError(f"Unknown LLM role {role!r}. Known roles: {list(ROLE_CONFIG)}")
    cfg = ROLE_CONFIG[role]
    api_key = _resolve_api_key(role, cfg)
    if not api_key:
        raise RuntimeError(
            f"No API key available for role {role!r}. Set "
            f"{cfg.get('api_key_env', 'NVIDIA_API_KEY')} or LLM_API_KEY_{role.upper()} in .env"
        )
    return NVIDIA(
        model=_override(role, "model", cfg["model"]),
        api_key=api_key,
        temperature=float(_override(role, "temperature", cfg["temperature"])),
        max_tokens=int(_override(role, "max_tokens", cfg["max_tokens"])),
        timeout=float(_override(role, "timeout", cfg.get("timeout", 60.0))),
    )
