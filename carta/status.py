"""On-demand carta status snapshot (the `carta status` command).

Gathers a read-only snapshot of a project's state — embed-run stage, corpus
file counts, and (optionally) live Qdrant/Ollama health — then renders it.
Pure aggregation over artifacts that already exist (.carta/embed-status.json,
.carta/embed.lock, sidecars); reuses carta.statusline + carta.embed.induct.
"""

import json
import socket
import time
from pathlib import Path

from carta.embed.induct import read_sidecar
from carta.statusline import _pid_alive, STALE_WINDOW_S

_CORPUS_STATUSES = ("done", "pending", "stale", "extraction_failed")


def _read_json(path: Path):
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def _lock_alive(lock_path: Path) -> bool:
    try:
        pid = int(lock_path.read_text().strip())
    except Exception:
        return False
    return pid > 0 and _pid_alive(pid)


def _gather_embed(repo_root: Path, now: float) -> dict:
    carta_dir = Path(repo_root) / ".carta"
    embed_status = _read_json(carta_dir / "embed-status.json")
    lock_alive = _lock_alive(carta_dir / "embed.lock")

    if embed_status is None:
        return {"state": "running" if lock_alive else "never"}

    hostname = socket.gethostname()
    pid = embed_status.get("pid")
    host = embed_status.get("host")

    def _alive() -> bool:
        if lock_alive:
            return True
        if host == hostname and pid:
            return _pid_alive(pid)
        return (now - float(embed_status.get("updated_at") or 0)) <= STALE_WINDOW_S

    phase = embed_status.get("phase")
    if phase == "running":
        state = "running" if _alive() else "interrupted"
    elif phase in ("done", "failed"):
        state = phase
    else:
        state = "idle"

    out = {
        "state": state,
        "current_idx": embed_status.get("current_idx", 0),
        "total": embed_status.get("total", 0),
        "current_file": embed_status.get("current_file"),
        "embedded": embed_status.get("embedded", 0),
        "skipped": embed_status.get("skipped", 0),
        "errors": embed_status.get("errors", 0),
        "chunks": embed_status.get("chunks", 0),
    }
    started = embed_status.get("current_file_started_at")
    if started is not None:
        out["file_elapsed_s"] = max(0.0, now - float(started))
    finished = embed_status.get("finished_at")
    if finished is not None:
        out["finished_at"] = finished
        out["age_s"] = max(0.0, now - float(finished))
    return out


def _gather_corpus(repo_root: Path) -> dict:
    counts = {"total": 0, "done": 0, "pending": 0, "stale": 0,
              "extraction_failed": 0, "other": 0}
    sidecars = Path(repo_root) / ".carta" / "sidecars"
    if not sidecars.is_dir():
        return counts
    for sc in sidecars.rglob("*.embed-meta.yaml"):
        data = read_sidecar(sc)
        if not data:
            continue
        counts["total"] += 1
        st = data.get("status")
        if st in _CORPUS_STATUSES:
            counts[st] += 1
        else:
            counts["other"] += 1
    return counts


def gather_project_status(repo_root, *, name: str, qdrant_url, check: bool = False,
                          ollama_url=None, now: float = None) -> dict:
    """Return a read-only status snapshot for one project. Never raises."""
    now = time.time() if now is None else now
    return {
        "name": name,
        "path": str(repo_root),
        "qdrant_url": qdrant_url,
        "embed": _gather_embed(repo_root, now),
        "corpus": _gather_corpus(repo_root),
        "check": None,
    }
