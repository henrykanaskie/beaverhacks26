#!/usr/bin/env python3
"""Inspect Chroma chunking for a repo collection. Run from app/:

  cd app && ../.venv/bin/python inspect_chroma.py
  cd app && ../.venv/bin/python inspect_chroma.py 7e3d4d9940b1f8f3506bd03cf4ff61aa
  cd app && ../.venv/bin/python inspect_chroma.py 7e3d4d9940b1f8f3506bd03cf4ff61aa --scan 2000
"""
from __future__ import annotations

import argparse
import collections
import os
import sys
from pathlib import Path

# Ensure cwd is app/ so db.py finds ./chroma_data
_APP = Path(__file__).resolve().parent
os.chdir(_APP)

from db import get_client

_CHROMA_DIR = _APP / "chroma_data"
_SQLITE = _CHROMA_DIR / "chroma.sqlite3"


def _print_storage_hint() -> None:
    print(f"Chroma persist dir: {_CHROMA_DIR}")
    print(f"  exists: {_CHROMA_DIR.is_dir()}  sqlite: {_SQLITE.is_file()}", end="")
    if _SQLITE.is_file():
        print(f"  ({_SQLITE.stat().st_size} bytes)")
    else:
        print()


def main() -> None:
    p = argparse.ArgumentParser(description="Inspect Chroma chunks for a repo_id collection.")
    p.add_argument(
        "repo_id",
        nargs="?",
        default=None,
        help="32-char hex collection name (MD5 of repo_url). Omit to list collections.",
    )
    p.add_argument("--peek", type=int, default=8, help="How many sample chunks to print (default 8)")
    p.add_argument(
        "--scan",
        type=int,
        metavar="N",
        default=0,
        help="Fetch up to N chunks (metadata only) and print per-file chunk counts",
    )
    args = p.parse_args()

    client = get_client()
    cols = client.list_collections()
    names = sorted(c.name for c in cols)

    if not args.repo_id:
        _print_storage_hint()
        print("\nCollections (repo_id values):\n")
        if not names:
            print("  (none)\n")
            print(
                "No collections usually means: indexing never finished successfully, "
                "embed failed and collection was rolled back, or this persist dir is empty "
                "while uvicorn used another cwd.\n"
                "Confirm POST /index returned status indexed + chunk_count > 0. "
                "Server must run with cwd = app/ (main.py chdir) same as this script.\n"
            )
        for n in names:
            try:
                c = client.get_collection(n)
                print(f"  {n}  count={c.count()}")
            except Exception as e:
                print(f"  {n}  error={e}")
        print("\nUsage: python inspect_chroma.py <repo_id>")
        sys.exit(0)

    rid = args.repo_id.strip()
    col = client.get_collection(rid)
    n_total = col.count()
    print(f"collection={rid}\ntotal_chunks={n_total}\n")

    peek_n = min(args.peek, n_total) if n_total else 0
    if peek_n:
        out = col.peek(limit=peek_n)
        ids = out["ids"]
        docs = out["documents"] or []
        metas = out["metadatas"] or []
        print(f"--- peek first {peek_n} (verify metadata + text shape) ---\n")
        for i in range(len(ids)):
            m = metas[i] if i < len(metas) else {}
            doc = docs[i] if i < len(docs) else ""
            lines = doc.count("\n") + (1 if doc else 0)
            print(f"id: {ids[i][:72]}...")
            print(f"  file_path={m.get('file_path')}  lang={m.get('language')}")
            print(f"  start_line={m.get('start_line')}  end_line={m.get('end_line')}  content_type={m.get('content_type')}")
            print(f"  text_lines≈{lines}  chars={len(doc)}")
            preview = doc.replace("\n", "\\n")[:180]
            print(f"  preview: {preview}...")
            print()

    if args.scan > 0 and n_total:
        lim = min(args.scan, n_total)
        got = col.get(include=["metadatas"], limit=lim)
        metas = got.get("metadatas") or []
        by_file: collections.Counter[str] = collections.Counter()
        for m in metas:
            fp = (m or {}).get("file_path", "?")
            by_file[fp] += 1
        print(f"--- per-file chunk counts (first {lim} chunks only) ---\n")
        for fp, cnt in by_file.most_common(40):
            print(f"  {cnt:4d}  {fp}")
        if len(by_file) > 40:
            print(f"  ... and {len(by_file) - 40} more files")
        print()


if __name__ == "__main__":
    main()
