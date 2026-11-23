"""Claude Code hook handlers for session memory.

``carta sessions hook stop`` (Stop and SessionEnd) indexes the session that just
ended: it reads the hook's stdin JSON, and when the project has session memory on,
starts ``carta sessions index --session <transcript>`` as a detached process (which
waits for a per-project lock, so two index runs never overlap) and returns at once. The child's output goes to a log file next to the index state, so
the hook itself never prints, never waits for Qdrant or Ollama, and a dead backend
costs nothing but a failed background run (whose transcript is retried next time).

Stop fires after every assistant turn, so on Stop a session is re-indexed at most
once per ``sessions.stop_hook_min_interval_s``; SessionEnd always indexes.

``carta sessions hook session-start`` prints the brief (``carta.sessions.brief``),
leaving out the session being started or resumed.

Every handler is fail-open: any error means no output and exit status 0.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable

DEFAULT_MIN_INTERVAL_S = 300
LOG_MAX_BYTES = 1 << 20
STAMP_MAX_AGE_S = 30 * 86400
LOCK_NAME = "index.lock"
LOCK_WAIT_S = 1800


def read_payload(stream) -> dict:
    """The hook's stdin JSON, or ``{}`` when it is missing or malformed."""
    try:
        data = json.loads(stream.read() or "{}")
    except (ValueError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _project(cwd: str | None):
    """``(cfg, repo_root)`` for the project containing ``cwd``; raises when there is none."""
    from carta.config import find_config, load_config
    cfg_path = find_config(Path(cwd) if cwd else None)
    return load_config(cfg_path), cfg_path.parent.parent


def _state_dir(cfg: dict) -> Path:
    """``carta.sessions.index.state_path(cfg).parent``, without importing the index
    module (Qdrant client, PDF parser): the Stop hook runs after every turn and
    blocks it while Python starts."""
    raw = (cfg.get("sessions") or {}).get("state_dir") or ""
    if raw:
        base = Path(os.path.expanduser(raw))
    else:
        from carta.registry import carta_home
        base = carta_home() / "sessions"
    return base / cfg["project_name"]


def _safe_name(session_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", session_id)[:128] or "_"


def spawn_detached(cmd: list[str], cwd: Path, log_path: Path) -> None:
    """Start ``cmd`` in its own session with output appended to ``log_path``; do not wait."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        if log_path.stat().st_size > LOG_MAX_BYTES:
            log_path.unlink()
    except OSError:
        pass
    with open(log_path, "ab") as log:
        shown = cmd[cmd.index("index"):] if "index" in cmd else cmd
        log.write(f"\n--- {time.strftime('%Y-%m-%dT%H:%M:%S')} {' '.join(shown)}\n"
                  .encode())
        log.flush()
        subprocess.Popen(cmd, cwd=str(cwd), stdin=subprocess.DEVNULL, stdout=log,
                         stderr=log, start_new_session=True, close_fds=True)


def _throttled(stamp: Path, min_interval: float, now: float) -> bool:
    try:
        return now - stamp.stat().st_mtime < min_interval
    except OSError:
        return False


def _touch(stamp: Path, now: float) -> None:
    stamp.parent.mkdir(parents=True, exist_ok=True)
    stamp.touch()
    os.utime(stamp, (now, now))
    # Drop stamps of long-finished sessions so the directory stays small.
    for old in stamp.parent.iterdir():
        try:
            if now - old.stat().st_mtime > STAMP_MAX_AGE_S:
                old.unlink()
        except OSError:
            pass


def handle_stop(payload: dict, *, now: float | None = None,
                spawn: Callable[[list[str], Path, Path], None] = spawn_detached) -> str:
    """Start indexing the ended session in the background. Returns what it did
    (for tests and ``--verbose``): ``spawned``, ``throttled``, ``disabled``,
    ``no-transcript`` or ``no-project``. Raises only on unexpected errors, which
    ``run_hook`` swallows.
    """
    transcript = payload.get("transcript_path")
    if not isinstance(transcript, str) or not transcript:
        return "no-transcript"
    tpath = Path(os.path.expanduser(transcript))
    if not tpath.is_file():
        return "no-transcript"
    try:
        cfg, repo_root = _project(payload.get("cwd"))
    except Exception:
        return "no-project"
    if not (cfg.get("modules") or {}).get("session_memory", True):
        return "disabled"

    state_dir = _state_dir(cfg)
    now = time.time() if now is None else now
    session_id = str(payload.get("session_id") or tpath.stem)
    stamp = state_dir / "hook-stamps" / _safe_name(session_id)
    if payload.get("hook_event_name", "Stop") == "Stop":
        interval = float((cfg.get("sessions") or {}).get(
            "stop_hook_min_interval_s", DEFAULT_MIN_INTERVAL_S))
        if _throttled(stamp, interval, now):
            return "throttled"
    _touch(stamp, now)
    spawn(index_command(tpath, Path(repo_root)), state_dir, state_dir / "hook-index.log")
    return "spawned"


def index_command(transcript: Path, repo_root: Path) -> list[str]:
    """The detached child's command line.

    The child must never import from the project it indexes: a repository is not
    trusted code, and ``python -m`` puts the working directory first on
    ``sys.path``, where a ``carta/`` folder in the repository would shadow the
    installed package. So the child starts in Carta's own state directory, with
    safe-path mode where the interpreter has it (3.11+), and is told the project
    root as an argument (see ``_enter_project``).
    """
    safe = ["-P"] if sys.version_info >= (3, 11) else []
    return [sys.executable, *safe, "-m", "carta.sessions.hooks", "index",
            str(transcript), str(repo_root)]


def _enter_project(repo_root: str) -> None:
    """Drop the start directory from ``sys.path``, then move to the project root."""
    start = os.getcwd()
    sys.path[:] = [p for p in sys.path
                   if p not in ("", ".") and os.path.realpath(p) != os.path.realpath(start)]
    os.chdir(repo_root)
    here = os.path.realpath(repo_root)
    sys.path[:] = [p for p in sys.path if os.path.realpath(p) != here]


def _default_index(argv: list[str]) -> int:
    from carta.cli import main
    sys.argv = ["carta", *argv]
    try:
        main()
    except SystemExit as e:
        return e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
    return 0


def index_locked(transcript: str, *, run: Callable[[list[str]], int] | None = None,
                 wait_s: float = LOCK_WAIT_S, poll_s: float = 1.0) -> int:
    """Body of the detached child: ``carta sessions index --session <transcript>``,
    run while holding the project's index lock.

    The index keeps one state file per project and rewrites it whole, so two runs at
    once (parallel sessions, or SessionEnd firing while a Stop run is still
    embedding) would drop each other's entries, and an older read of the same
    transcript could replace the newer one's points. Waiting here costs the session
    nothing: this process is already detached. Gives up (exit 1) after ``wait_s``.
    """
    run = run or _default_index
    cfg, _root = _project(None)          # cwd is the project root (see _enter_project)
    lock_path = _state_dir(cfg) / LOCK_NAME
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    argv = ["sessions", "index", "--session", transcript]
    try:
        import fcntl
    except ImportError:                  # no flock (Windows): run unserialised
        return run(argv)
    with open(lock_path, "a") as lock:
        deadline = time.monotonic() + wait_s
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    print(f"Error: index lock {lock_path} still held after {wait_s:.0f}s; "
                          f"giving up on {transcript}", file=sys.stderr)
                    return 1
                time.sleep(poll_s)
        return run(argv)


def handle_session_start(payload: dict, **brief_kwargs) -> list[str]:
    """The brief for the session being started; ``[]`` on any failure."""
    from carta.sessions.brief import safe_brief
    sid = payload.get("session_id")
    return safe_brief(payload.get("cwd") or None,
                      exclude_session=sid if isinstance(sid, str) else None, **brief_kwargs)


def run_hook(event: str, stdin=None, stdout=None) -> int:
    """Entry point for ``carta sessions hook <event>``. Always returns 0."""
    stdin = sys.stdin if stdin is None else stdin
    stdout = sys.stdout if stdout is None else stdout
    try:
        payload = read_payload(stdin)
        if event == "stop":
            handle_stop(payload)
        elif event == "session-start":
            lines = handle_session_start(payload)
            if lines:
                stdout.write("\n".join(lines) + "\n")
                stdout.flush()
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    # The detached child handle_stop starts (see index_command).
    if len(sys.argv) == 4 and sys.argv[1] == "index":
        _enter_project(sys.argv[3])
        sys.exit(index_locked(sys.argv[2]))
    print("usage: python -m carta.sessions.hooks index <transcript> <project-root>",
          file=sys.stderr)
    sys.exit(2)
