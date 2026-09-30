import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

os.environ["NDC_STATE"] = tempfile.mkdtemp()

from ndc import activator, classify, guardian, handoff, router, runner, store
from ndc.config import load_config
from ndc.usage import UsageUnavailable, parse

CFG = load_config()
CFG["usage"]["source"] = "file"  # tests must never spawn the real `claude`
NOW = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)


def usage(session, weekly=10, age=0):
    return parse(json.dumps({
        "updated_at": (NOW - timedelta(seconds=age)).isoformat(),
        "windows": {
            "session": {"used_pct": session, "resets_at": (NOW + timedelta(hours=2)).isoformat()},
            "weekly": {"used_pct": weekly, "resets_at": (NOW + timedelta(days=3)).isoformat()},
        }}), NOW, 900)


class UsageTests(unittest.TestCase):
    def test_stale_is_rejected(self):
        with self.assertRaises(UsageUnavailable):
            usage(10, age=3600)

    def test_garbage_is_rejected(self):
        with self.assertRaises(UsageUnavailable):
            parse("{}")


class StatuslineTests(unittest.TestCase):
    def cfg(self):
        c = json.loads(json.dumps(CFG))
        c["usage"]["file"] = str(Path(tempfile.mkdtemp()) / "usage.json")
        return c

    def payload(self, five=23.5, week=41.2, reset=None):
        r = reset or int((NOW + timedelta(hours=2)).timestamp())
        return {"rate_limits": {"five_hour": {"used_percentage": five, "resets_at": r},
                                "seven_day": {"used_percentage": week, "resets_at": r + 86400}}}

    def test_mirrors_docs_payload_into_usage(self):
        from ndc import statusline
        from ndc.usage import read_usage
        c = self.cfg()
        line = statusline.update(self.payload(), c, NOW)
        self.assertIn("session 24%", line)
        u = read_usage(c, NOW)
        self.assertAlmostEqual(u.get("session").remaining_pct, 76.5)
        self.assertAlmostEqual(u.get("weekly").used_pct, 41.2)

    def test_absent_limits_do_not_clobber(self):
        from ndc import statusline
        c = self.cfg()
        statusline.update(self.payload(), c, NOW)
        self.assertEqual(statusline.update({}, c, NOW), "usage n/a")
        self.assertEqual(json.loads(Path(c["usage"]["file"]).read_text())["windows"]["session"]["used_pct"], 23.5)

    def test_missing_window_keeps_previous(self):
        from ndc import statusline
        c = self.cfg()
        statusline.update(self.payload(), c, NOW)
        p = self.payload(five=50)
        del p["rate_limits"]["seven_day"]
        statusline.update(p, c, NOW)
        w = json.loads(Path(c["usage"]["file"]).read_text())["windows"]
        self.assertEqual((w["session"]["used_pct"], w["weekly"]["used_pct"]), (50.0, 41.2))

    def test_expired_window_reads_as_reset_even_when_stale(self):
        from ndc.usage import parse
        raw = json.dumps({"updated_at": (NOW - timedelta(hours=9)).isoformat(), "windows": {
            "session": {"used_pct": 95, "resets_at": (NOW - timedelta(hours=1)).isoformat()}}})
        self.assertEqual(parse(raw, NOW, 900).get("session").used_pct, 0.0)

    def test_live_window_with_old_data_is_still_stale(self):
        with self.assertRaises(UsageUnavailable):
            usage(10, age=3600)


REAL_USAGE = """You are currently using your subscription to power your Claude Code usage

Current session: 29% used · resets Sep 30 at 5:50am (America/Sao_Paulo)
Current week (all models): 16% used · resets Oct 4 at 2am (America/Sao_Paulo)

What's contributing to your limits usage?
"""


class UsageTextTests(unittest.TestCase):
    def test_parses_real_output(self):
        from ndc.usage import parse_usage_text
        now = datetime(2026, 9, 30, 8, 0, tzinfo=timezone.utc)  # 05:00 in Sao Paulo (UTC-3)
        u = parse_usage_text(REAL_USAGE, now)
        self.assertEqual((u.get("session").used_pct, u.get("weekly").used_pct), (29.0, 16.0))
        self.assertEqual(u.get("session").resets_at, datetime(2026, 9, 30, 8, 50, tzinfo=timezone.utc))
        self.assertEqual(u.get("weekly").resets_at, datetime(2026, 10, 4, 5, 0, tzinfo=timezone.utc))

    def test_time_without_date_means_next_occurrence(self):
        from ndc.usage import parse_usage_text
        now = datetime(2026, 9, 30, 8, 0, tzinfo=timezone.utc)
        u = parse_usage_text("Current session: 5% used · resets 4:30am (America/Sao_Paulo)", now)
        self.assertEqual(u.get("session").resets_at, datetime(2026, 10, 1, 7, 30, tzinfo=timezone.utc))

    def test_unknown_format_is_unavailable(self):
        from ndc.usage import parse_usage_text
        with self.assertRaises(UsageUnavailable):
            parse_usage_text("Usage: see claude.ai/settings")

    def test_bad_timezone_is_unavailable(self):
        from ndc.usage import parse_usage_text
        with self.assertRaises(UsageUnavailable):
            parse_usage_text("Current session: 5% used · resets 4am (Mars/Base)")

    def test_year_rollover(self):
        from ndc.usage import parse_usage_text
        now = datetime(2026, 12, 31, 12, 0, tzinfo=timezone.utc)
        u = parse_usage_text("Current week (all models): 5% used · resets Jan 3 at 2am (UTC)", now)
        self.assertEqual(u.get("weekly").resets_at.year, 2027)


class GuardianTests(unittest.TestCase):
    def d(self, session, cx, hist=None, weekly=10):
        return guardian.decide(usage(session, weekly), cx, CFG, lambda w, c: (hist or {}).get((w, c), []))

    def test_plenty_left_goes(self):
        self.assertEqual(self.d(20, "M").action, "GO")

    def test_low_but_small_task_fits(self):
        self.assertEqual(self.d(72, "S").action, "WIND_DOWN")

    def test_low_and_large_task_stops_with_resume(self):
        d = self.d(85, "L")
        self.assertEqual(d.action, "STOP")
        self.assertEqual(d.resume_at, NOW + timedelta(hours=2))

    def test_history_overrides_defaults(self):
        hist = {("session", "M"): [45, 45, 45]}
        self.assertEqual(self.d(50, "M", hist).action, "STOP")

    def test_weekly_window_can_bind(self):
        d = self.d(10, "M", weekly=99.5)
        self.assertEqual(d.action, "STOP")
        self.assertEqual(d.resume_at, NOW + timedelta(days=3))


class OutlookTests(unittest.TestCase):
    def tasks(self, *cx):
        db = store.connect(":memory:")
        for c in cx:
            store.add_task(db, c, complexity=c)
        return store.list_tasks(db)

    def test_everything_fits_when_budget_is_large(self):
        o = guardian.outlook(usage(10), self.tasks("S", "M", "L"), CFG, lambda w, c: [])
        self.assertTrue(o.all_fit)

    def test_partial_coverage_starts_monitoring(self):
        o = guardian.outlook(usage(75), self.tasks("M", "M", "L"), CFG, lambda w, c: [])
        self.assertEqual((o.covered, o.total), (2, 3))
        self.assertFalse(o.all_fit)

    def test_stops_counting_at_first_task_that_does_not_fit(self):
        o = guardian.outlook(usage(85), self.tasks("L", "S"), CFG, lambda w, c: [])
        self.assertEqual(o.covered, 0)


class RouterTests(unittest.TestCase):
    def t(self, **kw):
        return {"kind": "work", "complexity": "S", "failures": 0, "risk": "low", **kw}

    def test_tiers(self):
        self.assertEqual(router.route(self.t(), CFG), ("junior", "haiku"))
        self.assertEqual(router.route(self.t(complexity="M"), CFG), ("senior", "sonnet"))
        self.assertEqual(router.route(self.t(kind="plan"), CFG), ("po", "opus"))
        self.assertEqual(router.route(self.t(complexity="XL"), CFG)[0], "po")

    def test_escalates_after_failures_and_caps(self):
        self.assertEqual(router.route(self.t(failures=1), CFG)[0], "senior")
        self.assertEqual(router.route(self.t(failures=2), CFG)[0], "po")
        self.assertEqual(router.route(self.t(failures=99), CFG)[0], "po")


class RouterPolicyTests(unittest.TestCase):
    def t(self, **kw):
        return {"kind": "work", "complexity": "S", "failures": 0, "risk": "low", **kw}

    def test_cheap_kinds_go_to_haiku(self):
        for k in ("test", "explore", "docs", "chore"):
            self.assertEqual(router.route(self.t(kind=k, complexity="M"), CFG)[1], "haiku", k)

    def test_large_tests_go_senior(self):
        self.assertEqual(router.route(self.t(kind="test", complexity="L"), CFG)[1], "sonnet")

    def test_high_risk_work_never_haiku(self):
        self.assertEqual(router.route(self.t(risk="high"), CFG)[1], "sonnet")

    def test_measured_failures_bump_class(self):
        bad = lambda k, c, t: (10, 0.4)
        good = lambda k, c, t: (10, 0.9)
        few = lambda k, c, t: (2, 0.0)
        self.assertEqual(router.route(self.t(), CFG, bad)[1], "sonnet")
        self.assertEqual(router.route(self.t(), CFG, good)[1], "haiku")
        self.assertEqual(router.route(self.t(), CFG, few)[1], "haiku")


class ClassifierTests(unittest.TestCase):
    def test_po_cannot_lower_below_floor(self):
        cx, risk, why = classify.assess("Add login with password reset", "", "S")
        self.assertEqual((cx, risk), ("M", "high"))

    def test_many_files_raise_floor(self):
        d = "touch a/b.py c/d.py e/f.py g/h.py i/j.py k/l.py"
        self.assertEqual(classify.assess("x", d, "S")[0], "L")

    def test_plain_small_task_keeps_po_value(self):
        self.assertEqual(classify.assess("fix typo in README", "", "S")[:2], ("S", "low"))

    def test_omitted_complexity_defaults_conservative(self):
        self.assertEqual(classify.assess("do the thing")[0], "M")


class GateTests(unittest.TestCase):
    def task(self, cmd, red):
        return {"verify_cmd": cmd, "expect_red": red}

    def test_green_gate(self):
        self.assertTrue(runner.verify(self.task("true", False), 5)[0])
        self.assertFalse(runner.verify(self.task("false", False), 5)[0])

    def test_red_gate_rejects_vacuous_tests(self):
        self.assertTrue(runner.verify(self.task("false", True), 5)[0])
        ok, why = runner.verify(self.task("true", True), 5)
        self.assertFalse(ok)
        self.assertIn("check nothing", why)

    def test_no_gate_passes(self):
        self.assertTrue(runner.verify(self.task(None, False), 5)[0])

    def test_expect_red_requires_verify(self):
        db = store.connect(":memory:")
        with self.assertRaises(ValueError):
            store.add_task(db, "t", kind="test", expect_red=True)

    def test_success_rate_from_history(self):
        db = store.connect(":memory:")
        a = store.add_task(db, "a", complexity="S")
        for ok in (True, False, True, True):
            store.start(db, a, "junior", "haiku")
            store.finish(db, a, ok)
        self.assertEqual(store.success_rate(db, "work", "S", "junior"), (4, 0.75))


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.db = store.connect(":memory:")

    def test_dependencies_gate_readiness(self):
        a = store.add_task(self.db, "a")
        b = store.add_task(self.db, "b", depends_on=[a])
        self.assertEqual([t["id"] for t in store.ready_tasks(self.db)], [a])
        store.start(self.db, a, "senior", "sonnet")
        store.finish(self.db, a, True)
        self.assertEqual([t["id"] for t in store.ready_tasks(self.db)], [b])

    def test_unknown_dependency_rejected(self):
        with self.assertRaises(ValueError):
            store.add_task(self.db, "x", depends_on=[42])

    def test_failure_requeues_and_counts(self):
        a = store.add_task(self.db, "a")
        store.start(self.db, a, "junior", "haiku")
        store.finish(self.db, a, False)
        t = store.get(self.db, a)
        self.assertEqual((t["status"], t["failures"]), ("pending", 1))

    def test_history_only_counts_successes(self):
        a = store.add_task(self.db, "a", complexity="M")
        store.start(self.db, a, "senior", "sonnet")
        store.finish(self.db, a, True, {"session": 5.0})
        store.start(self.db, a, "senior", "sonnet")
        store.finish(self.db, a, False, {"session": 50.0})
        self.assertEqual(store.history(self.db, "session", "M"), [5.0])

    def test_interrupted_is_requeued(self):
        a = store.add_task(self.db, "a")
        store.start(self.db, a, "senior", "sonnet")
        store.requeue_interrupted(self.db)
        self.assertEqual(store.get(self.db, a)["status"], "pending")


class RunnerTests(unittest.TestCase):
    def test_picks_smaller_task_when_big_one_does_not_fit(self):
        db = store.connect(":memory:")
        store.add_task(db, "big", complexity="L")
        small = store.add_task(db, "small", complexity="S")
        task, d = runner.pick(db, usage(85), CFG)
        self.assertEqual(task["id"], small)

    def test_dry_run_does_not_mutate_queue(self):
        db = store.connect(":memory:")
        store.add_task(db, "a")
        os.environ["NDC_STATE"] = tempfile.mkdtemp()
        cfg = json.loads(json.dumps(CFG))
        p = Path(tempfile.mkdtemp()) / "u.json"
        p.write_text(json.dumps({"updated_at": datetime.now(timezone.utc).isoformat(),
                                 "windows": {"session": {"used_pct": 5, "resets_at": None}}}))
        cfg["usage"]["file"] = str(p)
        out = []
        self.assertEqual(runner.run(db, cfg, execute=False, log=out.append), "idle")
        self.assertEqual(store.get(db, 1)["status"], "pending")

    def test_unknown_usage_refuses(self):
        db = store.connect(":memory:")
        store.add_task(db, "a")
        cfg = json.loads(json.dumps(CFG))
        cfg["usage"]["file"] = "/nonexistent/usage.json"
        self.assertEqual(runner.run(db, cfg, log=lambda m: None), "unknown-usage")


class DeltaAndMonitoringTests(unittest.TestCase):
    def test_zero_delta_is_recorded_as_half_a_point(self):
        self.assertEqual(runner._deltas(usage(30), usage(30)), {"session": 0.5, "weekly": 0.5})
        self.assertEqual(runner._deltas(usage(30), usage(33))["session"], 3)

    def test_reset_between_readings_gives_no_sample(self):
        self.assertEqual(runner._deltas(usage(80), usage(2)).get("session"), None)

    def test_monitoring_turns_on_and_checkpoints(self):
        db = store.connect(":memory:")
        for cx in ("M", "M", "L"):
            store.add_task(db, cx, complexity=cx)
        cfg = json.loads(json.dumps(CFG))
        p = Path(tempfile.mkdtemp()) / "u.json"
        p.write_text(json.dumps({"updated_at": datetime.now(timezone.utc).isoformat(), "windows": {
            "session": {"used_pct": 75, "resets_at": None}}}))
        cfg["usage"]["file"] = str(p)
        out = []
        runner.run(db, cfg, execute=False, log=out.append)
        self.assertTrue(any("MONITORING ON: budget covers 2 of 3" in l for l in out), out)
        self.assertTrue(any("checkpoint written" in l for l in out))


class OutlookCoversBlockedTasksTests(unittest.TestCase):
    def test_monitoring_counts_tasks_waiting_on_dependencies(self):
        db = store.connect(":memory:")
        prev = None
        for cx in ("M", "M", "L"):  # a chain: only the first is ready
            prev = store.add_task(db, cx, complexity=cx, depends_on=[prev] if prev else [])
        cfg = json.loads(json.dumps(CFG))
        p = Path(tempfile.mkdtemp()) / "u.json"
        p.write_text(json.dumps({"updated_at": datetime.now(timezone.utc).isoformat(), "windows": {
            "session": {"used_pct": 75, "resets_at": None}}}))
        cfg["usage"]["file"] = str(p)
        out = []
        runner.run(db, cfg, execute=False, log=out.append)
        self.assertTrue(any("covers 2 of 3" in l for l in out), out)


class HandoffTests(unittest.TestCase):
    def test_report_sections(self):
        db = store.connect(":memory:")
        a = store.add_task(db, "done thing")
        store.start(db, a, "senior", "sonnet")
        store.finish(db, a, True)
        store.add_task(db, "next thing", depends_on=[a])
        text = handoff.build(db, notes="use pytest -x", reason="limit")
        for s in ("## Done", "done thing", "## Pending", "next thing", "use pytest -x", "Stopped because: limit"):
            self.assertIn(s, text)


class ActivatorTests(unittest.TestCase):
    def test_every_catalog_reference_exists(self):
        root = Path(__file__).resolve().parent.parent
        for name, d in activator.load_domains().items():
            groups = [d] + list(d.get("stacks", {}).values())
            for g in groups:
                for lvl in g["agents"].values():
                    for a in lvl:
                        self.assertTrue((root / "vendor/ecc/agents" / f"{a}.md").exists(), a)
                for s in g["skills"]:
                    self.assertTrue((root / "vendor/ecc/skills" / s / "SKILL.md").exists(), s)
            for a in d.get("ndc_agents", []):
                self.assertTrue((root / "core/agents" / f"{a}.md").exists(), a)

    def test_activate_switch_and_model_override(self):
        t = Path(tempfile.mkdtemp())
        activator.activate(["software"], t, CFG, ["python"])
        ag = t / ".claude/agents"
        self.assertTrue((ag / "po.md").exists() and (ag / "python-reviewer.md").exists())
        self.assertIn("model: haiku", (ag / "doc-updater.md").read_text())
        self.assertIn("model: opus", (ag / "planner.md").read_text())
        self.assertFalse((ag / "marketing-agent.md").exists())
        activator.activate(["marketing"], t, CFG)
        self.assertTrue((ag / "marketing-agent.md").exists())
        self.assertFalse((ag / "planner.md").exists())
        self.assertFalse((ag / "python-reviewer.md").exists())
        self.assertTrue((ag / "po.md").exists())  # core stays

    def test_add_keeps_previous(self):
        t = Path(tempfile.mkdtemp())
        activator.activate(["software"], t, CFG)
        activator.activate(["marketing"], t, CFG, add=True)
        self.assertTrue((t / ".claude/agents/planner.md").exists())
        self.assertTrue((t / ".claude/agents/marketing-agent.md").exists())

    def test_refuses_to_overwrite_foreign_agent(self):
        t = Path(tempfile.mkdtemp())
        (t / ".claude/agents").mkdir(parents=True)
        (t / ".claude/agents/planner.md").write_text("mine")
        with self.assertRaises(FileExistsError):
            activator.activate(["software"], t, CFG)
        self.assertEqual((t / ".claude/agents/planner.md").read_text(), "mine")

    def test_unknown_domain_and_stack(self):
        t = Path(tempfile.mkdtemp())
        with self.assertRaises(KeyError):
            activator.activate(["nope"], t, CFG)
        with self.assertRaises(KeyError):
            activator.activate(["software"], t, CFG, ["cobol"])


if __name__ == "__main__":
    unittest.main()
