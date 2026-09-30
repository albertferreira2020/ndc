"""The continuous loop: guard -> pick -> dispatch -> verify -> record, stopping cleanly on budget."""
from __future__ import annotations

import json
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from dataclasses import dataclass

from . import plan as planner, quality, scan, snapshot, store
from .env import claude_env
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


def build_prompt(task, tree_kept: bool = True) -> str:
    parts = [f"Task #{task['id']} ({task['complexity']}, {task['kind']}): {task['title']}", task["description"]]
    for dep in json.loads(task["depends_on"]):
        f = briefs_dir() / f"task-{dep}.md"
        if f.exists():
            parts.append(f"Context from task #{dep} (already done, do not redo it):\n{f.read_text()[:BRIEF_CAP]}")
    if task["last_report"]:
        state = ("The working tree still holds the previous attempt's changes." if tree_kept else
                 "The previous attempt's changes were discarded: start from the current code.")
        parts.append(f"This is a RETRY in a fresh session. {state} Report of that attempt:\n" + task["last_report"][:3000])
    if task["verify_cmd"]:
        goal = "must FAIL (the tests are written before the implementation)" if task["expect_red"] else "must pass"
        parts.append(f"Verification command: `{task['verify_cmd']}` {goal}.")
    parts.append("Work only on this task and stop when it is done. Do not start other tasks. "
                 "Do not re-read files you do not need. Final reply: at most 10 lines.")
    return "\n\n".join(p for p in parts if p)


def verify(task, timeout, cwd=None) -> tuple[bool, str]:
    """Runs the task's gate. For expect_red tasks the gate passes only if the command FAILS."""
    if not task["verify_cmd"]:
        return True, ""
    try:
        r = subprocess.run(task["verify_cmd"], shell=True, capture_output=True, text=True, timeout=timeout, cwd=cwd)
    except subprocess.TimeoutExpired:
        return False, f"verify timed out after {timeout}s"
    tail = (r.stdout + r.stderr)[-300:]
    if task["expect_red"]:
        if r.returncode == 0:
            return False, "tests passed before any implementation: they check nothing (expected red)"
        return True, f"red as expected: {tail}"
    return (r.returncode == 0), (tail if r.returncode else "verified")


@dataclass
class Outcome:
    ok: bool
    note: str
    report: str  # seeds the next attempt when this one failed
    out: str
    touched: tuple = ()


def _claude(task, model, cfg, timeout, cwd=None, tree_kept=True):
    try:
        r = subprocess.run(["claude", "-p", build_prompt(task, tree_kept), "--model", model, *cfg.get("claude_args", [])],
                           capture_output=True, text=True, timeout=timeout, cwd=cwd, env=claude_env(cfg))
        return r.returncode == 0, (r.stdout if r.returncode == 0 else r.stderr)
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        return False, str(e)


def _review_allowed(cfg, db) -> bool:
    try:
        us = read_usage(cfg)
    except UsageUnavailable:
        return False
    return decide(us, "S", cfg, lambda w, c: store.history(db, w, c)).action != "STOP"


def execute_task(db, cfg, task, tier, model, timeout, log=print, root=None, ask=None, tree_kept=True) -> Outcome:
    """Dispatch one task in a fresh session, then apply the gates, cheapest first:
    verify -> read-only/protected files -> regression -> scoped review."""
    root = root or Path.cwd()
    q, kind = cfg.get("quality", {}), task["kind"]
    vt = cfg.get("verify_timeout_seconds", 900)
    checks = []
    if kind == "work":
        checks = q.get("checks") or (quality.detect_checks(root) if q.get("auto_checks", True) else [])
    baseline = quality.run_checks(checks, root, vt)[0] if checks else None
    before = snapshot.take(root)

    ok, out = _claude(task, model, cfg, timeout, cwd=root, tree_kept=tree_kept)
    after = snapshot.take(root)
    ch = snapshot.diff(before, after) if before is not None and after is not None else None
    touched = sorted(ch["added"] | ch["changed"] | ch["removed"]) if ch else []
    all_touched = sorted(set(json.loads(task["files_touched"])) | set(touched))  # every attempt, for the review
    files = f"Changed files: {', '.join(all_touched[:20]) or '(none)'}\n"

    def fail(gate: str, why: str) -> Outcome:
        log(f"  gate '{gate}' failed: {why.strip().splitlines()[0][:160] if why.strip() else ''}")
        return Outcome(False, f"[{gate}] {why}"[:500], f"Attempt by {tier}/{model} failed at gate '{gate}':\n{why[:1200]}\n"
                       f"{files}Its final message:\n{out[:1200]}", out, tuple(touched))

    if not ok:
        return fail("dispatch", out[-500:])
    ok, vnote = verify(task, vt, cwd=root)
    if not ok:
        return fail("verify", vnote)
    note = vnote
    if ch is None:
        log("  warning: project too large to track file changes; read-only and protected-file gates skipped")
    else:
        if kind in q.get("readonly_kinds", ["explore", "plan"]) and touched:
            return fail("read-only", f"a {kind} task must not modify files, but changed: {', '.join(touched[:10])}")
        if q.get("protect_tests", True) and kind != "test" and not task["may_edit_tests"]:
            hit = sorted((ch["changed"] | ch["removed"]) & store.protected(db))
            if hit:
                return fail("protected-files", "it edited test files written by an earlier test task (fix the code, "
                            f"not the tests): {', '.join(hit[:10])}")
    if q.get("security_scan", True) and all_touched:
        present = [p for p in all_touched if (root / p).is_file()]
        found = scan.scan_files(root, present)
        if scan.touches_config(present):  # config findings count only for files this task touched
            found += [f for f in scan.scan_config(root) if f.file in set(present)]
        high = scan.at_least(found, "high")
        if high:
            return fail("security", "\n".join(str(f) for f in high[:8]))
        medium = scan.at_least(found, "medium")
        if medium:
            log(f"  security notes (not blocking): {len(medium)} medium finding(s), first: {medium[0]}")
            note += f" | security: {len(medium)} medium finding(s)"
    if checks and baseline:
        good, tail = quality.run_checks(checks, root, vt)
        if not good:
            return fail("regression", "project checks were green before this task and are red now: " + tail)
    elif checks and baseline is False:
        log("  project checks were already failing before this task: regression gate skipped")
    mode = q.get("review", "risk")
    if kind == "work" and all_touched and (mode == "all" or (mode == "risk" and (
            task["risk"] == "high" or task["complexity"] in ("L", "XL")))):
        if not _review_allowed(cfg, db):
            log("  review skipped: no budget headroom or usage unknown")
            note += " | review skipped (budget)"
        else:
            try:
                raw = (ask or planner.ask_claude)(quality.review_prompt(task, all_touched), cfg, root, cfg["models"]["senior"])
                issues = quality.parse_review(raw)
            except planner.PlanError as e:
                log(f"  review unavailable ({e}); not blocking")
                note += " | review unavailable"
            else:
                blockers = [i for i in issues if i["severity"] == "blocker"]
                if blockers:
                    return fail("review", quality.format_issues(issues))
                if issues:
                    log("  review notes (not blocking):\n" + quality.format_issues(issues))
                    note += f" | review: {len(issues)} non-blocking issue(s)"
                else:
                    note += " | review: clean"
    if kind == "test" and ch:
        store.protect(db, task["id"], sorted(ch["added"] | ch["changed"]))
    return Outcome(True, f"{note}\n{out[-500:]}".strip(), "", out, tuple(touched))


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


class Monitor:
    """Turns budget monitoring on (once) when the remaining budget stops covering the whole pending queue."""
    def __init__(self):
        self.on = False

    def update(self, db, usage, cfg, queue, log):
        o = outlook(usage, queue, cfg, lambda w, c: store.history(db, w, c))
        if not o.all_fit and not self.on:
            self.on = True
            log(f"MONITORING ON: budget covers {o.covered} of {o.total} pending tasks ({o.detail})")
            log(f"  checkpoint written: {write_handoff(db, reason='budget monitoring started')}")
        elif o.all_fit and self.on:
            self.on = False
            log(f"monitoring off: budget covers all {o.total} pending tasks")


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
    monitor, first = Monitor(), True
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
            monitor.update(db, usage, cfg, [t for t in store.list_tasks(db, "pending") if t["id"] not in seen], log)
        task, d = (avail[0], None) if usage is None else pick(db, usage, cfg, seen)
        if task is None:
            log(f"STOP: {d.reason}")
            log(f"handoff written: {write_handoff(db, reason=d.reason)}")
            log("  to continue in a fresh interactive session seeded with it: ndc session")
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
        res = execute_task(db, cfg, task, tier, model, timeout, log)
        ok, note, out = res.ok, res.note, res.out
        if ok and task["kind"] in BRIEF_KINDS:
            (briefs_dir() / f"task-{task['id']}.md").write_text(out[:BRIEF_CAP])
        after = None
        try:
            after = read_usage(cfg, refresh=True)
        except UsageUnavailable:
            pass
        store.finish(db, task["id"], ok, _deltas(usage, after), note, res.report, res.touched)
        if not ok:
            t = store.get(db, task["id"])
            log(f"  failed ({t['failures']}x); the retry goes one tier up, in a fresh session seeded with the attempt report")
            if t["failures"] >= MAX_FAILURES:
                store.block(db, task["id"], f"failed {t['failures']} times: {note[:200]}")
