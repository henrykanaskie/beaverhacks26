import os
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

_APP_DIR = Path(__file__).resolve().parent
os.chdir(_APP_DIR)
load_dotenv(_APP_DIR / ".env")

app = FastAPI(title="AccliMate")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health():
    return {"status": "ok"}


# Import and include routers once each module is implemented
from ingest import router as ingest_router
from files import router as files_router
from query import router as query_router
from trace import router as trace_router
from system import router as system_router
from projects import router as projects_router
from agent import router as agent_router
# from speech import router as speech_router
app.include_router(ingest_router)
app.include_router(files_router)
app.include_router(query_router)
app.include_router(trace_router)
app.include_router(system_router)
app.include_router(projects_router)
app.include_router(agent_router)
# app.include_router(speech_router)

app.mount(
    "/",
    StaticFiles(directory=str(_APP_DIR / "static"), html=True),
    name="static",
)

if __name__ == "__main__":
    import uvicorn

    # Exclude dirs from reload watcher — otherwise `git clone` during POST /index
    # touches thousands of files and restarts the server mid-request.
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
    )
