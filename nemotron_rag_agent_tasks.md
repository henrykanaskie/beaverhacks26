# Nemotron Code RAG — Agent Task List

Each task is fully self-contained. Agents must not assume context from other tasks.
Read the GLOBAL CONTEXT block before executing any task.

---

# GLOBAL CONTEXT

## What we are building

A web app where users paste a public GitHub or GitLab URL. The system clones the repo,
indexes the code, and lets users interact via four features:

1. **Chat + RAG** — natural language Q&A, scoped to selected files, with cited answers
2. **Trace** — given a file, highlights every file that imports or references it
3. **Podcast** — full speech-to-speech conversation powered by Nemotron VoiceChat
4. **Architecture diagram** — interactive collapsible tree showing the physical folder and file structure of the repo; folder nodes expand and collapse, file nodes open the code viewer

## Tech stack (complete, no omissions)


| Layer                | Technology                                                            |
| -------------------- | --------------------------------------------------------------------- |
| Backend              | FastAPI + uvicorn (Python 3.11+)                                      |
| Git cloning          | GitPython                                                             |
| Code chunking        | LlamaIndex CodeSplitter (AST-based, tree-sitter backend)              |
| Embedding            | Salesforce/codet5p-110m-embedding (local, via transformers)           |
| Vector store         | Chroma DB                                                             |
| Reranker             | cross-encoder/ms-marco-MiniLM-L-6-v2 via SentenceTransformers (local) |
| LLM (text)           | nvidia/llama-3.1-nemotron-70b-instruct via NVIDIA API Catalog         |
| LLM (voice)          | nvidia/nemotron-voicechat via NVIDIA API Catalog (Early Access)       |
| Dependency analysis  | tree-sitter AST import parsing                                        |
| Architecture diagram | D3.js v7 collapsible tree layout (cdnjs.cloudflare.com only)          |
| Frontend             | Plain HTML + JS + CSS — tailwind framework                            |


## Directory structure (strict — all agents must use these paths)

```
/app/
  main.py           # FastAPI entry point, imports all routers
  ingest.py         # POST /index, GET /status/{repo_id}
  query.py          # POST /query
  files.py          # GET /files/{repo_id}, GET /file/{repo_id}, GET /architecture/{repo_id}
  trace.py          # POST /trace
  speech.py         # POST /speech
  deps_builder.py   # Dependency graph builder — called by ingestion, not a router
  deps/             # Per-repo JSON files:
                    #   {repo_id}_deps.json  — reverse graph (used by Trace)
                    #   {repo_id}_files.json — flat file list (used by GET /files and Architecture)
  clones/           # Temporary clone directory — always deleted after indexing
  static/
    index.html
    app.js
    style.css
.env
requirements.txt
```

## Environment variables (all tasks use these exact names)

```
NVIDIA_API_KEY=...          # From build.nvidia.com
MAX_FILES_PER_REPO=2000     # File count guard — reject repos above this
CLONE_TIMEOUT_SECONDS=600   # Kill clone if it exceeds this
```

## Key constants (hardcoded, never change these without updating all tasks)

```python
import hashlib
repo_id = hashlib.md5(repo_url.encode()).hexdigest()   # Unique ID per repo URL
chroma_collection_name = repo_id                        # One collection per repo
dep_graph_path = f"deps/{repo_id}_deps.json"           # Reverse dependency graph (Trace)
files_list_path = f"deps/{repo_id}_files.json"         # Flat file list (Architecture + GET /files)
clone_dir = f"clones/{repo_id}"                        # Temporary clone location
```

## Chunk metadata schema (every chunk written to Chroma must carry these fields)

```python
metadata = {
    "file_path": "auth/login.py",    # Relative to repo root — no leading slash
    "language": "python",            # Lowercase: python, javascript, typescript, go, rust, java, cpp, csharp
    "start_line": 42,                # 1-indexed
    "end_line": 78,                  # 1-indexed, inclusive
    "content_type": "code"           # Always "code" for now
}
```

## Embedding rules (CRITICAL — violating these silently breaks retrieval)

- Model: `Salesforce/codet5p-110m-embedding` — runs locally via `transformers` + `torch`, no API key needed
- Used for BOTH ingestion and queries, no exceptions
- Loaded once via `AutoModel.from_pretrained("Salesforce/codet5p-110m-embedding", trust_remote_code=True)`
- Fallback: `all-MiniLM-L6-v2` via `chromadb.utils.embedding_functions.SentenceTransformerEmbeddingFunction` if CodeT5+ fails to load
- Never mix embedding models between ingestion and query — if you re-embed, re-index everything

## Complete API contracts

```
POST /index
  Request:  { "repo_url": "https://github.com/owner/repo" }
  Response: { "repo_id": "abc123", "status": "indexed", "chunk_count": 847 }
  Errors:   400 { "detail": "Invalid URL — only public GitHub/GitLab URLs accepted" }
            400 { "detail": "Repo too large — X files exceeds limit of 2000" }
            400 { "detail": "Auth required — only public repos supported" }
            408 { "detail": "Clone timed out after 60s" }
            500 { "detail": "Indexing failed: [reason]" }

GET /status/{repo_id}
  Response: { "repo_id": "abc123", "indexed": true }
  Errors:   404 { "detail": "Repo not found" }

POST /query
  Request:  { "repo_id": "abc123", "question": "how does auth work?", "scope": "auth/" }
            scope is optional — omit for whole-codebase search
  Response: { "answer": "...", "citations": [{ "file_path": "auth/login.py", "start_line": 42 }] }
  Errors:   404 { "detail": "Repo not indexed" }

GET /files/{repo_id}
  Response: { "repo_id": "abc123", "files": ["auth/login.py", "auth/jwt.py"] }
  Errors:   404

GET /file/{repo_id}?path=auth/login.py
  Response: { "file_path": "auth/login.py", "language": "python", "content": "..." }
  Errors:   404

POST /trace
  Request:  { "repo_id": "abc123", "file_path": "auth/login.py" }
  Response: { "file_path": "auth/login.py", "affected_files": ["app.py", "middleware/jwt.py"] }
  Errors:   404 { "detail": "Repo not indexed" }
            404 { "detail": "File not found in dependency graph" }

POST /speech
  Request:  { "repo_id": "abc123", "audio_base64": "<base64 encoded audio>" }
  Response: { "audio_base64": "...", "transcript": "...", "answer": "..." }
            If VoiceChat unavailable, answer is text only — audio_base64 will be null
  Errors:   404, 503

GET /architecture/{repo_id}
  Response: {
    "repo_id": "abc123",
    "tree": {
      "id": "/", "name": "/", "type": "folder",
      "children": [
        {
          "id": "auth", "name": "auth", "type": "folder",
          "children": [
            { "id": "auth/login.py", "name": "login.py", "type": "file", "language": "python" },
            { "id": "auth/jwt.py",   "name": "jwt.py",   "type": "file", "language": "python" }
          ]
        },
        { "id": "app.py", "name": "app.py", "type": "file", "language": "python" }
      ]
    },
    "total_files": 47,
    "total_folders": 12
  }
  Notes:    Edges represent physical containment (folder contains file/subfolder).
            Tree is derived from the flat file list — no import parsing involved.
            Folders are sorted before files at each level.
  Errors:   404 { "detail": "Repo not indexed" }
            404 { "detail": "File list not found — re-index the repo" }
```

## Included/excluded file extensions

```python
INCLUDE_EXTENSIONS = {".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java", ".cpp", ".cs"}
EXCLUDE_DIRS = {"node_modules", ".venv", "venv", "dist", "build", "__pycache__", ".git",
                "vendor", "site-packages", ".next", "coverage", ".mypy_cache"}
```

## Language map (extension → tree-sitter language name)

```python
LANGUAGE_MAP = {
    ".py": "python", ".js": "javascript", ".ts": "typescript",
    ".tsx": "tsx", ".jsx": "javascript", ".go": "go",
    ".rs": "rust", ".java": "java", ".cpp": "cpp", ".cs": "c_sharp"
}
```

---

# TASKS

---

## INFRA-01: Create project structure and install dependencies

**Epic**: Infrastructure
**Depends on**: nothing
**Blocks**: all other tasks

**Objective**: Create all directories and files, install all Python packages, pre-download the reranker model so it is available at runtime without a network call.

**Implementation**:

1. Create the directory structure exactly as shown in GLOBAL CONTEXT.
2. Create `/app/requirements.txt` with this exact content:

```
fastapi==0.115.0
uvicorn[standard]==0.30.6
gitpython==3.1.43
llama-index==0.12.0
llama-index-llms-nvidia
chromadb==0.5.15
transformers
torch
tree-sitter>=0.25.2
python-dotenv==1.0.1
tree-sitter-language-pack==1.6.2
```

1. Run: `pip install -r /app/requirements.txt`
2. Create `/app/.env` with placeholder values (a human will fill in real keys):

```
NVIDIA_API_KEY=REPLACE_ME
MAX_FILES_PER_REPO=2000
CLONE_TIMEOUT_SECONDS=600
```

1. Pre-download models so they are never downloaded at request time:

```python
from transformers import AutoModel, AutoTokenizer
AutoTokenizer.from_pretrained("Salesforce/codet5p-110m-embedding", trust_remote_code=True)
AutoModel.from_pretrained("Salesforce/codet5p-110m-embedding", trust_remote_code=True)
print("CodeT5+ embedding model downloaded successfully")

from sentence_transformers import CrossEncoder
CrossEncoder("cross-encoder/ms-marco-MiniLM-L-6-v2")
print("Reranker downloaded successfully")
```

1. Create empty placeholder files for all modules: `main.py`, `ingest.py`, `query.py`, `files.py`, `trace.py`, `speech.py` — each containing only a comment `# TODO: implemented by [task ID]`.

**Deliverables**:

- `/app/requirements.txt`
- `/app/.env`
- `/app/deps/` directory (empty)
- `/app/clones/` directory (empty)
- `/app/static/` directory (empty)
- All `.py` stub files
- CodeT5+ model cached at `~/.cache/huggingface/hub/`
- Reranker model cached at `~/.cache/torch/sentence_transformers/`

**Acceptance criteria**:

- `python -c "import fastapi, chromadb, llama_index, transformers, torch"` exits with code 0
- `python -c "from transformers import AutoModel; AutoModel.from_pretrained('Salesforce/codet5p-110m-embedding', trust_remote_code=True)"` loads successfully
- All directories exist: `/app/deps/`, `/app/clones/`, `/app/static/`

**Do not**:

- Do not install voyageai, openai, anthropic, or any other LLM/embedding library — Salesforce CodeT5+ (local) and NVIDIA only
- Do not hardcode real API keys in any file

---

## INFRA-02: Set up FastAPI application skeleton

**Epic**: Infrastructure
**Depends on**: INFRA-01
**Blocks**: all API tasks

**Objective**: Create a running FastAPI app in `main.py` with CORS enabled, `.env` loaded, all route stubs registered, and a health check endpoint that returns 200.

**Implementation**:

Create `/app/main.py`:

```python
import os
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from dotenv import load_dotenv

load_dotenv()  # Loads .env from current directory

app = FastAPI(title="Nemotron Code RAG")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],        # Restrict in production
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/health")
def health():
    return {"status": "ok"}

# Import and include routers once each module is implemented
# from ingest import router as ingest_router
# from query import router as query_router
# from files import router as files_router
# from trace import router as trace_router
# from speech import router as speech_router
# app.include_router(ingest_router)
# app.include_router(query_router)
# app.include_router(files_router)
# app.include_router(trace_router)
# app.include_router(speech_router)

app.mount("/", StaticFiles(directory="static", html=True), name="static")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
```

**Acceptance criteria**:

- `uvicorn main:app --reload` starts without error
- `curl http://localhost:8000/health` returns `{"status":"ok"}` with HTTP 200
- CORS headers are present on responses (`Access-Control-Allow-Origin: `*)

**Do not**:

- Do not import any router that does not yet exist — keep those lines commented
- Do not use debug=True in production config

---

## INFRA-03: Initialize Chroma DB client

**Epic**: Infrastructure
**Depends on**: INFRA-01
**Blocks**: ING-01, QRY-01

**Objective**: Create a shared Chroma DB client module that all other modules import. Client must be a singleton — created once at startup, reused everywhere.

**Implementation**:

Create `/app/db.py`:

```python
import chromadb

# Persistent client — data survives restarts
_client = chromadb.PersistentClient(path="./chroma_data")

def get_client() -> chromadb.Client:
    return _client

def get_or_create_collection(repo_id: str) -> chromadb.Collection:
    """Get or create a Chroma collection for a given repo_id."""
    return _client.get_or_create_collection(
        name=repo_id,
        metadata={"hnsw:space": "cosine"}   # Cosine similarity for embedding comparison
    )

def collection_exists(repo_id: str) -> bool:
    """Check if a collection already exists without creating it."""
    try:
        _client.get_collection(name=repo_id)
        return True
    except Exception:
        return False
```

**Deliverables**: `/app/db.py`

**Acceptance criteria**:

- `python -c "from db import get_client, collection_exists; print(collection_exists('test'))"` prints `False` without error
- `/app/chroma_data/` directory is created automatically on first import

**Do not**:

- Do not use `chromadb.Client()` (in-memory, not persistent) — always use `PersistentClient`
- Do not create a new client per request — the module-level `_client` is the singleton

---

## ING-01: Ingestion endpoint — URL validation and already-indexed check

**Epic**: Ingestion
**Depends on**: INFRA-02, INFRA-03
**Blocks**: ING-02

**Objective**: Implement the `POST /index` endpoint up to and including: parse the request, validate the URL, generate `repo_id`, check if already indexed, and return immediately if so.

**System context**:

- `repo_id = hashlib.md5(repo_url.encode()).hexdigest()` — this is the unique identifier for a repo
- A repo is "already indexed" if its Chroma collection exists (use `collection_exists(repo_id)` from `db.py`)
- Valid URLs: must start with `https://github.com/` or `https://gitlab.com/` and must NOT contain `@` (which indicates auth)

**Implementation**:

Create `/app/ingest.py` with this section:

```python
import hashlib
import os
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from db import collection_exists

router = APIRouter()

class IndexRequest(BaseModel):
    repo_url: str

def validate_url(repo_url: str) -> None:
    """Raise HTTPException if URL is not a valid public GitHub/GitLab URL."""
    if "@" in repo_url:
        raise HTTPException(status_code=400, detail="Auth required — only public repos supported")
    if not (repo_url.startswith("https://github.com/") or repo_url.startswith("https://gitlab.com/")):
        raise HTTPException(status_code=400, detail="Invalid URL — only public GitHub/GitLab URLs accepted")

def get_repo_id(repo_url: str) -> str:
    return hashlib.md5(repo_url.encode()).hexdigest()

@router.post("/index")
async def index_repo(request: IndexRequest):
    validate_url(request.repo_url)
    repo_id = get_repo_id(request.repo_url)

    # Already indexed — return immediately without re-processing
    if collection_exists(repo_id):
        from db import get_or_create_collection
        collection = get_or_create_collection(repo_id)
        return {"repo_id": repo_id, "status": "already_indexed", "chunk_count": collection.count()}

    # Remaining steps implemented by ING-02 through ING-07
    # Placeholder until those tasks complete:
    raise HTTPException(status_code=501, detail="Indexing not yet implemented")
```

Also implement the status endpoint:

```python
@router.get("/status/{repo_id}")
def get_status(repo_id: str):
    if not collection_exists(repo_id):
        raise HTTPException(status_code=404, detail="Repo not found")
    return {"repo_id": repo_id, "indexed": True}
```

Uncomment `from ingest import router as ingest_router` and `app.include_router(ingest_router)` in `main.py`.

**Acceptance criteria**:

- `POST /index { "repo_url": "git@github.com/x" }` returns 400 (@ in URL)
- `POST /index { "repo_url": "https://notgithub.com/x" }` returns 400
- `POST /index { "repo_url": "https://github.com/pallets/flask" }` returns 501 (not yet implemented)
- `GET /status/unknownid` returns 404
- Calling index twice on same URL returns `already_indexed` on second call (once full pipeline exists)

**Do not**:

- Do not allow `http://` URLs — HTTPS only
- Do not allow GitHub Enterprise or other domains — only github.com and gitlab.com

---

## ING-02: Git clone with shallow depth and timeout

**Epic**: Ingestion
**Depends on**: ING-01
**Blocks**: ING-03

**Objective**: Clone a public repo into `clones/{repo_id}/` using `depth=1` (shallow), enforce a 60-second timeout, handle failures gracefully, and clean up on any error.

**System context**:

- Clone target directory: `clones/{repo_id}/` relative to `/app/`
- `depth=1` is non-negotiable — without it, repos with long histories take minutes just to clone
- On any failure (timeout, network error, private repo), the clone directory must be deleted and an error returned
- The clone directory is always deleted after indexing completes (in ING-07) — this step only creates it

**Implementation**:

Add to `/app/ingest.py`:

```python
import shutil
import signal
import git

def clone_repo(repo_url: str, repo_id: str) -> str:
    """
    Clone repo to clones/{repo_id}/ with depth=1.
    Returns the clone path. Raises HTTPException on failure.
    Always call cleanup_clone(repo_id) in a finally block after calling this.
    """
    clone_path = f"clones/{repo_id}"
    timeout = int(os.getenv("CLONE_TIMEOUT_SECONDS", "60"))

    if os.path.exists(clone_path):
        shutil.rmtree(clone_path)   # Clean any previous partial clone

    def _timeout_handler(signum, frame):
        raise TimeoutError("Clone timed out")

    signal.signal(signal.SIGALRM, _timeout_handler)
    signal.alarm(timeout)
    try:
        git.Repo.clone_from(repo_url, clone_path, depth=1)
        signal.alarm(0)   # Cancel timeout
        return clone_path
    except TimeoutError:
        shutil.rmtree(clone_path, ignore_errors=True)
        raise HTTPException(status_code=408, detail=f"Clone timed out after {timeout}s")
    except git.GitCommandError as e:
        shutil.rmtree(clone_path, ignore_errors=True)
        if "Authentication" in str(e) or "could not read Username" in str(e):
            raise HTTPException(status_code=400, detail="Auth required — only public repos supported")
        raise HTTPException(status_code=500, detail=f"Clone failed: {str(e)}")
    finally:
        signal.alarm(0)

def cleanup_clone(repo_id: str):
    """Delete the clone directory. Always call this in a finally block."""
    clone_path = f"clones/{repo_id}"
    shutil.rmtree(clone_path, ignore_errors=True)
```

Note: `signal.SIGALRM` is Unix only. If running on Windows, replace with subprocess timeout.

**Acceptance criteria**:

- Cloning `https://github.com/pallets/flask` produces a directory at `clones/{repo_id}/`
- The clone has no git history beyond the tip commit (verify with `git log --oneline | wc -l` showing 1)
- Cloning an unreachable URL returns 408 within `CLONE_TIMEOUT_SECONDS + 2` seconds
- On any failure, `clones/{repo_id}/` does not remain on disk

**Do not**:

- Do not use `depth=0` or omit `depth` — always shallow clone
- Do not catch `Exception` broadly — catch specific git and timeout exceptions

---

## ING-03: File walker with include/exclude filters and file count guard

**Epic**: Ingestion
**Depends on**: ING-02
**Blocks**: ING-04

**Objective**: Walk the cloned repo directory, collect all files matching the include extensions while excluding specified directories, and enforce the `MAX_FILES_PER_REPO` limit before chunking begins.

**System context**:

```python
INCLUDE_EXTENSIONS = {".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java", ".cpp", ".cs"}
EXCLUDE_DIRS = {"node_modules", ".venv", "venv", "dist", "build", "__pycache__", ".git",
                "vendor", "site-packages", ".next", "coverage", ".mypy_cache"}
```

MAX_FILES_PER_REPO is read from env: `int(os.getenv("MAX_FILES_PER_REPO", "2000"))`

**Implementation**:

Add to `/app/ingest.py`:

```python
from pathlib import Path

INCLUDE_EXTENSIONS = {".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java", ".cpp", ".cs"}
EXCLUDE_DIRS = {"node_modules", ".venv", "venv", "dist", "build", "__pycache__", ".git",
                "vendor", "site-packages", ".next", "coverage", ".mypy_cache"}
LANGUAGE_MAP = {
    ".py": "python", ".js": "javascript", ".ts": "typescript",
    ".tsx": "tsx", ".jsx": "javascript", ".go": "go",
    ".rs": "rust", ".java": "java", ".cpp": "cpp", ".cs": "c_sharp"
}

def collect_files(clone_path: str) -> list[dict]:
    """
    Walk clone_path and return list of { path: str, language: str, rel_path: str }.
    rel_path is relative to clone_path with no leading slash.
    Raises HTTPException 400 if file count exceeds MAX_FILES_PER_REPO.
    """
    max_files = int(os.getenv("MAX_FILES_PER_REPO", "2000"))
    root = Path(clone_path)
    collected = []

    for file_path in root.rglob("*"):
        if not file_path.is_file():
            continue
        # Skip excluded directories
        if any(excluded in file_path.parts for excluded in EXCLUDE_DIRS):
            continue
        if file_path.suffix not in INCLUDE_EXTENSIONS:
            continue
        rel_path = str(file_path.relative_to(root))
        collected.append({
            "path": str(file_path),
            "rel_path": rel_path,
            "language": LANGUAGE_MAP[file_path.suffix]
        })

    if len(collected) > max_files:
        raise HTTPException(
            status_code=400,
            detail=f"Repo too large — {len(collected)} files exceeds limit of {max_files}"
        )
    return collected
```

**Acceptance criteria**:

- Walking `pallets/flask` returns only `.py` files, none from `.git/`
- `rel_path` values contain no leading `/` (e.g., `src/app.py` not `/src/app.py`)
- A repo with 2001 matching files raises HTTPException 400 with the exact message format shown
- Empty result (no matching files) returns an empty list — not an error

**Do not**:

- Do not silently truncate to `max_files` — either return all files or raise an error
- Do not follow symlinks — use `rglob` without `follow_symlinks=True`

---

## ING-04: AST-based code chunker

**Epic**: Ingestion
**Depends on**: ING-03
**Blocks**: ING-05

**Objective**: Chunk each source file into function/class-level segments using LlamaIndex's `CodeSplitter`. Return a list of chunks with text content and line number metadata.

**System context**:

- `CodeSplitter` from `llama_index.core.node_parser` uses tree-sitter to split at function/class boundaries, not at arbitrary token counts. This preserves code context.
- Chunk parameters: `chunk_lines=40`, `chunk_lines_overlap=5`, `max_chars=1500`
- Each chunk must carry: `file_path` (relative), `language`, `start_line`, `end_line`
- Language is determined from file extension using `LANGUAGE_MAP` (defined in ING-03)

**Implementation**:

Add to `/app/ingest.py`:

```python
from llama_index.core.node_parser import CodeSplitter
from llama_index.core import Document

def chunk_files(files: list[dict]) -> list[dict]:
    """
    Chunk all collected files using AST-aware splitting.
    Returns list of chunks: { text, file_path, language, start_line, end_line }
    Files that fail to parse are skipped with a warning, not crashed on.
    """
    chunks = []
    for file_info in files:
        try:
            with open(file_info["path"], "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
            if not content.strip():
                continue

            splitter = CodeSplitter(
                language=file_info["language"],
                chunk_lines=40,
                chunk_lines_overlap=5,
                max_chars=1500
            )
            doc = Document(text=content)
            nodes = splitter.get_nodes_from_documents([doc])

            for node in nodes:
                # Extract line numbers from node metadata if available
                start_line = node.metadata.get("start_line", 1)
                end_line = node.metadata.get("end_line", len(content.splitlines()))
                chunks.append({
                    "text": node.text,
                    "file_path": file_info["rel_path"],
                    "language": file_info["language"],
                    "start_line": int(start_line),
                    "end_line": int(end_line)
                })
        except Exception as e:
            print(f"Warning: skipping {file_info['rel_path']}: {e}")
            continue
    return chunks
```

**Acceptance criteria**:

- A Python file with 3 functions produces at least 3 chunks
- Each chunk's `text` is valid Python/JS/etc (not cut mid-token)
- `file_path` on each chunk matches the `rel_path` from the input file info
- Files that fail to parse (e.g. binary accidentally included) are skipped, not crashed on

**Do not**:

- Do not use `SentenceSplitter` or `TokenTextSplitter` — these are not code-aware
- Do not use the same `CodeSplitter` instance across different languages — instantiate one per language

---

## ING-05: Dependency graph builder

**Epic**: Ingestion
**Depends on**: ING-03
**Blocks**: TRC-01

**Objective**: Parse import statements from all collected source files using AST analysis. Build a reverse dependency graph mapping each file to the list of files that import it. Save to `deps/{repo_id}_deps.json`.

**System context**:

- This runs at ingestion time so Trace queries are O(1) — just a JSON key lookup
- The graph is a reverse map: `{ "auth/login.py": ["app.py", "middleware/jwt.py"] }`
- We need to parse actual import paths and resolve them to relative file paths in the repo
- For Python: parse `import X` and `from X import Y` → resolve to `.py` file path
- For JS/TS: parse `import X from 'Y'` and `require('Y')` → resolve to `.js/.ts` file path
- Imports that cannot be resolved to a file in the repo (e.g. third-party packages) are ignored

**Implementation**:

Create `/app/deps_builder.py`:

```python
import ast
import json
import os
import re
from pathlib import Path

def build_dependency_graph(files: list[dict], clone_path: str, repo_id: str) -> dict:
    """
    Build reverse dependency graph: { file_path: [files_that_import_it] }
    Saves to deps/{repo_id}_deps.json and returns the graph dict.
    """
    file_set = {f["rel_path"] for f in files}
    forward = {}   # file_path -> [imported_rel_paths]

    for file_info in files:
        imports = _extract_imports(file_info["path"], file_info["language"])
        resolved = _resolve_imports(imports, file_info["rel_path"], file_set, clone_path)
        forward[file_info["rel_path"]] = resolved

    # Reverse the graph
    reverse = {f: [] for f in file_set}
    for src, targets in forward.items():
        for target in targets:
            if target in reverse:
                reverse[target].append(src)

    os.makedirs("deps", exist_ok=True)
    with open(f"deps/{repo_id}_deps.json", "w") as fh:
        json.dump(reverse, fh)
    return reverse

def _extract_imports(file_path: str, language: str) -> list[str]:
    """Extract raw import strings from a file."""
    try:
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
    except Exception:
        return []

    imports = []
    if language == "python":
        try:
            tree = ast.parse(content)
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imports.extend(n.name for n in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imports.append(node.module)
        except SyntaxError:
            pass
    elif language in ("javascript", "typescript", "tsx"):
        # Regex-based for JS/TS — covers both import and require
        imports.extend(re.findall(r"from\s+['\"]([^'\"]+)['\"]", content))
        imports.extend(re.findall(r"require\(['\"]([^'\"]+)['\"]\)", content))
    return imports

def _resolve_imports(imports: list[str], source_rel_path: str, file_set: set, clone_path: str) -> list[str]:
    """Resolve import strings to relative file paths. Returns only paths that exist in the repo."""
    resolved = []
    source_dir = str(Path(source_rel_path).parent)
    for imp in imports:
        candidates = _generate_candidates(imp, source_dir)
        for candidate in candidates:
            if candidate in file_set:
                resolved.append(candidate)
                break
    return resolved

def _generate_candidates(imp: str, source_dir: str) -> list[str]:
    """Generate possible file paths for a given import string."""
    candidates = []
    # Relative import (./foo, ../bar)
    if imp.startswith("."):
        base = str(Path(source_dir) / imp.lstrip("./").replace(".", "/"))
        for ext in [".py", ".js", ".ts", ".tsx"]:
            candidates.append(base + ext)
            candidates.append(base + "/__init__" + ext)
    else:
        # Absolute import — try as a path from repo root
        as_path = imp.replace(".", "/")
        for ext in [".py", ".js", ".ts", ".tsx"]:
            candidates.append(as_path + ext)
            candidates.append(as_path + "/__init__" + ext)
    return candidates
```

**Acceptance criteria**:

- `deps/{repo_id}_deps.json` is created after ingestion
- For `pallets/flask`, `flask/__init__.py` should appear as a key with multiple files that import it
- Third-party imports (e.g. `import requests`) do not appear as keys — only files in the repo
- File runs in under 30 seconds on a 1,000-file repo

**Do not**:

- Do not fail the entire ingestion if one file's imports can't be parsed — skip and continue

---

## ING-06: CodeT5+ embedding and Chroma DB write

**Epic**: Ingestion
**Depends on**: ING-04, INFRA-03
**Blocks**: ING-07

**Objective**: Embed all code chunks using Salesforce/codet5p-110m-embedding (local) in batches of 128, then write each chunk vector with its metadata to the Chroma collection for this repo.

**System context**:

- Embedding model: `Salesforce/codet5p-110m-embedding` — runs locally via `transformers`, no API key
- Batch size: 128 chunks per embedding call
- Chroma collection name = `repo_id`
- Each Chroma document needs: a unique ID, the embedding vector, the text, and the metadata dict
- Unique ID per chunk: `f"{repo_id}_{file_path}_{start_line}_{hash8}"` (deterministic, collision-resistant)

**Implementation**:

Add to `/app/ingest.py`:

```python
import os
import hashlib
import torch
from transformers import AutoModel, AutoTokenizer
from db import get_or_create_collection

_tokenizer = AutoTokenizer.from_pretrained("Salesforce/codet5p-110m-embedding", trust_remote_code=True)
_model = AutoModel.from_pretrained("Salesforce/codet5p-110m-embedding", trust_remote_code=True)
_model.eval()

def _embed_batch(texts: list[str]) -> list[list[float]]:
    inputs = _tokenizer(texts, padding=True, truncation=True, max_length=512, return_tensors="pt")
    with torch.no_grad():
        outputs = _model(**inputs)
    return outputs.tolist()

def embed_and_store(chunks: list[dict], repo_id: str) -> int:
    """Embed chunks in batches and write to Chroma. Returns total chunks stored."""
    collection = get_or_create_collection(repo_id)
    batch_size = 128
    total_stored = 0

    for i in range(0, len(chunks), batch_size):
        batch = chunks[i:i + batch_size]
        texts = [c["text"] for c in batch]
        embeddings = _embed_batch(texts)

        ids = [_chunk_id(repo_id, c) for c in batch]
        metadatas = [{
            "file_path": c["file_path"],
            "language": c["language"],
            "start_line": c["start_line"],
            "end_line": c["end_line"],
            "content_type": "code"
        } for c in batch]

        collection.add(
            ids=ids,
            embeddings=embeddings,
            documents=texts,
            metadatas=metadatas
        )
        total_stored += len(batch)

    return total_stored
```

**Acceptance criteria**:

- After embedding a repo, `collection.count()` is > 0
- Each stored document has all 5 metadata fields: `file_path`, `language`, `start_line`, `end_line`, `content_type`
- IDs are deterministic — re-running embedding on the same file produces the same IDs
- No API key or network connection required for embedding

**Do not**:

- Do not use any remote embedding API (Nomic, OpenAI, etc.)
- Do not send more than 128 texts in a single batch

---

## ING-07: Wire ingestion pipeline, cleanup, and expose file list endpoint

**Epic**: Ingestion
**Depends on**: ING-01, ING-02, ING-03, ING-04, ING-05, ING-06
**Blocks**: QRY-01, TRC-01, FE-01

**Objective**: Wire all ingestion steps into the `POST /index` endpoint. Ensure clone is always deleted on completion or error. Implement `GET /files/{repo_id}` and `GET /file/{repo_id}` endpoints.

**Implementation**:

Update the `index_repo` function in `/app/ingest.py` to call all prior steps:

```python
@router.post("/index")
async def index_repo(request: IndexRequest):
    validate_url(request.repo_url)
    repo_id = get_repo_id(request.repo_url)

    if collection_exists(repo_id):
        from db import get_or_create_collection
        collection = get_or_create_collection(repo_id)
        return {"repo_id": repo_id, "status": "already_indexed", "chunk_count": collection.count()}

    clone_path = None
    try:
        clone_path = clone_repo(request.repo_url, repo_id)
        files = collect_files(clone_path)
        chunks = chunk_files(files)
        build_dependency_graph(files, clone_path, repo_id)  # from deps_builder.py
        chunk_count = embed_and_store(chunks, repo_id)

        # Store file list for GET /files endpoint
        import json
        os.makedirs("deps", exist_ok=True)
        with open(f"deps/{repo_id}_files.json", "w") as fh:
            json.dump([f["rel_path"] for f in files], fh)

        return {"repo_id": repo_id, "status": "indexed", "chunk_count": chunk_count}
    finally:
        if clone_path:
            cleanup_clone(repo_id)   # Always runs, even on error
```

Also create `/app/files.py` for the file content endpoints:

```python
import json
import os
from fastapi import APIRouter, HTTPException
from db import collection_exists

router = APIRouter()

@router.get("/files/{repo_id}")
def list_files(repo_id: str):
    if not collection_exists(repo_id):
        raise HTTPException(status_code=404, detail="Repo not indexed")
    path = f"deps/{repo_id}_files.json"
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="File list not found")
    with open(path) as f:
        return {"repo_id": repo_id, "files": json.load(f)}

@router.get("/file/{repo_id}")
def get_file_content(repo_id: str, path: str):
    # path is a query parameter: /file/{repo_id}?path=auth/login.py
    # We cannot serve this without re-cloning, so serve from Chroma documents instead
    if not collection_exists(repo_id):
        raise HTTPException(status_code=404, detail="Repo not indexed")
    from db import get_or_create_collection
    collection = get_or_create_collection(repo_id)
    results = collection.query(
        query_texts=[""],
        where={"file_path": path},
        n_results=100,
        include=["documents", "metadatas"]
    )
    if not results["documents"][0]:
        raise HTTPException(status_code=404, detail="File not found")
    # Reconstruct file content from sorted chunks
    chunks = sorted(zip(results["documents"][0], results["metadatas"][0]),
                    key=lambda x: x[1]["start_line"])
    content = "\n".join(c[0] for c in chunks)
    language = chunks[0][1]["language"] if chunks else "unknown"
    return {"file_path": path, "language": language, "content": content}
```

Uncomment all router imports in `main.py` once all router files exist.

**Acceptance criteria**:

- Full run: `POST /index { "repo_url": "https://github.com/pallets/flask" }` completes and returns `chunk_count > 0`
- `clones/{repo_id}/` does not exist after completion (cleaned up)
- `GET /files/{repo_id}` returns a list of `.py` file paths
- `GET /file/{repo_id}?path=src/flask/__init__.py` returns content

**Do not**:

- Do not let the endpoint return before `cleanup_clone` has run — always use `finally`
- Do not store the raw clone content anywhere permanently — chunks in Chroma and file list JSON only

---

## QRY-01: Query endpoint — embedding, scoped search, and reranking

**Epic**: Query Pipeline
**Depends on**: ING-07, INFRA-03
**Blocks**: GEN-01

**Objective**: Implement `POST /query`. Embed the query with CodeT5+ (same local model used at ingestion), perform similarity search in the correct Chroma collection with an optional scope filter, and rerank the top-20 results to top-5.

**System context**:

- Embedding must use the SAME model as ingestion (`Salesforce/codet5p-110m-embedding`) — mixing models breaks retrieval
- Scope: if `scope` is provided in the request (e.g., `"auth/"`), add a Chroma `where` filter: `{"file_path": {"$contains": scope}}`. This narrows retrieval without separate collections.
- Similarity search: `n_results=20` (before reranking)
- Reranker: `cross-encoder/ms-marco-MiniLM-L-6-v2` — load once at module level, not per request.
- Reranker input: list of (query, passage) tuples. Output: scores. Take top-5 by score.

**Implementation**:

Create `/app/query.py`:

```python
import torch
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sentence_transformers import CrossEncoder
from transformers import AutoModel, AutoTokenizer
from db import get_or_create_collection, collection_exists

router = APIRouter()

# Load reranker ONCE at module level — not per request
_reranker = CrossEncoder("cross-encoder/ms-marco-MiniLM-L-6-v2")

# Load embedding model ONCE — same model as ingestion (CodeT5+)
_tokenizer = AutoTokenizer.from_pretrained("Salesforce/codet5p-110m-embedding", trust_remote_code=True)
_embed_model = AutoModel.from_pretrained("Salesforce/codet5p-110m-embedding", trust_remote_code=True)
_embed_model.eval()

def _embed_query(text: str) -> list[float]:
    inputs = _tokenizer([text], padding=True, truncation=True, max_length=512, return_tensors="pt")
    with torch.no_grad():
        outputs = _embed_model(**inputs)
    return outputs[0].tolist()

class QueryRequest(BaseModel):
    repo_id: str
    question: str
    scope: str | None = None   # Optional: "auth/" narrows to files under auth/

@router.post("/query")
async def query_repo(request: QueryRequest):
    if not collection_exists(request.repo_id):
        raise HTTPException(status_code=404, detail="Repo not indexed")

    # Embed the query (same CodeT5+ model used at ingestion)
    query_embedding = _embed_query(request.question)

    # Build Chroma where filter for scope
    where = None
    if request.scope:
        where = {"file_path": {"$contains": request.scope}}

    # Search Chroma — top 20 candidates
    collection = get_or_create_collection(request.repo_id)
    search_kwargs = {
        "query_embeddings": [query_embedding],
        "n_results": min(20, collection.count()),
        "include": ["documents", "metadatas", "distances"]
    }
    if where:
        search_kwargs["where"] = where

    results = collection.query(**search_kwargs)

    docs = results["documents"][0]
    metas = results["metadatas"][0]

    if not docs:
        return {"answer": "No relevant code found for this query.", "citations": []}

    # Rerank: score each (question, passage) pair
    pairs = [(request.question, doc) for doc in docs]
    scores = _reranker.predict(pairs)
    ranked = sorted(zip(scores, docs, metas), key=lambda x: x[0], reverse=True)
    top5 = ranked[:5]

    # Return top chunks with metadata — GEN-01 will use these for generation
    top_chunks = [{"text": doc, "metadata": meta, "score": float(score)}
                  for score, doc, meta in top5]

    # Generation step added by GEN-01
    # Placeholder return for testing retrieval independently:
    return {
        "chunks": top_chunks,
        "answer": "Generation not yet implemented — see GEN-01",
        "citations": [{"file_path": m["file_path"], "start_line": m["start_line"]}
                      for _, _, m in top5]
    }
```

**Acceptance criteria**:

- `POST /query { "repo_id": "...", "question": "how does routing work?" }` returns 5 chunks
- `POST /query` with `"scope": "auth/"` returns only chunks with `file_path` containing `"auth/"`
- `POST /query` on an unindexed `repo_id` returns 404
- Reranker does not download anything at query time — it uses the cached model from INFRA-01

**Do not**:

- Do not use `task_type="search_document"` for query embedding — that is for ingestion only
- Do not instantiate the reranker inside the request handler — keep it at module level

---

## GEN-01: Prompt template, Nemotron call, and citation extraction

**Epic**: Generation
**Depends on**: QRY-01
**Blocks**: FE-03

**Objective**: Take the top-5 reranked chunks from QRY-01, build a structured prompt, call Nemotron 70B, extract citations from the response, and return the final answer with citations.

**System context**:

- Nemotron 70B endpoint: `https://integrate.api.nvidia.com/v1` (NVIDIA API Catalog, OpenAI-compatible)
- Model: `nvidia/llama-3.1-nemotron-70b-instruct`
- Temperature: 0.1 (keep low — factual code Q&A)
- Max tokens: 1024
- Each chunk in the prompt is labeled `[file_path:start_line]` before its code block. The model is instructed to cite these labels in its answer.
- Citations are parsed from the model's answer text by finding patterns like `[auth/login.py:42]`

**Prompt template** (use this exact template):

```
You are a senior software engineer assistant. Answer the question using ONLY the code context provided below.
For every piece of code you reference in your answer, cite its source using the format [file_path:line_number].
If the answer cannot be determined from the provided context, say "I cannot determine this from the available code."
Do not hallucinate code that is not in the context.

CONTEXT:
{chunks_block}

QUESTION: {question}

ANSWER:
```

Where `chunks_block` is:

```
[auth/login.py:42]
```python
def login(user, password):
    ...
```

[middleware/jwt.py:18]

```python
def verify_token(token):
    ...
```

```

**Implementation**:

Update `/app/query.py` to add generation:
```python
from llama_index.llms.nvidia import NVIDIA
import re

_llm = None
def _get_llm():
    global _llm
    if _llm is None:
        _llm = NVIDIA(
            model="nvidia/llama-3.1-nemotron-70b-instruct",
            api_key=os.getenv("NVIDIA_API_KEY"),
            temperature=0.1,
            max_tokens=1024
        )
    return _llm

def build_prompt(question: str, chunks: list[dict]) -> str:
    chunks_block = ""
    for chunk in chunks:
        meta = chunk["metadata"]
        chunks_block += f"[{meta['file_path']}:{meta['start_line']}]\n"
        chunks_block += f"```{meta['language']}\n{chunk['text']}\n```\n\n"

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
    # If model didn't cite anything, fall back to top chunk metadata
    if not citations and chunks:
        for chunk in chunks[:3]:
            m = chunk["metadata"]
            citations.append({"file_path": m["file_path"], "start_line": m["start_line"]})
    return citations
```

Update the `query_repo` endpoint to call generation after retrieval:

```python
    # Replace the placeholder return with:
    prompt = build_prompt(request.question, top_chunks)
    llm = _get_llm()
    response = llm.complete(prompt)
    answer = str(response)
    citations = extract_citations(answer, top_chunks)
    return {"answer": answer, "citations": citations}
```

**Acceptance criteria**:

- `POST /query { "question": "how does routing work?" }` returns an answer that references code
- Response contains `citations` array with at least one `{ file_path, start_line }` entry
- Answer does not contain information not present in the retrieved chunks (verify manually)
- LLM is cached at module level — not re-initialized per request

**Do not**:

- Do not set temperature above 0.2 — this is factual code Q&A, not creative writing
- Do not include chunks that scored below 0 from the reranker — filter them out

---

## TRC-01: Trace endpoint

**Epic**: Trace
**Depends on**: ING-07
**Blocks**: FE-05

**Objective**: Implement `POST /trace`. Load the pre-built dependency graph JSON for the repo, look up the given file, and return the list of files that import or reference it.

**System context**:

- Dependency graph is built at ingestion time by `ING-05` and stored at `deps/{repo_id}_deps.json`
- The graph format is: `{ "auth/login.py": ["app.py", "middleware/jwt.py"] }` — a reverse map
- file_path in the request must match the keys in this JSON exactly (relative path, no leading slash)
- This is a pure JSON lookup — no Chroma, no LLM, no embeddings

**Implementation**:

Create `/app/trace.py`:

```python
import json
import os
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from db import collection_exists

router = APIRouter()

class TraceRequest(BaseModel):
    repo_id: str
    file_path: str   # e.g. "auth/login.py" — relative path, no leading slash

@router.post("/trace")
def trace_file(request: TraceRequest):
    if not collection_exists(request.repo_id):
        raise HTTPException(status_code=404, detail="Repo not indexed")

    dep_path = f"deps/{request.repo_id}_deps.json"
    if not os.path.exists(dep_path):
        raise HTTPException(status_code=404, detail="Dependency graph not found for this repo")

    with open(dep_path) as f:
        graph = json.load(f)

    # Normalize input path — strip leading slash if present
    file_path = request.file_path.lstrip("/")

    if file_path not in graph:
        raise HTTPException(status_code=404, detail=f"File '{file_path}' not found in dependency graph")

    return {
        "file_path": file_path,
        "affected_files": graph[file_path]
    }
```

**Acceptance criteria**:

- `POST /trace { "repo_id": "...", "file_path": "flask/__init__.py" }` returns `affected_files` list
- `POST /trace` with a non-existent file returns 404 with a clear message
- Response time is under 50ms — this is a JSON lookup, not a search
- Leading slash in `file_path` is handled gracefully (stripped, not errored)

**Do not**:

- Do not call Chroma, embedding models, or Nemotron in this endpoint — it is pure JSON lookup

---

## POD-01: Podcast feature — Web Speech API fallback (build first)

**Epic**: Podcast
**Depends on**: GEN-01
**Blocks**: POD-02, FE-06

**Objective**: Implement the podcast backend endpoint using a text-based fallback path. The frontend sends transcribed text (from Web Speech API), the backend runs the standard Chat+RAG pipeline, and returns the answer as text for the frontend to speak via Web Speech API `SpeechSynthesis`.

**System context**:

- This is the fallback path — build it first and always keep it working
- VoiceChat (POD-02) is Early Access and may not be available — this fallback must always work
- The frontend will handle STT and TTS using the browser's Web Speech API:
  - STT: `window.SpeechRecognition` (captures speech → text)
  - TTS: `window.speechSynthesis.speak(new SpeechSynthesisUtterance(text))`
- The backend for the fallback only needs to accept text and return text — no audio processing

**Implementation**:

Create `/app/speech.py`:

```python
import os
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from db import collection_exists

router = APIRouter()

class SpeechRequest(BaseModel):
    repo_id: str
    transcript: str          # Text from STT (sent by browser)
    audio_base64: str | None = None   # Reserved for VoiceChat path (POD-02)

@router.post("/speech")
async def speech_query(request: SpeechRequest):
    if not collection_exists(request.repo_id):
        raise HTTPException(status_code=404, detail="Repo not indexed")

    # Fallback path: treat transcript as a text query through the standard pipeline
    # Import the query logic directly rather than making an internal HTTP call
    from query import QueryRequest, query_repo
    query_request = QueryRequest(repo_id=request.repo_id, question=request.transcript)
    result = await query_repo(query_request)

    return {
        "transcript": request.transcript,
        "answer": result["answer"],
        "citations": result["citations"],
        "audio_base64": None   # Null for fallback — TTS handled in browser
    }
```

**Acceptance criteria**:

- `POST /speech { "repo_id": "...", "transcript": "how does auth work?" }` returns an answer
- `audio_base64` in the response is `null` for the fallback path
- The response contains the same `answer` and `citations` as `POST /query` would return

**Do not**:

- Do not process audio on the server in this task — that is POD-02
- Do not call any external TTS API in this task — TTS is the browser's responsibility in the fallback

---

## POD-02: Podcast feature — Nemotron VoiceChat integration

**Epic**: Podcast
**Depends on**: POD-01
**Blocks**: FE-06

**⚠ EARLY ACCESS REQUIRED**: This task can only be implemented after Early Access to Nemotron 3 VoiceChat is granted at `developer.nvidia.com/nemotron-voicechat-early-access`. Do not block other tasks on this — POD-01 fallback must work independently.

**Objective**: Extend the `POST /speech` endpoint to detect if audio is provided, retrieve RAG context from Chroma, inject context as a system prompt, call the VoiceChat API with the audio, and return the audio response.

**System context**:

- VoiceChat model: `nvidia/nemotron-voicechat` via NVIDIA API Catalog
- VoiceChat is full-duplex — it handles ASR, LLM reasoning, and TTS in one call
- RAG context is injected via system prompt (text) alongside the audio input
- The system prompt should be brief — VoiceChat has a smaller effective context window than Nemotron 70B — use top-3 chunks maximum, not top-5
- Audio format expected: webm or wav (browser MediaRecorder default is webm)
- Response: audio stream (base64 encode it for JSON transport)

**Implementation**:

Update the `speech_query` function in `/app/speech.py`:

```python
import base64
import httpx

async def _call_voicechat(audio_bytes: bytes, system_prompt: str) -> bytes:
    """Call the NVIDIA VoiceChat API. Returns audio bytes of the response."""
    # Note: Exact API schema TBD — check https://build.nvidia.com/nvidia/nemotron-voicechat
    # for the current endpoint spec when Early Access is granted.
    # This is a placeholder implementation — update once API docs are available.
    api_key = os.getenv("NVIDIA_API_KEY")
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            "https://integrate.api.nvidia.com/v1/audio/speech-to-speech",
            headers={"Authorization": f"Bearer {api_key}"},
            content=audio_bytes,
            params={"system_prompt": system_prompt, "model": "nvidia/nemotron-voicechat"}
        )
        response.raise_for_status()
        return response.content

@router.post("/speech")
async def speech_query(request: SpeechRequest):
    if not collection_exists(request.repo_id):
        raise HTTPException(status_code=404, detail="Repo not indexed")

    # If audio is provided, use VoiceChat path
    if request.audio_base64:
        audio_bytes = base64.b64decode(request.audio_base64)

        # Get RAG context — use top-3 chunks for VoiceChat (smaller context window)
        from query import QueryRequest, query_repo, _embed_query, _reranker, build_prompt

        # We need the transcript to retrieve relevant context
        # If transcript not provided, VoiceChat must transcribe first — two-step approach
        if request.transcript:
            query_embedding = _embed_query(request.transcript)
            from db import get_or_create_collection
            collection = get_or_create_collection(request.repo_id)
            results = collection.query(query_embeddings=[query_embedding], n_results=10)
            docs = results["documents"][0]
            metas = results["metadatas"][0]
            pairs = [(request.transcript, doc) for doc in docs]
            scores = _reranker.predict(pairs)
            ranked = sorted(zip(scores, docs, metas), reverse=True)[:3]
            context_block = "\n\n".join(
                f"[{m['file_path']}:{m['start_line']}]\n{doc}"
                for _, doc, m in ranked
            )
            system_prompt = f"You are a code assistant. Use this context:\n{context_block}"
        else:
            system_prompt = "You are a code assistant."

        try:
            audio_response = await _call_voicechat(audio_bytes, system_prompt)
            return {
                "transcript": request.transcript,
                "answer": None,
                "citations": [],
                "audio_base64": base64.b64encode(audio_response).decode()
            }
        except Exception as e:
            # Fall through to text fallback if VoiceChat fails
            print(f"VoiceChat failed, falling back to text: {e}")

    # Fallback text path
    from query import QueryRequest, query_repo
    query_request = QueryRequest(repo_id=request.repo_id, question=request.transcript or "")
    result = await query_repo(query_request)
    return {
        "transcript": request.transcript,
        "answer": result["answer"],
        "citations": result["citations"],
        "audio_base64": None
    }
```

**Acceptance criteria**:

- When `audio_base64` is null, the fallback text path is used — same behavior as POD-01
- When `audio_base64` is provided and VoiceChat is available, `audio_base64` in the response is non-null
- When VoiceChat call fails for any reason, endpoint falls back to text path gracefully — never 500

**Do not**:

- Do not pass more than top-3 chunks to VoiceChat system prompt
- Do not let VoiceChat failure propagate as an error — always fall back to text

---

## FE-01: Repo input UI, indexing flow, and polling

**Epic**: Frontend
**Depends on**: ING-07
**Blocks**: FE-02

**Objective**: Build the repo URL input form, trigger indexing on submit, poll `GET /status/{repo_id}` every 3 seconds until indexed, show loading state, then unlock the query interface.

**System context**:

- Backend URL: `http://localhost:8000`
- `POST /index` returns `{ repo_id, status, chunk_count }`
- `GET /status/{repo_id}` returns `{ indexed: true }` when ready
- `repo_id` must be stored in `window.__repoId` so all other UI components can access it
- On successful indexing, call `window.__onIndexed(repo_id)` — this function will be defined by FE-02 onwards

**Implementation** — add to `/app/static/index.html`:

```html
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Nemotron Code RAG</title>
  <link rel="stylesheet" href="style.css">
</head>
<body>
  <div id="app">
    <section id="index-section">
      <h1>Nemotron Code RAG</h1>
      <div id="repo-form">
        <input type="url" id="repo-url" placeholder="https://github.com/owner/repo"
               required pattern="https://(github|gitlab)\.com/.*">
        <button id="index-btn" onclick="startIndexing()">Index repo</button>
      </div>
      <div id="index-status" hidden>
        <span id="status-text">Cloning and indexing...</span>
        <span class="spinner"></span>
      </div>
      <div id="index-error" hidden style="color:red"></div>
    </section>

    <section id="main-section" hidden>
      <!-- FE-02 (file tree), FE-03 (chat), FE-04 (code viewer), FE-05 (trace), FE-06 (podcast) inject here -->
    </section>
  </div>
  <script src="app.js"></script>
</body>
</html>
```

Add to `/app/static/app.js`:

```javascript
const API = "http://localhost:8000";
window.__repoId = null;
window.__onIndexed = (repoId) => {};  // Overridden by FE-02 onwards

async function startIndexing() {
  const url = document.getElementById("repo-url").value.trim();
  if (!url) return;

  document.getElementById("index-btn").disabled = true;
  document.getElementById("index-error").hidden = true;
  document.getElementById("index-status").hidden = false;
  document.getElementById("status-text").textContent = "Cloning and indexing...";

  try {
    const res = await fetch(`${API}/index`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ repo_url: url })
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Indexing failed");

    const repoId = data.repo_id;
    window.__repoId = repoId;

    if (data.status === "already_indexed") {
      onIndexingComplete(repoId, data.chunk_count);
    } else {
      pollStatus(repoId);
    }
  } catch (err) {
    showError(err.message);
  }
}

function pollStatus(repoId) {
  document.getElementById("status-text").textContent = "Embedding chunks...";
  const interval = setInterval(async () => {
    try {
      const res = await fetch(`${API}/status/${repoId}`);
      if (res.ok) {
        const data = await res.json();
        if (data.indexed) {
          clearInterval(interval);
          onIndexingComplete(repoId, null);
        }
      }
    } catch (e) { /* keep polling */ }
  }, 3000);
}

function onIndexingComplete(repoId, chunkCount) {
  document.getElementById("index-status").hidden = true;
  document.getElementById("index-section").style.opacity = "0.5";
  document.getElementById("main-section").hidden = false;
  window.__repoId = repoId;
  window.__onIndexed(repoId);
}

function showError(msg) {
  const el = document.getElementById("index-error");
  el.textContent = msg;
  el.hidden = false;
  document.getElementById("index-status").hidden = true;
  document.getElementById("index-btn").disabled = false;
}
```

**Acceptance criteria**:

- Pasting a valid GitHub URL and clicking "Index repo" triggers `POST /index`
- A spinner is visible during indexing and disappears when complete
- An already-indexed repo skips polling and goes straight to complete
- Invalid URL (http://, not github.com) shows an error message
- `window.__repoId` is set to the repo_id after successful indexing

**Do not**:

- Do not auto-submit on URL paste — wait for button click
- Do not re-index if `window.__repoId` is already set to the same URL — check first

---

## FE-02: File tree

**Epic**: Frontend
**Depends on**: FE-01, ING-07
**Blocks**: FE-03, FE-05

**Objective**: Fetch the list of files for the indexed repo, render a collapsible file tree in the sidebar, and track the selected file/folder path as `window.__scope` for Chat to use as a retrieval filter.

**System context**:

- `GET /files/{repo_id}` returns `{ files: ["auth/login.py", "app.py", ...] }`
- `window.__scope` stores the selected path prefix (e.g. `"auth/"`) or `null` for whole codebase
- When a file is selected, also call `window.__onFileSelected(file_path)` — used by FE-05 (Trace)
- Tree must be collapsible by folder — clicking a folder expands/collapses it

**Implementation** — add to `app.js`:

```javascript
window.__scope = null;
window.__onFileSelected = (path) => {};   // Overridden by FE-05

window.__onIndexed = async (repoId) => {
  const res = await fetch(`${API}/files/${repoId}`);
  const data = await res.json();
  renderFileTree(data.files, repoId);
};

function renderFileTree(files, repoId) {
  // Build folder structure
  const tree = {};
  files.forEach(f => {
    const parts = f.split("/");
    let node = tree;
    parts.forEach((part, i) => {
      if (!node[part]) node[part] = i === parts.length - 1 ? null : {};
      node = node[part] || {};
    });
  });

  const container = document.createElement("div");
  container.id = "file-tree";
  container.innerHTML = "<h3>Files</h3>";
  container.appendChild(buildTreeNode(tree, ""));
  document.getElementById("main-section").prepend(container);
}

function buildTreeNode(node, prefix) {
  const ul = document.createElement("ul");
  Object.keys(node).sort().forEach(name => {
    const li = document.createElement("li");
    const fullPath = prefix ? `${prefix}/${name}` : name;
    if (node[name] === null) {
      // File
      li.textContent = name;
      li.className = "file-node";
      li.onclick = (e) => {
        e.stopPropagation();
        document.querySelectorAll(".file-node.selected, .folder-node.selected")
                .forEach(el => el.classList.remove("selected"));
        li.classList.add("selected");
        window.__scope = fullPath;
        window.__onFileSelected(fullPath);
      };
    } else {
      // Folder
      li.textContent = name + "/";
      li.className = "folder-node";
      const children = buildTreeNode(node[name], fullPath);
      children.hidden = true;
      li.appendChild(children);
      li.onclick = (e) => {
        e.stopPropagation();
        document.querySelectorAll(".file-node.selected, .folder-node.selected")
                .forEach(el => el.classList.remove("selected"));
        li.classList.add("selected");
        children.hidden = !children.hidden;
        window.__scope = fullPath + "/";
      };
    }
    ul.appendChild(li);
  });
  return ul;
}
```

**Acceptance criteria**:

- After indexing, the file tree renders with the repo's file structure
- Clicking a folder toggles its children visible/hidden
- Clicking a file sets `window.__scope` to that file path
- Clicking a folder sets `window.__scope` to that folder path with trailing `/`
- Clicking the same item twice does NOT toggle scope off — a second click on a different item changes scope

**Do not**:

- Do not fetch file content in this task — that is FE-04's responsibility
- Do not sort files before folders — sort folders first, files second within each level

---

## FE-03: Chat UI with citations

**Epic**: Frontend
**Depends on**: FE-01, GEN-01
**Blocks**: FE-04

**Objective**: Build the chat message thread, question input, and "Ask" button. On each response, render the answer text and a row of citation chips below it. Each citation chip is clickable and opens the code viewer (FE-04).

**System context**:

- `POST /query` request: `{ repo_id: window.__repoId, question: "...", scope: window.__scope }`
- `POST /query` response: `{ answer: "...", citations: [{ file_path, start_line }] }`
- Citations are rendered as chips showing `file_path:start_line`
- Clicking a citation calls `window.__openCodeViewer(file_path, start_line)` — defined by FE-04

**Implementation** — add to `app.js`:

```javascript
window.__openCodeViewer = (filePath, startLine) => {};   // Overridden by FE-04

function initChat() {
  const section = document.getElementById("main-section");
  section.insertAdjacentHTML("beforeend", `
    <div id="chat-panel">
      <div id="chat-scope-indicator">Scope: <span id="scope-text">Whole codebase</span></div>
      <div id="chat-messages"></div>
      <div id="chat-input-row">
        <input type="text" id="chat-input" placeholder="Ask about this codebase...">
        <button id="chat-btn" onclick="sendChatMessage()">Ask</button>
      </div>
    </div>
  `);
  // Update scope indicator when scope changes
  const originalOnFileSelected = window.__onFileSelected;
  window.__onFileSelected = (path) => {
    document.getElementById("scope-text").textContent = path;
    originalOnFileSelected(path);
  };
}

async function sendChatMessage() {
  const input = document.getElementById("chat-input");
  const question = input.value.trim();
  if (!question || !window.__repoId) return;

  appendMessage("user", question);
  input.value = "";
  document.getElementById("chat-btn").disabled = true;

  try {
    const res = await fetch(`${API}/query`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        repo_id: window.__repoId,
        question,
        scope: window.__scope
      })
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Query failed");
    appendMessage("assistant", data.answer, data.citations);
  } catch (err) {
    appendMessage("error", `Error: ${err.message}`);
  } finally {
    document.getElementById("chat-btn").disabled = false;
  }
}

function appendMessage(role, text, citations = []) {
  const messages = document.getElementById("chat-messages");
  const div = document.createElement("div");
  div.className = `message message-${role}`;
  div.innerHTML = `<p class="message-text">${escapeHtml(text)}</p>`;

  if (citations.length) {
    const citeRow = document.createElement("div");
    citeRow.className = "citation-row";
    citations.forEach(c => {
      const chip = document.createElement("span");
      chip.className = "citation-chip";
      chip.textContent = `${c.file_path}:${c.start_line}`;
      chip.onclick = () => window.__openCodeViewer(c.file_path, c.start_line);
      citeRow.appendChild(chip);
    });
    div.appendChild(citeRow);
  }
  messages.appendChild(div);
  messages.scrollTop = messages.scrollHeight;
}

function escapeHtml(str) {
  const d = document.createElement("div");
  d.appendChild(document.createTextNode(str));
  return d.innerHTML;
}

// Call initChat once indexing is complete
const origOnIndexed = window.__onIndexed;
window.__onIndexed = (repoId) => { origOnIndexed(repoId); initChat(); };
```

**Acceptance criteria**:

- Typing a question and clicking Ask sends the request and displays the answer
- Citations appear as clickable chips below each assistant message
- Scope indicator shows the current `window.__scope` or "Whole codebase"
- Send button is disabled while a request is in flight — prevents double-submit
- Chat input clears after sending

**Do not**:

- Do not render raw HTML from the API in message text — always escape it
- Do not allow sending when `window.__repoId` is null

---

## FE-04: Read-only code viewer

**Epic**: Frontend
**Depends on**: FE-03, ING-07
**Blocks**: nothing

**Objective**: Build a read-only code panel that opens when a citation chip is clicked, fetches the file content from `GET /file/{repo_id}?path=...`, renders it with syntax highlighting, and scrolls to the cited line. The viewer must be strictly non-editable.

**System context**:

- `GET /file/{repo_id}?path=auth/login.py` returns `{ file_path, language, content }`
- Syntax highlighting: use Prism.js from cdnjs — `https://cdnjs.cloudflare.com/ajax/libs/prism/1.29.0/prism.min.js`
- The cited `start_line` must be highlighted and scrolled into view
- The panel must be read-only — no `contenteditable`, no textarea

**Implementation** — add to `app.js` and `index.html`:

In `index.html`, add Prism.js in `<head>`:

```html
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/prism/1.29.0/themes/prism.min.css">
<script src="https://cdnjs.cloudflare.com/ajax/libs/prism/1.29.0/prism.min.js"></script>
```

In `app.js`:

```javascript
window.__openCodeViewer = async (filePath, startLine) => {
  let viewer = document.getElementById("code-viewer");
  if (!viewer) {
    viewer = document.createElement("div");
    viewer.id = "code-viewer";
    viewer.innerHTML = `
      <div id="code-viewer-header">
        <span id="code-viewer-path"></span>
        <button onclick="document.getElementById('code-viewer').hidden=true">×</button>
      </div>
      <pre id="code-viewer-pre"><code id="code-viewer-code"></code></pre>
    `;
    document.getElementById("main-section").appendChild(viewer);
  }
  viewer.hidden = false;
  document.getElementById("code-viewer-path").textContent = `${filePath}:${startLine}`;
  document.getElementById("code-viewer-code").textContent = "Loading...";

  const res = await fetch(`${API}/file/${window.__repoId}?path=${encodeURIComponent(filePath)}`);
  const data = await res.json();

  const codeEl = document.getElementById("code-viewer-code");
  codeEl.className = `language-${data.language}`;
  codeEl.textContent = data.content;
  Prism.highlightElement(codeEl);

  // Scroll to cited line
  const lines = document.getElementById("code-viewer-pre").querySelectorAll(".token");
  const lineHeight = 20;  // px — adjust via CSS
  document.getElementById("code-viewer-pre").scrollTop = (startLine - 1) * lineHeight;

  // Highlight the cited line
  const allLines = data.content.split("\n");
  const highlighted = allLines.map((line, i) =>
    i + 1 === startLine ? `<mark>${line}</mark>` : line
  ).join("\n");
  // Re-render with highlight (simplified — exact implementation depends on Prism version)
  codeEl.innerHTML = highlighted;
};
```

**Acceptance criteria**:

- Clicking a citation chip opens the code viewer with the correct file content
- The viewer scrolls to approximately the cited line
- The code is syntax-highlighted (not plain text)
- There is no way to edit the content in the viewer — no textarea, no contenteditable
- The × button closes the viewer

**Do not**:

- Do not use `<textarea>` or `contenteditable` anywhere in the code viewer
- Do not load Prism from any domain other than `cdnjs.cloudflare.com`

---

## FE-05: Trace view

**Epic**: Frontend
**Depends on**: FE-02, TRC-01
**Blocks**: nothing

**Objective**: When a file is selected in the file tree, show a "Show affected files" button. On click, call `POST /trace` and highlight the returned affected files in the file tree.

**System context**:

- `POST /trace` request: `{ repo_id: window.__repoId, file_path: selectedFile }`
- `POST /trace` response: `{ file_path, affected_files: ["app.py", "middleware/jwt.py"] }`
- Highlight affected files in the file tree by adding class `trace-affected` to their `<li>` elements
- Clear previous trace highlights before showing new ones

**Implementation** — add to `app.js`:

```javascript
function initTrace() {
  const section = document.getElementById("main-section");
  section.insertAdjacentHTML("beforeend", `
    <div id="trace-panel" hidden>
      <button id="trace-btn" onclick="runTrace()">Show affected files</button>
      <div id="trace-results"></div>
    </div>
  `);

  const originalOnFileSelected = window.__onFileSelected;
  window.__onFileSelected = (path) => {
    originalOnFileSelected(path);
    // Only show trace button for files (not folders)
    const isFile = !path.endsWith("/");
    document.getElementById("trace-panel").hidden = !isFile;
    document.getElementById("trace-results").textContent = "";
    clearTraceHighlights();
  };
}

async function runTrace() {
  const filePath = window.__scope;
  if (!filePath || filePath.endsWith("/")) return;

  const btn = document.getElementById("trace-btn");
  btn.disabled = true;
  clearTraceHighlights();

  try {
    const res = await fetch(`${API}/trace`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ repo_id: window.__repoId, file_path: filePath })
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail);

    const affected = data.affected_files;
    document.getElementById("trace-results").textContent =
      affected.length ? `${affected.length} file(s) affected` : "No files import this file";

    // Highlight affected files in the tree
    affected.forEach(path => {
      document.querySelectorAll(".file-node").forEach(node => {
        if (node.textContent === path.split("/").pop()) {
          node.classList.add("trace-affected");
        }
      });
    });
  } catch (err) {
    document.getElementById("trace-results").textContent = `Error: ${err.message}`;
  } finally {
    btn.disabled = false;
  }
}

function clearTraceHighlights() {
  document.querySelectorAll(".trace-affected").forEach(el => el.classList.remove("trace-affected"));
}

const origOnIndexedTrace = window.__onIndexed;
window.__onIndexed = (repoId) => { origOnIndexedTrace(repoId); initTrace(); };
```

**Acceptance criteria**:

- Selecting a file shows the "Show affected files" button
- Selecting a folder hides the trace button
- Clicking "Show affected files" highlights imported-by files in the tree with a visible color
- Previous trace highlights are cleared when a new file is selected
- Count of affected files is displayed

**Do not**:

- Do not run trace automatically on file selection — only on explicit button click

---

## FE-06: Podcast UI

**Epic**: Frontend
**Depends on**: FE-01, POD-01
**Blocks**: nothing

**Objective**: Build the podcast interface. Use the browser's `SpeechRecognition` API for STT and `SpeechSynthesis` for TTS. Send the transcript to `POST /speech` and speak the returned answer. If `audio_base64` is returned (VoiceChat), play that instead.

**System context**:

- Web Speech API: `window.SpeechRecognition || window.webkitSpeechRecognition`
- Best support: Chrome and Edge. Firefox has partial support. Always show a text fallback.
- `POST /speech` request: `{ repo_id, transcript, audio_base64: null }` — for fallback path
- `POST /speech` response: `{ transcript, answer, audio_base64 }` — `audio_base64` is null for fallback

**Implementation** — add to `app.js`:

```javascript
function initPodcast() {
  const section = document.getElementById("main-section");
  const hasSpeech = "SpeechRecognition" in window || "webkitSpeechRecognition" in window;

  section.insertAdjacentHTML("beforeend", `
    <div id="podcast-panel">
      <h3>Podcast mode</h3>
      ${hasSpeech ? `
        <button id="mic-btn" onclick="toggleRecording()">🎤 Hold to speak</button>
        <div id="podcast-transcript"></div>
        <div id="podcast-answer"></div>
      ` : `
        <p>Voice not supported in this browser. Use the chat above.</p>
      `}
    </div>
  `);
}

let _recognition = null;
let _isRecording = false;

function toggleRecording() {
  if (_isRecording) { stopRecording(); return; }
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  _recognition = new SR();
  _recognition.lang = "en-US";
  _recognition.interimResults = false;
  _recognition.onresult = async (e) => {
    const transcript = e.results[0][0].transcript;
    document.getElementById("podcast-transcript").textContent = `You: ${transcript}`;
    await sendSpeech(transcript);
  };
  _recognition.onerror = () => stopRecording();
  _recognition.onend = () => { _isRecording = false; document.getElementById("mic-btn").textContent = "🎤 Hold to speak"; };
  _recognition.start();
  _isRecording = true;
  document.getElementById("mic-btn").textContent = "🔴 Recording... click to stop";
}

function stopRecording() {
  if (_recognition) _recognition.stop();
  _isRecording = false;
}

async function sendSpeech(transcript) {
  const answerEl = document.getElementById("podcast-answer");
  answerEl.textContent = "Thinking...";
  try {
    const res = await fetch(`${API}/speech`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ repo_id: window.__repoId, transcript, audio_base64: null })
    });
    const data = await res.json();

    if (data.audio_base64) {
      // VoiceChat path — play audio response
      const audioBlob = base64ToBlob(data.audio_base64, "audio/wav");
      const audioUrl = URL.createObjectURL(audioBlob);
      new Audio(audioUrl).play();
      answerEl.textContent = "";
    } else {
      // Fallback — speak using Web Speech API
      answerEl.textContent = `Assistant: ${data.answer}`;
      const utterance = new SpeechSynthesisUtterance(data.answer);
      window.speechSynthesis.speak(utterance);
    }
  } catch (err) {
    answerEl.textContent = `Error: ${err.message}`;
  }
}

function base64ToBlob(b64, mime) {
  const bytes = atob(b64);
  const arr = new Uint8Array(bytes.length);
  for (let i = 0; i < bytes.length; i++) arr[i] = bytes.charCodeAt(i);
  return new Blob([arr], { type: mime });
}

const origOnIndexedPodcast = window.__onIndexed;
window.__onIndexed = (repoId) => { origOnIndexedPodcast(repoId); initPodcast(); };
```

**Acceptance criteria**:

- On Chrome/Edge: clicking the mic button starts recording, clicking again stops it
- The transcript is displayed before the answer is fetched
- The answer is spoken aloud via `SpeechSynthesis` on the fallback path
- On Firefox or unsupported browsers: a "not supported" message is shown, not an error
- `window.__repoId` is checked before sending — nothing is sent if no repo is indexed

**Do not**:

- Do not use `autoplay` attribute for audio elements — some browsers block it; use `.play()` directly
- Do not fail silently if speech fails — show the text answer as fallback

---

## ARCH-01: Backend — expose physical repository structure as a nested tree

**Epic**: Architecture Diagram
**Depends on**: ING-07 (ingestion wired end-to-end — writes `{repo_id}_files.json`), INFRA-03 (Chroma DB client)
**Blocks**: ARCH-02

**Objective**: Implement `GET /architecture/{repo_id}` in `files.py`. Read the flat file list from `deps/{repo_id}_files.json`, build a nested folder tree from it, and return it as a JSON structure the frontend D3.js diagram can pass directly to `d3.hierarchy()`.

**System context**:

The edges in this diagram represent **physical containment** — a folder contains files and subfolders. This is the physical structure of the repo on disk, not import relationships. No AST parsing, no dependency graph, no changes to `deps_builder.py` are needed.

Source of truth: `deps/{repo_id}_files.json` — written by ING-07 at the end of ingestion.
Format: a flat JSON array of relative file paths with no leading slash:

```json
["app.py", "auth/login.py", "auth/jwt.py", "utils/helpers.py"]
```

Required response format — a nested tree that `d3.hierarchy()` can consume directly:

```json
{
  "repo_id": "abc123",
  "tree": {
    "id": "/",
    "name": "/",
    "type": "folder",
    "children": [
      {
        "id": "auth",
        "name": "auth",
        "type": "folder",
        "children": [
          { "id": "auth/login.py", "name": "login.py", "type": "file", "language": "python" },
          { "id": "auth/jwt.py",   "name": "jwt.py",   "type": "file", "language": "python" }
        ]
      },
      { "id": "app.py", "name": "app.py", "type": "file", "language": "python" }
    ]
  },
  "total_files": 47,
  "total_folders": 12
}
```

Node field definitions:

- `id`: for files — the full relative path (e.g. `"auth/login.py"`); for folders — the relative folder path (e.g. `"auth"`)
- `name`: the final path segment only — filename or folder name (e.g. `"login.py"`, `"auth"`)
- `type`: `"file"` or `"folder"`
- `language`: present on file nodes only — derived from extension using the language map below
- `children`: present on folder nodes only — array of child folder and file nodes

Sort order within `children`: folders before files, each group sorted alphabetically.

Language map (extension → language name):

```python
ext_to_language = {
    ".py": "python", ".js": "javascript", ".ts": "typescript",
    ".tsx": "tsx", ".jsx": "javascript", ".go": "go",
    ".rs": "rust", ".java": "java", ".cpp": "cpp", ".cs": "c_sharp"
}
```

**Implementation**:

Add to `/app/files.py`:

```python
@router.get("/architecture/{repo_id}")
def get_architecture(repo_id: str):
    """
    Return the physical folder/file tree of the repo.
    Edges represent containment: a folder contains files and subfolders.
    Built from the flat file list saved at ingestion time — no AST parsing.
    """
    if not collection_exists(repo_id):
        raise HTTPException(status_code=404, detail="Repo not indexed")

    files_path = f"deps/{repo_id}_files.json"
    if not os.path.exists(files_path):
        raise HTTPException(status_code=404, detail="File list not found — re-index the repo")

    with open(files_path) as f:
        all_files = json.load(f)   # List of relative paths, e.g. ["auth/login.py", "app.py"]

    ext_to_language = {
        ".py": "python", ".js": "javascript", ".ts": "typescript",
        ".tsx": "tsx", ".jsx": "javascript", ".go": "go",
        ".rs": "rust", ".java": "java", ".cpp": "cpp", ".cs": "c_sharp"
    }

    # Build the nested tree structure from flat paths
    root = {"id": "/", "name": "/", "type": "folder", "children": []}

    def get_or_create_folder(node: dict, folder_path: str) -> dict:
        """Walk down the tree, creating folder nodes as needed. Returns the target folder node."""
        parts = folder_path.split("/")
        current = node
        accumulated = ""
        for part in parts:
            accumulated = f"{accumulated}/{part}".lstrip("/")
            # Find existing child folder
            existing = next((c for c in current["children"]
                             if c["type"] == "folder" and c["name"] == part), None)
            if existing is None:
                new_folder = {"id": accumulated, "name": part, "type": "folder", "children": []}
                current["children"].append(new_folder)
                existing = new_folder
            current = existing
        return current

    for file_path in sorted(all_files):
        parts = file_path.split("/")
        filename = parts[-1]
        ext = Path(filename).suffix
        language = ext_to_language.get(ext, "unknown")
        file_node = {"id": file_path, "name": filename, "type": "file", "language": language}

        if len(parts) == 1:
            # Root-level file
            root["children"].append(file_node)
        else:
            folder_path = "/".join(parts[:-1])
            folder_node = get_or_create_folder(root, folder_path)
            folder_node["children"].append(file_node)

    # Sort each folder's children: folders first, then files, each group alphabetical
    def sort_children(node: dict):
        if node["type"] != "folder":
            return
        node["children"].sort(key=lambda c: (0 if c["type"] == "folder" else 1, c["name"]))
        for child in node["children"]:
            sort_children(child)

    sort_children(root)

    # Count totals
    total_files = sum(1 for f in all_files)
    total_folders = _count_folders(root) - 1   # Subtract root itself

    return {
        "repo_id": repo_id,
        "tree": root,
        "total_files": total_files,
        "total_folders": total_folders
    }


def _count_folders(node: dict) -> int:
    """Recursively count folder nodes."""
    if node["type"] == "file":
        return 0
    return 1 + sum(_count_folders(c) for c in node.get("children", []))
```

No changes to `deps_builder.py`, `ingest.py`, or `main.py` are required. This endpoint is added to the existing `files` router which is already registered.

**Acceptance criteria**:

- `GET /architecture/{repo_id}` returns HTTP 200 with a `tree` field after indexing
- `tree.type` is `"folder"` and `tree.id` is `"/"`
- Every file in `deps/{repo_id}_files.json` appears exactly once as a leaf node with `type: "file"`
- Folder nodes have `type: "folder"` and contain a `children` array — never `null`
- File nodes have a `language` field; folder nodes do not
- Within each folder, folders appear before files; each group is sorted alphabetically
- A repo with files at `auth/login.py` and `auth/jwt.py` produces a single `"auth"` folder node containing both files — not two separate `"auth"` nodes
- `total_files` equals the count of file nodes in the tree
- `total_folders` equals the count of folder nodes excluding the root
- `GET /architecture/unknownid` returns 404

**Do not**:

- Do not read or parse import statements — this endpoint describes physical structure only
- Do not call Chroma DB for anything except the `collection_exists` check
- Do not create `deps/{repo_id}_forward.json` — that file is not part of this system
- Do not return a flat nodes+edges array — the response must be the nested `tree` format shown above so the frontend can call `d3.hierarchy(data.tree)` directly

---

## ARCH-02: Frontend — interactive collapsible D3.js repository structure tree

**Epic**: Architecture Diagram
**Depends on**: ARCH-01, FE-01 (repo input and polling), FE-04 (code viewer — defines `window.__openCodeViewer`)
**Blocks**: nothing

**Objective**: Build an interactive collapsible tree diagram that visualizes the physical folder and file structure of the indexed repo. Folder nodes expand and collapse on click. File nodes open the code viewer on click. The diagram supports zoom, pan, and search. Rendered with D3.js v7 using a left-to-right tree layout.

**System context**:

API: `GET /architecture/{repo_id}` — defined in ARCH-01.
The response contains a `tree` field which is a nested JSON object compatible with `d3.hierarchy()`.

Library: D3.js v7 — load from this exact URL (the only allowed CDN):

```
https://cdnjs.cloudflare.com/ajax/libs/d3/7.8.5/d3.min.js
```

Existing globals this task reads and calls:

- `window.__repoId` — set by FE-01 after indexing
- `window.__openCodeViewer(filePath, startLine)` — defined by FE-04; call with `startLine=1`
- `window.__onIndexed` — chain-extend using the same pattern as FE-02 through FE-06
- `API` — backend base URL (`http://localhost:8000`)

Layout: left-to-right horizontal tree (`d3.tree()`). Root is on the left, leaves extend to the right. This mirrors the conventional IDE file tree reading direction.

Node visual rules:

- **Folder nodes**: circle radius 8, fill `#6B7280` (gray), white label to the right
- **File nodes**: circle radius 5, fill by language (see color map below), label to the right
- **Collapsed folder**: circle fill `#374151` (darker gray) to indicate hidden children
- **Expanded folder**: circle fill `#6B7280` (normal gray)

File color map by language (hardcoded hex — do not use CSS variables):

```javascript
const LANGUAGE_COLORS = {
  python:      "#4B8BBE",
  javascript:  "#F7DF1E",
  typescript:  "#3178C6",
  tsx:         "#61DAFB",
  go:          "#00ADD8",
  rust:        "#CE422B",
  java:        "#B07219",
  cpp:         "#F34B7D",
  c_sharp:     "#178600",
  unknown:     "#888888"
};
```

Collapse/expand behavior:

- On load: all folders are **collapsed** — only the root's immediate children are shown
- Click a folder node: toggle its children visible/hidden
- Store hidden children in `node._children`; set `node.children = null` to collapse
- Animate expand/collapse with a 300ms transition
- After toggling, call `update(root)` to re-render

**Implementation**:

**Step 1 — Add D3.js to `index.html`** inside `<head>`:

```html
<script src="https://cdnjs.cloudflare.com/ajax/libs/d3/7.8.5/d3.min.js"></script>
```

**Step 2 — Add diagram container to `index.html`** inside `<div id="main-section">`:

```html
<button id="arch-toggle-btn" onclick="toggleArchPanel()" hidden>
  Architecture diagram
</button>

<div id="arch-panel" hidden>
  <div id="arch-toolbar">
    <span id="arch-counts"></span>
    <input type="text" id="arch-search" placeholder="Search files and folders..."
           oninput="archSearch(this.value)">
    <button onclick="archExpandAll()">Expand all</button>
    <button onclick="archCollapseAll()">Collapse all</button>
    <button onclick="resetArchZoom()">Reset zoom</button>
  </div>
  <svg id="arch-svg"></svg>
</div>
```

**Step 3 — Add CSS to `style.css`**:

```css
#arch-panel {
  width: 100%;
  height: 620px;
  position: relative;
  border: 1px solid #ddd;
  border-radius: 8px;
  overflow: hidden;
  background: #1e1e2e;   /* Dark background — tree lines read better on dark */
}
#arch-svg { width: 100%; height: 100%; cursor: grab; }
#arch-svg:active { cursor: grabbing; }

#arch-toolbar {
  position: absolute; top: 8px; left: 8px; z-index: 10;
  display: flex; gap: 6px; align-items: center; flex-wrap: wrap;
  background: rgba(30,30,46,0.92); padding: 6px 10px;
  border-radius: 6px; font-size: 12px; color: #cdd6f4;
}
#arch-toolbar button {
  background: #313244; color: #cdd6f4; border: 1px solid #45475a;
  border-radius: 4px; padding: 2px 8px; font-size: 11px; cursor: pointer;
}
#arch-toolbar button:hover { background: #45475a; }
#arch-search {
  width: 200px; font-size: 12px; padding: 3px 7px;
  background: #313244; border: 1px solid #45475a;
  border-radius: 4px; color: #cdd6f4;
}

.arch-link { fill: none; stroke: #45475a; stroke-width: 1; }
.arch-node text { font-size: 11px; font-family: monospace; }
.arch-node-folder text { fill: #cdd6f4; }
.arch-node-file   text { fill: #a6adc8; }
.arch-node-match  text { fill: #f38ba8 !important; font-weight: bold; }
.arch-node-match circle { stroke: #f38ba8 !important; stroke-width: 2 !important; }
```

**Step 4 — Add all diagram logic to `app.js`**:

```javascript
const LANGUAGE_COLORS = {
  python: "#4B8BBE", javascript: "#F7DF1E", typescript: "#3178C6",
  tsx: "#61DAFB", go: "#00ADD8", rust: "#CE422B", java: "#B07219",
  cpp: "#F34B7D", c_sharp: "#178600", unknown: "#888888"
};

let _archRoot = null;       // D3 hierarchy root — mutated for collapse/expand
let _archSvgSel = null;     // D3 selection of the SVG
let _archZoom = null;       // D3 zoom behavior — stored for resetArchZoom()
let _archData = null;       // Raw API response — cached, never re-fetched
let _archRendered = false;
const ARCH_NODE_WIDTH = 220;  // Horizontal spacing between depth levels
const ARCH_NODE_HEIGHT = 22;  // Vertical spacing between sibling nodes

function toggleArchPanel() {
  const panel = document.getElementById("arch-panel");
  panel.hidden = !panel.hidden;
  if (!panel.hidden && !_archRendered && window.__repoId) {
    loadArchDiagram(window.__repoId);
  }
}

async function loadArchDiagram(repoId) {
  if (_archData) { _renderArch(); return; }
  try {
    const res = await fetch(`${API}/architecture/${repoId}`);
    if (!res.ok) throw new Error("Architecture data unavailable");
    _archData = await res.json();
    document.getElementById("arch-counts").textContent =
      `${_archData.total_files} files · ${_archData.total_folders} folders`;
    _renderArch();
  } catch (err) {
    console.error("Architecture diagram:", err.message);
  }
}

function _renderArch() {
  _archRendered = true;
  const container = document.getElementById("arch-panel");
  const W = container.clientWidth  || 900;
  const H = container.clientHeight || 620;

  _archSvgSel = d3.select("#arch-svg").attr("viewBox", [0, 0, W, H]);
  _archSvgSel.selectAll("*").remove();

  // Zoom and pan
  _archZoom = d3.zoom().scaleExtent([0.05, 3]).on("zoom", e => g.attr("transform", e.transform));
  _archSvgSel.call(_archZoom);

  const g = _archSvgSel.append("g").attr("transform", `translate(60, ${H / 2})`);
  _archSvgSel._g = g;   // Store for update calls

  // Build hierarchy
  _archRoot = d3.hierarchy(_archData.tree, d => d.children);
  _archRoot.x0 = 0;
  _archRoot.y0 = 0;

  // Collapse all folders initially — show only root's immediate children
  _archRoot.descendants().forEach(d => {
    if (d.depth > 0 && d.data.type === "folder" && d.children) {
      d._children = d.children;
      d.children = null;
    }
  });

  _archUpdate(_archRoot, g);
}

function _archUpdate(source, g) {
  // Count visible nodes to size the tree
  const visibleCount = _archRoot.descendants().filter(d => d.children || !d.parent?.children).length;
  const treeHeight = Math.max(visibleCount * ARCH_NODE_HEIGHT, 200);

  const treeLayout = d3.tree().nodeSize([ARCH_NODE_HEIGHT + 4, ARCH_NODE_WIDTH]);
  treeLayout(_archRoot);

  const nodes = _archRoot.descendants();
  const links = _archRoot.links();

  const duration = 300;

  // Links
  const link = g.selectAll(".arch-link").data(links, d => d.target.data.id);

  link.join(
    enter => enter.append("path").attr("class", "arch-link")
      .attr("d", () => {
        const o = { x: source.x0 || 0, y: source.y0 || 0 };
        return _archLinkPath({ source: o, target: o });
      }),
    update => update,
    exit => exit.transition().duration(duration)
      .attr("d", () => {
        const o = { x: source.x, y: source.y };
        return _archLinkPath({ source: o, target: o });
      }).remove()
  ).transition().duration(duration).attr("d", _archLinkPath);

  // Nodes
  const node = g.selectAll(".arch-node").data(nodes, d => d.data.id);

  const nodeEnter = node.enter().append("g")
    .attr("class", d => `arch-node arch-node-${d.data.type}`)
    .attr("transform", () => `translate(${source.y0 || 0},${source.x0 || 0})`)
    .style("opacity", 0)
    .on("click", (e, d) => {
      if (d.data.type === "folder") {
        _archToggle(d);
        _archUpdate(d, g);
      } else {
        window.__openCodeViewer(d.data.id, 1);
      }
    });

  nodeEnter.append("circle")
    .attr("r", d => d.data.type === "folder" ? 8 : 5)
    .attr("fill", d => {
      if (d.data.type === "folder") return d._children ? "#374151" : "#6B7280";
      return LANGUAGE_COLORS[d.data.language] || LANGUAGE_COLORS.unknown;
    })
    .attr("stroke", "#1e1e2e")
    .attr("stroke-width", 1.5)
    .attr("cursor", "pointer");

  nodeEnter.append("text")
    .attr("x", d => d.data.type === "folder" ? 14 : 10)
    .attr("dy", "0.35em")
    .attr("cursor", "pointer")
    // Truncate to 28 chars — full path available in code viewer
    .text(d => {
      const n = d.data.name;
      return n.length > 28 ? n.slice(0, 25) + "…" : n;
    });

  // Folder expand/collapse indicator
  nodeEnter.filter(d => d.data.type === "folder").append("text")
    .attr("class", "arch-expand-icon")
    .attr("x", -14).attr("dy", "0.35em")
    .attr("fill", "#6B7280").attr("font-size", "10px")
    .text(d => d._children ? "▶" : d.children ? "▼" : "");

  // Merge and transition
  const nodeUpdate = nodeEnter.merge(node);
  nodeUpdate.transition().duration(duration)
    .attr("transform", d => `translate(${d.y},${d.x})`)
    .style("opacity", 1);

  // Update expand icon and circle fill after toggle
  nodeUpdate.select("circle")
    .attr("fill", d => {
      if (d.data.type === "folder") return d._children ? "#374151" : "#6B7280";
      return LANGUAGE_COLORS[d.data.language] || LANGUAGE_COLORS.unknown;
    });
  nodeUpdate.select(".arch-expand-icon")
    .text(d => d._children ? "▶" : d.children ? "▼" : "");

  node.exit().transition().duration(duration)
    .attr("transform", () => `translate(${source.y},${source.x})`)
    .style("opacity", 0).remove();

  // Store positions for next transition
  nodes.forEach(d => { d.x0 = d.x; d.y0 = d.y; });
}

function _archLinkPath(d) {
  return `M ${d.target.y} ${d.target.x}
          C ${(d.target.y + d.source.y) / 2} ${d.target.x},
            ${(d.target.y + d.source.y) / 2} ${d.source.x},
            ${d.source.y} ${d.source.x}`;
}

function _archToggle(d) {
  if (d.children) {
    d._children = d.children;
    d.children = null;
  } else {
    d.children = d._children;
    d._children = null;
  }
}

function archExpandAll() {
  if (!_archRoot) return;
  _archRoot.descendants().forEach(d => {
    if (d._children) { d.children = d._children; d._children = null; }
  });
  _archUpdate(_archRoot, _archSvgSel._g);
}

function archCollapseAll() {
  if (!_archRoot) return;
  _archRoot.descendants().forEach(d => {
    if (d.depth > 0 && d.children) { d._children = d.children; d.children = null; }
  });
  _archUpdate(_archRoot, _archSvgSel._g);
}

function resetArchZoom() {
  if (_archZoom && _archSvgSel) {
    _archSvgSel.call(_archZoom.transform, d3.zoomIdentity.translate(60, 0));
  }
}

function archSearch(query) {
  if (!_archRoot || !_archSvgSel) return;
  const q = query.toLowerCase().trim();
  // First expand all so matches are visible
  if (q) archExpandAll();
  _archSvgSel.selectAll(".arch-node")
    .classed("arch-node-match", d => q.length > 0 && d.data.name.toLowerCase().includes(q));
  if (!q) {
    _archSvgSel.selectAll(".arch-node").classed("arch-node-match", false);
  }
}

// Show the toggle button and load diagram once repo is indexed
const _origOnIndexedArch = window.__onIndexed;
window.__onIndexed = (repoId) => {
  _origOnIndexedArch(repoId);
  document.getElementById("arch-toggle-btn").hidden = false;
};
```

**Acceptance criteria**:

- "Architecture diagram" button appears only after indexing — hidden before
- On first open, the diagram renders with only the root's immediate children visible — all sub-folders are collapsed
- Clicking a folder node expands its children with a 300ms animated transition
- Clicking an already-expanded folder collapses it with a 300ms animated transition
- Collapsed folder circles are visibly darker than expanded ones
- Folder nodes show a `▶` indicator when collapsed and `▼` when expanded
- Clicking a file node calls `window.__openCodeViewer(filePath, 1)`
- File nodes are colored by language — Python nodes are `#4B8BBE`, JS nodes are `#F7DF1E`
- Folder nodes are gray (`#6B7280`)
- "Expand all" expands every folder in the tree
- "Collapse all" collapses every folder except the root level
- Typing in search expands all folders, then highlights matching node labels in red and bolds them
- Clearing the search removes all highlights
- Zoom in/out works with scroll wheel; pan works with click-drag on the background
- "Reset zoom" returns to the default view
- Toggling the diagram panel closed and reopening it does not trigger a second network request
- Filenames longer than 28 characters are truncated with an ellipsis in the label
- D3.js loaded from `cdnjs.cloudflare.com` only — verify with browser network tab

**Do not**:

- Do not use `innerHTML` to set node label text — use D3 `.text()` to prevent XSS from crafted filenames
- Do not load D3.js from any CDN other than `cdnjs.cloudflare.com`
- Do not mutate `_archData` — create a fresh `d3.hierarchy()` from it on each render call; mutations go only on the hierarchy nodes
- Do not show all nodes expanded on initial render — start collapsed at depth > 0 so large repos are readable
- Do not make the diagram background white — the dark background (`#1e1e2e`) is intentional for readability of the tree lines and colored file nodes

---

*End of task list. 28 tasks total. Each task is self-contained and can be given to an independent agent.*