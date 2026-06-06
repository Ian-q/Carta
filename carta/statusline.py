"""`carta statusline` — prints a compact embed-progress segment for the
Claude Code status line, and wires that segment into a user's status-line
script.

The printer reads ``.carta/embed-status.json`` (written by run_embed). It must
be fast (<30ms, pure file read) and must NEVER raise — any failure prints
nothing.
"""

import json
import os
import socket
import sys
import time
from pathlib import Path

from carta.embed.status import STATUS_FILENAME

# Reuse the embed spinner frames for visual consistency.
_SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
_NAME_W = 28
FLASH_WINDOW_S = 30
STALE_WINDOW_S = 60

# ANSI (kept dim/subtle to match typical status lines)
_RESET = "\033[0m"
_DIM = "\033[2m"
_CYAN = "\033[36m"
_GREEN = "\033[32m"
_RED = "\033[31m"


def _fmt_elapsed(seconds: float) -> str:
    s = int(max(0, seconds))
    if s < 60:
        return f"{s}s"
    m = s // 60
    if m < 60:
        return f"{m}m"
    h, m = divmod(m, 60)
    return f"{h}h{m}m"


def _fmt_chunks(n: int) -> str:
    if n < 1000:
        return str(n)
    return f"{n / 1000:.1f}k"


def _truncate(s: str, max_len: int) -> str:
    if len(s) <= max_len:
        return s
    return s[: max_len - 1] + "…"


def resolve_state(status: dict, *, now: float, hostname: str, pid_alive_fn) -> str:
    """Return one of: 'running', 'done', 'failed', 'idle'.

    pid_alive_fn(pid)->bool is only consulted when the run is on this host.
    """
    phase = status.get("phase")
    if phase == "running":
        if status.get("host") == hostname:
            pid = status.get("pid")
            return "running" if (pid and pid_alive_fn(pid)) else "idle"
        # Different host: can't check PID, fall back to heartbeat freshness.
        if now - float(status.get("updated_at") or 0) <= STALE_WINDOW_S:
            return "running"
        return "idle"
    if phase in ("done", "failed"):
        if now - float(status.get("finished_at") or 0) <= FLASH_WINDOW_S:
            return phase
        return "idle"
    return "idle"


def format_segment(status: dict, state: str, *, now: float, color: bool = True) -> str:
    """Render the segment string for a resolved state. 'idle' -> ''."""
    def c(code, text):
        return f"{code}{text}{_RESET}" if color else text

    if state == "running":
        spin = _SPINNER_FRAMES[int(now / 0.1) % len(_SPINNER_FRAMES)]
        idx = status.get("current_idx", 0)
        total = status.get("total", 0)
        name = _truncate(status.get("current_file") or "", _NAME_W)
        started = status.get("current_file_started_at") or now
        elapsed = _fmt_elapsed(now - float(started))
        body = f"carta {idx}/{total}  {name}  {elapsed}"
        return f"{c(_CYAN, spin)} {body}"
    if state == "done":
        files = status.get("embedded", 0) + status.get("skipped", 0)
        chunks = _fmt_chunks(status.get("chunks", 0))
        return c(_GREEN, f"✓ carta {files} files · {chunks} chunks")
    if state == "failed":
        idx = status.get("current_idx", 0)
        total = status.get("total", 0)
        errs = status.get("errors", 0)
        return c(_RED, f"✗ carta {idx}/{total} · {errs} errors")
    return ""
