"""Claude Code statusline hook: mirrors the subscription rate limits into the usage file.

Claude Code pipes session JSON to the statusline command on stdin. `rate_limits.five_hour` and
`rate_limits.seven_day` (Pro/Max only, after the first API response) carry `used_percentage` and
`resets_at` (Unix seconds). No credentials are involved.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone

from .usage import usage_path

NAMES = {"five_hour": "session", "seven_day": "weekly"}


def update(payload: dict, cfg: dict, now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    limits = payload.get("rate_limits") or {}
    path = usage_path(cfg)
    try:
        old = json.loads(path.read_text()).get("windows", {})
    except (OSError, ValueError, AttributeError):
        old = {}
    windows = {}
    for key, name in NAMES.items():
        w = limits.get(key)
        if isinstance(w, dict) and w.get("used_percentage") is not None:
            resets = w.get("resets_at")
            windows[name] = {"used_pct": float(w["used_percentage"]),
                             "resets_at": datetime.fromtimestamp(resets, timezone.utc).isoformat() if resets else None}
        elif name in old:  # a window may be absent from one payload; keep the last value until it expires
            windows[name] = old[name]
    if not limits or not windows:
        return "usage n/a"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"updated_at": now.isoformat(), "windows": windows}, indent=2))
    os.replace(tmp, path)  # atomic: the runner never reads a half-written file
    return " ".join(f"{n} {w['used_pct']:.0f}%" for n, w in windows.items())
