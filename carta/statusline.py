"""`carta statusline` — prints a compact embed-progress segment for the
Claude Code status line, and wires that segment into a user's status-line
script.

The printer reads ``.carta/embed-status.json`` (written by run_embed). It must
be fast (<30ms, pure file read) and must NEVER raise — any failure prints
nothing.
"""

import json
import os
import re
import shlex
import socket
import sys
import time
from pathlib import Path

from carta.embed.status import STATUS_FILENAME

# Reuse the embed spinner frames for visual consistency.
_SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
_NAME_W = 28
FLASH_WINDOW_S = 30
STALE_WINDOW_S = 180

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
        return f"{c(_CYAN, spin)} {c(_DIM, body)}"
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


# ---------------------------------------------------------------------------
# IO functions (Task 5)
# ---------------------------------------------------------------------------

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


def read_status(start) -> "dict | None":
    """Walk up from *start* for .carta/embed-status.json; return parsed dict or None."""
    try:
        cur = Path(start).resolve()
    except Exception:
        return None
    while True:
        candidate = cur / ".carta" / STATUS_FILENAME
        if candidate.exists():
            try:
                return json.loads(candidate.read_text())
            except Exception:
                return None
        parent = cur.parent
        if parent == cur:
            return None
        cur = parent


def _cwd_from_stdin() -> str:
    raw = sys.stdin.read()
    if not raw.strip():
        return os.getcwd()
    data = json.loads(raw)
    return (
        (data.get("workspace") or {}).get("current_dir")
        or data.get("cwd")
        or os.getcwd()
    )


def print_segment() -> None:
    """Read session JSON from stdin, print the embed segment (or nothing).

    Never raises: any failure results in empty output.
    """
    try:
        cwd = _cwd_from_stdin()
        status = read_status(cwd)
        if not status:
            return
        now = time.time()
        state = resolve_state(
            status, now=now, hostname=socket.gethostname(), pid_alive_fn=_pid_alive
        )
        seg = format_segment(status, state, now=now, color=True)
        if seg:
            sys.stdout.write(seg)
    except Exception:
        return


# ---------------------------------------------------------------------------
# Wiring helpers (Task 6): install / uninstall / find
# ---------------------------------------------------------------------------

MARKER_START = "# >>> carta statusline >>>"
MARKER_END = "# <<< carta statusline <<<"

_SNIPPET_LINES = [
    MARKER_START,
    'seg=$(command -v carta >/dev/null && carta statusline <<<"$input" 2>/dev/null)',
    '[ -n "$seg" ] && parts="$parts │ $seg"',
    MARKER_END,
]

_OUTPUT_RE = re.compile(r"^\s*(echo|printf)\b.*\bparts\b")


def find_statusline_script(settings_path: Path):
    """Return the Path to a wireable status-line script, or None.

    Only command-type statusLines that reference an existing .sh/.bash file
    are wireable; inline commands and missing files return None.
    """
    try:
        data = json.loads(Path(settings_path).read_text())
    except Exception:
        return None
    sl_cfg = data.get("statusLine") or {}
    if sl_cfg.get("type") != "command":
        return None
    cmd = sl_cfg.get("command", "")
    try:
        tokens = shlex.split(cmd)
    except ValueError:
        return None
    for tok in tokens:
        p = Path(os.path.expanduser(tok))
        if p.suffix in (".sh", ".bash") and p.exists():
            return p
    return None


def install_into_script(script_path: Path, *, confirm) -> str:
    """Insert the carta segment block before the script's output line.

    Returns one of: 'installed', 'already', 'declined', 'unsupported'.
    confirm(message)->bool gates the edit (prompt in real use, lambda in tests).
    """
    script_path = Path(script_path)
    text = script_path.read_text()
    if MARKER_START in text:
        return "already"
    if not re.search(r"\binput\b", text) or "parts" not in text:
        return "unsupported"
    lines = text.splitlines()
    out_idx = None
    for i, line in enumerate(lines):
        if _OUTPUT_RE.match(line):
            out_idx = i  # take the LAST matching output line
    if out_idx is None:
        return "unsupported"
    if not confirm(f"Wire carta progress segment into {script_path}?"):
        return "declined"
    script_path.with_name(script_path.name + ".bak").write_text(text)
    new_lines = lines[:out_idx] + _SNIPPET_LINES + lines[out_idx:]
    trailing = "\n" if text.endswith("\n") else ""
    script_path.write_text("\n".join(new_lines) + trailing)
    return "installed"


def uninstall_from_script(script_path: Path) -> str:
    """Remove the carta marker block. Returns 'removed' or 'absent'."""
    script_path = Path(script_path)
    text = script_path.read_text()
    if MARKER_START not in text:
        return "absent"
    out, skipping = [], False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped == MARKER_START:
            skipping = True
            continue
        if stripped == MARKER_END:
            skipping = False
            continue
        if not skipping:
            out.append(line)
    trailing = "\n" if text.endswith("\n") else ""
    script_path.write_text("\n".join(out) + trailing)
    return "removed"


def offer_install(settings_path=None, *, interactive: bool = True) -> str:
    """Locate the user's status-line script and offer to wire in the segment.

    Returns 'installed' | 'already' | 'declined' | 'unsupported' | 'unavailable'.
    'unavailable' means no wireable script was found (nothing changed).
    """
    if settings_path is None:
        settings_path = Path.home() / ".claude" / "settings.json"
    script = find_statusline_script(settings_path)
    if script is None:
        return "unavailable"

    if not interactive:
        return "declined"

    def _confirm(msg):
        return input(f"{msg} [y/N] ").strip().lower() == "y"

    return install_into_script(script, confirm=_confirm)
