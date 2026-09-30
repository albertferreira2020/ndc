"""Git worktree lifecycle for parallel tasks: one isolated checkout per task, merged back when it passes."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


class GitError(Exception):
    pass


def git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    r = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)
    if check and r.returncode != 0:
        raise GitError(f"git {' '.join(args[:2])} failed: {(r.stderr or r.stdout).strip()[:300]}")
    return r


def preflight(root: Path) -> None:
    """Parallel mode merges into the current branch, so the repository must be in a state where that is safe."""
    top = git(root, "rev-parse", "--show-toplevel", check=False)
    if top.returncode != 0:
        raise GitError("--parallel needs a git repository (each task gets its own worktree)")
    if Path(top.stdout.strip()).resolve() != root.resolve():
        raise GitError(f"run NDC from the repository root ({top.stdout.strip()}), not from a subdirectory")
    if git(root, "rev-parse", "--verify", "HEAD", check=False).returncode != 0:
        raise GitError("the repository has no commits yet: make an initial commit first")
    dirty = git(root, "status", "--porcelain").stdout.strip()
    if dirty:
        raise GitError("the working tree has uncommitted changes; commit or stash them first, because parallel "
                       "results are merged into it:\n" + "\n".join(dirty.splitlines()[:8]))
    if git(root, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == "HEAD":
        raise GitError("HEAD is detached: check out a branch so the results have somewhere to be merged")


def _identity(root: Path) -> list[str]:
    if git(root, "config", "user.name", check=False).stdout.strip():
        return []
    return ["-c", "user.name=NDC", "-c", "user.email=ndc@localhost"]


def create(root: Path, task_id: int) -> tuple[Path, str]:
    """A fresh worktree on its own branch, starting from the current HEAD (which includes earlier merges)."""
    path, branch = root / ".ndc" / "worktrees" / f"task-{task_id}", f"ndc/task-{task_id}"
    remove(root, path, branch)  # leftovers of a crashed run
    path.parent.mkdir(parents=True, exist_ok=True)
    git(root, "worktree", "add", "-q", "-b", branch, str(path), "HEAD")
    if (root / ".claude").is_dir():  # untracked team files (agents, skills, rules) so the task sees the same setup
        shutil.copytree(root / ".claude", path / ".claude", dirs_exist_ok=True, symlinks=True)
    return path, branch


def commit_all(root: Path, wt: Path, message: str) -> bool:
    git(wt, "add", "-A")
    if git(wt, "diff", "--cached", "--quiet", check=False).returncode == 0:
        return False
    git(wt, *_identity(root), "commit", "-q", "-m", message)
    return True


def merge(root: Path, branch: str, message: str) -> tuple[bool, list[str]]:
    """Merge into the checked-out branch. On conflict: abort cleanly and report the files."""
    r = git(root, *_identity(root), "merge", "--no-ff", "-m", message, branch, check=False)
    if r.returncode == 0:
        return True, []
    conflicts = git(root, "diff", "--name-only", "--diff-filter=U", check=False).stdout.split()
    git(root, "merge", "--abort", check=False)
    if not conflicts:  # not a content conflict (e.g. a local file would be overwritten): keep the reason
        conflicts = [(r.stderr or r.stdout).strip().splitlines()[0][:200] if (r.stderr or r.stdout).strip() else "merge failed"]
    return False, conflicts


def remove(root: Path, path: Path, branch: str) -> None:
    git(root, "worktree", "remove", "--force", str(path), check=False)
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)
    git(root, "worktree", "prune", check=False)
    git(root, "branch", "-D", branch, check=False)
