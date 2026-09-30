"""Reads the remaining quota from a bridge, never from credentials.

Expected JSON (written by a bridge to Claude Usage, a statusline script, or `ndc usage set`):

    {"updated_at": "2026-09-30T12:00:00+00:00",
     "windows": {"session": {"used_pct": 62, "resets_at": "2026-09-30T15:00:00+00:00"},
                 "weekly":  {"used_pct": 40, "resets_at": "2026-10-04T09:00:00+00:00"}}}
"""
from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import state_dir


class UsageUnavailable(Exception):
    pass


@dataclass
class Window:
    name: str
    used_pct: float
    resets_at: datetime | None

    @property
    def remaining_pct(self) -> float:
        return max(0.0, 100.0 - self.used_pct)


@dataclass
class Usage:
    windows: list[Window]
    updated_at: datetime

    def get(self, name: str) -> Window | None:
        return next((w for w in self.windows if w.name == name), None)


def _dt(s):
    if not s:
        return None
    d = datetime.fromisoformat(s)
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def parse(raw: str, now: datetime | None = None, max_age: float | None = None) -> Usage:
    try:
        data = json.loads(raw)
        updated = _dt(data["updated_at"])
        windows = [Window(n, float(w["used_pct"]), _dt(w.get("resets_at"))) for n, w in data["windows"].items()]
    except (ValueError, KeyError, TypeError, AttributeError) as e:
        raise UsageUnavailable(f"invalid usage payload: {e}") from e
    if not windows:
        raise UsageUnavailable("usage payload has no windows")
    now = now or datetime.now(timezone.utc)
    # A window whose reset time has passed is back to 0%, however old the data is.
    live = []
    for w in windows:
        if w.resets_at and w.resets_at <= now:
            w.used_pct = 0.0
        else:
            live.append(w)
    if live and max_age is not None and (now - updated).total_seconds() > max_age:
        raise UsageUnavailable(f"usage data is stale (updated {updated.isoformat()})")
    return Usage(windows, updated)


def usage_path(cfg: dict) -> Path:
    p = Path(cfg["usage"]["file"]).expanduser()
    return p if p.is_absolute() else Path.cwd() / p


LINE_RE = re.compile(
    r"Current (session|week \(all models\)):\s*([\d.]+)% used(?:\s*·\s*resets\s+(.+?)\s*\(([\w/+-]+)\))?", re.I)
MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def _reset_time(text: str, tz: str, now: datetime) -> datetime | None:
    """'Sep 30 at 5:50am' or '5:50am' (next occurrence) in the given timezone."""
    zone = ZoneInfo(tz)
    local = now.astimezone(zone)
    m = re.search(r"(?:([A-Za-z]{3})[a-z]*\s+(\d{1,2})\s+at\s+)?(\d{1,2})(?::(\d{2}))?\s*([ap]m)", text, re.I)
    if not m:
        return None
    mon, day, hh, mm, ap = m.groups()
    hour = int(hh) % 12 + (12 if ap.lower() == "pm" else 0)
    minute = int(mm or 0)
    if mon:
        if mon.lower() not in MONTHS:
            return None
        t = datetime(local.year, MONTHS[mon.lower()], int(day), hour, minute, tzinfo=zone)
        if t < local - timedelta(days=2):
            t = t.replace(year=local.year + 1)
    else:
        t = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if t <= local:
            t += timedelta(days=1)
    return t.astimezone(timezone.utc)


def parse_usage_text(text: str, now: datetime | None = None) -> Usage:
    """Parses the output of `claude -p "/usage"`. Strict: unknown output is UsageUnavailable."""
    now = now or datetime.now(timezone.utc)
    windows = []
    for m in LINE_RE.finditer(text):
        kind, pct, when, tz = m.groups()
        try:
            resets = _reset_time(when, tz, now) if when else None
        except (KeyError, ValueError) as e:
            raise UsageUnavailable(f"cannot read reset time '{when} ({tz})': {e}") from e
        windows.append(Window("session" if kind.lower() == "session" else "weekly", float(pct), resets))
    if not windows:
        raise UsageUnavailable("no 'Current session' line in /usage output (format changed?)")
    return Usage(windows, now)


def _from_claude(u: dict, now: datetime | None) -> Usage:
    try:
        r = subprocess.run(u.get("command") or ["claude", "-p", "/usage"], capture_output=True, text=True,
                           timeout=u.get("timeout_seconds", 90), cwd=Path.home(),
                           shell=isinstance(u.get("command"), str))
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        raise UsageUnavailable(f"could not run /usage: {e}") from e
    if r.returncode != 0:
        raise UsageUnavailable(f"/usage failed: {(r.stderr or r.stdout).strip()[:200]}")
    return parse_usage_text(r.stdout, now)


def read_usage(cfg: dict, now: datetime | None = None, refresh: bool = False) -> Usage:
    """source 'claude' asks `/usage` (cached briefly in the usage file unless refresh=True)."""
    u = cfg["usage"]
    max_age = u.get("max_age_seconds")
    if u["source"] == "claude":
        p = usage_path(cfg)
        if not refresh and p.exists():
            try:
                return parse(p.read_text(), now, u.get("cache_seconds", 120))
            except UsageUnavailable:
                pass
        us = _from_claude(u, now)
        _write(p, us)
        return us
    if u["source"] == "command":
        if not u.get("command"):
            raise UsageUnavailable("usage.source is 'command' but usage.command is empty")
        try:
            r = subprocess.run(u["command"], shell=True, capture_output=True, text=True, timeout=30)
        except subprocess.TimeoutExpired as e:
            raise UsageUnavailable("usage command timed out") from e
        if r.returncode != 0:
            raise UsageUnavailable(f"usage command failed: {r.stderr.strip()[:200]}")
        return parse(r.stdout, now, max_age)
    p = usage_path(cfg)
    if not p.exists():
        raise UsageUnavailable(f"no usage file at {p}")
    return parse(p.read_text(), now, max_age)


def write_usage(cfg: dict, windows: dict[str, tuple[float, str | None]], now: datetime | None = None) -> Path:
    now = now or datetime.now(timezone.utc)
    payload = {
        "updated_at": now.isoformat(),
        "windows": {n: {"used_pct": used, "resets_at": resets} for n, (used, resets) in windows.items()},
    }
    p = usage_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload, indent=2))
    return p


def _write(p: Path, us: Usage) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps({"updated_at": us.updated_at.isoformat(), "windows": {
        w.name: {"used_pct": w.used_pct, "resets_at": w.resets_at.isoformat() if w.resets_at else None}
        for w in us.windows}}, indent=2))
    tmp.replace(p)
