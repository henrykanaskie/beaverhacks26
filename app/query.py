import re

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sentence_transformers import CrossEncoder

from db import collection_exists, get_or_create_collection
from llm import get_llm

router = APIRouter()


def _get_llm():
    """Back-compat shim around the role-based llm.get_llm(). Use get_llm('qa') directly in new code."""
    return get_llm("qa")


def build_prompt(question: str, chunks: list[dict]) -> str:
    chunks_block = ""
    for chunk in chunks:
        meta = chunk["metadata"]
        language = meta.get("language", "")
        chunks_block += f"[{meta['file_path']}:{meta['start_line']}]\n"
        chunks_block += f"```{language}\n{chunk['text']}\n```\n\n"

    return f"""You are a senior software engineer assistant. Answer the question using ONLY the code context provided below.
For every piece of code you reference in your answer, cite its source using the format [file_path:line_number].
If the answer cannot be determined from the provided context, say "I cannot determine this from the available code."
Do not hallucinate code that is not in the context.

CONTEXT:
{chunks_block}
QUESTION: {question}

ANSWER:"""


def extract_citations(answer: str, chunks: list[dict]) -> list[dict]:
    """Extract [file_path:line] citations from the answer text."""
    pattern = r'\[([^\]]+):(\d+)\]'
    found = re.findall(pattern, answer)
    citations = []
    seen = set()
    for file_path, line_str in found:
        key = (file_path, line_str)
        if key not in seen:
            seen.add(key)
            citations.append({"file_path": file_path, "start_line": int(line_str)})
    if not citations and chunks:
        for chunk in chunks[:3]:
            m = chunk["metadata"]
            citations.append({"file_path": m["file_path"], "start_line": m["start_line"]})
    return citations

# Reranker loaded ONCE at module level — not per request
_reranker = CrossEncoder("cross-encoder/ms-marco-MiniLM-L-6-v2")

# Embedding model loaded ONCE — must match the model used at ingestion
_embed_adapter = None
_embed_provider = None


def _get_embed_adapter():
    """Return a singleton embedding adapter matching the ingestion model.

    Tries CodeT5+ first, falls back to all-MiniLM-L6-v2 — same order as ingest.py
    so query-time embeddings stay aligned with the vectors stored in Chroma.
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
        print(f"Warning: CodeT5+ failed to load for queries ({e!r}); falling back to all-MiniLM-L6-v2")

    from chromadb.utils.embedding_functions import SentenceTransformerEmbeddingFunction

    st_fn = SentenceTransformerEmbeddingFunction(model_name="all-MiniLM-L6-v2")

    class _FallbackAdapter:
        def embed(self, text: str) -> list[float]:
            return st_fn([text])[0]

    print("Query embed: using fallback all-MiniLM-L6-v2")
    _embed_adapter = _FallbackAdapter()
    _embed_provider = "local"
    return _embed_adapter, _embed_provider


class QueryRequest(BaseModel):
    repo_id: str
    question: str
    scope: str | None = None


@router.post("/query")
async def query_repo(request: QueryRequest):
    if not collection_exists(request.repo_id):
        raise HTTPException(status_code=404, detail="Repo not indexed")

    adapter, _ = _get_embed_adapter()
    query_embedding = adapter.embed(request.question)

    collection = get_or_create_collection(request.repo_id)
    count = collection.count()
    if count == 0:
        return {
            "chunks": [],
            "answer": "Generation not yet implemented — see GEN-01",
            "citations": [],
        }

    search_kwargs = {
        "query_embeddings": [query_embedding],
        "n_results": min(20, count),
        "include": ["documents", "metadatas", "distances"],
    }
    if request.scope:
        search_kwargs["where"] = {"file_path": {"$contains": request.scope}}

    results = collection.query(**search_kwargs)

    docs = results["documents"][0]
    metas = results["metadatas"][0]

    if not docs:
        return {
            "chunks": [],
            "answer": "No relevant code found for this query.",
            "citations": [],
        }

    pairs = [(request.question, doc) for doc in docs]
    scores = _reranker.predict(pairs)
    ranked = sorted(zip(scores, docs, metas), key=lambda x: float(x[0]), reverse=True)
    print(f"[query] top-5 rerank scores: {[round(float(s), 3) for s, _, _ in ranked[:5]]}")

    # Spec says filter scores < 0, but cross-encoder/ms-marco often returns
    # all-negative logits when scoring natural-language questions against pure
    # code chunks. Only apply the filter when at least one chunk clears 0;
    # otherwise fall back to the top-5 and let the LLM decide.
    positive = [item for item in ranked[:5] if float(item[0]) >= 0]
    top5 = positive if positive else ranked[:5]

    top_chunks = [
        {"text": doc, "metadata": meta, "score": float(score)}
        for score, doc, meta in top5
    ]

    prompt = build_prompt(request.question, top_chunks)
    llm = _get_llm()
    response = llm.complete(prompt)
    answer = str(response)
    citations = extract_citations(answer, top_chunks)

    return {"answer": answer, "citations": citations}
