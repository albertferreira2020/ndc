"""Opt-in runtime hooks copied from ECC: safety hooks and session memory.

Hooks run code on every tool call, so they are never installed implicitly. `ndc hooks enable` copies the audited
runtime into `.ndc/runtime/` and registers only the hooks of the chosen profile in `.claude/settings.local.json`
(local to the machine, hidden from git). Entries NDC owns carry a marker; everything else in the file is preserved.
"""
from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
from pathlib import Path

from . import activator, project
from .config import catalog_root

PROFILES = ("minimal", "standard", "strict")
MARK = "NDC_HOOK=1"  # ownership marker inside every command we register
SETTINGS = ".claude/settings.local.json"


def registry() -> list[dict]:
    return json.loads((catalog_root() / "runtime" / "hooks.json").read_text())["hooks"]


def selected(profile: str) -> list[dict]:
    return [h for h in registry() if profile in h["profiles"].split(",")]


def _q(x: str) -> str:
    return shlex.quote(x) if os.name != "nt" else subprocess.list2cmdline([x])


def _command(target: Path, h: dict, profile: str) -> str:
    rt, data = target / ".ndc" / "runtime", target / ".ndc" / "agent-data"
    args = [MARK, profile, str(data), str(rt), h["id"], h["script"], h["profiles"]]
    return f"node {_q(str(rt / 'scripts/ndc-run.js'))} " + " ".join(_q(x) for x in args)


def _owned(hook: dict) -> bool:
    return isinstance(hook, dict) and MARK in str(hook.get("command", ""))


def _load(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except ValueError as e:
        raise ValueError(f"{path} is not valid JSON ({e}); NDC will not touch it. Fix it and retry.") from e
    if not isinstance(data, dict):
        raise ValueError(f"{path} must hold a JSON object")
    return data


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    os.replace(tmp, path)


def _strip_owned(data: dict) -> dict:
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        return data
    for event in list(hooks):
        groups = []
        for g in hooks[event] if isinstance(hooks[event], list) else []:
            inner = [h for h in g.get("hooks", []) if not _owned(h)] if isinstance(g, dict) else None
            if inner is None:
                groups.append(g)
            elif inner:
                groups.append({**g, "hooks": inner})
        if groups:
            hooks[event] = groups
        else:
            del hooks[event]
    if not hooks:
        del data["hooks"]
    return data


def _save_manifest(target: Path, m: dict, hooks_state) -> dict:
    m = dict(m, hooks=hooks_state)
    activator.write_manifest(target, m)
    project.write_block(target, m, m.get("gitignore", False))
    return m


def enable(target: Path, profile: str = "standard") -> dict:
    if profile not in PROFILES:
        raise ValueError(f"profile must be one of {PROFILES}")
    if not shutil.which("node"):
        raise ValueError("Node.js is required for the hooks (they are ECC's Node scripts) and `node` is not on PATH")
    settings = target / SETTINGS
    if project.is_tracked(target, SETTINGS):
        raise ValueError(f"git tracks {SETTINGS}; NDC will not modify a committed file. Run `git rm --cached "
                         f"{SETTINGS}` first (it is meant to be personal)")
    data = _load(settings)
    m = activator.read_manifest(target)
    created = (m.get("hooks") or {}).get("created_settings", not settings.exists())

    rt = target / ".ndc" / "runtime"
    shutil.rmtree(rt, ignore_errors=True)
    shutil.copytree(catalog_root() / "runtime", rt, ignore=shutil.ignore_patterns(".DS_Store"))

    data = _strip_owned(data)
    hooks = data.setdefault("hooks", {})
    chosen = selected(profile)
    for h in chosen:
        entry = {"type": "command", "command": _command(target, h, profile)}
        if h.get("timeout"):
            entry["timeout"] = h["timeout"]
        if h.get("async"):
            entry["async"] = True
        hooks.setdefault(h["event"], []).append({"matcher": h["matcher"], "hooks": [entry]})
    _write(settings, data)
    state = {"profile": profile, "ids": [h["id"] for h in chosen], "created_settings": created}
    _save_manifest(target, m, state)
    return state


def disable(target: Path) -> dict:
    m = activator.read_manifest(target)
    state = m.get("hooks")
    settings = target / SETTINGS
    removed = 0
    if settings.exists():
        data = _load(settings)
        before = json.dumps(data)
        data = _strip_owned(data)
        removed = len(state["ids"]) if state else 0
        if not data and (state or {}).get("created_settings"):
            settings.unlink()  # we created it and nothing else lives in it
        elif json.dumps(data) != before:
            _write(settings, data)
    shutil.rmtree(target / ".ndc" / "runtime", ignore_errors=True)
    if state:
        _save_manifest(target, m, None)
    return {"removed": removed, "memory_kept": (target / ".ndc" / "agent-data").exists()}


def status(target: Path) -> dict:
    m = activator.read_manifest(target)
    state = m.get("hooks")
    out = {"enabled": bool(state), "node": bool(shutil.which("node")), "profile": None, "ids": [], "problems": []}
    if not state:
        return out
    out.update(profile=state["profile"], ids=state["ids"])
    rt = target / ".ndc" / "runtime"
    if not (rt / "scripts/hooks/run-with-flags.js").exists():
        out["problems"].append("the runtime folder is missing (the project was moved or .ndc was deleted): run `ndc hooks enable` again")
    if not out["node"]:
        out["problems"].append("node is not on PATH: every hook will fail")
    try:
        found = {h["command"] for g in (_load(target / SETTINGS).get("hooks", {}) or {}).values() for grp in g
                 for h in grp.get("hooks", []) if _owned(h)}
    except ValueError as e:
        out["problems"].append(str(e))
        found = set()
    if len(found) != len(state["ids"]):
        out["problems"].append(f"{SETTINGS} holds {len(found)} NDC hook(s), expected {len(state['ids'])}: run `ndc hooks enable` again")
    stale = [c for c in found if str(rt) not in c]
    if stale:
        out["problems"].append("hook commands point to another folder (the project was moved): run `ndc hooks enable` again")
    return out
