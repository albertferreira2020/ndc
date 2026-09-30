"""Maps a task to a tier and model.

Cheap tiers get work that is read-only, mechanically checkable, or low-risk:
  junior (haiku): explore, docs, chore, test (S/M), and S work that is low risk
  senior (sonnet): M/L work, high-risk work, L+ tests/chores
  po (opus): planning and XL
Measured failures move work up: per-class success rate, then per-task failure count.
"""
from __future__ import annotations

TIER_ORDER = ["junior", "senior", "po"]
WORK_TIER = {"S": "junior", "M": "senior", "L": "senior", "XL": "po"}
CHEAP_KINDS = ("test", "explore", "docs", "chore")


def base_tier(task) -> str:
    if task["kind"] == "plan":
        return "po"
    if task["kind"] in CHEAP_KINDS:
        return "junior" if task["complexity"] in ("S", "M") else "senior"
    tier = WORK_TIER[task["complexity"]]
    if task["risk"] == "high" and tier == "junior":
        tier = "senior"
    return tier


def route(task, cfg: dict, success_fn=None) -> tuple[str, str]:
    tier = base_tier(task)
    r = cfg.get("router", {})
    if success_fn and tier == "junior":
        n, rate = success_fn(task["kind"], task["complexity"], "junior")
        if n >= r.get("min_samples", 5) and rate < r.get("min_success_rate", 0.7):
            tier = "senior"  # haiku is measurably failing this class
    bumps = task["failures"] // max(1, cfg["escalate_after_failures"])
    tier = TIER_ORDER[min(len(TIER_ORDER) - 1, TIER_ORDER.index(tier) + bumps)]
    return tier, cfg["models"][tier]
