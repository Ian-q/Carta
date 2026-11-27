"""Which transcripts belong to a project, and which worktree a session ran in.

A transcript belongs to a project when the first ``cwd`` it records is inside the
project root or inside one of the project's git worktrees (which may live outside
the root). Claude Code stores transcripts under ``<transcripts_dir>/<slug>/`` where
the slug is the session's cwd with every non-alphanumeric character replaced by
``-``; that slug is only used as a cheap pre-filter, and the recorded cwd decides.
"""
from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

# Worktree layouts nested inside the main checkout. A session's worktree is named
# from these even after the worktree itself has been removed.
NESTED_WORKTREE_DIRS = ((".worktrees",), (".claude", "worktrees"))


@dataclass(frozen=True)
class Location:
    repo: str            # the project root's directory name
    repo_root: str       # absolute project root
    worktree: str        # worktree directory name (the root's own name for the main checkout)
    worktree_path: str   # absolute worktree root


def transcript_slug(path: str | Path) -> str:
    """Claude Code's transcript directory name for a working directory."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(path))


def git_worktrees(repo_root: Path) -> list[Path]:
    """Live worktree roots of the repo at ``repo_root`` (the main checkout excluded).

    Best-effort: returns ``[]`` when git is missing or this is not a repository.
    """
    try:
        out = subprocess.run(
            ["git", "-C", str(repo_root), "worktree", "list", "--porcelain"],
            capture_output=True, text=True, timeout=10, check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    root = _norm(repo_root)
    paths = []
    for line in out.splitlines():
        if line.startswith("worktree "):
            p = Path(line[len("worktree "):])
            if _norm(p) != root:
                paths.append(p)
    return paths


def _norm(p: str | Path) -> str:
    """Absolute, symlink-resolved path string without requiring it to exist."""
    return os.path.realpath(os.path.expanduser(str(p)))


def _is_within(child: str, parent: str) -> bool:
    return child == parent or child.startswith(parent.rstrip(os.sep) + os.sep)


def locate(cwd: str, repo_root: Path, worktrees: list[Path] | None = None) -> Location | None:
    """Place a session cwd in the project, or ``None`` when it is outside it.

    The deepest live worktree containing ``cwd`` wins. Otherwise, inside the root,
    a path under ``.worktrees/<name>`` or ``.claude/worktrees/<name>`` names that
    worktree even if it no longer exists; anything else is the main checkout.
    """
    if not cwd:
        return None
    c = _norm(cwd)
    root = _norm(repo_root)
    repo = os.path.basename(root)
    best = None
    for wt in worktrees or []:
        w = _norm(wt)
        if _is_within(c, w) and (best is None or len(w) > len(best)):
            best = w
    if best is not None:
        return Location(repo, root, os.path.basename(best), best)
    if not _is_within(c, root):
        return None
    parts = Path(os.path.relpath(c, root)).parts if c != root else ()
    for prefix in NESTED_WORKTREE_DIRS:
        n = len(prefix)
        if len(parts) > n and tuple(parts[:n]) == prefix:
            wt_path = os.path.join(root, *parts[:n + 1])
            return Location(repo, root, parts[n], wt_path)
    return Location(repo, root, repo, root)


def candidate_transcripts(transcripts_dir: Path, roots: list[Path]) -> list[Path]:
    """Transcript files that may belong to one of ``roots`` (pre-filter only).

    Looks at ``*.jsonl`` directly in ``transcripts_dir`` (an archive laid out flat)
    and in each subdirectory whose name starts with the slug of a root. Subagent
    transcripts (``<session>/subagents/*.jsonl``) are never included: their text is
    sidechain traffic, not the user's session.
    """
    transcripts_dir = Path(os.path.expanduser(str(transcripts_dir)))
    if not transcripts_dir.is_dir():
        return []
    slugs = {transcript_slug(_norm(r)) for r in roots} | {transcript_slug(r) for r in roots}
    found: list[Path] = sorted(p for p in transcripts_dir.glob("*.jsonl") if p.is_file())
    for sub in sorted(transcripts_dir.iterdir()):
        if sub.is_dir() and any(sub.name.startswith(s) for s in slugs):
            found.extend(sorted(p for p in sub.glob("*.jsonl") if p.is_file()))
    return found
