"""Live status file for the carta status-line progress widget.

`carta embed` writes ``.carta/embed-status.json`` as it works; the
``carta statusline`` command reads it. Writes are atomic (temp + os.replace)
and best-effort: a status-write failure must never abort an embed.
"""

import json
import os
import socket
import sys
import time
from pathlib import Path

STATUS_FILENAME = "embed-status.json"
SCHEMA = 1


class StatusWriter:
    """Owns the embed-status.json lifecycle for one embed run.

    Pass ``enabled=False`` (e.g. cfg ``embed.status_file`` off, or tests/MCP)
    to make every method a no-op that writes nothing.
    """

    def __init__(self, repo_root: Path, enabled: bool = True):
        self.path = Path(repo_root) / ".carta" / STATUS_FILENAME
        self.enabled = enabled
        self._state: dict = {}

    def start(self, total: int) -> None:
        if not self.enabled:
            return
        now = time.time()
        self._state = {
            "schema": SCHEMA,
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "phase": "running",
            "started_at": now,
            "updated_at": now,
            "finished_at": None,
            "total": total,
            "current_idx": 0,
            "current_file": None,
            "current_file_started_at": None,
            "embedded": 0,
            "skipped": 0,
            "errors": 0,
            "chunks": 0,
        }
        self._write()

    def file_start(self, idx: int, filename: str) -> None:
        if not self.enabled or not self._state:
            return
        now = time.time()
        self._state["current_idx"] = idx
        self._state["current_file"] = os.path.basename(filename)
        self._state["current_file_started_at"] = now
        self._state["updated_at"] = now
        self._write()

    def file_done(self, *, embedded: int = 0, skipped: int = 0,
                  errors: int = 0, chunks: int = 0) -> None:
        if not self.enabled or not self._state:
            return
        self._state["embedded"] += embedded
        self._state["skipped"] += skipped
        self._state["errors"] += errors
        self._state["chunks"] += chunks
        self._state["updated_at"] = time.time()
        self._write()

    def finish(self, phase: str = "done") -> None:
        if not self.enabled or not self._state:
            return
        now = time.time()
        self._state["phase"] = phase
        self._state["finished_at"] = now
        self._state["updated_at"] = now
        self._state["current_file"] = None
        self._write()

    def _write(self) -> None:
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        try:
            with tmp.open("w") as f:
                json.dump(self._state, f)
            os.replace(tmp, self.path)
        except Exception as exc:  # best-effort: never abort the embed
            print(f"Warning: status file write failed: {exc}",
                  file=sys.stderr, flush=True)
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
