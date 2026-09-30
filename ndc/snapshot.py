"""File-level change tracking around a task. Works without git, costs no tokens."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

SKIP_DIRS = {".git", ".ndc", ".claude", "node_modules", "__pycache__", ".venv", "venv", "dist", "build",
             ".pytest_cache", ".mypy_cache", "target"}
MAX_FILES = 5000
MAX_BYTES = 2_000_000


def take(root: Path):
    """{relative path: sha1}. None when the tree is too large to track (gates then degrade, loudly)."""
    out = {}
    for dp, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for f in files:
            if f == ".DS_Store" or f.endswith(".pyc"):
                continue
            p = Path(dp) / f
            try:
                if p.stat().st_size > MAX_BYTES:
                    continue
                if len(out) >= MAX_FILES:
                    return None
                out[str(p.relative_to(root))] = hashlib.sha1(p.read_bytes()).hexdigest()
            except OSError:
                continue
    return out


def diff(before: dict, after: dict) -> dict:
    b, a = set(before), set(after)
    return {"added": a - b, "removed": b - a, "changed": {p for p in a & b if before[p] != after[p]}}
