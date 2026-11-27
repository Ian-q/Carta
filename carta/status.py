"""On-demand carta status snapshot (the `carta status` command).

Gathers a read-only snapshot of a project's state — embed-run stage, corpus
file counts, and (optionally) live Qdrant/Ollama health — then renders it.
Pure aggregation over artifacts that already exist (.carta/embed-status.json,
.carta/embed.lock, sidecars); reuses carta.statusline + carta.embed.induct.
"""

import json
import os
import socket
import subprocess
import time
from pathlib import Path

from carta.embed.induct import read_sidecar
from carta.statusline import (
    _pid_alive, _fmt_elapsed, _fmt_chunks, STALE_WINDOW_S,
    _RESET, _DIM, _CYAN, _GREEN, _RED,
)

# The embed pipeline writes "embedded" on success (carta/embed/pipeline.py); "done"
# is accepted for legacy/forward compatibility. Both roll up to the "done" count.
_DONE_STATUSES = ("embedded", "done")
_CORPUS_STATUSES = ("pending", "stale", "extraction_failed", "no_text_content")


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
              "extraction_failed": 0, "no_text_content": 0, "other": 0,
              "flagged": 0, "awaiting_deep_scan": 0, "enrichment_stale": 0}
    sidecars = Path(repo_root) / ".carta" / "sidecars"
    if not sidecars.is_dir():
        return counts
    for sc in sidecars.rglob("*.embed-meta.yaml"):
        data = read_sidecar(sc)
        if not data:
            continue
        counts["total"] += 1
        st = data.get("status")
        if st in _DONE_STATUSES:
            counts["done"] += 1
        elif st in _CORPUS_STATUSES:
            counts[st] += 1
        else:
            counts["other"] += 1
        if data.get("priority") == "high":
            counts["flagged"] += 1
            if data.get("deep_scan") == "requested":
                counts["awaiting_deep_scan"] += 1
        rec = data.get("enrichment_source_hash")
        if rec and rec != data.get("file_hash", ""):
            counts["enrichment_stale"] += 1
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
# Project discovery beyond the registry (#117)
# ---------------------------------------------------------------------------
# The registry only knows projects a carta command has run in. Worktrees carry
# their own .carta/ (and so their own, possibly stale, qdrant_url), and a config
# that was copied in or hand-written is never registered at all.

# Directory names never worth descending into while scanning for configs.
_SCAN_PRUNE = frozenset({
    ".git", ".carta", "node_modules", ".venv", "venv", "__pycache__", ".tox",
    ".mypy_cache", ".pytest_cache", ".cache", ".npm", ".cargo", ".rustup",
    "Library", ".Trash", "site-packages", "dist", "build",
})
SCAN_MAX_DEPTH = 6


def _has_config(root: Path) -> bool:
    return (Path(root) / ".carta" / "config.yaml").is_file()


def read_project_config(root) -> tuple | None:
    """Return ``(project_name, qdrant_url)`` from a project's config, or None.

    Read raw so a half-written config still reports where it points. Never raises.
    """
    try:
        import yaml
        data = yaml.safe_load((Path(root) / ".carta" / "config.yaml").read_text())
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    name = data.get("project_name") or Path(root).name
    return str(name), data.get("qdrant_url")


def list_worktrees(repo_root) -> list:
    """Other git worktrees of ``repo_root`` that carry a .carta/config.yaml.

    Uses ``git worktree list --porcelain``; a non-git directory, a missing git,
    or a slow git yields []. ``repo_root`` itself is excluded.
    """
    try:
        out = subprocess.run(
            ["git", "-C", str(repo_root), "worktree", "list", "--porcelain"],
            capture_output=True, text=True, timeout=5,
        )
    except Exception:
        return []
    if out.returncode != 0:
        return []
    try:
        own = Path(repo_root).resolve()
    except OSError:
        own = Path(repo_root)
    found = []
    for line in out.stdout.splitlines():
        if not line.startswith("worktree "):
            continue
        wt = Path(line[len("worktree "):])
        try:
            if wt.resolve() == own:
                continue
        except OSError:
            continue
        if _has_config(wt):
            found.append(wt)
    return found


def scan_for_projects(root, max_depth: int = SCAN_MAX_DEPTH) -> list:
    """Walk ``root`` (to ``max_depth`` levels) for directories holding .carta/config.yaml.

    Hidden directories are walked (worktrees live under ``.worktrees/`` and
    ``.claude/worktrees/``); dependency, cache and VCS directories are pruned,
    and symlinks are not followed. Never raises.
    """
    root = Path(root).expanduser()
    if not root.is_dir():
        return []
    base_depth = len(root.parts)
    found = []
    for dirpath, dirnames, _files in os.walk(root, onerror=lambda e: None):
        here = Path(dirpath)
        if _has_config(here):
            found.append(here)
        if len(here.parts) - base_depth >= max_depth:
            dirnames[:] = []
            continue
        dirnames[:] = sorted(d for d in dirnames if d not in _SCAN_PRUNE)
    return found


def probe_qdrant(url) -> bool:
    """True if anything answers ``GET <url>/collections`` within 2 s.

    Any HTTP response counts as reachable (a 401 is a live server with an auth
    problem, not a dead endpoint); only a connection failure or timeout is not.
    """
    if not url:
        return False
    try:
        import requests
        requests.get(str(url).rstrip("/") + "/collections", timeout=2)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Rendering layer — pure string formatters, no I/O
# ---------------------------------------------------------------------------

_BOLD = "\033[1m"
_YELLOW = "\033[33m"


def _col(color: bool, code: str, text: str) -> str:
    return f"{code}{text}{_RESET}" if (color and code) else text


def _home_path(path: str) -> str:
    try:
        home = str(Path.home())
        if path == home or path.startswith(home + "/"):
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
    if co.get("no_text_content"):
        parts.append(f"{co['no_text_content']} no-text")
    if co["other"]:
        parts.append(f"{co['other']} other")
    return "docs    " + " · ".join(parts)


def _flag_line(co: dict, color: bool) -> str:
    if not (co.get("flagged") or co.get("enrichment_stale")):
        return ""
    parts = []
    if co.get("flagged"):
        parts.append(f"flagged {co['flagged']} ({co.get('awaiting_deep_scan', 0)} awaiting deep scan)")
    if co.get("enrichment_stale"):
        parts.append(f"enrichment stale: {co['enrichment_stale']}")
    return "flags   " + " · ".join(parts)


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
    header = (
        _col(color, _BOLD, "carta · " + snap["name"])
        + "   "
        + _col(color, _DIM, _home_path(snap["path"]))
    )
    lines = [header, "  " + _embed_line(snap["embed"], color),
             "  " + _corpus_line(snap["corpus"], color)]
    flag_line = _flag_line(snap["corpus"], color)
    if flag_line:
        lines.append("  " + flag_line)
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
    line = f"  {snap['name']:<14} {word} {summary}   {_home_path(snap['path'])}"
    source = snap.get("source")
    if source in ("worktree", "unregistered"):
        line += "  " + _col(color, _YELLOW, f"[{source}]")
    if snap.get("qdrant_url"):
        line += "\n  " + " " * 15 + _col(color, _DIM, f"qdrant {snap['qdrant_url']}")
        reachable = snap.get("endpoint_reachable")
        if reachable is False:
            line += " " + _col(color, _RED, "unreachable")
        elif reachable is True:
            line += " " + _col(color, _GREEN, "up")
    return line
