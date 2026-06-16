"""Single-writer embed lock shared by every mutating embed entry point.

Only one embed / repair / visual run may write a project's Qdrant collections at a
time. Otherwise one run's stale-point cleanup (delete_other_points, which deletes
every point for a file except the IDs *this* run just wrote) can delete points the
*other* run just wrote — corrupting the collection mid-run. The CLI (`carta embed`
and its ``--repair`` / ``--visual`` / ``--files`` branches) and the MCP
``carta_embed`` tool all acquire this lock so there is exactly one writer per
project (audit CA-2/5/12).
"""
from __future__ import annotations

import atexit
import os
import signal
import sys
from contextlib import contextmanager
from pathlib import Path


class EmbedLockHeld(Exception):
    """Raised when the embed lock is already held by another live process."""

    def __init__(self, pid: int):
        self.pid = pid
        super().__init__(f"embed already running (PID {pid})")


def _read_pid(lock_path: Path):
    try:
        return int(lock_path.read_text().strip())
    except (ValueError, OSError):
        return None


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists but owned by another user
    except OSError:
        return False
    return True


def _safe_unlink(lock_path: Path) -> None:
    try:
        lock_path.unlink(missing_ok=True)
    except OSError:
        pass


def acquire(lock_path: Path) -> None:
    """Create the lock atomically, reclaiming a stale one (dead PID).

    Raises:
        EmbedLockHeld: if another *live* process already holds the lock.
    """
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    while True:
        if not lock_path.exists():
            try:
                fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
                with os.fdopen(fd, "w") as f:
                    f.write(str(os.getpid()))
                return
            except FileExistsError:
                continue  # lost the race; re-evaluate the holder

        existing_pid = _read_pid(lock_path)
        if existing_pid is None or existing_pid <= 0:
            _safe_unlink(lock_path)  # corrupt/empty lock — reclaim
            continue
        if _pid_alive(existing_pid):
            raise EmbedLockHeld(existing_pid)
        _safe_unlink(lock_path)  # stale lock from a dead PID — reclaim


@contextmanager
def embed_lock(lock_path: Path):
    """Hold the single-writer embed lock for the duration of the block.

    Registers atexit + SIGTERM/SIGINT cleanup so an abrupt exit doesn't strand the
    lock. Signal handlers are skipped when not on the main thread (e.g. under the
    MCP event loop), where ``signal.signal`` raises ValueError; atexit still covers
    cleanup there.

    Raises:
        EmbedLockHeld: if another live process holds the lock.
    """
    acquire(lock_path)
    released = {"v": False}

    def _release():
        if released["v"]:
            return
        released["v"] = True
        _safe_unlink(lock_path)

    atexit.register(_release)

    prev_handlers = {}
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            prev_handlers[sig] = signal.signal(
                sig, lambda signum, frame: (_release(), sys.exit(128 + signum))
            )
        except (ValueError, OSError):
            pass  # not the main thread — atexit still handles cleanup

    try:
        yield
    finally:
        _release()
        for sig, handler in prev_handlers.items():
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError, TypeError):
                pass
