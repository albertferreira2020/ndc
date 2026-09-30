"""Keeps NDC's footprint out of the user's repository.

By default the ignore rules go to `.git/info/exclude` (local, never committed, invisible to the team).
`--gitignore` writes the same block to the project's `.gitignore` instead.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

START = "# >>> ndc (managed by ndc, do not edit) >>>"
END = "# <<< ndc <<<"


def patterns(manifest: dict, state_only: bool = False) -> list[str]:
    if state_only:
        return ["/.ndc/"]
    out = ["/.ndc/", "/.claude/.ndc-managed.json"]
    out += [f"/.claude/agents/{a}.md" for a in manifest.get("agents", [])]
    out += [f"/.claude/skills/{s}/" for s in manifest.get("skills", [])]
    if manifest.get("rules"):
        out.append("/.claude/rules/ndc/")
    if manifest.get("hooks"):
        out.append("/.claude/settings.local.json")
    return out


def ignore_file(target: Path, use_gitignore: bool) -> Path | None:
    if use_gitignore:
        return target / ".gitignore"
    r = subprocess.run(["git", "-C", str(target), "rev-parse", "--git-path", "info/exclude"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        return None  # not a git repository
    p = Path(r.stdout.strip())
    return p if p.is_absolute() else target / p


def _strip(text: str) -> str:
    if START not in text:
        return text
    head, _, rest = text.partition(START)
    _, _, tail = rest.partition(END)
    return (head.rstrip("\n") + ("\n" if head.strip() else "") + tail.lstrip("\n")).rstrip("\n") + ("\n" if (head + tail).strip() else "")


def write_block(target: Path, manifest: dict, use_gitignore: bool = False, state_only: bool = False) -> Path | None:
    f = ignore_file(target, use_gitignore)
    if f is None:
        return None
    f.parent.mkdir(parents=True, exist_ok=True)
    text = _strip(f.read_text()) if f.exists() else ""
    block = "\n".join([START, *patterns(manifest, state_only), END]) + "\n"
    f.write_text((text + ("\n" if text and not text.endswith("\n\n") else "") + block))
    return f


def remove_block(target: Path, use_gitignore: bool = False) -> bool:
    f = ignore_file(target, use_gitignore)
    if f is None or not f.exists() or START not in f.read_text():
        return False
    f.write_text(_strip(f.read_text()))
    return True


def tracked(target: Path, manifest: dict) -> list[str]:
    """NDC paths git already tracks: the ignore rule does not apply to those."""
    paths = [p.strip("/") for p in patterns(manifest)]
    r = subprocess.run(["git", "-C", str(target), "ls-files", "--", *paths], capture_output=True, text=True)
    return r.stdout.split() if r.returncode == 0 else []


def is_tracked(target: Path, rel: str) -> bool:
    r = subprocess.run(["git", "-C", str(target), "ls-files", "--error-unmatch", "--", rel], capture_output=True, text=True)
    return r.returncode == 0
