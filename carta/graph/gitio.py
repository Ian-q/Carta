"""Git plumbing for the graph arm, through ``subprocess`` only.

File names, and through them everything git prints about a tree, are
untrusted: a name may hold any byte but NUL and ``/``. So every reader here
follows the same rules.

* Output is NUL-delimited (``-z``) and split on NUL alone. A token is never
  stripped, filtered or re-split.
* Each stream is read as a fixed grammar: a record has a known number of
  fields, object names must match ``_OID`` in full, a status must be one of
  the letters that reader expects. A token that fits nowhere raises
  ``GitError``; nothing is skipped to get back in step.
* Commit boundaries never share a channel with paths: the commit list comes
  from ``rev-list`` (object names only) and is checked on its own.
* A revision is passed after ``--end-of-options`` and is refused if it is
  empty, starts with ``-`` or holds a control character. No shell is used.

``--end-of-options`` needs git 2.30 or newer (2.24 for everything but
``rev-parse``).
"""
from __future__ import annotations

import os
import re
import stat
import subprocess
from pathlib import Path
from typing import Dict, FrozenSet, List, Optional, Sequence, Set, Tuple

# Variables that point git at another repository, object store or config.
# They are set inside hooks (and can be set by anything that launches Carta),
# and any one of them would silently redirect every call below.
# GIT_CONFIG_GLOBAL / _SYSTEM / _NOSYSTEM stay: they are how a user or a test
# isolates git from the machine's config, and they are not set by git itself.
_SCRUBBED_ENV = (
    "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_PREFIX", "GIT_COMMON_DIR",
    "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_NAMESPACE",
    "GIT_CONFIG", "GIT_CONFIG_PARAMETERS", "GIT_CONFIG_COUNT",
)
_SCRUBBED_PREFIXES = ("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")
_GIT = ("git", "-c", "core.quotepath=off")

# A full object name: SHA-1 or SHA-256, nothing else and nothing after it.
_OID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")
_OID_B = rb"(?:[0-9a-f]{40}|[0-9a-f]{64})"
_RAW_META = re.compile(rb":([0-7]{6}) ([0-7]{6}) (" + _OID_B + rb") (" + _OID_B + rb") ([AMDT])")
_TREE_META = re.compile(rb"([0-7]{6}) (blob|tree|commit) (" + _OID_B + rb")")
_BATCH_HEADER = re.compile(rb"(" + _OID_B + rb") ([a-z]+) ([0-9]{1,18})\n")
_RENAME = re.compile(r"R[0-9]{1,3}")
_GITLINK = b"160000"
_MAX_IGNORE_RUNS = 64


class GitError(RuntimeError):
    """git is missing, exited non-zero, or printed something no reader here expects."""


class BlobTooLarge(GitError):
    """The object is larger than the caller allowed. The reader is still usable."""


def _env() -> dict:
    env = {name: value for name, value in os.environ.items()
           if name not in _SCRUBBED_ENV and not name.startswith(_SCRUBBED_PREFIXES)}
    env["LC_ALL"] = "C"
    env["GIT_OPTIONAL_LOCKS"] = "0"
    return env


def _run(repo: Path, args: Sequence[str], input_bytes: Optional[bytes] = None
         ) -> "subprocess.CompletedProcess[bytes]":
    try:
        return subprocess.run([*_GIT, *args], cwd=str(repo), input=input_bytes,
                              capture_output=True, env=_env(), check=False)
    except (OSError, ValueError) as exc:
        raise GitError(f"could not run git: {exc}") from exc


def run_git(repo: Path, *args: str, input_bytes: Optional[bytes] = None) -> bytes:
    """Run git in ``repo`` and return stdout. Raises ``GitError`` on any failure."""
    proc = _run(repo, args, input_bytes)
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", "replace").strip()
        raise GitError(f"git {' '.join(args)} failed ({proc.returncode}): {detail}")
    return proc.stdout


# -- the parsing primitives -------------------------------------------------

def _is_oid(text: str) -> bool:
    return _OID.fullmatch(text) is not None


def _rev(arg: str, what: str = "revision") -> str:
    """``arg`` if it can be handed to git as a revision, else ``GitError``."""
    if (not isinstance(arg, str) or not arg or arg.startswith("-")
            or any(ord(ch) < 0x20 or ord(ch) == 0x7f for ch in arg)):
        raise GitError(f"{arg!r} is not a {what}")
    return arg


def _records(out: bytes) -> List[bytes]:
    """The NUL-terminated tokens of a ``-z`` stream, exactly as git wrote them."""
    if not out:
        return []
    if not out.endswith(b"\0"):
        raise GitError("git output ended in the middle of a record")
    return out[:-1].split(b"\0")


def _text(token: bytes) -> str:
    return token.decode("utf-8", "surrogateescape")


def _path(token: bytes) -> str:
    if not token:
        raise GitError("git printed an empty path")
    return _text(token)


def _oid_rows(out: bytes) -> List[List[str]]:
    """Lines of space-separated object names (``rev-list`` output), checked in full."""
    if not out:
        return []
    if not out.endswith(b"\n"):
        raise GitError("git output ended in the middle of a line")
    rows = []
    for line in out[:-1].split(b"\n"):
        ids = line.decode("ascii", "replace").split(" ")
        if not all(_is_oid(item) for item in ids):
            raise GitError(f"expected object names from git, got {line[:80]!r}")
        rows.append(ids)
    return rows


# -- the working tree ---------------------------------------------------------

def is_work_tree(path: Path) -> bool:
    """True when ``path`` is inside a git work tree."""
    try:
        return run_git(path, "rev-parse", "--is-inside-work-tree") == b"true\n"
    except GitError:
        return False


def ls_files(repo: Path) -> List[str]:
    """Tracked plus untracked-but-not-ignored paths, relative to ``repo``, sorted.

    Run with ``cwd=repo``, so the list is limited to that directory even when
    it is a subdirectory of a larger checkout.
    """
    out = run_git(repo, "ls-files", "-z", "--cached", "--others", "--exclude-standard")
    return sorted({_path(token) for token in _records(out)})


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


def diff_names(repo: Path, range_spec: str) -> List[str]:
    """Paths changed across a revision range, relative to ``repo``, sorted.

    Renames are not followed: a moved file is listed under both names.
    Raises ``GitError`` when the range is not a revision range.
    """
    return list(diff_name_status(repo, range_spec))


def _parse_name_status(out: bytes) -> Dict[str, str]:
    tokens = _records(out)
    if len(tokens) % 2:
        raise GitError("git diff printed a status without a path")
    found: Dict[str, str] = {}
    for i in range(0, len(tokens), 2):
        status = tokens[i]
        if status not in (b"A", b"D", b"M", b"T", b"U", b"X"):
            raise GitError(f"unexpected status {status[:16]!r} from git diff")
        found[_path(tokens[i + 1])] = _text(status) if status in (b"A", b"D") else "M"
    return {path: found[path] for path in sorted(found)}


def diff_name_status(repo: Path, range_spec: str) -> Dict[str, str]:
    """``path -> status`` (``A``, ``D`` or ``M``) across a revision range, relative to ``repo``.

    Renames are not followed: a moved file is a ``D`` and an ``A``. Every
    status other than added and deleted is reported as ``M``. Raises
    ``GitError`` when the range is not a revision range (``A..B`` or
    ``A...B``): a single revision would be compared with the working tree.
    """
    _rev(range_spec, "revision range")
    if ".." not in range_spec:
        raise GitError(f"{range_spec!r} is not a revision range")
    out = run_git(repo, "diff", "--name-status", "--no-renames", "--relative", "-z",
                  "--end-of-options", range_spec, "--")
    return _parse_name_status(out)


def _parse_deleted_renamed(out: bytes) -> Tuple[FrozenSet[str], Dict[str, str]]:
    tokens = _records(out)
    deleted: Set[str] = set()
    renamed: Dict[str, str] = {}
    seen: Set[str] = set()
    i, n = 0, len(tokens)
    while i < n:
        status = tokens[i].decode("ascii", "replace")
        if status == "D":
            arity = 1
        elif _RENAME.fullmatch(status):
            arity = 2
        else:
            raise GitError(f"unexpected status {tokens[i][:16]!r} in git's deletion log")
        if i + arity >= n:
            raise GitError("git's deletion log ended in the middle of a record")
        old = _path(tokens[i + 1])
        new = _path(tokens[i + 2]) if arity == 2 else ""
        if old not in seen:
            seen.add(old)
            if arity == 2:
                renamed[old] = new
            else:
                deleted.add(old)
        i += 1 + arity
    return frozenset(deleted), renamed


def deleted_and_renamed(repo: Path, limit: int) -> Tuple[FrozenSet[str], Dict[str, str]]:
    """Paths that history deleted or renamed away, from the newest ``limit`` commits.

    Returns ``(deleted, renamed)``: the paths removed outright, and
    ``old -> new`` for renames. Paths are relative to ``repo``. A path has one
    record only, its newest: a file renamed away and later re-created and
    deleted is in ``deleted``.
    """
    # Two steps. The bound is on commits searched, so the commits are listed
    # first; and the changes are then read with no commit header in the
    # stream at all, so it is nothing but (status, path[, path]) records.
    out = run_git(repo, "rev-list", "--no-merges", "-n", str(max(0, int(limit))),
                  "--end-of-options", "HEAD", "--")
    rows = _oid_rows(out)
    if not rows:
        return frozenset(), {}
    if any(len(row) != 1 for row in rows):
        raise GitError("git rev-list printed more than one object name per line")
    out = run_git(repo, "diff-tree", "--stdin", "-r", "-z", "-M", "--diff-filter=DR",
                  "--name-status", "--no-commit-id", "--root", "--relative",
                  input_bytes="".join(row[0] + "\n" for row in rows).encode("ascii"))
    return _parse_deleted_renamed(out)


def _beyond_symlink(repo: Path, rel: str) -> bool:
    """True when git cannot be asked about ``rel``: it is not a plain path below ``repo``,
    or one of its parent directories is a symbolic link."""
    parts = rel.split("/")
    if rel.startswith("/") or any(part in ("", ".", "..") for part in parts[:-1]):
        return True
    current = str(repo)
    for part in parts[:-1]:
        current = os.path.join(current, part)
        try:
            if stat.S_ISLNK(os.lstat(current).st_mode):
                return True
        except OSError:
            return False
    return False


def _parse_check_ignore(out: bytes, batch: Sequence[str]) -> Set[str]:
    """``check-ignore -z -v -n``: four fields per path asked, in the order asked."""
    tokens = _records(out)
    if len(tokens) != 4 * len(batch):
        raise GitError(f"git check-ignore answered for {len(tokens) // 4} of "
                       f"{len(batch)} paths")
    ignored: Set[str] = set()
    for i, asked in enumerate(batch):
        _source, _line, pattern, name = tokens[4 * i:4 * i + 4]
        if _text(name) != asked:
            raise GitError("git check-ignore answered about a path that was not asked")
        if pattern and not pattern.startswith(b"!"):
            ignored.add(asked)
    return ignored


def ignored_paths(repo: Path, paths: Sequence[str]) -> FrozenSet[str]:
    """The subset of ``paths`` that git's ignore rules match. Empty outside a repository.

    A path git refuses to judge (it lies beyond a symbolic link, or inside a
    submodule) is reported as not ignored and does not affect the others.
    Raises ``GitError`` when git fails in a way that is not about one path.
    """
    wanted = sorted(set(p for p in paths if p and "\0" not in p))
    if not wanted or not is_work_tree(repo):
        return frozenset()
    askable = [p for p in wanted if not _beyond_symlink(repo, p)]
    ignored: Set[str] = set()
    runs = 0
    pending = [askable] if askable else []
    while pending:
        batch = pending.pop()
        runs += 1
        if runs > _MAX_IGNORE_RUNS:
            raise GitError("git check-ignore keeps failing; giving up on the ignore check")
        payload = b"".join(p.encode("utf-8", "surrogateescape") + b"\0" for p in batch)
        proc = _run(repo, ("check-ignore", "-z", "--stdin", "-v", "-n"), payload)
        if proc.returncode in (0, 1):               # 0: something is ignored; 1: nothing is
            ignored |= _parse_check_ignore(proc.stdout, batch)
        elif len(batch) > 1:                        # one path poisons a whole batch: isolate it
            half = len(batch) // 2
            pending.append(batch[half:])
            pending.append(batch[:half])
        # else: git cannot judge this one path; it is not known to be ignored
    return frozenset(ignored)


# -- history readers (the replay eval) --------------------------------------

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
    text = _text(out[:-1] if out.endswith(b"\n") else out)
    return Path(text) if text else None


def rev_parse(repo: Path, rev: str) -> str:
    """The full commit id ``rev`` names. Raises ``GitError`` when it names no commit."""
    _rev(rev)
    out = run_git(repo, "rev-parse", "--verify", "--quiet", "--end-of-options",
                  rev + "^{commit}")
    rows = _oid_rows(out)
    if len(rows) != 1 or len(rows[0]) != 1:
        raise GitError(f"{rev!r} does not name one commit")
    return rows[0][0]


def rev_list_first_parent(repo: Path, range_spec: str) -> List[str]:
    """Commit ids on the first-parent line of a revision or range, oldest first."""
    _rev(range_spec, "revision range")
    out = run_git(repo, "rev-list", "--first-parent", "--reverse", "--end-of-options",
                  range_spec, "--")
    rows = _oid_rows(out)
    if any(len(row) != 1 for row in rows):
        raise GitError("git rev-list printed more than one object name per line")
    return [row[0] for row in rows]


def _raw_change(match: "re.Match[bytes]", path: str) -> Optional[Tuple[str, str, str]]:
    """One ``--raw`` record as ``(status, path, new blob)``; ``None`` for a gitlink."""
    old_mode, new_mode, _old, new_id, status_b = match.groups()
    new, status = new_id.decode("ascii"), status_b.decode("ascii")
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


def _parse_raw_log(out: bytes, commits: Sequence[str]) -> Dict[str, Dict[str, Tuple[str, str]]]:
    """``diff-tree --stdin -z --raw`` as ``commit -> path -> (status, new blob)``.

    The stream is ``<commit> NUL`` followed by ``<meta> NUL <path> NUL``
    records, for each commit that changed something, in the order asked. A
    path is only ever read as the one token after a meta token, so no file
    name can pass for a commit or a record.
    """
    order = {sha: i for i, sha in enumerate(commits)}
    found: Dict[str, Dict[str, Tuple[str, str]]] = {}
    tokens = _records(out)
    current: Optional[Dict[str, Tuple[str, str]]] = None
    last = -1
    i, n = 0, len(tokens)
    while i < n:
        token = tokens[i]
        if token[:1] == b":":
            match = _RAW_META.fullmatch(token)
            if match is None or current is None or i + 1 >= n:
                raise GitError(f"unexpected record {token[:80]!r} in git's change log")
            path = _path(tokens[i + 1])
            change = _raw_change(match, path)
            if change is not None:
                current[path] = (change[0], change[2])
            i += 2
            continue
        sha = token.decode("ascii", "replace")
        position = order.get(sha, -1) if _is_oid(sha) else -1
        if position <= last:
            raise GitError(f"unexpected token {token[:80]!r} in git's change log")
        last = position
        current = found[sha] = {}
        i += 1
    return found


def first_parent_log(repo: Path, rev: str = "HEAD") -> List[LogEntry]:
    """The first-parent history of ``rev``, oldest first.

    Every commit is compared with its **first** parent, so a merge commit
    carries the whole change it merged and a squash commit carries one pull
    request. Renames are not detected: a move is a delete plus an add.
    Submodule entries are left out; paths are relative to the repository root
    whatever directory ``repo`` is.
    """
    _rev(rev)
    rows = _oid_rows(run_git(repo, "rev-list", "--first-parent", "--reverse", "--parents",
                             "--end-of-options", rev, "--"))
    if not rows:
        return []
    # One line per commit: the commit, then the parent to compare it with. A
    # commit given alone is compared with nothing (--root): everything is added.
    asked = "".join(" ".join(row[:2]) + "\n" for row in rows).encode("ascii")
    out = run_git(repo, "diff-tree", "--stdin", "-r", "-z", "--raw", "--no-abbrev",
                  "--no-renames", "--root", input_bytes=asked)
    found = _parse_raw_log(out, [row[0] for row in rows])
    entries: List[LogEntry] = []
    for row in rows:
        changed = found.get(row[0], {})
        changes = tuple((changed[p][0], p) for p in sorted(changed))
        blobs = {p: changed[p][1] for p in changed if changed[p][0] != "D"}
        entries.append(LogEntry(row[0], tuple(row[1:]), changes, blobs))
    return entries


def ls_tree(repo: Path, commit: str) -> Dict[str, str]:
    """``path -> blob id`` for every file in a commit's tree (submodule entries skipped)."""
    _rev(commit)
    out = run_git(repo, "ls-tree", "-r", "-z", "--full-tree", "--end-of-options", commit)
    tree: Dict[str, str] = {}
    for record in _records(out):
        meta, tab, path = record.partition(b"\t")
        match = _TREE_META.fullmatch(meta)
        if match is None or not tab:
            raise GitError(f"unexpected record {record[:80]!r} from git ls-tree")
        if match.group(2) == b"blob":
            tree[_path(path)] = match.group(3).decode("ascii")
    return tree


class BlobReader:
    """Reads blob contents through one long-lived ``git cat-file --batch``.

    The pipe is a strict request/reply protocol, so anything that could put it
    out of step ends the reader: an id that is not exactly one object name is
    refused before it is written, and a reply that is not the header of the
    blob asked for closes the process. A closed reader raises on every read.
    """

    def __init__(self, repo: Path) -> None:
        self.reads = 0
        try:
            self._proc: Optional[subprocess.Popen] = subprocess.Popen(
                [*_GIT, "cat-file", "--batch"], cwd=str(repo), env=_env(),
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

    def _fail(self, message: str) -> GitError:
        """Close the reader (the pipe can no longer be trusted) and build the error."""
        self.close()
        return GitError(message)

    def read(self, blob_id: str, max_bytes: Optional[int] = None) -> bytes:
        """The content of one blob.

        Raises ``GitError`` for a malformed id, an id that is not in the
        repository, or an object that is not a blob; ``BlobTooLarge`` when
        ``max_bytes`` is given and the blob is larger (its content is then
        discarded without being held in memory).
        """
        proc = self._proc
        if proc is None or proc.stdin is None or proc.stdout is None:
            raise GitError("the blob reader is closed")
        if not isinstance(blob_id, str) or not _is_oid(blob_id):
            raise GitError(f"{blob_id!r} is not an object id")
        wanted = blob_id.encode("ascii")
        try:
            proc.stdin.write(wanted + b"\n")
            proc.stdin.flush()
            header = proc.stdout.readline(256)
        except OSError as exc:
            raise self._fail(f"git cat-file failed: {exc}") from exc
        if not header:
            raise self._fail("git cat-file exited (not a git repository?)")
        if header == wanted + b" missing\n":        # a complete reply: still in step
            raise GitError(f"object {blob_id} is not in the repository")
        match = _BATCH_HEADER.fullmatch(header)
        if match is None or match.group(1) != wanted:
            raise self._fail(f"unexpected reply from git cat-file for {blob_id}")
        if match.group(2) != b"blob":
            raise self._fail(f"object {blob_id} is a {match.group(2).decode('ascii')}, "
                             f"not a blob")
        size = int(match.group(3))
        try:
            if max_bytes is not None and size > max_bytes:
                left = size + 1
                while left > 0:
                    chunk = proc.stdout.read(min(left, 1 << 20))
                    if not chunk:
                        raise self._fail(f"short read for object {blob_id}")
                    left -= len(chunk)
                raise BlobTooLarge(f"object {blob_id} is {size} bytes, over the "
                                   f"{max_bytes} allowed")
            data = proc.stdout.read(size + 1)       # the content, then one newline
        except OSError as exc:
            raise self._fail(f"git cat-file failed: {exc}") from exc
        if len(data) != size + 1 or data[-1:] != b"\n":
            raise self._fail(f"short read for object {blob_id}")
        self.reads += 1
        return data[:size]
