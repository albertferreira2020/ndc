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
    agents, skills, ndc_agents, rules = {}, [], [], []
    for n in ["core"] + [x for x in names if x != "core"]:
        if n not in domains:
            raise KeyError(f"unknown domain '{n}'. Available: {', '.join(sorted(domains))}")
        d = domains[n]
        ndc_agents += d.get("ndc_agents", [])
        skills += d["skills"]
        rules += d.get("rules", [])
        for lvl in LEVELS:
            for a in d["agents"].get(lvl, []):
                agents.setdefault(a, lvl)
        for s in stacks:
            st = d.get("stacks", {}).get(s)
            if st:
                skills += st["skills"]
                rules += st.get("rules", [])
                for lvl in LEVELS:
                    for a in st["agents"].get(lvl, []):
                        agents.setdefault(a, lvl)
    known = {s for d in domains.values() for s in d.get("stacks", {})}
    bad = [s for s in stacks if s not in known]
    if bad:
        raise KeyError(f"unknown stack(s): {bad}. Available: {sorted(known)}")
    return agents, list(dict.fromkeys(skills)), ndc_agents, list(dict.fromkeys(rules))


def _set_model(text: str, model: str) -> str:
    return re.sub(r"(?m)^model:.*$", f"model: {model}", text, count=1)


def read_manifest(target: Path) -> dict:
    p = target / ".claude" / MANIFEST
    base = {"domains": [], "stacks": [], "agents": [], "skills": [], "rules": [], "no_rules": False, "hooks": None,
            "gitignore": False}
    return base | json.loads(p.read_text()) if p.exists() else base


def write_manifest(target: Path, m: dict) -> None:
    (target / ".claude").mkdir(parents=True, exist_ok=True)
    (target / ".claude" / MANIFEST).write_text(json.dumps(m, indent=2))


def rules_footprint(groups, root: Path | None = None) -> dict:
    """What the rules cost in context: files without `paths:` frontmatter load every session, the rest only when
    a matching file is opened."""
    root = (root or catalog_root()) / "vendor/ecc/rules"
    files = always = lazy = 0
    for g in groups:
        for f in sorted((root / g).glob("*.md")):
            text = f.read_text()
            files += 1
            if re.match(r"---\s*\n(?:.*\n)*?paths:", text):
                lazy += len(text.encode())
            else:
                always += len(text.encode())
    return {"files": files, "always_bytes": always, "lazy_bytes": lazy}


def activate(names: list[str], target: Path, cfg: dict, stacks=(), add=False, gitignore=None, rules=None) -> dict:
    domains = load_domains()
    prev = read_manifest(target)
    if gitignore is None:
        gitignore = prev.get("gitignore", False)
    rules_on = (not prev.get("no_rules", False)) if rules is None else bool(rules)
    if add:
        names = list(dict.fromkeys(prev["domains"] + names))
        stacks = list(dict.fromkeys(prev["stacks"] + list(stacks)))
    names = [n for n in names if n != "core"]
    agents, skills, ndc_agents, rule_groups = _resolve(domains, names, list(stacks))
    root = catalog_root()
    claude = target / ".claude"
    adir, sdir, rdir = claude / "agents", claude / "skills", claude / "rules" / "ndc"

    wanted_agents = set(agents) | set(ndc_agents)
    wanted_skills = set(skills)
    wanted_rules = set(rule_groups) if rules_on else set()

    # Check every conflict with the user's own files BEFORE changing anything: no half-applied state.
    for a in wanted_agents - set(prev["agents"]):
        if (adir / f"{a}.md").exists():
            raise FileExistsError(f"{adir / (a + '.md')} exists and is not managed by NDC; refusing to overwrite")
    for sk in wanted_skills - set(prev["skills"]):
        if (sdir / sk).exists() or (sdir / sk).is_symlink():
            raise FileExistsError(f"{sdir / sk} exists and is not managed by NDC; refusing to overwrite")
    for g in wanted_rules - set(prev["rules"]):
        if (rdir / g).exists():
            raise FileExistsError(f"{rdir / g} exists and is not managed by NDC; refusing to overwrite")

    adir.mkdir(parents=True, exist_ok=True)
    sdir.mkdir(parents=True, exist_ok=True)
    for a in set(prev["agents"]) - wanted_agents:
        (adir / f"{a}.md").unlink(missing_ok=True)
    for sk in set(prev["skills"]) - wanted_skills:
        _remove(sdir / sk)
    for g in set(prev["rules"]) - wanted_rules:
        _remove(rdir / g)

    for a, lvl in agents.items():
        text = (root / "vendor/ecc/agents" / f"{a}.md").read_text()
        (adir / f"{a}.md").write_text(_set_model(text, cfg["models"][lvl]))
    for a in ndc_agents:
        shutil.copyfile(root / "core/agents" / f"{a}.md", adir / f"{a}.md")
    for sk in skills:
        dst = sdir / sk
        if dst.exists() or dst.is_symlink():
            _remove(dst)
        shutil.copytree(root / "vendor/ecc/skills" / sk, dst, ignore=shutil.ignore_patterns(".DS_Store"))  # copies, not links: portable
    for g in sorted(wanted_rules):
        dst = rdir / g
        if dst.exists():
            _remove(dst)
        dst.mkdir(parents=True)
        for f in sorted((root / "vendor/ecc/rules" / g).glob("*.md")):
            shutil.copyfile(f, dst / f.name)
    _rmdir_if_empty(rdir, rdir.parent)

    manifest = {"domains": names, "stacks": list(stacks), "agents": sorted(wanted_agents),
                "skills": sorted(wanted_skills), "rules": sorted(wanted_rules), "no_rules": not rules_on,
                "hooks": prev.get("hooks"), "gitignore": bool(gitignore)}  # hooks are managed by `ndc hooks`
    write_manifest(target, manifest)
    (target / ".ndc").mkdir(exist_ok=True)
    if prev.get("gitignore", False) != bool(gitignore):
        project.remove_block(target, prev.get("gitignore", False))  # switching mode: clean the old file
    project.write_block(target, manifest, bool(gitignore))
    return manifest


def _rmdir_if_empty(*dirs: Path) -> None:
    for d in dirs:
        if d.is_dir() and not any(d.iterdir()):
            d.rmdir()


def init(target: Path, gitignore=False) -> Path | None:
    """Prepares a project without activating any team: creates .ndc/ and hides it from git."""
    (target / ".ndc").mkdir(exist_ok=True)
    return project.write_block(target, read_manifest(target) | {"gitignore": gitignore}, gitignore)


def uninstall(target: Path, purge=False) -> dict:
    """Removes only what NDC installed (per the manifest). Queue state in .ndc/ is kept unless purge."""
    m = read_manifest(target)
    if m.get("hooks"):
        from . import hooks  # local import: hooks depends on this module
        hooks.disable(target)
        m = read_manifest(target)
    adir, sdir = target / ".claude" / "agents", target / ".claude" / "skills"
    for a in m["agents"]:
        (adir / f"{a}.md").unlink(missing_ok=True)
    for sk in m["skills"]:
        _remove(sdir / sk)
    rdir = target / ".claude" / "rules" / "ndc"
    for g in m["rules"]:
        _remove(rdir / g)
    (target / ".claude" / MANIFEST).unlink(missing_ok=True)
    _rmdir_if_empty(adir, sdir, rdir, rdir.parent)
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
    return {"agents": len(m["agents"]), "skills": len(m["skills"]), "rules": len(m["rules"]),
            "ignore_block_removed": hidden, "purged": purge}


def _remove(p: Path):
    if p.is_symlink() or p.is_file():
        p.unlink()
    elif p.exists():
        shutil.rmtree(p)
