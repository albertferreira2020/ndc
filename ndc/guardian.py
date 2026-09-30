"""Decides whether the next task fits in the remaining quota."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .usage import Usage

# Percentage points of a window that one task of each class is ASSUMED to cost
# until real history exists. These are starting guesses, not measurements.
DEFAULTS = {
    "session": {"S": 1.5, "M": 4.0, "L": 9.0, "XL": 20.0},
    "weekly": {"S": 0.3, "M": 0.8, "L": 1.8, "XL": 4.0},
}
MIN_SAMPLES = 3


@dataclass
class Decision:
    action: str  # GO | WIND_DOWN | STOP | UNKNOWN
    reason: str
    resume_at: datetime | None = None


def _p80(xs: list[float]) -> float:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(len(xs) * 0.8))]


def estimate(window: str, complexity: str, samples: list[float]) -> tuple[float, str]:
    if len(samples) >= MIN_SAMPLES:
        return _p80(samples), f"p80 of {len(samples)} runs"
    table = DEFAULTS.get(window, DEFAULTS["session"])
    return table[complexity], "default (no history yet)"


def decide(usage: Usage, complexity: str, cfg: dict, history_fn) -> Decision:
    g = cfg["guardian"]
    floor, winddown, safety = g["hard_floor_remaining_pct"], g["winddown_remaining_pct"], g["safety_factor"]
    stops, winding, notes = [], False, []
    for w in usage.windows:
        est, basis = estimate(w.name, complexity, history_fn(w.name, complexity))
        need = est * safety
        left = w.remaining_pct
        notes.append(f"{w.name}: {left:.0f}% left, need ~{need:.1f}% ({basis})")
        if left - need < floor:
            stops.append(w)
        elif left <= winddown:
            winding = True
    if stops:
        resets = [w.resets_at for w in stops if w.resets_at]
        resume = max(resets) if resets else None
        return Decision("STOP", "does not fit: " + "; ".join(notes), resume)
    if winding:
        return Decision("WIND_DOWN", "fits, budget is low: " + "; ".join(notes))
    return Decision("GO", "; ".join(notes))


@dataclass
class Outlook:
    covered: int  # how many pending tasks (queue order) fit in the remaining budget
    total: int
    detail: str

    @property
    def all_fit(self) -> bool:
        return self.covered >= self.total


def outlook(usage: Usage, tasks, cfg: dict, history_fn) -> Outlook:
    """Does the remaining budget cover the WHOLE pending queue? Greedy in queue order, all windows."""
    g = cfg["guardian"]
    floor, safety = g["hard_floor_remaining_pct"], g["safety_factor"]
    spent = {w.name: 0.0 for w in usage.windows}
    covered, tasks = 0, list(tasks)
    for t in tasks:
        need = {w.name: estimate(w.name, t["complexity"], history_fn(w.name, t["complexity"]))[0] * safety
                for w in usage.windows}
        if any(w.remaining_pct - spent[w.name] - need[w.name] < floor for w in usage.windows):
            break
        for n in spent:
            spent[n] += need[n]
        covered += 1
    detail = "; ".join(f"{w.name}: {w.remaining_pct:.0f}% left, queue needs ~{spent[w.name]:.1f}% for {covered}" for w in usage.windows)
    return Outlook(covered, len(tasks), detail)
