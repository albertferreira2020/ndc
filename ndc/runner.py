"""The continuous loop: guard -> pick -> dispatch -> verify -> record, stopping cleanly on budget."""
from __future__ import annotations

import json
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from . import store
from .config import state_dir
from .guardian import decide, outlook
from .handoff import write as write_handoff
from .router import route
from .usage import UsageUnavailable, read_usage

MAX_FAILURES = 4
BRIEF_KINDS = ("explore", "plan", "test")  # outputs worth handing to dependent tasks
BRIEF_CAP = 6000


def briefs_dir() -> Path:
    d = state_dir().parent / "briefs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def build_prompt(task) -> str:
    parts = [f"Task #{task['id']} ({task['complexity']}, {task['kind']}): {task['title']}", task["description"]]
    for dep in json.loads(task["depends_on"]):
        f = briefs_dir() / f"task-{dep}.md"
        if f.exists():
            parts.append(f"Context from task #{dep} (already done, do not redo it):\n{f.read_text()[:BRIEF_CAP]}")
    if task["verify_cmd"]:
        goal = "must FAIL (the tests are written before the implementation)" if task["expect_red"] else "must pass"
        parts.append(f"Verification command: `{task['verify_cmd']}` {goal}.")
    parts.append("Work only on this task and stop when it is done. Do not start other tasks. "
                 "Do not re-read files you do not need. Final reply: at most 10 lines.")
    return "\n\n".join(p for p in parts if p)


def verify(task, timeout) -> tuple[bool, str]:
    """Runs the task's gate. For expect_red tasks the gate passes only if the command FAILS."""
    if not task["verify_cmd"]:
        return True, ""
    try:
        r = subprocess.run(task["verify_cmd"], shell=True, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, f"verify timed out after {timeout}s"
    tail = (r.stdout + r.stderr)[-300:]
    if task["expect_red"]:
        if r.returncode == 0:
            return False, "tests passed before any implementation: they check nothing (expected red)"
        return True, f"red as expected: {tail}"
    return (r.returncode == 0), (tail if r.returncode else "verified")


def _deltas(before, after):
    if not before or not after:
        return {}
    out = {}
    for w in after.windows:
        b = before.get(w.name)
        if not b or w.used_pct < b.used_pct:  # missing, or the window reset in between: no valid sample
            continue
        # /usage reports whole percentages, so "no change" means "under one point": record half a step
        out[w.name] = (w.used_pct - b.used_pct) or 0.5
    return out


def pick(db, usage, cfg, skip=()):
    """First ready task that fits; the queue order is the PO's priority order."""
    last = None
    for t in store.ready_tasks(db):
        if t["id"] in skip:
            continue
        d = decide(usage, t["complexity"], cfg, lambda w, c: store.history(db, w, c))
        last = d
        if d.action in ("GO", "WIND_DOWN"):
            return t, d
    return None, last


def run(db, cfg, execute=False, ignore_usage=False, wait=False, timeout=3600, log=print) -> str:
    store.requeue_interrupted(db)
    seen = set()  # dry run only: never mutates the queue
    monitoring, first = False, True
    success = lambda k, c, t: store.success_rate(db, k, c, t)
    while True:
        avail = [t for t in store.ready_tasks(db) if t["id"] not in seen]
        if not avail:
            pending = store.list_tasks(db, "pending")
            log("queue empty" if not pending else "no ready tasks (dependencies blocked)")
            return "idle"
        try:
            # fresh /usage on the first pass; afterwards the reading taken right after the last task is reused
            usage = read_usage(cfg, refresh=execute and first)
        except UsageUnavailable as e:
            if not ignore_usage:
                log(f"usage unknown, refusing to start a task: {e}")
                return "unknown-usage"
            usage = None
        first = False
        if usage is not None:
            queue = [t for t in store.list_tasks(db, "pending") if t["id"] not in seen]  # whole queue, not just ready tasks
            o = outlook(usage, queue, cfg, lambda w, c: store.history(db, w, c))
            if not o.all_fit and not monitoring:
                monitoring = True
                log(f"MONITORING ON: budget covers {o.covered} of {o.total} pending tasks ({o.detail})")
                log(f"  checkpoint written: {write_handoff(db, reason='budget monitoring started')}")
            elif o.all_fit and monitoring:
                monitoring = False
                log(f"monitoring off: budget covers all {o.total} pending tasks")
        task, d = (avail[0], None) if usage is None else pick(db, usage, cfg, seen)
        if task is None:
            log(f"STOP: {d.reason}")
            log(f"handoff written: {write_handoff(db, reason=d.reason)}")
            if d.resume_at:
                log(f"limit resets at {d.resume_at.isoformat()}")
            if not (wait and d.resume_at):
                return "stopped"
            secs = max(0, (d.resume_at - datetime.now(timezone.utc)).total_seconds()) + 60
            log(f"waiting {secs / 60:.0f} min for the reset")
            time.sleep(secs)
            continue
        tier, model = route(task, cfg, success)
        log(f"#{task['id']} [{task['kind']}/{task['complexity']}/{task['risk']}] {task['title']} -> {tier}/{model}"
            + (f" [{d.action}]" if d else ""))
        if not execute:
            log("  dry run: not dispatching (use --execute)")
            seen.add(task["id"])
            continue
        store.start(db, task["id"], tier, model)
        try:
            r = subprocess.run(["claude", "-p", build_prompt(task), "--model", model, *cfg.get("claude_args", [])],
                               capture_output=True, text=True, timeout=timeout)
            ok, out = r.returncode == 0, (r.stdout if r.returncode == 0 else r.stderr)
        except (subprocess.TimeoutExpired, FileNotFoundError) as e:
            ok, out = False, str(e)
        note = out[-500:]
        if ok:
            ok, vnote = verify(task, cfg.get("verify_timeout_seconds", 900))
            note = f"{vnote}\n{note}".strip()
        if ok and task["kind"] in BRIEF_KINDS:
            (briefs_dir() / f"task-{task['id']}.md").write_text(out[:BRIEF_CAP])
        after = None
        try:
            after = read_usage(cfg, refresh=True)
        except UsageUnavailable:
            pass
        store.finish(db, task["id"], ok, _deltas(usage, after), note)
        if not ok:
            t = store.get(db, task["id"])
            log(f"  failed ({t['failures']}x), will retry one tier up: {note[:120]}")
            if t["failures"] >= MAX_FAILURES:
                store.block(db, task["id"], f"failed {t['failures']} times: {note[:200]}")
