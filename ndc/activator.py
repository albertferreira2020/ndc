"""Activates only the agents and skills of the chosen domains inside a target project."""
from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path

from . import project
from .config import catalog_root

MANIFEST = ".ndc-managed.json"
LEVELS = ("po", "senior", "junior")


def load_domains() -> dict:
    out = {}
    for f in sorted((catalog_root() / "domains").glob("*/team.json")):
        out[f.parent.name] = json.loads(f.read_text())
    return out


def _resolve(domains: dict, names: list[str], stacks: list[str]):
    agents, skills, ndc_agents = {}, [], []
    for n in ["core"] + [x for x in names if x != "core"]:
        if n not in domains:
            raise KeyError(f"unknown domain '{n}'. Available: {', '.join(sorted(domains))}")
        d = domains[n]
        ndc_agents += d.get("ndc_agents", [])
        skills += d["skills"]
        for lvl in LEVELS:
            for a in d["agents"].get(lvl, []):
                agents.setdefault(a, lvl)
        for s in stacks:
            st = d.get("stacks", {}).get(s)
            if st:
                skills += st["skills"]
                for lvl in LEVELS:
                    for a in st["agents"].get(lvl, []):
                        agents.setdefault(a, lvl)
    known = {s for d in domains.values() for s in d.get("stacks", {})}
    bad = [s for s in stacks if s not in known]
    if bad:
        raise KeyError(f"unknown stack(s): {bad}. Available: {sorted(known)}")
    return agents, list(dict.fromkeys(skills)), ndc_agents


def _set_model(text: str, model: str) -> str:
    return re.sub(r"(?m)^model:.*$", f"model: {model}", text, count=1)


def read_manifest(target: Path) -> dict:
    p = target / ".claude" / MANIFEST
    return json.loads(p.read_text()) if p.exists() else {"domains": [], "stacks": [], "agents": [], "skills": [], "gitignore": False}


def activate(names: list[str], target: Path, cfg: dict, stacks=(), add=False, gitignore=None) -> dict:
    domains = load_domains()
    prev = read_manifest(target)
    if gitignore is None:
        gitignore = prev.get("gitignore", False)
    if add:
        names = list(dict.fromkeys(prev["domains"] + names))
        stacks = list(dict.fromkeys(prev["stacks"] + list(stacks)))
    names = [n for n in names if n != "core"]
    agents, skills, ndc_agents = _resolve(domains, names, list(stacks))
    root = catalog_root()
    adir, sdir = target / ".claude" / "agents", target / ".claude" / "skills"
    adir.mkdir(parents=True, exist_ok=True)
    sdir.mkdir(parents=True, exist_ok=True)

    wanted_agents = set(agents) | set(ndc_agents)
    wanted_skills = set(skills)
    for a in set(prev["agents"]) - wanted_agents:
        (adir / f"{a}.md").unlink(missing_ok=True)
    for s in set(prev["skills"]) - wanted_skills:
        _remove(sdir / s)

    for a in wanted_agents - set(prev["agents"]):
        if (adir / f"{a}.md").exists():
            raise FileExistsError(f"{adir / (a + '.md')} exists and is not managed by NDC; refusing to overwrite")
    for a, lvl in agents.items():
        text = (root / "vendor/ecc/agents" / f"{a}.md").read_text()
        (adir / f"{a}.md").write_text(_set_model(text, cfg["models"][lvl]))
    for a in ndc_agents:
        shutil.copyfile(root / "core/agents" / f"{a}.md", adir / f"{a}.md")
    for s in skills:
        dst = sdir / s
        if dst.exists() or dst.is_symlink():
            if s not in prev["skills"]:
                raise FileExistsError(f"{dst} exists and is not managed by NDC; refusing to overwrite")
            _remove(dst)
        shutil.copytree(root / "vendor/ecc/skills" / s, dst, ignore=shutil.ignore_patterns(".DS_Store"))  # copies, not links: portable

    manifest = {"domains": names, "stacks": list(stacks), "agents": sorted(wanted_agents),
                "skills": sorted(wanted_skills), "gitignore": bool(gitignore)}
    (target / ".claude" / MANIFEST).write_text(json.dumps(manifest, indent=2))
    (target / ".ndc").mkdir(exist_ok=True)
    if prev.get("gitignore", False) != bool(gitignore):
        project.remove_block(target, prev.get("gitignore", False))  # switching mode: clean the old file
    project.write_block(target, manifest, bool(gitignore))
    return manifest


def init(target: Path, gitignore=False) -> Path | None:
    """Prepares a project without activating any team: creates .ndc/ and hides it from git."""
    (target / ".ndc").mkdir(exist_ok=True)
    return project.write_block(target, read_manifest(target) | {"gitignore": gitignore}, gitignore)


def uninstall(target: Path, purge=False) -> dict:
    """Removes only what NDC installed (per the manifest). Queue state in .ndc/ is kept unless purge."""
    m = read_manifest(target)
    adir, sdir = target / ".claude" / "agents", target / ".claude" / "skills"
    for a in m["agents"]:
        (adir / f"{a}.md").unlink(missing_ok=True)
    for sk in m["skills"]:
        _remove(sdir / sk)
    (target / ".claude" / MANIFEST).unlink(missing_ok=True)
    for d in (adir, sdir):
        if d.is_dir() and not any(d.iterdir()):
            d.rmdir()
    if purge and (target / ".ndc").exists():
        shutil.rmtree(target / ".ndc")
    if (target / ".ndc").exists():  # state kept: keep hiding it from git
        project.write_block(target, m, m.get("gitignore", False), state_only=True)
        hidden = False
    else:
        hidden = project.remove_block(target, m.get("gitignore", False))
    for d in (target / ".claude",):
        if d.is_dir() and not any(d.iterdir()):
            d.rmdir()
    return {"agents": len(m["agents"]), "skills": len(m["skills"]), "ignore_block_removed": hidden, "purged": purge}


def _remove(p: Path):
    if p.is_symlink() or p.is_file():
        p.unlink()
    elif p.exists():
        shutil.rmtree(p)
