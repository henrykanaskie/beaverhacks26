# Nemotron Code RAG — Test Cases

Test cases for each task in [nemotron_rag_agent_tasks.md](nemotron_rag_agent_tasks.md).
Run from the `/app/` directory unless stated otherwise. Replace `{repo_id}` with the actual hash returned from `/index`.

**Test repo used throughout**: `https://github.com/pallets/flask` (small, public, all-Python — predictable outputs).

---

## Setup helpers (run once before testing)

```powershell
# Compute a repo_id for use in tests
python -c "import hashlib; print(hashlib.md5(b'https://github.com/pallets/flask').hexdigest())"

# Start the server in the background for endpoint tests
./run_dev.sh
```

---

# INFRASTRUCTURE

## INFRA-01 — Project structure and dependencies

| # | Test | Expected result |
|---|------|----------------|
| 1 | `python -c "import fastapi, chromadb, llama_index, transformers, torch, dotenv"` | Exits 0, no ImportError |
| 2 | `python -c "from transformers import AutoModel; AutoModel.from_pretrained('Salesforce/codet5p-110m-embedding', trust_remote_code=True)"` with WiFi disabled | Loads from cache, no network call |
| 3 | Check directories exist: `ls app/deps`, `ls app/clones`, `ls app/static` | All three exist |
| 4 | Check `.env` file contents | Contains `NVIDIA_API_KEY`, `MAX_FILES_PER_REPO`, `CLONE_TIMEOUT_SECONDS` — no real keys committed |
| 5 | `python -c "import voyageai"` and `python -c "import openai"` | Both should fail — those libs must NOT be installed |
| 6 | All stub `.py` files exist in `/app/` | `main.py`, `ingest.py`, `query.py`, `files.py`, `trace.py`, `speech.py` |

---

## INFRA-02 — FastAPI skeleton

| # | Test | Expected result |
|---|------|----------------|
| 1 | `./run_dev.sh` | Starts without error on port 8000 |
| 2 | `curl http://localhost:8000/health` | `{"status":"ok"}` with HTTP 200 |
| 3 | `curl -I -X OPTIONS http://localhost:8000/health -H "Origin: http://example.com"` | Response headers contain `Access-Control-Allow-Origin: *` |
| 4 | `curl http://localhost:8000/` | Serves static `index.html` (or 404 if file empty — acceptable until FE-01) |
| 5 | Inspect `main.py` | Router imports for ingest/query/files/trace/speech are commented until each module is implemented |

---

## INFRA-03 — Chroma DB client

| # | Test | Expected result |
|---|------|----------------|
| 1 | `python -c "from db import collection_exists; print(collection_exists('nonexistent'))"` | Prints `False`, no exception |
| 2 | After step 1, `ls chroma_data/` | Directory exists (auto-created on first import) |
| 3 | `python -c "from db import get_or_create_collection; c=get_or_create_collection('test123'); print(c.count())"` | Prints `0` |
| 4 | Run step 3 twice | Both runs succeed, collection persists |
| 5 | `python -c "from db import get_client; print(get_client() is get_client())"` | Prints `True` (singleton) |
| 6 | Inspect `db.py` for `chromadb.PersistentClient` | Present — NOT `chromadb.Client()` |

---

# INGESTION

## ING-01 — URL validation, already-indexed check

| # | Test | Expected result |
|---|------|----------------|
| 1 | `POST /index {"repo_url": "git@github.com/x/y"}` | 400, detail mentions "Auth required" |
| 2 | `POST /index {"repo_url": "https://notgithub.com/x/y"}` | 400, detail mentions "Invalid URL" |
| 3 | `POST /index {"repo_url": "http://github.com/x/y"}` | 400 (HTTP not HTTPS) |
| 4 | `POST /index {"repo_url": "https://github.enterprise.com/x/y"}` | 400 |
| 5 | `POST /index {"repo_url": "https://github.com/pallets/flask"}` (before ING-02 wired) | 501 "Indexing not yet implemented" |
| 6 | `GET /status/abc123nonexistent` | 404 "Repo not found" |
| 7 | After full pipeline exists: call `/index` twice on same URL | Second call returns `"status": "already_indexed"` |

---

## ING-02 — Git clone with timeout

| # | Test | Expected result |
|---|------|----------------|
| 1 | Call `clone_repo("https://github.com/pallets/flask", "test_id")` | `clones/test_id/` directory created |
| 2 | `git -C clones/test_id log --oneline | wc -l` | Returns `1` (shallow clone) |
| 3 | Call `clone_repo("https://github.com/nonexistent-user/nonexistent-repo-xyz123", ...)` | Raises HTTPException; `clones/test_id/` does NOT exist on disk afterward |
| 4 | Call `clone_repo("https://github.com/some/private-repo", ...)` | 400 with "Auth required" message |
| 5 | Set `CLONE_TIMEOUT_SECONDS=2`, clone large repo (e.g. torvalds/linux) | Returns 408 within ~4 seconds; clone dir cleaned up |
| 6 | Call `cleanup_clone("test_id")` after manual clone | Directory removed, no error if already missing |
| 7 | Windows: confirm signal.SIGALRM replacement works (subprocess timeout) | Timeouts still trigger; document if not yet ported |

---

## ING-03 — File walker with filters

Assumes you have a clone at `clones/test/`.

| # | Test | Expected result |
|---|------|----------------|
| 1 | `collect_files("clones/test")` on flask | Returns list of dicts with `path`, `rel_path`, `language` |
| 2 | Inspect rel_paths returned | None contain `node_modules`, `.git`, `__pycache__`, `dist`, `build`, `.venv` |
| 3 | Inspect rel_paths | None start with `/` (no leading slash) |
| 4 | Inspect file extensions | All in `{.py, .js, .ts, .tsx, .jsx, .go, .rs, .java, .cpp, .cs}` |
| 5 | Set `MAX_FILES_PER_REPO=10`, walk a 100-file repo | Raises HTTPException 400, message format: `"Repo too large — N files exceeds limit of 10"` |
| 6 | Walk an empty directory | Returns `[]`, no exception |
| 7 | Add a symlink loop in clone, walk it | Does not infinitely recurse |

---

## ING-04 — AST chunker

| # | Test | Expected result |
|---|------|----------------|
| 1 | Create test file with 3 Python functions, run `chunk_files(...)` | Returns ≥3 chunks |
| 2 | Inspect each chunk's `text` | Looks like syntactically valid code (not cut mid-statement) |
| 3 | Inspect chunks | All contain `file_path`, `language`, `start_line` (int), `end_line` (int) |
| 4 | Pass file_info with `rel_path="auth/login.py"` | Resulting chunks have `file_path == "auth/login.py"` |
| 5 | Include a binary file (e.g. `.png` renamed to `.py`) in input | Skipped with warning, no crash |
| 6 | Include an empty `.py` file | Skipped (not zero-length chunks) |
| 7 | Mix Python + JS files | Both produce chunks (different splitter instances per language) |

---

## ING-05 — Dependency graph builder

After indexing flask:

| # | Test | Expected result |
|---|------|----------------|
| 1 | `cat deps/{repo_id}_deps.json` | File exists, valid JSON object |
| 2 | `python -c "import json; g=json.load(open('deps/{repo_id}_deps.json')); print('src/flask/__init__.py' in g)"` | `True` |
| 3 | Check the value `g['src/flask/__init__.py']` | List of files (the importers); non-empty |
| 4 | Search keys for `requests`, `os`, `sys` | Not present (only repo-internal files are keys) |
| 5 | Time the build on flask: `time python -c "from deps_builder import build_dependency_graph; ..."` | Completes in <30 seconds |
| 6 | Add a file with a syntax error to clone, rebuild | Build does not fail; bad file is skipped |
| 7 | All values in the dict | Lists (never `None`); empty list for files no one imports |

---

## ING-06 — Embedding and Chroma write

| # | Test | Expected result |
|---|------|----------------|
| 1 | Run `embed_and_store(chunks, repo_id)` on ~200 chunks | Returns int = total stored; no exception |
| 2 | `from db import get_or_create_collection; get_or_create_collection(repo_id).count()` | `> 0`, equals returned count |
| 3 | Inspect a stored doc: `collection.peek(1)["metadatas"][0]` | Contains all 5 fields: `file_path`, `language`, `start_line`, `end_line`, `content_type` |
| 4 | Re-run with same chunks | Same IDs; Chroma upserts (no duplicates); count unchanged |
| 5 | Code review: confirm `task_type="search_document"` in Salesforce/codet5p-110m-Embedding init | Present, not `search_query` |
| 6 | Code review: batch loop uses `batch_size = 128` | Present |
| 7 | Mock Salesforce/codet5p-110m-Embedding to throw rate-limit error twice then succeed | Function retries with backoff and ultimately succeeds |
| 8 | With invalid `Salesforce/codet5p-110m-_API_KEY` | Function raises an exception (after retries) — does not store partial data silently |

---

## ING-07 — Wire pipeline + file endpoints

| # | Test | Expected result |
|---|------|----------------|
| 1 | `POST /index {"repo_url": "https://github.com/pallets/flask"}` end-to-end | Returns `{repo_id, status: "indexed", chunk_count: >0}` |
| 2 | After call: `ls clones/{repo_id}` | Directory does NOT exist (cleaned up) |
| 3 | After call: `ls deps/{repo_id}_files.json` and `ls deps/{repo_id}_deps.json` | Both exist |
| 4 | Force an error mid-pipeline (e.g. revoke API key after clone) | `clones/{repo_id}/` still gets cleaned up (finally block ran) |
| 5 | `GET /files/{repo_id}` | Returns `{repo_id, files: [...]}` with `.py` files |
| 6 | `GET /files/unknownid` | 404 |
| 7 | `GET /file/{repo_id}?path=src/flask/__init__.py` | Returns `{file_path, language: "python", content: "..."}` |
| 8 | `GET /file/{repo_id}?path=does/not/exist.py` | 404 |
| 9 | Confirm `main.py` has all 5 routers uncommented and included | `app.include_router(...)` for ingest, query, files, trace, speech |

---

# QUERY + GENERATION

## QRY-01 — Query: embed, search, rerank

After indexing flask:

| # | Test | Expected result |
|---|------|----------------|
| 1 | `POST /query {"repo_id": "{repo_id}", "question": "how does routing work?"}` | Returns object with `chunks` array of length 5 |
| 2 | Each chunk in response has `text`, `metadata`, `score` (float) | Yes |
| 3 | Scores are monotonically decreasing | `chunks[0].score >= chunks[1].score >= ...` |
| 4 | `POST /query` with `"scope": "src/flask/"` | All returned chunks have `metadata.file_path` containing `"src/flask/"` |
| 5 | `POST /query` with unknown repo_id | 404 |
| 6 | `POST /query {"question": "asdfqwerzzz nothing matches"}` | Returns gracefully (chunks may all have low scores; no crash) |
| 7 | Code review: `_reranker = CrossEncoder(...)` at module level (not inside handler) | Confirmed |
| 8 | Code review: `task_type="search_query"` in Salesforce/codet5p-110m-Embedding for queries | Confirmed (NOT `search_document`) |
| 9 | Time first query vs second query | Comparable (no model download on second; reranker is cached) |

---

## GEN-01 — Prompt + Nemotron + citations

| # | Test | Expected result |
|---|------|----------------|
| 1 | `POST /query {"question": "how does routing work?"}` | Returns `{answer: "...", citations: [...]}` (chunks field optional now) |
| 2 | `answer` is non-empty natural-language text | Yes |
| 3 | `citations` is non-empty array of `{file_path, start_line}` | Yes |
| 4 | Manually verify: every citation file exists in flask | Yes |
| 5 | `POST /query {"question": "what is the capital of France?"}` (off-topic) | Answer says "I cannot determine this from the available code." |
| 6 | Code review: `_llm = None; _get_llm()` lazy singleton pattern | Present |
| 7 | Code review: `temperature=0.1`, `max_tokens=1024` | Present |
| 8 | `extract_citations("see [auth/login.py:42] and [auth/login.py:42]", ...)` | Deduplicates — returns only one citation |
| 9 | `extract_citations("answer with no citations", chunks)` | Falls back to top 3 chunk metadata as citations |

---

# TRACE

## TRC-01 — Trace endpoint

| # | Test | Expected result |
|---|------|----------------|
| 1 | `POST /trace {"repo_id": "{repo_id}", "file_path": "src/flask/__init__.py"}` | 200 with `{file_path, affected_files: [...]}` |
| 2 | `POST /trace` with leading slash: `"file_path": "/src/flask/__init__.py"` | 200 (slash stripped, not 404) |
| 3 | `POST /trace` with non-existent file path | 404, message includes the file path |
| 4 | `POST /trace` with unknown repo_id | 404 "Repo not indexed" |
| 5 | `POST /trace` on a repo where dep file was deleted | 404 "Dependency graph not found..." |
| 6 | Time the request | <50ms |
| 7 | Code review: no Chroma/Salesforce/codet5p-110m-/Nemotron calls in `trace.py` | Confirmed |

---

# PODCAST

## POD-01 — Speech endpoint (text fallback)

| # | Test | Expected result |
|---|------|----------------|
| 1 | `POST /speech {"repo_id": "{repo_id}", "transcript": "how does auth work?"}` | 200 with `{transcript, answer, citations, audio_base64: null}` |
| 2 | `audio_base64` field | Always `null` for fallback path |
| 3 | `answer` matches what `POST /query` would return for same question | Yes (same pipeline) |
| 4 | `POST /speech` with unknown repo_id | 404 |
| 5 | `POST /speech` with empty transcript | Returns gracefully (empty answer or "I cannot determine...") |

## POD-02 — VoiceChat (skip if no Early Access)

| # | Test | Expected result |
|---|------|----------------|
| 1 | `POST /speech` with `audio_base64: null` | Behaves like POD-01 (text fallback) |
| 2 | `POST /speech` with valid base64 audio + transcript | If VoiceChat available: returns non-null `audio_base64` |
| 3 | Mock VoiceChat to raise an exception | Endpoint still returns 200 with fallback text answer (never 500) |
| 4 | Code review: `n_results=10` reranked to top-3 (not top-5) for VoiceChat | Confirmed |

---

# FRONTEND

## FE-01 — Repo input + indexing

Open `http://localhost:8000` in browser:

| # | Test | Expected result |
|---|------|----------------|
| 1 | Page loads showing URL input and "Index repo" button | Yes |
| 2 | Paste valid GitHub URL, click button | Spinner appears, status text "Cloning and indexing..." |
| 3 | After indexing completes | Spinner disappears, main-section is unhidden, index-section is faded |
| 4 | Open DevTools console: `window.__repoId` | Set to a 32-char hex string |
| 5 | Paste `http://github.com/x/y` (HTTP) and submit | Browser-level pattern validation prevents submit OR backend returns 400 shown in error div |
| 6 | Submit URL for already-indexed repo | Skips polling, jumps straight to complete |
| 7 | Network tab: confirm `GET /status/{id}` polls every 3s during indexing | Yes |
| 8 | Index button | Disabled while indexing in progress |

---

## FE-02 — File tree

| # | Test | Expected result |
|---|------|----------------|
| 1 | After indexing, file tree appears in sidebar | Yes |
| 2 | Click a folder | Children show/hide |
| 3 | Click a file, then `window.__scope` in console | Equals the file's full path (e.g. `"src/flask/app.py"`) |
| 4 | Click a folder, `window.__scope` | Ends with `/` (e.g. `"src/flask/"`) |
| 5 | Click another file | Previous selection's `.selected` class is removed |
| 6 | Within each folder | Folders listed before files; alphabetical |

---

## FE-03 — Chat with citations

| # | Test | Expected result |
|---|------|----------------|
| 1 | Type "how does routing work?" and click Ask | User message appears, then assistant message |
| 2 | Citations rendered below answer | Yes, as clickable chips like `src/flask/app.py:42` |
| 3 | Click chip | Calls `window.__openCodeViewer(...)` (no-op until FE-04 wired) |
| 4 | Send another message while one is in flight | Send button is disabled until response arrives |
| 5 | Input field after submit | Cleared |
| 6 | Select a folder in tree, ask question | Network tab shows `scope` field in request body |
| 7 | XSS test: ask question whose answer might contain `<script>` | Rendered as text, not executed (escapeHtml works) |
| 8 | Scope indicator | Shows current scope or "Whole codebase" |

---

## FE-04 — Code viewer

| # | Test | Expected result |
|---|------|----------------|
| 1 | Click a citation chip from chat | Code viewer panel opens |
| 2 | View | Shows file content with syntax highlighting (colored tokens, not plain text) |
| 3 | Cited line | Highlighted (e.g. yellow `<mark>`) |
| 4 | Scroll | Approximately at the cited line on open |
| 5 | Click × button | Viewer hidden |
| 6 | Inspect HTML | No `<textarea>`, no `contenteditable` attribute anywhere in viewer |
| 7 | Network tab | Prism.js loaded from `cdnjs.cloudflare.com` only — not jsdelivr or other |

---

## FE-05 — Trace view

| # | Test | Expected result |
|---|------|----------------|
| 1 | Select a file in tree | "Show affected files" button appears |
| 2 | Select a folder in tree | Trace button hidden |
| 3 | Click "Show affected files" on `src/flask/__init__.py` | Affected files in tree get `.trace-affected` class (visible color change) |
| 4 | Result text | Shows "N file(s) affected" or "No files import this file" |
| 5 | Select a different file | Previous trace highlights cleared |
| 6 | Trace runs only on button click | Not automatically when file selected |

---

## FE-06 — Podcast UI

Test in Chrome (best Web Speech API support):

| # | Test | Expected result |
|---|------|----------------|
| 1 | Podcast panel appears after indexing | Yes |
| 2 | Click mic button, grant mic permission, speak a question | Transcript appears as "You: ..." |
| 3 | After transcript | Answer text shown and spoken aloud via `SpeechSynthesis` |
| 4 | Click mic button while recording | Recording stops |
| 5 | Open in Firefox (or browser without SpeechRecognition) | Shows "Voice not supported" message, no crash |
| 6 | Click mic before any repo is indexed | Nothing sent (`window.__repoId` null check) |
| 7 | If VoiceChat returns audio | Plays via `Audio` element (not `<audio autoplay>`) |

---

# ARCHITECTURE DIAGRAM

## ARCH-01 — Architecture endpoint

After indexing flask:

| # | Test | Expected result |
|---|------|----------------|
| 1 | `GET /architecture/{repo_id}` | 200 with `{repo_id, tree, total_files, total_folders}` |
| 2 | `tree.id == "/"`, `tree.type == "folder"` | Yes |
| 3 | Walk tree recursively, count files | Equals `total_files` and equals length of `deps/{repo_id}_files.json` |
| 4 | Walk tree, count folders excluding root | Equals `total_folders` |
| 5 | Every file node has `language` field; no folder node has `language` | Confirmed |
| 6 | Every folder has `children` (array, possibly empty) — never null | Confirmed |
| 7 | Within any folder | Folders before files; each group alphabetical |
| 8 | Files `auth/login.py` and `auth/jwt.py` | Single `auth` folder node containing both — not duplicated |
| 9 | `GET /architecture/unknownid` | 404 "Repo not indexed" |
| 10 | Delete `deps/{repo_id}_files.json` then call endpoint | 404 "File list not found — re-index the repo" |
| 11 | A `.unknownext` file (if any) | `language: "unknown"` |
| 12 | Code review: no AST/import parsing in `get_architecture` | Confirmed |

Verification script:

```python
import requests, json
data = requests.get("http://localhost:8000/architecture/{repo_id}").json()
def count(node, files=0, folders=0):
    if node["type"] == "file":
        return files + 1, folders
    folders += 1
    for c in node["children"]:
        files, folders = count(c, files, folders)
    return files, folders
f, fo = count(data["tree"])
assert f == data["total_files"]
assert fo - 1 == data["total_folders"]
print("OK")
```

---

## ARCH-02 — Frontend D3 diagram

| # | Test | Expected result |
|---|------|----------------|
| 1 | Before indexing, "Architecture diagram" button | Hidden |
| 2 | After indexing, button appears | Yes |
| 3 | Click button | Panel opens, diagram renders |
| 4 | Initial state | Only root's immediate children visible — sub-folders collapsed |
| 5 | Click a folder node | Children expand with animation (~300ms) |
| 6 | Click again | Collapses with animation |
| 7 | Inspect circle fill: collapsed folder | `#374151` (darker gray) |
| 8 | Inspect circle fill: expanded folder | `#6B7280` (lighter gray) |
| 9 | Folder icon | `▶` when collapsed, `▼` when expanded |
| 10 | Click a Python file node | `window.__openCodeViewer(filePath, 1)` called; code viewer opens |
| 11 | Inspect Python file circle | Fill `#4B8BBE` |
| 12 | Inspect JS file circle (if any) | Fill `#F7DF1E` |
| 13 | Click "Expand all" | Every folder shown |
| 14 | Click "Collapse all" | Only root level visible |
| 15 | Type "init" in search box | All folders expand; matching nodes get red bold label and red circle stroke |
| 16 | Clear search | Highlights removed |
| 17 | Scroll wheel on diagram | Zooms in/out |
| 18 | Click and drag background | Pans the view |
| 19 | Click "Reset zoom" | Returns to default transform |
| 20 | Close panel and reopen | No second `/architecture/...` request in network tab |
| 21 | Filename longer than 28 chars | Truncated with `…` |
| 22 | Network tab | D3.js loaded from `cdnjs.cloudflare.com` only |
| 23 | Inject filename containing `<script>` (in test repo) | Rendered as text by `.text()`, not executed |

---

# END-TO-END SMOKE TEST

Single happy-path run after all tasks implemented:

1. Start server: `./run_dev.sh`
2. Open `http://localhost:8000`
3. Index `https://github.com/pallets/flask` — wait for completion
4. Browse file tree, click `src/flask/app.py`
5. Ask in chat: "How are routes registered?" — verify answer + clickable citations
6. Click a citation chip — verify code viewer opens at correct line with syntax highlighting
7. Click "Show affected files" on `src/flask/__init__.py` — verify highlights in tree
8. Open architecture diagram — verify collapsed root, expand a folder, click a file → opens viewer
9. Search "app" in diagram — verify highlights; clear → highlights gone
10. Click mic, ask "what is Flask?" — verify spoken answer (Chrome only)
11. Reload page, re-index same URL — verify "already_indexed" path is fast (no re-clone)
12. Verify `clones/` is empty after every indexing run
