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
from carta.statusline import _pid_alive, _fmt_elapsed, _fmt_chunks, STALE_WINDOW_S

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


def _gather_check(name: str, qdrant_url: str, ollama_url: str | None) -> dict:
    out = {"qdrant": {"reachable": False}, "ollama": {"reachable": False}}
    try:
        from qdrant_client import QdrantClient
        client = QdrantClient(url=qdrant_url, timeout=2)
        prefix = f"{name}_"
        collections = {}
        for c in client.get_collections().collections:
            if c.name.startswith(prefix):
                # points_count is Optional[int] in qdrant-client (None while a
                # collection optimizes); coerce so renderers can always format it.
                collections[c.name] = client.get_collection(c.name).points_count or 0
        out["qdrant"] = {"reachable": True, "collections": collections}
    except Exception:
        out["qdrant"] = {"reachable": False}
    try:
        import requests
        url = (ollama_url or "").rstrip("/") + "/api/tags"
        resp = requests.get(url, timeout=2)
        out["ollama"] = {"reachable": resp.status_code == 200}
    except Exception:
        out["ollama"] = {"reachable": False}
    return out


def gather_project_status(repo_root, *, name: str, qdrant_url, check: bool = False,
                          ollama_url=None, now: float = None) -> dict:
    """Return a read-only status snapshot for one project. Never raises."""
    now = time.time() if now is None else now
    snap = {
        "name": name,
        "path": str(repo_root),
        "qdrant_url": qdrant_url,
        "embed": _gather_embed(repo_root, now),
        "corpus": _gather_corpus(repo_root),
        "check": None,
    }
    if check:
        snap["check"] = _gather_check(name, qdrant_url, ollama_url)
    return snap


# ---------------------------------------------------------------------------
# Rendering layer — pure string formatters, no I/O
# ---------------------------------------------------------------------------

_RESET = "\033[0m"
_DIM = "\033[2m"
_BOLD = "\033[1m"
_CYAN = "\033[36m"
_GREEN = "\033[32m"
_RED = "\033[31m"
_YELLOW = "\033[33m"


def _col(color: bool, code: str, text: str) -> str:
    return f"{code}{text}{_RESET}" if (color and code) else text


def _home_path(path: str) -> str:
    try:
        home = str(Path.home())
        if path.startswith(home):
            return "~" + path[len(home):]
    except Exception:
        pass
    return path


def _embed_line(e: dict, color: bool) -> str:
    st = e.get("state")
    if st == "running":
        parts = [f"{e.get('current_idx', 0)}/{e.get('total', 0)}"]
        if e.get("current_file"):
            parts.append(e["current_file"])
        if "file_elapsed_s" in e:
            parts.append(_fmt_elapsed(e["file_elapsed_s"]))
        return "embed   " + _col(color, _CYAN, "running") + " — " + " · ".join(parts)
    if st in ("done", "failed"):
        age = _fmt_elapsed(e.get("age_s", 0))
        summary = (f"{e.get('embedded', 0)} embedded, {e.get('skipped', 0)} skipped, "
                   f"{e.get('errors', 0)} errors ({_fmt_chunks(e.get('chunks', 0))} chunks)")
        if st == "done":
            return "embed   " + _col(color, _GREEN, "idle") + f" — last run {age} ago: {summary}"
        return "embed   " + _col(color, _RED, "failed") + f" — last run {age} ago: {summary}"
    if st == "interrupted":
        return "embed   " + _col(color, _YELLOW, "interrupted") + " — previous run did not finish"
    if st == "never":
        return "embed   " + _col(color, _DIM, "never run")
    return "embed   idle"


def _corpus_line(co: dict, color: bool) -> str:
    if co["total"] == 0:
        return "docs    none embedded yet"
    parts = [f"{co['total']} total", f"{co['done']} done"]
    if co["pending"]:
        parts.append(f"{co['pending']} pending")
    if co["stale"]:
        parts.append(f"{co['stale']} stale")
    if co["extraction_failed"]:
        parts.append(f"{co['extraction_failed']} extraction-failed")
    if co["other"]:
        parts.append(f"{co['other']} other")
    return "docs    " + " · ".join(parts)


def _qdrant_lines(snap: dict, color: bool) -> list:
    chk = snap.get("check")
    if not chk:
        return [f"qdrant  {snap['qdrant_url']}   (--check for live counts)"]
    lines = []
    q = chk.get("qdrant", {})
    if q.get("reachable"):
        cols = q.get("collections", {})
        if cols:
            body = " · ".join(f"{n} {p:,} pts" for n, p in cols.items())
            lines.append("qdrant  " + _col(color, _GREEN, "up") + " · " + body)
        else:
            lines.append("qdrant  " + _col(color, _GREEN, "up") + " · no collections for this project")
    else:
        lines.append("qdrant  " + _col(color, _RED, "down") + f" · {snap['qdrant_url']}")
    o = chk.get("ollama", {})
    lines.append("ollama  " + (_col(color, _GREEN, "up") if o.get("reachable")
                               else _col(color, _RED, "down")))
    return lines


def format_current(snap: dict, *, color: bool = True) -> str:
    """Render the detailed multi-line block for the current project."""
    header = _col(color, _BOLD, "carta · " + snap["name"]) + "   " + \
        _col(color, _DIM, _home_path(snap["path"]))
    lines = [header, "  " + _embed_line(snap["embed"], color),
             "  " + _corpus_line(snap["corpus"], color)]
    lines += ["  " + ln for ln in _qdrant_lines(snap, color)]
    return "\n".join(lines)


def format_other(snap: dict, *, color: bool = True) -> str:
    """Render the compact one-liner for a non-current project."""
    e = snap["embed"]
    co = snap["corpus"]
    st = e.get("state")
    word_plain = {"running": "running", "done": "idle", "failed": "failed",
                  "interrupted": "interrupted", "never": "never",
                  "idle": "idle"}.get(st, "idle")
    code = {"running": _CYAN, "failed": _RED, "interrupted": _YELLOW,
            "never": _DIM}.get(st, "")
    word = _col(color, code, f"{word_plain:<11}")
    if st == "running":
        summary = f"{e.get('current_idx', 0)}/{e.get('total', 0)} · {e.get('current_file') or 'embedding'}"
    else:
        parts = [f"{co['total']} docs"] if co["total"] else ["empty"]
        if co["pending"]:
            parts.append(f"{co['pending']} pending")
        if co["stale"]:
            parts.append(f"{co['stale']} stale")
        if co["total"] and not co["pending"] and not co["stale"]:
            parts.append("all done")
        summary = " · ".join(parts)
    return f"  {snap['name']:<14} {word} {summary}   {_home_path(snap['path'])}"
