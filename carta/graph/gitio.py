"""Git plumbing for the graph arm, through ``subprocess`` only."""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Dict, FrozenSet, List, Optional, Sequence, Set, Tuple

_SCRUBBED_ENV = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_PREFIX")


class GitError(RuntimeError):
    """git is missing, or exited non-zero."""


def _env() -> dict:
    env = dict(os.environ)
    for name in _SCRUBBED_ENV:      # set inside git hooks; they would redirect every call
        env.pop(name, None)
    env["LC_ALL"] = "C"
    env["GIT_OPTIONAL_LOCKS"] = "0"
    return env


def run_git(repo: Path, *args: str, input_bytes: Optional[bytes] = None) -> bytes:
    """Run git in ``repo`` and return stdout. Raises ``GitError`` on any failure."""
    cmd = ["git", "-c", "core.quotepath=off", *args]
    try:
        proc = subprocess.run(cmd, cwd=str(repo), input=input_bytes, capture_output=True,
                              env=_env(), check=False)
    except (OSError, ValueError) as exc:
        raise GitError(f"could not run git: {exc}") from exc
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", "replace").strip()
        raise GitError(f"git {' '.join(args)} failed ({proc.returncode}): {detail}")
    return proc.stdout


def is_work_tree(path: Path) -> bool:
    """True when ``path`` is inside a git work tree."""
    try:
        return run_git(path, "rev-parse", "--is-inside-work-tree").strip() == b"true"
    except GitError:
        return False


def ls_files(repo: Path) -> List[str]:
    """Tracked plus untracked-but-not-ignored paths, relative to ``repo``, sorted.

    Run with ``cwd=repo``, so the list is limited to that directory even when
    it is a subdirectory of a larger checkout.
    """
    out = run_git(repo, "ls-files", "-z", "--cached", "--others", "--exclude-standard")
    names = {name.decode("utf-8", "surrogateescape") for name in out.split(b"\0") if name}
    return sorted(names)


def default_branch(repo: Path) -> str:
    """The remote's default branch name, or ``main`` when there is no ``origin/HEAD``."""
    try:
        ref = run_git(repo, "rev-parse", "--abbrev-ref", "origin/HEAD").decode(
            "utf-8", "replace").strip()
    except GitError:
        return "main"
    if not ref or ref == "origin/HEAD":
        return "main"
    return ref.split("/", 1)[1] if "/" in ref else ref


def _names(out: bytes) -> List[str]:
    return [name.decode("utf-8", "surrogateescape") for name in out.split(b"\0") if name]


def diff_names(repo: Path, range_spec: str) -> List[str]:
    """Paths changed across a revision range, relative to ``repo``, sorted.

    Renames are not followed: a moved file is listed under both names.
    Raises ``GitError`` when the range is not a revision range.
    """
    if not range_spec or range_spec.startswith("-"):
        raise GitError(f"{range_spec!r} is not a revision range")
    out = run_git(repo, "diff", "--name-only", "--no-renames", "--relative", "-z",
                  range_spec, "--")
    return sorted(set(_names(out)))


_STATUS_RE = re.compile(r"^[DR][0-9]*$")


def deleted_and_renamed(repo: Path, limit: int) -> Tuple[FrozenSet[str], Dict[str, str]]:
    """Paths that history deleted or renamed away, from the newest ``limit`` commits.

    Returns ``(deleted, renamed)``: the paths removed outright, and
    ``old -> new`` for renames. Paths are relative to ``repo``. A path has one
    record only, its newest: a file renamed away and later re-created and
    deleted is in ``deleted``.
    """
    # Two steps, because ``git log -n`` counts the commits it prints: with a
    # diff filter it would walk all of history looking for ``limit`` commits
    # that deleted something. The bound is on commits searched.
    commits = run_git(repo, "rev-list", "--no-merges", "-n", str(max(0, int(limit))), "HEAD")
    if not commits.strip():
        return frozenset(), {}
    out = run_git(repo, "log", "--no-walk=unsorted", "--stdin", "-M", "--diff-filter=DR",
                  "--name-status", "--format=", "-z", "--relative", input_bytes=commits)
    tokens = [t.lstrip("\n") for t in out.decode("utf-8", "surrogateescape").split("\0")]
    tokens = [t for t in tokens if t]
    deleted: Set[str] = set()
    renamed: Dict[str, str] = {}
    seen: Set[str] = set()
    i = 0
    while i < len(tokens):
        status = tokens[i]
        if not _STATUS_RE.match(status):
            i += 1
            continue
        if status.startswith("R") and i + 2 < len(tokens):
            old, new = tokens[i + 1], tokens[i + 2]
            if old not in seen:
                seen.add(old)
                renamed[old] = new
            i += 3
        elif status == "D" and i + 1 < len(tokens):
            old = tokens[i + 1]
            if old not in seen:
                seen.add(old)
                deleted.add(old)
            i += 2
        else:
            i += 1
    return frozenset(deleted), renamed


def ignored_paths(repo: Path, paths: Sequence[str]) -> FrozenSet[str]:
    """The subset of ``paths`` that git's ignore rules match. Empty outside a repository."""
    wanted = sorted(set(p for p in paths if p))
    if not wanted:
        return frozenset()
    payload = b"\0".join(p.encode("utf-8", "surrogateescape") for p in wanted) + b"\0"
    cmd = ["git", "-c", "core.quotepath=off", "check-ignore", "-z", "--stdin"]
    try:
        proc = subprocess.run(cmd, cwd=str(repo), input=payload, capture_output=True,
                              env=_env(), check=False)
    except (OSError, ValueError):
        return frozenset()
    if proc.returncode != 0:          # 1: nothing ignored; 128: not a repository
        return frozenset()
    return frozenset(_names(proc.stdout))
