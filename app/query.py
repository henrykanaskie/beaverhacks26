from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sentence_transformers import CrossEncoder

from db import collection_exists, get_or_create_collection

router = APIRouter()

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
    top5 = ranked[:5]

    top_chunks = [
        {"text": doc, "metadata": meta, "score": float(score)}
        for score, doc, meta in top5
    ]

    return {
        "chunks": top_chunks,
        "answer": "Generation not yet implemented — see GEN-01",
        "citations": [
            {"file_path": meta["file_path"], "start_line": meta["start_line"]}
            for _, _, meta in top5
        ],
    }
