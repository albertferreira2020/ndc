from __future__ import annotations

import json
import os
from pathlib import Path

PKG_ROOT = Path(__file__).resolve().parent.parent


def catalog_root() -> Path:
    return Path(os.environ.get("NDC_ROOT", PKG_ROOT))


def state_dir() -> Path:
    """Per-project runtime state. Lives in the working directory, not in the catalog."""
    d = Path(os.environ.get("NDC_STATE", Path.cwd() / ".ndc")) / "state"
    d.mkdir(parents=True, exist_ok=True)
    return d


def load_config() -> dict:
    cfg = json.loads((catalog_root() / "ndc.config.json").read_text())
    local = Path.cwd() / "ndc.config.json"
    if local.exists() and local.resolve() != (catalog_root() / "ndc.config.json").resolve():
        cfg = _merge(cfg, json.loads(local.read_text()))
    return cfg


def _merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out
