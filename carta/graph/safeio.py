"""File access that never follows a symbolic link.

A repository can contain a symlink wherever Carta expects a file of its own
or a doc: a doc that points at a file outside the repository, a ``.carta``
entry that points at one. Every read of repository content and every file
the graph arm writes goes through this module, so neither can be redirected.

Reads refuse a symlink in any path component below the root (the root itself
is whatever the user named, link or not), refuse anything that is not a
regular file, and check the size before reading. Writes create a new file
beside the destination and rename it into place, which replaces a symlink
instead of writing through it.
"""
from __future__ import annotations

import errno
import os
import stat
from pathlib import Path
from typing import Union

_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_O_NONBLOCK = getattr(os, "O_NONBLOCK", 0)
_O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_O_BINARY = getattr(os, "O_BINARY", 0)
_DIR_FD = bool(_O_NOFOLLOW and _O_DIRECTORY and os.open in getattr(os, "supports_dir_fd", ()))

PathLike = Union[str, Path]

# Why a file was deliberately not read. These are decisions, not failures: the
# same tree gives the same answer every time.
SKIP_REASONS = ("symlink", "not_regular", "too_large", "missing", "unsafe_path")


class SkippedFile(Exception):
    """The file is not one this module reads; ``reason`` is one of ``SKIP_REASONS``."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def is_symlink(path: PathLike) -> bool:
    """True when the path itself is a symbolic link (dangling or not)."""
    try:
        return stat.S_ISLNK(os.lstat(str(path)).st_mode)
    except OSError:
        return False


def _parts(rel: str) -> list:
    parts = rel.split("/")
    if not rel or rel.startswith("/") or "\0" in rel or any(p in ("", ".", "..") for p in parts):
        raise SkippedFile("unsafe_path")
    return parts


def _classify(root: str, parts: list) -> None:
    """Walk the components with ``lstat``; raise ``SkippedFile`` for anything unreadable by rule."""
    current = root
    last = len(parts) - 1
    for i, part in enumerate(parts):
        current = os.path.join(current, part)
        try:
            mode = os.lstat(current).st_mode
        except (FileNotFoundError, NotADirectoryError):
            raise SkippedFile("missing") from None
        if stat.S_ISLNK(mode):
            raise SkippedFile("symlink")
        if i < last and not stat.S_ISDIR(mode):
            raise SkippedFile("missing")
        if i == last and not stat.S_ISREG(mode):
            raise SkippedFile("not_regular")


def _open(root: str, parts: list) -> int:
    """Open the file read-only without following a link in any component."""
    final = os.O_RDONLY | _O_NOFOLLOW | _O_NONBLOCK | _O_CLOEXEC | _O_BINARY
    if not _DIR_FD:
        return os.open(os.path.join(root, *parts), final)
    step = os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW | _O_CLOEXEC
    dir_fd = os.open(root, os.O_RDONLY | _O_DIRECTORY | _O_CLOEXEC)
    try:
        for part in parts[:-1]:
            nxt = os.open(part, step, dir_fd=dir_fd)
            os.close(dir_fd)
            dir_fd = nxt
        return os.open(parts[-1], final, dir_fd=dir_fd)
    finally:
        os.close(dir_fd)


def read_regular_file(root: PathLike, rel: str, max_bytes: int) -> bytes:
    """The bytes of ``root/rel``, a repo-relative POSIX path.

    Raises ``SkippedFile`` when the path is, or passes through, a symbolic
    link; is not a regular file (a FIFO, a device, a directory); is larger
    than ``max_bytes``; or is not there. Raises ``OSError`` when a regular
    file that is there cannot be read (permissions, an I/O error): that one is
    a failure, and callers must not treat it as "the file is gone".
    """
    root = str(root)
    parts = _parts(rel)
    _classify(root, parts)
    try:
        fd = _open(root, parts)
    except OSError as exc:
        # The tree changed between the walk and the open, or the platform
        # reports a link its own way. Decide from what is there now.
        if exc.errno in (errno.ELOOP, errno.EMLINK, errno.ENOTDIR, errno.ENOENT):
            _classify(root, parts)
            if exc.errno in (errno.ELOOP, errno.EMLINK):
                raise SkippedFile("symlink") from None
        raise
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise SkippedFile("not_regular")
        if info.st_size > max_bytes:
            raise SkippedFile("too_large")
        data = handle.read(max_bytes + 1)
    if len(data) > max_bytes:               # it grew while being read
        raise SkippedFile("too_large")
    return data


def create_exclusive(path: PathLike, text: str, mode: int = 0o644) -> bool:
    """Create ``path`` holding ``text`` only if nothing is there. Never follows a link.

    Returns False, writing nothing, when the name is taken by anything at
    all, a dangling symbolic link included.
    """
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW | _O_CLOEXEC | _O_BINARY
    try:
        fd = os.open(str(path), flags, mode)
    except FileExistsError:
        return False
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.EMLINK):
            return False
        raise
    with os.fdopen(fd, "wb") as handle:
        handle.write(text.encode("utf-8"))
    return True


def write_text_replacing(path: PathLike, text: str, mode: int = 0o644) -> None:
    """Write ``text`` to ``path`` by creating a new file and renaming it into place.

    If ``path`` is a symbolic link the link is replaced and its target is left
    alone. The directory is the one the caller named; it is created when
    missing. Raises ``OSError`` when the file cannot be written.
    """
    target = Path(path)
    parent = target.parent
    parent.mkdir(parents=True, exist_ok=True)
    if target.is_dir() and not is_symlink(target):
        raise IsADirectoryError(errno.EISDIR, "is a directory", str(target))
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW | _O_CLOEXEC | _O_BINARY
    fd = -1
    temp = parent / f".{target.name}.{os.getpid()}.tmp"
    for attempt in range(100):
        temp = parent / f".{target.name}.{os.getpid()}.{attempt}.tmp"
        try:
            fd = os.open(str(temp), flags, mode)
            break
        except FileExistsError:
            continue
    if fd < 0:
        raise FileExistsError(errno.EEXIST, "no free temporary name beside", str(target))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(text.encode("utf-8"))
        os.replace(str(temp), str(target))
    except BaseException:
        try:
            os.unlink(str(temp))
        except OSError:
            pass
        raise
