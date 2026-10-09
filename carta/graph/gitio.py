"""Git plumbing for the graph arm, through ``subprocess`` only."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import List, Optional

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
