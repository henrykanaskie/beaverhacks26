import chromadb

# Persistent client — data survives restarts
_client = chromadb.PersistentClient(path="./chroma_data")


def get_client() -> chromadb.Client:
    return _client


def get_or_create_collection(repo_id: str) -> chromadb.Collection:
    """Get or create a Chroma collection for a given repo_id."""
    return _client.get_or_create_collection(
        name=repo_id,
        metadata={"hnsw:space": "cosine"},
    )


def collection_exists(repo_id: str) -> bool:
    """Check if a collection already exists without creating it."""
    try:
        _client.get_collection(name=repo_id)
        return True
    except Exception:
        return False
