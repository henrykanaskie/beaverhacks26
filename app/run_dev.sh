#!/usr/bin/env bash
# Dev server — no --reload.
# uvicorn's WatchFiles reload is fundamentally broken for subdirectories:
# clone/embed writes .py files under clones/ and --reload-exclude can't
# reliably suppress them, killing in-flight /index requests.
# Just restart manually (Ctrl-C + re-run) when you edit source.
cd "$(dirname "$0")"
exec ../.venv/bin/python -m uvicorn main:app --host 0.0.0.0 --port 8000
