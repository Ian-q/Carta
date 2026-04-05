"""Interactive progress reporting for carta embed and scan.

Auto-detects TTY vs plain mode. Pass a Progress instance into pipeline
functions; pass progress=None to suppress all output (used by MCP server
and tests).
"""

import os
import sys
import threading
import time
from typing import Optional

_SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

# ANSI escape codes
_RESET  = "\033[0m"
_BOLD   = "\033[1m"
_DIM    = "\033[2m"
_GREEN  = "\033[32m"
_RED    = "\033[31m"
_CYAN   = "\033[36m"
_CLR    = "\r\033[K"   # move to line start + clear to end

_TICK_INTERVAL = 0.1  # seconds between spinner redraws


class Progress:
    """Context manager for progress reporting during embed and scan.

    TTY mode: in-place spinner lines with ANSI color and a background thread
    that animates the spinner every 100ms regardless of how slow each step is.
    Plain mode: scrolling print statements (same as verbose=True output).

    Usage::

        with Progress(total=12) as p:
            p.file(idx=1, name="foo.pdf")
            p.step("extracting 10 pages")
            p.done(chunks=80, elapsed=5.2)
        p.summary(embedded=10, skipped=1, errors=1)
    """

    def __init__(self, total: int = 0):
        self._total = total
        self._idx = 0
        self._name = ""
        self._frame = 0
        self._start: Optional[float] = None
        self._tty = sys.stdout.isatty()
        self._no_color = "NO_COLOR" in os.environ
        self._active = False      # True while a \r spinner line is "open"
        self._scan_mode = False   # True when inside scan_step (affects redraw format)
        self._current_msg = ""    # last step/scan_step message, for background redraw

        # Background animation
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._spinner_thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    def __enter__(self) -> "Progress":
        if self._tty:
            self._stop_event.clear()
            self._spinner_thread = threading.Thread(
                target=self._tick, daemon=True, name="carta-spinner"
            )
            self._spinner_thread.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self._stop_event.set()
        if self._spinner_thread is not None:
            self._spinner_thread.join(timeout=0.5)
        with self._lock:
            if self._active and self._tty:
                sys.stdout.write("\n")
                sys.stdout.flush()
        return False  # never suppress exceptions

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _c(self, code: str, text: str) -> str:
        """Apply ANSI code unless plain mode or NO_COLOR."""
        if not self._tty or self._no_color:
            return text
        return f"{code}{text}{_RESET}"

    def _spin(self) -> str:
        """Advance and return next spinner character. Caller must hold _lock."""
        ch = _SPINNER_FRAMES[self._frame % len(_SPINNER_FRAMES)]
        self._frame += 1
        return ch

    def _elapsed(self) -> float:
        return time.monotonic() - self._start if self._start is not None else 0.0

    def _write_embed_line(self) -> None:
        """Redraw embed spinner line. Caller must hold _lock."""
        sp   = self._c(_CYAN, self._spin())
        idx  = self._c(_DIM, f"{self._idx}/{self._total}")
        name = self._c(_BOLD, self._name)
        sub  = self._c(_DIM, f"▸ {self._current_msg}")
        el   = self._c(_DIM, f"{self._elapsed():.0f}s")
        sys.stdout.write(f"{_CLR}{sp}  {idx}  {name}  {sub}  {el}")
        sys.stdout.flush()

    def _write_scan_line(self) -> None:
        """Redraw scan spinner line. Caller must hold _lock."""
        sp  = self._c(_CYAN, self._spin())
        lbl = self._c(_BOLD, "Scanning")
        sub = self._c(_DIM, self._current_msg)
        sys.stdout.write(f"{_CLR}{sp}  {lbl}  {sub}")
        sys.stdout.flush()

    def _tick(self) -> None:
        """Background thread: redraw spinner every _TICK_INTERVAL seconds."""
        while not self._stop_event.wait(_TICK_INTERVAL):
            with self._lock:
                if self._active and self._current_msg:
                    if self._scan_mode:
                        self._write_scan_line()
                    else:
                        self._write_embed_line()

    # ------------------------------------------------------------------
    # Embed progress API
    # ------------------------------------------------------------------

    def file(self, idx: int, name: str) -> None:
        """Signal that a new file is starting."""
        with self._lock:
            self._idx = idx
            self._name = name
            self._start = time.monotonic()
            self._scan_mode = False
        if not self._tty:
            print(f"  [{idx}/{self._total}] Embedding: {name} ...", flush=True)

    def step(self, msg: str) -> None:
        """Report a sub-step within the current file."""
        if self._tty:
            with self._lock:
                self._current_msg = msg
                self._active = True
                self._scan_mode = False
                self._write_embed_line()
        else:
            print(f"    {msg}", flush=True)

    def done(self, chunks: int, elapsed: float) -> None:
        """Signal that the current file completed successfully."""
        if self._tty:
            with self._lock:
                self._active = False
                self._current_msg = ""
                check    = self._c(_GREEN, "✓")
                idx      = self._c(_DIM,   f"{self._idx}/{self._total}")
                name     = self._c(_BOLD,  self._name)
                chunks_s = self._c(_DIM,   f"{chunks} chunks")
                el_s     = self._c(_DIM,   f"{elapsed:.1f}s")
                sys.stdout.write(f"{_CLR}{check}  {idx}  {name}  {chunks_s}  {el_s}\n")
                sys.stdout.flush()
        else:
            print(
                f"  [{self._idx}/{self._total}] OK: {self._name}"
                f" — {chunks} chunk(s) in {elapsed:.1f}s",
                flush=True,
            )

    def skip(self, reason: str) -> None:
        """Signal that the current file was skipped."""
        if self._tty:
            with self._lock:
                self._active = False
                self._current_msg = ""
                dash     = self._c(_DIM, "–")
                idx      = self._c(_DIM, f"{self._idx}/{self._total}")
                name     = self._c(_DIM, self._name)
                reason_s = self._c(_DIM, f"skipped: {reason}")
                sys.stdout.write(f"{_CLR}{dash}  {idx}  {name}  {reason_s}\n")
                sys.stdout.flush()
        else:
            print(
                f"  [{self._idx}/{self._total}] SKIP ({reason}): {self._name}",
                flush=True,
            )

    def error(self, msg: str) -> None:
        """Signal that the current file errored."""
        if self._tty:
            with self._lock:
                self._active = False
                self._current_msg = ""
                x   = self._c(_RED,  "✗")
                idx = self._c(_DIM,  f"{self._idx}/{self._total}")
                name = self._c(_BOLD, self._name)
                err = self._c(_RED,  f"ERROR: {msg}")
                sys.stderr.write(f"{_CLR}{x}  {idx}  {name}  {err}\n")
                sys.stderr.flush()
        else:
            print(
                f"  [{self._idx}/{self._total}] ERROR: {self._name}: {msg}",
                file=sys.stderr,
                flush=True,
            )

    def summary(self, embedded: int, skipped: int, errors: int) -> None:
        """Print final embed summary line."""
        if self._tty:
            parts = [
                self._c(_GREEN, f"Embedded: {embedded}"),
                self._c(_DIM,   f"Skipped: {skipped}"),
            ]
            if errors:
                parts.append(self._c(_RED, f"Errors: {errors}"))
            print("  ".join(parts), flush=True)
        else:
            print(
                f"Embedded: {embedded}, Skipped: {skipped}, Errors: {errors}",
                flush=True,
            )

    # ------------------------------------------------------------------
    # Scan progress API
    # ------------------------------------------------------------------

    def scan_step(self, msg: str) -> None:
        """Report the current scan check phase."""
        if self._tty:
            with self._lock:
                self._current_msg = msg
                self._active = True
                self._scan_mode = True
                self._write_scan_line()
        else:
            print(f"  {msg}", flush=True)

    def scan_done(self, elapsed: float, issue_count: int) -> None:
        """Print final scan summary line."""
        if self._tty:
            with self._lock:
                self._active = False
                self._current_msg = ""
                check = self._c(_GREEN, "✓")
                lbl   = self._c(_BOLD, "Scan complete")
                n     = self._c(_DIM if issue_count == 0 else _RED, f"{issue_count} issue(s)")
                el    = self._c(_DIM, f"{elapsed:.1f}s")
                sys.stdout.write(f"{_CLR}{check}  {lbl} — {n}  {el}\n")
                sys.stdout.flush()
        else:
            print(f"Scan complete — {issue_count} issue(s)", flush=True)
