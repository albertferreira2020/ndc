"""Parallel execution: several ready tasks at once, each in its own git worktree, merged back when it passes.

The budget guard reserves the estimated cost of every running task, so N workers never promise more quota
than is left. Results are merged one at a time into the checked-out branch; a merge conflict fails the
attempt cleanly and the retry starts again from the updated code.
"""
from __future__ import annotations

import threading
import time
from concurrent import futures as cf
from datetime import datetime, timezone
from pathlib import Path

from . import activator, runner, store, worktree
from .guardian import decide, estimate
from .handoff import write as write_handoff
from .router import route
from .usage import UsageUnavailable, read_usage


def _db_file(db) -> str:
    f = db.execute("PRAGMA database_list").fetchone()[2]
    if not f:
        raise ValueError("parallel mode needs the queue in a file, not in memory")
    return f


def _reserve(task, usage, cfg, db) -> dict:
    safety = cfg["guardian"]["safety_factor"]
    return {w.name: estimate(w.name, task["complexity"], store.history(db, w.name, task["complexity"]))[0] * safety
            for w in usage.windows}


def _sum(running: dict) -> dict:
    out: dict = {}
    for info in running.values():
        for k, v in info["reserve"].items():
            out[k] = out.get(k, 0.0) + v
    return out


def _work(root, db_file, cfg, task, tier, model, timeout, lock, log):
    """Runs in a worker thread. Own DB connection; git operations that touch the main tree are serialized."""
    wdb = store.connect(db_file)
    wt, branch = None, f"ndc/task-{task['id']}"
    try:
        with lock:
            wt, branch = worktree.create(root, task["id"])
        res = runner.execute_task(wdb, cfg, task, tier, model, timeout, log, root=wt, tree_kept=False)
        if res.ok:
            msg = f"ndc: task #{task['id']} {task['title']}"
            if worktree.commit_all(root, wt, msg):
                with lock:
                    merged, conflicts = worktree.merge(root, branch, f"ndc: merge task #{task['id']} {task['title']}")
                if not merged:
                    why = f"merging into the main branch conflicted on: {', '.join(conflicts[:8])}"
                    log(f"  gate 'merge-conflict' failed: {why}")
                    return runner.Outcome(
                        False, f"[merge-conflict] {why}"[:500],
                        f"Attempt by {tier}/{model} passed its gates but {why}. Its worktree was discarded. "
                        "Redo the task on top of the current code (other tasks were merged meanwhile).",
                        res.out, res.touched)
        return res
    except worktree.GitError as e:
        return runner.Outcome(False, f"[git] {e}"[:500], f"Attempt failed in git: {e}", "", ())
    finally:
        if wt is not None:
            with lock:
                worktree.remove(root, wt, branch)
        wdb.close()


def _finish(db, cfg, tid, fut, info, log):
    try:
        res = fut.result()
    except Exception as e:  # a crashed worker must not take the loop down with it
        res = runner.Outcome(False, f"[crash] {e}"[:500], f"internal error: {e}", "", ())
    task = store.get(db, tid)
    if res.ok and task["kind"] in runner.BRIEF_KINDS:
        (runner.briefs_dir() / f"task-{tid}.md").write_text(res.out[:runner.BRIEF_CAP])
    deltas = {}
    if not info["shared"] and info["before"] is not None:  # only a task that ran alone has a clean usage delta
        try:
            deltas = runner._deltas(info["before"], read_usage(cfg, refresh=True))
        except UsageUnavailable:
            pass
    store.finish(db, tid, res.ok, deltas, res.note, res.report, res.touched)
    if res.ok:
        log(f"#{tid} done")
    else:
        t = store.get(db, tid)
        log(f"#{tid} failed ({t['failures']}x); the retry goes one tier up on a fresh worktree")
        if t["failures"] >= runner.MAX_FAILURES:
            store.block(db, tid, f"failed {t['failures']} times: {res.note[:200]}")


def run_parallel(db, cfg, workers, ignore_usage=False, wait=False, timeout=3600, log=print, root=None) -> str:
    root = Path(root or Path.cwd()).resolve()
    activator.init(root)  # hides `.ndc/` from git first: its own state must not make the tree look dirty
    worktree.preflight(root)
    db_file = _db_file(db)
    store.requeue_interrupted(db)
    lock, monitor = threading.Lock(), runner.Monitor()
    success = lambda k, c, t: store.success_rate(db, k, c, t)
    running: dict = {}
    pending_futures: dict = {}
    first = True
    with cf.ThreadPoolExecutor(max_workers=workers) as pool:
        while True:
            for f in [f for f in pending_futures if f.done()]:
                tid = pending_futures.pop(f)
                _finish(db, cfg, tid, f, running.pop(tid), log)
            ready = [t for t in store.ready_tasks(db) if t["id"] not in running]
            if not ready and not pending_futures:
                log("queue empty" if not store.list_tasks(db, "pending") else "no ready tasks (dependencies blocked)")
                return "idle"
            usage, can_start = None, True
            try:
                usage = read_usage(cfg, refresh=first)
            except UsageUnavailable as e:
                if not ignore_usage:
                    if not pending_futures:
                        log(f"usage unknown, refusing to start a task: {e}")
                        return "unknown-usage"
                    can_start = False  # let the running tasks finish, start nothing new
            first = False
            if usage is not None:
                monitor.update(db, usage, cfg, [t for t in store.list_tasks(db, "pending") if t["id"] not in running], log)
            launched, last = 0, None
            for t in ready if can_start else []:
                if len(pending_futures) >= workers:
                    break
                if usage is not None:
                    last = decide(usage, t["complexity"], cfg, lambda w, c: store.history(db, w, c), _sum(running))
                    if last.action not in ("GO", "WIND_DOWN"):
                        continue
                tier, model = route(t, cfg, success)
                for other in running.values():
                    other["shared"] = True
                store.start(db, t["id"], tier, model)
                running[t["id"]] = {"reserve": _reserve(t, usage, cfg, db) if usage else {}, "shared": bool(running),
                                    "before": usage}
                pending_futures[pool.submit(_work, root, db_file, cfg, dict(t), tier, model, timeout, lock, log)] = t["id"]
                launched += 1
                log(f"#{t['id']} [{t['kind']}/{t['complexity']}/{t['risk']}] {t['title']} -> {tier}/{model}"
                    f" [{len(running)} running]" + (f" [{last.action}]" if last else ""))
            if pending_futures:
                cf.wait(list(pending_futures), timeout=1, return_when=cf.FIRST_COMPLETED)
                continue
            if launched == 0 and last is not None:
                log(f"STOP: {last.reason}")
                log(f"handoff written: {write_handoff(db, reason=last.reason)}")
                if last.resume_at:
                    log(f"limit resets at {last.resume_at.isoformat()}")
                log("  to continue in a fresh interactive session seeded with it: ndc session")
                if not (wait and last.resume_at):
                    return "stopped"
                secs = max(0, (last.resume_at - datetime.now(timezone.utc)).total_seconds()) + 60
                log(f"waiting {secs / 60:.0f} min for the reset")
                time.sleep(secs)
