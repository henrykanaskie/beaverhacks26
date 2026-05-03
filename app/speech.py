"""Speech query endpoint — POD-01 (text fallback) + POD-02 (Nemotron VoiceChat).

Request
-------
  repo_id       str            — indexed repository identifier
  transcript    str            — user's spoken question as text (required for fallback)
  audio_base64  str | None     — base64-encoded webm/wav audio (triggers VoiceChat path)

Response
--------
  transcript    str
  answer        str | None     — present on text fallback path
  citations     list[dict]     — present on text fallback path
  audio_base64  str | None     — present when VoiceChat returns audio

Path selection
--------------
  audio_base64 provided  →  retrieve top-3 chunks → call VoiceChat → return audio
  VoiceChat fails/absent →  graceful fallback to text Q&A pipeline (never raises 500)
  no audio provided      →  text Q&A pipeline directly
"""
from __future__ import annotations

import base64
import os
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from db import collection_exists, get_or_create_collection
from llm import get_llm
from query import (
    _get_embed_adapter,
    _reranker,
    build_prompt,
    extract_citations,
)

router = APIRouter()

# VoiceChat model + endpoint (nvidia/nemotron-voicechat via NIM)
_VOICECHAT_MODEL = os.getenv("VOICECHAT_MODEL", "nvidia/nemotron-voicechat")
_VOICECHAT_URL = os.getenv(
    "VOICECHAT_URL",
    "https://integrate.api.nvidia.com/v1/audio/voice",
)


# ── Pydantic models ───────────────────────────────────────────────────────────

class SpeechRequest(BaseModel):
    repo_id: str
    transcript: str
    audio_base64: str | None = None


class SpeechResponse(BaseModel):
    transcript: str
    answer: str | None = None
    citations: list[dict] = []
    audio_base64: str | None = None


# ── RAG helpers ───────────────────────────────────────────────────────────────

async def _retrieve_top_chunks(repo_id: str, question: str, n: int = 3) -> list[dict]:
    """Retrieve and rerank the top-n code chunks for *question*."""
    adapter, _ = _get_embed_adapter()
    query_embedding = adapter.embed(question)

    collection = get_or_create_collection(repo_id)
    count = collection.count()
    if count == 0:
        return []

    results = collection.query(
        query_embeddings=[query_embedding],
        n_results=min(max(n * 4, 12), count),
        include=["documents", "metadatas", "distances"],
    )

    docs = results["documents"][0]
    metas = results["metadatas"][0]
    if not docs:
        return []

    pairs = [(question, doc) for doc in docs]
    scores = _reranker.predict(pairs)
    ranked = sorted(zip(scores, docs, metas), key=lambda x: float(x[0]), reverse=True)
    return [
        {"text": doc, "metadata": meta, "score": float(score)}
        for score, doc, meta in ranked[:n]
    ]


def _build_voice_system_prompt(chunks: list[dict]) -> str:
    """Build a concise system prompt for VoiceChat (top-3 chunk limit)."""
    context_lines: list[str] = []
    for chunk in chunks:
        meta = chunk["metadata"]
        end = meta.get("end_line", meta["start_line"])
        context_lines.append(
            f"[{meta['file_path']}:{meta['start_line']}-{end}]\n{chunk['text']}"
        )
    context = "\n\n".join(context_lines)
    return (
        "You are a helpful code assistant. Answer the user's spoken question about their "
        "codebase using the code context below. Be concise and conversational — your "
        "response will be spoken aloud.\n\nCONTEXT:\n" + context
    )


# ── VoiceChat client ──────────────────────────────────────────────────────────

async def _call_voicechat(audio_bytes: bytes, system_prompt: str) -> bytes | None:
    """Call NVIDIA VoiceChat API.

    Returns raw audio bytes on success, None if the model is unavailable or
    the call fails.  Callers must never propagate exceptions from here.
    """
    api_key = os.getenv("NVIDIA_API_KEY")
    if not api_key:
        return None

    audio_b64 = base64.b64encode(audio_bytes).decode()
    payload = {
        "model": _VOICECHAT_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": [
                    {"type": "audio", "audio": {"data": audio_b64, "format": "webm"}},
                ],
            },
        ],
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=90.0) as client:
        resp = await client.post(_VOICECHAT_URL, json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()
        # Expected: choices[0].message.audio.data (base64-encoded audio)
        audio_data = (
            data.get("choices", [{}])[0]
            .get("message", {})
            .get("audio", {})
            .get("data")
        )
        if audio_data:
            return base64.b64decode(audio_data)
    return None


# ── Text fallback (reuses Q&A pipeline) ──────────────────────────────────────

async def _text_fallback(repo_id: str, transcript: str) -> dict[str, Any]:
    """Run the standard Q&A retrieval+generation pipeline as a text fallback."""
    adapter, _ = _get_embed_adapter()
    query_embedding = adapter.embed(transcript)

    collection = get_or_create_collection(repo_id)
    count = collection.count()
    if count == 0:
        return {"answer": "No indexed code found for this repository.", "citations": []}

    results = collection.query(
        query_embeddings=[query_embedding],
        n_results=min(20, count),
        include=["documents", "metadatas", "distances"],
    )

    docs = results["documents"][0]
    metas = results["metadatas"][0]
    if not docs:
        return {"answer": "No relevant code found for this query.", "citations": []}

    pairs = [(transcript, doc) for doc in docs]
    scores = _reranker.predict(pairs)
    ranked = sorted(zip(scores, docs, metas), key=lambda x: float(x[0]), reverse=True)

    positive = [item for item in ranked[:5] if float(item[0]) >= 0]
    top5 = positive if positive else ranked[:5]
    top_chunks = [
        {"text": doc, "metadata": meta, "score": float(score)}
        for score, doc, meta in top5
    ]

    prompt = build_prompt(transcript, top_chunks)
    llm = get_llm("qa")
    response = llm.complete(prompt)
    answer = str(response)
    citations = extract_citations(answer, top_chunks)
    return {"answer": answer, "citations": citations}


# ── Route ─────────────────────────────────────────────────────────────────────

@router.post("/speech", response_model=SpeechResponse)
async def speech_query(request: SpeechRequest) -> SpeechResponse:
    if not collection_exists(request.repo_id):
        raise HTTPException(status_code=404, detail="Repo not indexed")

    # VoiceChat path — only attempted when audio is provided
    if request.audio_base64:
        try:
            audio_bytes = base64.b64decode(request.audio_base64)
            chunks = await _retrieve_top_chunks(request.repo_id, request.transcript, n=3)
            system_prompt = _build_voice_system_prompt(chunks)
            audio_out = await _call_voicechat(audio_bytes, system_prompt)
            if audio_out:
                return SpeechResponse(
                    transcript=request.transcript,
                    audio_base64=base64.b64encode(audio_out).decode(),
                )
        except Exception as exc:
            # VoiceChat is not yet GA — always fall through gracefully
            print(f"[speech] VoiceChat unavailable, falling back to text: {exc!r}")

    # Text fallback — runs when VoiceChat is absent/fails or no audio was sent
    result = await _text_fallback(request.repo_id, request.transcript)
    return SpeechResponse(
        transcript=request.transcript,
        answer=result["answer"],
        citations=result["citations"],
        audio_base64=None,
    )
