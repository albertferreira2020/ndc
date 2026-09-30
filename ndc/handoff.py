"""Handoff report that seeds a fresh session."""
from __future__ import annotations

from pathlib import Path

from . import store
from .config import state_dir


def build(db, notes: str = "", reason: str = "") -> str:
    by = lambda s: store.list_tasks(db, s)
    line = lambda t: f"- #{t['id']} [{t['complexity']}] {t['title']}"
    out = ["# NDC handoff", ""]
    if reason:
        out += [f"Stopped because: {reason}", ""]
    out += ["## Done"] + ([line(t) for t in by("done")] or ["- (nothing yet)"]) + [""]
    running = by("running")
    if running:
        out += ["## Interrupted (re-run from scratch, check the working tree first)"] + [line(t) for t in running] + [""]
    blocked = by("blocked")
    if blocked:
        out += ["## Blocked"] + [f"{line(t)}: {t['notes']}" for t in blocked] + [""]
    ready = {t["id"] for t in store.ready_tasks(db)}
    out += ["## Pending (next first)"]
    for t in by("pending"):
        tag = "ready" if t["id"] in ready else f"waits on {t['depends_on']}"
        extra = f" - {t['failures']} failed attempt(s)" if t["failures"] else ""
        out.append(f"{line(t)} ({tag}){extra}")
    if not by("pending"):
        out.append("- (nothing pending)")
    out += ["", "## Notes from the session", notes.strip() or "(none provided)", "",
            "## How to continue",
            "Run `python3 -m ndc run --execute` to resume the queue. Do not redo the Done items."]
    return "\n".join(out) + "\n"


def write(db, notes="", reason="") -> Path:
    from datetime import datetime, timezone
    d = state_dir().parent / "handoff"
    d.mkdir(parents=True, exist_ok=True)
    text = build(db, notes, reason)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    (d / f"HANDOFF-{stamp}.md").write_text(text)
    latest = d / "latest.md"
    latest.write_text(text)
    return latest
