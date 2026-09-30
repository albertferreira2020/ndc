from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import activator, classify, handoff, runner, store
from .config import load_config
from .guardian import decide
from .router import route
from .usage import UsageUnavailable, read_usage, write_usage


def _csv(s):
    return [x for x in (s or "").split(",") if x]


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="ndc", description="Nonstop Development Crew")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("domains", help="list domains and stacks")

    a = sub.add_parser("activate", help="activate domain teams in a project")
    a.add_argument("domains", nargs="+")
    a.add_argument("--target", default=".")
    a.add_argument("--stack", default="", help="comma list, e.g. python,react")
    a.add_argument("--add", action="store_true", help="keep already active domains")

    s = sub.add_parser("status", help="show what is active in a project")
    s.add_argument("--target", default=".")

    u = sub.add_parser("usage", help="show or set the remaining quota")
    u.add_argument("action", nargs="?", choices=["show", "set"], default="show")
    u.add_argument("--session", type=float)
    u.add_argument("--session-resets")
    u.add_argument("--weekly", type=float)
    u.add_argument("--weekly-resets")

    sub.add_parser("statusline", help="Claude Code statusline hook: reads JSON on stdin, mirrors rate limits")

    t = sub.add_parser("task", help="manage the queue")
    ts = t.add_subparsers(dest="tcmd", required=True)
    ta = ts.add_parser("add")
    ta.add_argument("title")
    ta.add_argument("--desc", default="")
    ta.add_argument("--complexity", choices=store.COMPLEXITIES, help="omit to let the classifier decide")
    ta.add_argument("--kind", default="work", choices=store.KINDS)
    ta.add_argument("--depends", default="")
    ta.add_argument("--verify", help="gate command; must pass (or fail, with --expect-red)")
    ta.add_argument("--expect-red", action="store_true", help="for test tasks: command must fail before implementation")
    tl = ts.add_parser("list")
    tl.add_argument("--status")

    g = sub.add_parser("guard", help="would the next ready task fit?")
    g.add_argument("--complexity", help="check a class instead of the next task")

    h = sub.add_parser("handoff", help="write the handoff report")
    h.add_argument("--notes", default="")
    h.add_argument("--reason", default="")
    sub.add_parser("resume-prompt", help="print the latest handoff as a session prompt")

    r = sub.add_parser("run", help="run the queue (dry run unless --execute)")
    r.add_argument("--execute", action="store_true", help="dispatch tasks to `claude -p`")
    r.add_argument("--ignore-usage", action="store_true", help="run even when usage is unknown")
    r.add_argument("--wait", action="store_true", help="sleep until the limit resets, then continue")
    r.add_argument("--timeout", type=int, default=3600)

    args = p.parse_args(argv)
    cfg = load_config()
    try:
        return _dispatch(args, cfg)
    except (KeyError, ValueError, FileExistsError) as e:
        print(f"error: {e.args[0] if isinstance(e, KeyError) else e}", file=sys.stderr)
        return 2


def _dispatch(args, cfg) -> int:
    if args.cmd == "domains":
        for n, d in activator.load_domains().items():
            n_ag = sum(len(v) for v in d["agents"].values())
            stacks = f"  stacks: {','.join(d['stacks'])}" if d.get("stacks") else ""
            print(f"{n:11} {n_ag:2} agents {len(d['skills']):2} skills  {d['description']}{stacks}")
        return 0
    if args.cmd == "activate":
        m = activator.activate(args.domains, Path(args.target).resolve(), cfg, _csv(args.stack), args.add)
        print(f"active: core + {', '.join(m['domains']) or '(none)'}  |  {len(m['agents'])} agents, {len(m['skills'])} skills")
        return 0
    if args.cmd == "status":
        print(json.dumps(activator.read_manifest(Path(args.target).resolve()), indent=2))
        return 0
    if args.cmd == "statusline":
        from .statusline import update
        try:
            print(update(json.loads(sys.stdin.read() or "{}"), cfg))
        except (ValueError, OSError) as e:  # a statusline must never break the session
            print(f"ndc: {e}")
        return 0
    if args.cmd == "usage":
        if args.action == "set":
            w = {}
            if args.session is not None:
                w["session"] = (args.session, args.session_resets)
            if args.weekly is not None:
                w["weekly"] = (args.weekly, args.weekly_resets)
            if not w:
                raise ValueError("pass --session and/or --weekly (percent used)")
            print(f"wrote {write_usage(cfg, w)}")
            return 0
        try:
            us = read_usage(cfg)
        except UsageUnavailable as e:
            print(f"unavailable: {e}", file=sys.stderr)
            return 1
        for w in us.windows:
            print(f"{w.name:8} {w.used_pct:5.1f}% used  {w.remaining_pct:5.1f}% left  resets {w.resets_at.isoformat() if w.resets_at else '?'}")
        return 0

    db = store.connect()
    if args.cmd == "task":
        if args.tcmd == "add":
            cx, risk, why = classify.assess(args.title, args.desc, args.complexity)
            tid = store.add_task(db, args.title, args.desc, cx, args.kind, [int(x) for x in _csv(args.depends)],
                                 risk, args.verify, args.expect_red)
            print(f"task #{tid} added: {args.kind}/{cx}/risk={risk}")
            for w in why:
                print(f"  classifier: {w}")
        else:
            for t in store.list_tasks(db, args.status):
                tier, model = route(t, cfg, lambda k, c, ti: store.success_rate(db, k, c, ti))
                print(f"#{t['id']:<3} {t['status']:8} {t['kind']:7} {t['complexity']:2} {t['risk']:4} -> {tier}/{model}  {t['title']}")
        return 0
    if args.cmd == "guard":
        complexity = args.complexity
        if not complexity:
            ready = store.ready_tasks(db)
            if not ready:
                print("IDLE: no ready tasks")
                return 0
            complexity = ready[0]["complexity"]
        try:
            us = read_usage(cfg)
        except UsageUnavailable as e:
            print(f"UNKNOWN: {e}")
            return 1
        d = decide(us, complexity, cfg, lambda w, c: store.history(db, w, c))
        print(f"{d.action}: {d.reason}" + (f" | resume at {d.resume_at.isoformat()}" if d.resume_at else ""))
        return 0 if d.action in ("GO", "WIND_DOWN") else 3
    if args.cmd == "handoff":
        print(handoff.write(db, args.notes, args.reason))
        return 0
    if args.cmd == "resume-prompt":
        latest = Path.cwd() / ".ndc" / "handoff" / "latest.md"
        if not latest.exists():
            print("no handoff yet", file=sys.stderr)
            return 1
        print("Continue the work described in this handoff.\n\n" + latest.read_text())
        return 0
    if args.cmd == "run":
        res = runner.run(db, cfg, args.execute, args.ignore_usage, args.wait, args.timeout)
        return 0 if res == "idle" else 3
    return 1
