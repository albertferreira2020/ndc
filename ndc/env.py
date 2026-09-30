"""Environment for the `claude` processes NDC starts itself (task attempts, PO, reviewer)."""
from __future__ import annotations

import os


def claude_env(cfg: dict) -> dict:
    """Never let a hook spend tokens behind NDC's back, and skip session-memory hooks in one-task sessions:
    they inject context into every session, and the runner opens one session per task. Safety hooks stay on."""
    env = dict(os.environ)
    env["ECC_SKIP_LLM_SUMMARY"] = "1"
    disabled = {x.strip() for x in env.get("ECC_DISABLED_HOOKS", "").split(",") if x.strip()}
    disabled |= set(cfg.get("hooks", {}).get("runner_disabled", []))
    if disabled:
        env["ECC_DISABLED_HOOKS"] = ",".join(sorted(disabled))
    return env
