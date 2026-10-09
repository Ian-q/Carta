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
    return list(diff_name_status(repo, range_spec))


def diff_name_status(repo: Path, range_spec: str) -> Dict[str, str]:
    """``path -> status`` (``A``, ``D`` or ``M``) across a revision range, relative to ``repo``.

    Renames are not followed: a moved file is a ``D`` and an ``A``. Every
    status other than added and deleted is reported as ``M``. Raises
    ``GitError`` when the range is not a revision range.
    """
    if not range_spec or range_spec.startswith("-"):
        raise GitError(f"{range_spec!r} is not a revision range")
    out = run_git(repo, "diff", "--name-status", "--no-renames", "--relative", "-z",
                  range_spec, "--")
    tokens = _names(out)
    found: Dict[str, str] = {}
    for i in range(0, len(tokens) - 1, 2):
        status = tokens[i][:1]
        found[tokens[i + 1]] = status if status in ("A", "D") else "M"
    return {path: found[path] for path in sorted(found)}


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


# -- history readers (the replay eval) --------------------------------------

_GITLINK = "160000"
_HEX = re.compile(r"^[0-9a-f]{40,64}$")


class LogEntry:
    """One mainline commit and what it changed against its first parent."""

    __slots__ = ("sha", "parents", "changes", "blobs")

    def __init__(self, sha: str, parents: Tuple[str, ...],
                 changes: Tuple[Tuple[str, str], ...], blobs: Dict[str, str]) -> None:
        self.sha = sha
        self.parents = parents
        self.changes = changes          # ((status, path), ...) sorted by path; status is A, M or D
        self.blobs = blobs              # path -> blob id, for every added or modified path

    def __repr__(self) -> str:
        return f"LogEntry({self.sha[:12]}, {len(self.changes)} changes)"


def toplevel(path: Path) -> Optional[Path]:
    """The root of the work tree holding ``path``, or ``None`` outside a repository."""
    try:
        out = run_git(path, "rev-parse", "--show-toplevel")
    except GitError:
        return None
    text = out.decode("utf-8", "surrogateescape").strip()
    return Path(text) if text else None


def rev_parse(repo: Path, rev: str) -> str:
    """The full commit id ``rev`` names. Raises ``GitError`` when it names no commit."""
    if not rev or rev.startswith("-"):
        raise GitError(f"{rev!r} is not a revision")
    out = run_git(repo, "rev-parse", "--verify", "--quiet", rev + "^{commit}")
    return out.decode("ascii", "replace").strip()


def rev_list_first_parent(repo: Path, range_spec: str) -> List[str]:
    """Commit ids on the first-parent line of a revision or range, oldest first."""
    if not range_spec or range_spec.startswith("-"):
        raise GitError(f"{range_spec!r} is not a revision range")
    out = run_git(repo, "rev-list", "--first-parent", "--reverse", range_spec, "--")
    return out.decode("ascii", "replace").split()


def _raw_change(meta: str, path: str) -> Optional[Tuple[str, str, str]]:
    """One ``--raw`` record as ``(status, path, new blob)``; ``None`` for a gitlink."""
    fields = meta.lstrip(":").split(" ")
    if len(fields) < 5:
        return None
    old_mode, new_mode, _old, new, status = fields[:5]
    status = status[:1]
    old_link, new_link = old_mode == _GITLINK, new_mode == _GITLINK
    if old_link and new_link:
        return None
    if new_link:                        # a file replaced by a submodule entry: the file is gone
        return None if status == "A" else ("D", path, "")
    if old_link:                        # the reverse: a file appears
        return None if status == "D" else ("A", path, new)
    if status == "D":
        return ("D", path, "")
    if status == "A":
        return ("A", path, new)
    return ("M", path, new)             # M and T (a mode or type change keeps the path)


def first_parent_log(repo: Path, rev: str = "HEAD") -> List[LogEntry]:
    """The first-parent history of ``rev``, oldest first, read in one git process.

    Every commit is compared with its **first** parent, so a merge commit
    carries the whole change it merged and a squash commit carries one pull
    request. Renames are not detected: a move is a delete plus an add.
    Submodule entries are left out; paths are relative to the repository root
    whatever directory ``repo`` is.
    """
    if not rev or rev.startswith("-"):
        raise GitError(f"{rev!r} is not a revision")
    out = run_git(repo, "-c", "log.showRoot=true", "log", "--first-parent",
                  "--diff-merges=first-parent", "--no-renames", "--raw", "--no-abbrev", "-z",
                  "--reverse", "--format=%x01%H %P%x02", rev, "--")
    entries: List[LogEntry] = []
    for record in out.decode("utf-8", "surrogateescape").split("\x01")[1:]:
        head, _, body = record.partition("\x02")
        ids = head.split()
        tokens = [t.lstrip("\n") for t in body.split("\0")]
        found: Dict[str, Tuple[str, str]] = {}
        i = 0
        while i + 1 < len(tokens):
            if not tokens[i].startswith(":"):
                i += 1
                continue
            change = _raw_change(tokens[i], tokens[i + 1])
            if change is not None:
                found[change[1]] = (change[0], change[2])
            i += 2
        changes = tuple((found[p][0], p) for p in sorted(found))
        blobs = {p: found[p][1] for p in found if found[p][0] != "D"}
        entries.append(LogEntry(ids[0], tuple(ids[1:]), changes, blobs))
    return entries


def ls_tree(repo: Path, commit: str) -> Dict[str, str]:
    """``path -> blob id`` for every file in a commit's tree (submodule entries skipped)."""
    if not commit or commit.startswith("-"):
        raise GitError(f"{commit!r} is not a revision")
    out = run_git(repo, "ls-tree", "-r", "-z", "--full-tree", commit)
    tree: Dict[str, str] = {}
    for record in out.decode("utf-8", "surrogateescape").split("\0"):
        meta, tab, path = record.partition("\t")
        if not tab:
            continue
        fields = meta.split(" ")
        if len(fields) >= 3 and fields[1] == "blob":
            tree[path] = fields[2]
    return tree


class BlobReader:
    """Reads blob contents through one long-lived ``git cat-file --batch``."""

    def __init__(self, repo: Path) -> None:
        self.reads = 0
        try:
            self._proc: Optional[subprocess.Popen] = subprocess.Popen(
                ["git", "cat-file", "--batch"], cwd=str(repo), env=_env(),
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        except (OSError, ValueError) as exc:
            raise GitError(f"could not run git: {exc}") from exc

    def __enter__(self) -> "BlobReader":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        for stream in (proc.stdin, proc.stdout):
            try:
                if stream is not None:
                    stream.close()
            except OSError:
                pass
        try:
            proc.wait(timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            proc.kill()
            proc.wait()

    def read(self, blob_id: str) -> bytes:
        """The content of one object. Raises ``GitError`` for an unknown or malformed id."""
        proc = self._proc
        if proc is None or proc.stdin is None or proc.stdout is None:
            raise GitError("the blob reader is closed")
        if not _HEX.match(blob_id):
            raise GitError(f"{blob_id!r} is not an object id")
        try:
            proc.stdin.write(blob_id.encode("ascii") + b"\n")
            proc.stdin.flush()
            header = proc.stdout.readline()
        except OSError as exc:
            raise GitError(f"git cat-file failed: {exc}") from exc
        fields = header.split()
        if len(fields) != 3:
            if not header:
                raise GitError("git cat-file exited (not a git repository?)")
            raise GitError(f"object {blob_id} is not in the repository")
        size = int(fields[2])
        data = proc.stdout.read(size + 1)           # the content, then one newline
        if len(data) != size + 1:
            raise GitError(f"short read for object {blob_id}")
        self.reads += 1
        return data[:size]
