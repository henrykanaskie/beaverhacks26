import re

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sentence_transformers import CrossEncoder

from db import collection_exists, get_or_create_collection
from llm import (
    Turn,
    extract_citations,
    get_agent_llm,
    get_embed_adapter,
    get_llm,
    iter_deltas,
    ndjson_event,
)

router = APIRouter()

MAX_HISTORY_TURNS = 6
MAX_HISTORY_CHARS_PER_TURN = 1200

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


def _is_direct_answer_ok(question: str) -> bool:
    q = (question or "").strip()
    if not q:
        return True

    if _SMALLTALK_PATTERNS.match(q):
        return True
    if _META_PATTERNS.search(q):
        return True

    if len(q.split()) <= 3 and not re.search(
        r"(file|code|function|class|import|module|dir|folder|main|readme|config)",
        q,
        re.IGNORECASE,
    ):
        return True

    general_markers = re.search(
        r"\b(what is a|explain|define|how does .* work in general|in python|in javascript|algorithm)\b",
        q,
        re.IGNORECASE,
    )
    codebase_markers = re.search(
        r"\b(this|the|our|here|codebase|repo|project|file|where|start|main\.py|readme)\b",
        q,
        re.IGNORECASE,
    )
    if general_markers and not codebase_markers:
        return True

    return False


class QueryRequest(BaseModel):
    repo_id: str
    question: str
    scope: str | None = None
    history: list[Turn] = []


def _format_history(history: list[Turn]) -> str:
    """Render the last N turns as a plain transcript. Empty string if no history."""
    if not history:
        return ""
    recent = [t for t in history if t.role in ("user", "assistant")][-MAX_HISTORY_TURNS:]
    if not recent:
        return ""
    lines = []
    for turn in recent:
        content = turn.content.strip()
        if len(content) > MAX_HISTORY_CHARS_PER_TURN:
            content = content[:MAX_HISTORY_CHARS_PER_TURN] + " …[truncated]"
        speaker = "USER" if turn.role == "user" else "ASSISTANT"
        lines.append(f"{speaker}: {content}")
    return "PRIOR CONVERSATION:\n" + "\n".join(lines) + "\n\n"


def _build_retrieval_query(question: str, history: list[Turn]) -> str:
    """Make a standalone query for embedding by prepending the last user turn.

    Follow-ups like "what about X?" embed poorly on their own — adding the
    previous user question gives the embedding model the missing topic.
    """
    prior_user = next(
        (t.content for t in reversed(history) if t.role == "user"),
        None,
    )
    if not prior_user:
        return question
    return f"{prior_user.strip()}\n{question.strip()}"


def build_prompt(question: str, chunks: list[dict], history: list[Turn] | None = None) -> str:
    """One prompt for every kind of question. The model decides whether to chat,
    explain generally, or ground in CONTEXT, based on the question and what's
    actually in the chunks."""
    if chunks:
        chunks_block = ""
        for chunk in chunks:
            meta = chunk["metadata"]
            language = meta.get("language", "")
            chunks_block += f"[{meta['file_path']}:{meta['start_line']}]\n"
            chunks_block += f"```{language}\n{chunk['text']}\n```\n\n"
        context_section = f"CONTEXT (top relevant code from the indexed repo):\n{chunks_block}"
    else:
        context_section = "CONTEXT: (no code chunks retrieved)\n\n"

    history_block = _format_history(history or [])

    return f"""You are a friendly senior software engineer assistant helping a user understand a codebase. The user has indexed a repo, and CONTEXT below contains the most relevant code chunks for their question.

How to respond:
- For greetings, thanks, or small talk, reply naturally and briefly. No citations.
- For general programming or CS questions that don't reference this specific repo, answer from your own knowledge. No citations.
- For questions about THIS codebase, ground every claim in the CONTEXT and cite each referenced snippet using [file_path:line_number]. Do NOT invent file paths, function names, class names, or code that doesn't appear in the CONTEXT.
- If the user asks a code-specific question and CONTEXT doesn't contain enough information to answer, say so plainly: "I don't have enough code context for that — try asking about a specific file or function." Don't guess.
- Use PRIOR CONVERSATION (if present) to resolve references like "it" or "that function".

{history_block}{context_section}QUESTION: {question}

ANSWER:"""


# Reranker loaded ONCE at module level — not per request
_reranker = CrossEncoder("cross-encoder/ms-marco-MiniLM-L-6-v2")


def _retrieve(question: str, history: list[Turn], repo_id: str, scope: str | None) -> list[dict]:
    """Embed → vector search → rerank. Returns top chunks (possibly empty)."""
    adapter, _ = get_embed_adapter()
    retrieval_query = _build_retrieval_query(question, history)
    query_embedding = adapter.embed(retrieval_query)

    collection = get_or_create_collection(repo_id)
    count = collection.count()
    if count == 0:
        return []

    search_kwargs = {
        "query_embeddings": [query_embedding],
        "n_results": min(20, count),
        "include": ["documents", "metadatas", "distances"],
    }
    if scope:
        search_kwargs["where"] = {"file_path": {"$contains": scope}}

    results = collection.query(**search_kwargs)
    docs = results["documents"][0]
    metas = results["metadatas"][0]
    if not docs:
        return []

    pairs = [(question, doc) for doc in docs]
    scores = _reranker.predict(pairs)
    ranked = sorted(zip(scores, docs, metas), key=lambda x: float(x[0]), reverse=True)
    print(f"[query] top-5 rerank scores: {[round(float(s), 3) for s, _, _ in ranked[:5]]}")

    # Cross-encoder/ms-marco often returns all-negative logits for code chunks.
    # Only filter when at least one clears 0; otherwise keep top-5.
    positive = [item for item in ranked[:5] if float(item[0]) >= 0]
    top5 = positive if positive else ranked[:5]
    return [
        {"text": doc, "metadata": meta, "score": float(score)}
        for score, doc, meta in top5
    ]


def _stream_answer(question: str, history: list[Turn], repo_id: str, scope: str | None):
    """Single response path: status events fill the pre-token wait, then tokens
    stream as the model generates."""
    # Fast path for small talk/general questions: skip retrieval+rereank latency.
    if _is_direct_answer_ok(question):
        yield ndjson_event({"type": "status", "text": "Thinking…"})
        history_block = _format_history(history)
        user_block = f"{history_block}QUESTION: {question}" if history_block else question
        prompt = (
            "You are a concise, friendly software engineer assistant. "
            "Answer naturally. If it's a greeting/small talk, keep it to 1-2 short sentences.\n\n"
            f"USER:\n{user_block}\n\nASSISTANT:\n"
        )
        full = ""
        try:
            for delta in iter_deltas(get_agent_llm().stream_complete(prompt)):
                full += delta
                yield ndjson_event({"type": "token", "text": delta})
        except Exception as e:
            yield ndjson_event({"type": "error", "message": f"generation failed: {e!r}"})
            return
        full = full.strip() or "Hey! Ask me anything about this codebase."
        yield ndjson_event({"type": "done", "citations": [], "answer": full})
        return

    yield ndjson_event({"type": "status", "text": "Searching code…"})
    try:
        top_chunks = _retrieve(question, history, repo_id, scope)
    except Exception as e:
        yield ndjson_event({"type": "error", "message": f"retrieval failed: {e!r}"})
        return

    if top_chunks:
        n = len(top_chunks)
        yield ndjson_event({"type": "status", "text": f"Reading {n} snippet{'s' if n != 1 else ''}…"})
    else:
        yield ndjson_event({"type": "status", "text": "No code matched — answering from general knowledge…"})

    yield ndjson_event({"type": "status", "text": "Thinking…"})

    prompt = build_prompt(question, top_chunks, history)
    full = ""
    try:
        for delta in iter_deltas(get_llm().stream_complete(prompt)):
            full += delta
            yield ndjson_event({"type": "token", "text": delta})
    except Exception as e:
        yield ndjson_event({"type": "error", "message": f"generation failed: {e!r}"})
        return

    citations = extract_citations(full, top_chunks)
    yield ndjson_event({"type": "done", "citations": citations, "answer": full})


_STREAM_MEDIA_TYPE = "application/x-ndjson"


@router.post("/query")
async def query_repo(request: QueryRequest):
    if not collection_exists(request.repo_id):
        raise HTTPException(status_code=404, detail="Repo not indexed")
    return StreamingResponse(
        _stream_answer(
            request.question, request.history, request.repo_id, request.scope
        ),
        media_type=_STREAM_MEDIA_TYPE,
    )
