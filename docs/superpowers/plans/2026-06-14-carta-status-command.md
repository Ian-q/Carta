---
id: 2026-06-14-carta-status-command
title: "`carta status` Command Implementation Plan"
status: shipped
related:
  - 2026-06-14-carta-status-command-design
date: 2026-06-14
---

# `carta status` Command Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `carta status` command that prints a quick snapshot of carta's state — the current project in detail (embed stage, file counts) plus every other carta project on the machine as one-liners — local-only by default with `--check` for live Qdrant/Ollama.

**Architecture:** A new `~/.carta/registry.json` (managed by `carta/registry.py`) records known projects as carta runs. `carta/status.py` gathers a read-only snapshot dict per project (reusing `statusline` and `induct` helpers) and renders it. `cmd_status` in `carta/cli.py` ties it together; `cmd_init`/`cmd_embed`/`cmd_status` upsert into the registry.

**Tech Stack:** Python 3.10+, stdlib (`json`, `os`, `socket`, `time`, `pathlib`), `PyYAML` (sidecars), `qdrant-client` + `requests` (only under `--check`), pytest + `monkeypatch`/`capsys`.

**Spec:** `docs/superpowers/specs/2026-06-14-carta-status-command-design.md`

## File Structure

- **Create** `carta/registry.py` — global project registry (`register_project`, `load_registry`) at `~/.carta/registry.json` (override via `CARTA_HOME`). Best-effort, never raises.
- **Create** `carta/status.py` — `gather_project_status()` (snapshot dict) + `format_current()` / `format_other()` renderers. Pure aggregation; reuses `carta.statusline` and `carta.embed.induct.read_sidecar`.
- **Create** `carta/tests/test_registry.py`, `carta/tests/test_status.py`.
- **Modify** `carta/cli.py` — `cmd_status` + `status` subparser + dispatch entry; best-effort `register_project` calls in `cmd_init` and `cmd_embed`.
- **Modify** `carta/tests/test_cli.py` — `TestCmdStatus` class.

---

### Task 1: Project registry (`carta/registry.py`)

**Files:**
- Create: `carta/registry.py`
- Test: `carta/tests/test_registry.py`

- [ ] **Step 1: Write the failing tests**

Create `carta/tests/test_registry.py`:

```python
import json
import shutil
from carta import registry


def _mk_project(tmp_path, name="proj"):
    root = tmp_path / name
    (root / ".carta").mkdir(parents=True)
    (root / ".carta" / "config.yaml").write_text(
        f"project_name: {name}\nqdrant_url: http://localhost:6333\n"
    )
    return root


def test_register_creates_file(tmp_path, monkeypatch):
    monkeypatch.setenv("CARTA_HOME", str(tmp_path / "home"))
    root = _mk_project(tmp_path)
    registry.register_project(root, "proj", "http://localhost:6333", now=100.0)
    data = json.loads((tmp_path / "home" / "registry.json").read_text())
    key = str(root.resolve())
    assert data["projects"][key]["name"] == "proj"
    assert data["projects"][key]["qdrant_url"] == "http://localhost:6333"
    assert data["projects"][key]["last_seen"] == 100.0


def test_register_updates_in_place(tmp_path, monkeypatch):
    monkeypatch.setenv("CARTA_HOME", str(tmp_path / "home"))
    root = _mk_project(tmp_path)
    registry.register_project(root, "proj", "u1", now=1.0)
    registry.register_project(root, "proj", "u2", now=2.0)
    entries = registry.load_registry()
    assert len(entries) == 1
    assert entries[0]["qdrant_url"] == "u2"
    assert entries[0]["last_seen"] == 2.0


def test_load_prunes_deleted_projects(tmp_path, monkeypatch):
    monkeypatch.setenv("CARTA_HOME", str(tmp_path / "home"))
    root = _mk_project(tmp_path, "gone")
    registry.register_project(root, "gone", "u", now=1.0)
    shutil.rmtree(root)
    assert registry.load_registry() == []


def test_load_missing_file_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("CARTA_HOME", str(tmp_path / "home"))
    assert registry.load_registry() == []


def test_register_recovers_from_corrupt_file(tmp_path, monkeypatch):
    monkeypatch.setenv("CARTA_HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    (tmp_path / "home" / "registry.json").write_text("{ not json")
    root = _mk_project(tmp_path)
    registry.register_project(root, "proj", "u", now=1.0)
    entries = registry.load_registry()
    assert len(entries) == 1
    assert entries[0]["name"] == "proj"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest carta/tests/test_registry.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'carta.registry'`

- [ ] **Step 3: Write the implementation**

Create `carta/registry.py`:

```python
"""Global registry of carta projects on this machine.

Records each project carta runs in so `carta status` can list projects beyond
the current directory — without crawling the filesystem. Lives at
``~/.carta/registry.json`` (override the home dir with ``CARTA_HOME``).

All functions are best-effort and never raise: a registry failure must never
affect the command that triggered it.
"""

import json
import os
import time
from pathlib import Path

SCHEMA = 1


def _carta_home() -> Path:
    override = os.environ.get("CARTA_HOME")
    return Path(override) if override else Path.home() / ".carta"


def registry_path() -> Path:
    return _carta_home() / "registry.json"


def _read_raw() -> dict:
    """Return a well-formed {schema, projects} dict; recover from missing/corrupt."""
    try:
        data = json.loads(registry_path().read_text())
        if isinstance(data, dict) and isinstance(data.get("projects"), dict):
            return data
    except Exception:
        pass
    return {"schema": SCHEMA, "projects": {}}


def _config_exists(path: str) -> bool:
    return (Path(path) / ".carta" / "config.yaml").exists()


def register_project(repo_root, name: str, qdrant_url, *, now: float = None) -> None:
    """Upsert a project (keyed by absolute root path), pruning dead entries.

    Best-effort: any error is swallowed.
    """
    try:
        key = str(Path(repo_root).resolve())
        data = _read_raw()
        projects = {p: e for p, e in data["projects"].items() if _config_exists(p)}
        projects[key] = {
            "name": name,
            "qdrant_url": qdrant_url,
            "last_seen": time.time() if now is None else now,
        }
        data["projects"] = projects
        data["schema"] = SCHEMA
        path = registry_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        os.replace(tmp, path)
    except Exception:
        pass


def load_registry() -> list[dict]:
    """Return live registry entries (pruning projects whose config is gone).

    Each entry: {"path", "name", "qdrant_url", "last_seen"}. Never raises.
    """
    data = _read_raw()
    out = []
    for path, entry in data["projects"].items():
        if not _config_exists(path):
            continue
        out.append({
            "path": path,
            "name": entry.get("name") or Path(path).name,
            "qdrant_url": entry.get("qdrant_url"),
            "last_seen": entry.get("last_seen") or 0,
        })
    return out
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest carta/tests/test_registry.py -v`
Expected: PASS (5 passed)

- [ ] **Step 5: Commit**

```bash
git add carta/registry.py carta/tests/test_registry.py
git commit -m "feat(status): project registry at ~/.carta/registry.json"
```

---

### Task 2: Snapshot gathering — corpus + embed state (`carta/status.py`)

**Files:**
- Create: `carta/status.py`
- Test: `carta/tests/test_status.py`

- [ ] **Step 1: Write the failing tests**

Create `carta/tests/test_status.py`:

```python
import json
import socket

import yaml

from carta import status


def _project(tmp_path):
    root = tmp_path / "proj"
    (root / ".carta" / "sidecars").mkdir(parents=True)
    return root


def _sidecar(sidecars_dir, stem, st):
    (sidecars_dir / f"{stem}.embed-meta.yaml").write_text(
        yaml.dump({"slug": stem, "status": st})
    )


def test_corpus_counts_by_status(tmp_path):
    root = _project(tmp_path)
    sc = root / ".carta" / "sidecars"
    _sidecar(sc, "a", "done")
    _sidecar(sc, "b", "done")
    _sidecar(sc, "c", "pending")
    _sidecar(sc, "d", "stale")
    _sidecar(sc, "e", "extraction_failed")
    _sidecar(sc, "f", "weird")
    snap = status.gather_project_status(root, name="proj", qdrant_url="u")
    assert snap["corpus"] == {
        "total": 6, "done": 2, "pending": 1, "stale": 1,
        "extraction_failed": 1, "other": 1,
    }


def test_corpus_empty_when_no_sidecars(tmp_path):
    root = _project(tmp_path)
    snap = status.gather_project_status(root, name="proj", qdrant_url="u")
    assert snap["corpus"]["total"] == 0
    assert snap["check"] is None
    assert snap["name"] == "proj"


def test_embed_state_never(tmp_path):
    root = _project(tmp_path)
    snap = status.gather_project_status(root, name="proj", qdrant_url="u")
    assert snap["embed"]["state"] == "never"


def test_embed_state_done_with_age(tmp_path):
    root = _project(tmp_path)
    (root / ".carta" / "embed-status.json").write_text(json.dumps({
        "phase": "done", "finished_at": 900.0,
        "embedded": 10, "skipped": 2, "errors": 0, "chunks": 50,
    }))
    snap = status.gather_project_status(root, name="proj", qdrant_url="u", now=1000.0)
    e = snap["embed"]
    assert e["state"] == "done"
    assert e["age_s"] == 100.0
    assert e["embedded"] == 10


def test_embed_state_interrupted_when_pid_dead(tmp_path, monkeypatch):
    root = _project(tmp_path)
    (root / ".carta" / "embed-status.json").write_text(json.dumps({
        "phase": "running", "host": "thishost", "pid": 424242,
        "current_idx": 3, "total": 9, "current_file": "x.pdf", "updated_at": 0.0,
    }))
    monkeypatch.setattr(status, "_pid_alive", lambda pid: False)
    monkeypatch.setattr(socket, "gethostname", lambda: "thishost")
    snap = status.gather_project_status(root, name="proj", qdrant_url="u", now=10_000.0)
    assert snap["embed"]["state"] == "interrupted"


def test_embed_state_running_via_live_lock(tmp_path, monkeypatch):
    root = _project(tmp_path)
    (root / ".carta" / "embed-status.json").write_text(json.dumps({
        "phase": "running", "host": "other", "pid": 1,
        "current_idx": 3, "total": 9, "current_file": "x.pdf",
        "current_file_started_at": 0.0, "updated_at": 0.0,
    }))
    (root / ".carta" / "embed.lock").write_text("4242")
    monkeypatch.setattr(status, "_pid_alive", lambda pid: True)
    snap = status.gather_project_status(root, name="proj", qdrant_url="u", now=100.0)
    assert snap["embed"]["state"] == "running"
    assert snap["embed"]["file_elapsed_s"] == 100.0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest carta/tests/test_status.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'carta.status'`

- [ ] **Step 3: Write the implementation**

Create `carta/status.py`:

```python
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
        return json.loads(Path(path).read_text())
    except Exception:
        return None


def _lock_alive(lock_path: Path) -> bool:
    try:
        pid = int(Path(lock_path).read_text().strip())
    except Exception:
        return False
    return pid > 0 and _pid_alive(pid)


def _gather_embed(repo_root: Path, now: float) -> dict:
    carta_dir = Path(repo_root) / ".carta"
    status = _read_json(carta_dir / "embed-status.json")
    lock_alive = _lock_alive(carta_dir / "embed.lock")

    if status is None:
        return {"state": "running" if lock_alive else "never"}

    hostname = socket.gethostname()
    pid = status.get("pid")
    host = status.get("host")

    def _alive() -> bool:
        if lock_alive:
            return True
        if host == hostname and pid:
            return _pid_alive(pid)
        return (now - float(status.get("updated_at") or 0)) <= STALE_WINDOW_S

    phase = status.get("phase")
    if phase == "running":
        state = "running" if _alive() else "interrupted"
    elif phase in ("done", "failed"):
        state = phase
    else:
        state = "idle"

    out = {
        "state": state,
        "current_idx": status.get("current_idx", 0),
        "total": status.get("total", 0),
        "current_file": status.get("current_file"),
        "embedded": status.get("embedded", 0),
        "skipped": status.get("skipped", 0),
        "errors": status.get("errors", 0),
        "chunks": status.get("chunks", 0),
    }
    started = status.get("current_file_started_at")
    if started:
        out["file_elapsed_s"] = max(0.0, now - float(started))
    finished = status.get("finished_at")
    if finished:
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest carta/tests/test_status.py -v`
Expected: PASS (6 passed)

- [ ] **Step 5: Commit**

```bash
git add carta/status.py carta/tests/test_status.py
git commit -m "feat(status): gather embed-state + corpus counts snapshot"
```

---

### Task 3: Snapshot gathering — `--check` Qdrant/Ollama (`carta/status.py`)

**Files:**
- Modify: `carta/status.py`
- Test: `carta/tests/test_status.py`

- [ ] **Step 1: Write the failing test**

Append to `carta/tests/test_status.py`:

```python
def test_check_populates_qdrant_and_ollama(tmp_path, monkeypatch):
    root = _project(tmp_path)

    class _Coll:
        def __init__(self, name):
            self.name = name

    class _Colls:
        collections = [_Coll("proj_doc"), _Coll("other_doc")]

    class _Info:
        points_count = 42

    class _FakeClient:
        def __init__(self, **kwargs):
            pass

        def get_collections(self):
            return _Colls()

        def get_collection(self, name):
            return _Info()

    import qdrant_client
    monkeypatch.setattr(qdrant_client, "QdrantClient", _FakeClient)

    class _Resp:
        status_code = 200

    import requests
    monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp())

    snap = status.gather_project_status(
        root, name="proj", qdrant_url="http://q", check=True, ollama_url="http://o"
    )
    assert snap["check"]["qdrant"]["reachable"] is True
    assert snap["check"]["qdrant"]["collections"] == {"proj_doc": 42}
    assert snap["check"]["ollama"]["reachable"] is True


def test_check_handles_unreachable_services(tmp_path, monkeypatch):
    root = _project(tmp_path)

    def _boom(*a, **k):
        raise ConnectionError("down")

    import qdrant_client
    monkeypatch.setattr(qdrant_client, "QdrantClient", _boom)
    import requests
    monkeypatch.setattr(requests, "get", _boom)

    snap = status.gather_project_status(
        root, name="proj", qdrant_url="http://q", check=True, ollama_url="http://o"
    )
    assert snap["check"]["qdrant"]["reachable"] is False
    assert snap["check"]["ollama"]["reachable"] is False
    # Local data still present despite service failures.
    assert snap["corpus"]["total"] == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest carta/tests/test_status.py::test_check_populates_qdrant_and_ollama -v`
Expected: FAIL — `assert None ...` (snap["check"] is None; no check branch yet)

- [ ] **Step 3: Add `_gather_check` and wire it into `gather_project_status`**

In `carta/status.py`, add this function above `gather_project_status`:

```python
def _gather_check(name: str, qdrant_url, ollama_url) -> dict:
    out = {"qdrant": {"reachable": False}, "ollama": {"reachable": False}}
    try:
        from qdrant_client import QdrantClient
        client = QdrantClient(url=qdrant_url, timeout=2)
        prefix = f"{name}_"
        collections = {}
        for c in client.get_collections().collections:
            if c.name.startswith(prefix):
                collections[c.name] = client.get_collection(c.name).points_count
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
```

Then replace the body of `gather_project_status` with:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest carta/tests/test_status.py -v`
Expected: PASS (8 passed)

- [ ] **Step 5: Commit**

```bash
git add carta/status.py carta/tests/test_status.py
git commit -m "feat(status): --check Qdrant point counts + Ollama health"
```

---

### Task 4: Snapshot renderers (`carta/status.py`)

**Files:**
- Modify: `carta/status.py`
- Test: `carta/tests/test_status.py`

- [ ] **Step 1: Write the failing tests**

Append to `carta/tests/test_status.py`:

```python
def test_format_current_done_plaintext():
    snap = {
        "name": "proj", "path": "/tmp/proj", "qdrant_url": "http://q",
        "embed": {"state": "done", "age_s": 120.0, "embedded": 10,
                  "skipped": 2, "errors": 0, "chunks": 2334},
        "corpus": {"total": 12, "done": 10, "pending": 2, "stale": 0,
                   "extraction_failed": 0, "other": 0},
        "check": None,
    }
    out = status.format_current(snap, color=False)
    assert "carta · proj" in out
    assert "embed   idle — last run 2m ago: 10 embedded, 2 skipped, 0 errors (2.3k chunks)" in out
    assert "docs    12 total · 10 done · 2 pending" in out
    assert "qdrant  http://q   (--check for live counts)" in out


def test_format_current_never_with_check():
    snap = {
        "name": "proj", "path": "/tmp/proj", "qdrant_url": "http://q",
        "embed": {"state": "never"},
        "corpus": {"total": 0, "done": 0, "pending": 0, "stale": 0,
                   "extraction_failed": 0, "other": 0},
        "check": {"qdrant": {"reachable": True, "collections": {"proj_doc": 12043}},
                  "ollama": {"reachable": True}},
    }
    out = status.format_current(snap, color=False)
    assert "embed   never run" in out
    assert "docs    none embedded yet" in out
    assert "qdrant  up · proj_doc 12,043 pts" in out
    assert "ollama  up" in out


def test_format_other_running_plaintext():
    snap = {
        "name": "some-repo", "path": "/tmp/some-repo", "qdrant_url": "u",
        "embed": {"state": "running", "current_idx": 42, "total": 350,
                  "current_file": "foo.pdf"},
        "corpus": {"total": 1, "done": 0, "pending": 1, "stale": 0,
                   "extraction_failed": 0, "other": 0},
        "check": None,
    }
    line = status.format_other(snap, color=False)
    assert "some-repo" in line
    assert "running" in line
    assert "42/350" in line
    assert "foo.pdf" in line


def test_format_other_idle_plaintext():
    snap = {
        "name": "doc-audit-cc", "path": "/tmp/doc-audit-cc", "qdrant_url": "u",
        "embed": {"state": "done", "age_s": 60.0, "embedded": 1, "skipped": 0,
                  "errors": 0, "chunks": 5},
        "corpus": {"total": 881, "done": 881, "pending": 0, "stale": 0,
                   "extraction_failed": 0, "other": 0},
        "check": None,
    }
    line = status.format_other(snap, color=False)
    assert "doc-audit-cc" in line
    assert "idle" in line
    assert "881 docs" in line
    assert "all done" in line
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest carta/tests/test_status.py::test_format_current_done_plaintext -v`
Expected: FAIL — `AttributeError: module 'carta.status' has no attribute 'format_current'`

- [ ] **Step 3: Add renderers to `carta/status.py`**

Add to `carta/status.py` (after the imports, the ANSI constants and helpers; the functions can go at the end of the file):

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest carta/tests/test_status.py -v`
Expected: PASS (12 passed)

- [ ] **Step 5: Commit**

```bash
git add carta/status.py carta/tests/test_status.py
git commit -m "feat(status): format_current + format_other renderers"
```

---

### Task 5: `carta status` CLI command + registry hooks (`carta/cli.py`)

**Files:**
- Modify: `carta/cli.py` (add `cmd_status`; register `status` subparser; add to `dispatch`; best-effort `register_project` in `cmd_init` and `cmd_embed`)
- Test: `carta/tests/test_cli.py` (new `TestCmdStatus` class)

- [ ] **Step 1: Write the failing tests**

Append to `carta/tests/test_cli.py`:

```python
class TestCmdStatus:
    """carta status: current project detail + other-project list."""

    def _project(self, tmp_path, name):
        root = tmp_path / name
        (root / ".carta" / "sidecars").mkdir(parents=True)
        (root / ".carta" / "config.yaml").write_text(
            f"project_name: {name}\nqdrant_url: http://localhost:6333\n"
        )
        return root

    def test_status_in_project_shows_detail_and_registers(self, tmp_path, monkeypatch, capsys):
        import argparse
        import json
        from carta.cli import cmd_status
        monkeypatch.setenv("CARTA_HOME", str(tmp_path / "home"))
        root = self._project(tmp_path, "alpha")
        monkeypatch.chdir(root)
        cmd_status(argparse.Namespace(check=False, json=False))
        out = capsys.readouterr().out
        assert "carta · alpha" in out
        reg = json.loads((tmp_path / "home" / "registry.json").read_text())
        assert any(e["name"] == "alpha" for e in reg["projects"].values())

    def test_status_lists_other_projects(self, tmp_path, monkeypatch, capsys):
        import argparse
        from carta.cli import cmd_status
        from carta.registry import register_project
        monkeypatch.setenv("CARTA_HOME", str(tmp_path / "home"))
        other = self._project(tmp_path, "beta")
        register_project(other, "beta", "http://localhost:6333", now=5.0)
        current = self._project(tmp_path, "alpha")
        monkeypatch.chdir(current)
        cmd_status(argparse.Namespace(check=False, json=False))
        out = capsys.readouterr().out
        assert "carta · alpha" in out
        assert "Other projects (1):" in out
        assert "beta" in out

    def test_status_outside_project_empty_registry(self, tmp_path, monkeypatch, capsys):
        import argparse
        from carta.cli import cmd_status
        monkeypatch.setenv("CARTA_HOME", str(tmp_path / "home"))
        empty = tmp_path / "nowhere"
        empty.mkdir()
        monkeypatch.chdir(empty)
        cmd_status(argparse.Namespace(check=False, json=False))
        out = capsys.readouterr().out
        assert "Not inside a carta project" in out

    def test_status_json_output(self, tmp_path, monkeypatch, capsys):
        import argparse
        import json
        from carta.cli import cmd_status
        monkeypatch.setenv("CARTA_HOME", str(tmp_path / "home"))
        root = self._project(tmp_path, "alpha")
        monkeypatch.chdir(root)
        cmd_status(argparse.Namespace(check=False, json=True))
        doc = json.loads(capsys.readouterr().out)
        assert doc["current"]["name"] == "alpha"
        assert doc["checked"] is False
        assert isinstance(doc["others"], list)

    def test_status_command_registered_in_help(self):
        result = run_carta(["status", "--help"])
        assert result.returncode == 0
        assert "--check" in result.stdout
        assert "--json" in result.stdout
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest carta/tests/test_cli.py::TestCmdStatus -v`
Expected: FAIL — `ImportError: cannot import name 'cmd_status' from 'carta.cli'`

- [ ] **Step 3: Add `cmd_status` to `carta/cli.py`**

In `carta/cli.py`, add this function (place it just before `def main():`):

```python
def cmd_status(args):
    """Print a quick snapshot of carta state: current project + other projects."""
    import json as _json
    from carta.config import load_config
    from carta.registry import register_project, load_registry
    from carta import status as status_mod

    check = getattr(args, "check", False)
    as_json = getattr(args, "json", False)
    color = sys.stdout.isatty() and not as_json

    try:
        cfg_path = find_config()
    except FileNotFoundError:
        cfg_path = None

    current = None
    current_path = None
    if cfg_path is not None:
        cfg = load_config(cfg_path)
        repo_root = cfg_path.parent.parent
        current_path = str(repo_root.resolve())
        name = cfg["project_name"]
        qdrant_url = cfg.get("qdrant_url")
        ollama_url = cfg.get("embed", {}).get("ollama_url", "http://localhost:11434")
        try:
            register_project(repo_root, name, qdrant_url)
        except Exception:
            pass
        current = status_mod.gather_project_status(
            repo_root, name=name, qdrant_url=qdrant_url,
            check=check, ollama_url=ollama_url,
        )

    others = []
    for entry in sorted(load_registry(), key=lambda e: e["last_seen"], reverse=True):
        if current_path and str(Path(entry["path"]).resolve()) == current_path:
            continue
        others.append(status_mod.gather_project_status(
            Path(entry["path"]), name=entry["name"], qdrant_url=entry["qdrant_url"],
        ))

    if as_json:
        print(_json.dumps(
            {"current": current, "others": others, "checked": bool(check)}, indent=2
        ))
        return

    if current is None and not others:
        print("Not inside a carta project, and none registered yet — "
              "run a carta command inside a project first.")
        return

    if current is not None:
        print(status_mod.format_current(current, color=color))
    if others:
        if current is not None:
            print()
        print(f"Other projects ({len(others)}):")
        for snap in others:
            print(status_mod.format_other(snap, color=color))
```

- [ ] **Step 4: Register the `status` subparser**

In `carta/cli.py`, inside `main()`, after the `import_p` block (just before `args = parser.parse_args()`), add:

```python
    status_p = sub.add_parser(
        "status",
        help="Show carta status for this project and other known projects",
    )
    status_p.add_argument(
        "--check", action="store_true",
        help="Also query Qdrant/Ollama for the current project (live counts + health)",
    )
    status_p.add_argument(
        "--json", action="store_true", help="Output status as JSON",
    )
```

- [ ] **Step 5: Add `status` to the dispatch table**

In `carta/cli.py`, in the `dispatch = {...}` dict inside `main()`, add the entry:

```python
        "status": cmd_status,
```

- [ ] **Step 6: Add best-effort registry hooks to `cmd_init` and `cmd_embed`**

In `cmd_embed`, immediately after the `doc_embed` module check (the block that ends with `sys.exit(1)` for the disabled module), insert:

```python
    # Best-effort: record this project in the global registry for `carta status`.
    try:
        from carta.registry import register_project
        register_project(cfg_path.parent.parent, cfg["project_name"], cfg.get("qdrant_url"))
    except Exception:
        pass
```

In `cmd_init`, just before the final `_notify_if_update()` call, insert:

```python
    # Best-effort: record the freshly-initialised project for `carta status`.
    try:
        from carta.config import load_config
        from carta.registry import register_project
        cp = find_config(Path.cwd())
        c = load_config(cp)
        register_project(cp.parent.parent, c["project_name"], c.get("qdrant_url"))
    except Exception:
        pass
```

- [ ] **Step 7: Run the new CLI tests to verify they pass**

Run: `python -m pytest carta/tests/test_cli.py::TestCmdStatus -v`
Expected: PASS (5 passed)

- [ ] **Step 8: Commit**

```bash
git add carta/cli.py carta/tests/test_cli.py
git commit -m "feat(status): carta status command + registry hooks on init/embed"
```

---

### Task 6: Full-suite verification + manual smoke test

**Files:** none (verification only)

- [ ] **Step 1: Run the entire test suite**

Run: `python -m pytest carta/tests/ -q`
Expected: All pass (previous green count + the new tests; no regressions, no errors).

- [ ] **Step 2: Manual smoke — status in this repo**

Run: `python -m carta.cli status`
Expected: a `carta · carta   ~/dev/doc-audit-cc` header, an `embed` line, a `docs` line, and a `qdrant ...   (--check for live counts)` line. No traceback.

- [ ] **Step 3: Manual smoke — JSON + outside a project**

Run: `python -m carta.cli status --json`
Expected: valid JSON with `current`, `others`, `checked: false`.

Run: `cd /tmp && python -m carta.cli status; cd -`
Expected: either the registered-project list (since this repo self-registered in Step 2) or the "Not inside a carta project" hint — never a traceback.

- [ ] **Step 4: Manual smoke — `--check` (services may be down)**

Run: `python -m carta.cli status --check`
Expected: the `qdrant` line shows `up · ... pts` if Qdrant is running, else `down · <url>`; an `ollama up/down` line appears. The command returns within a couple of seconds even when a service is down.

- [ ] **Step 5: Final commit (if any verification fixes were needed)**

```bash
git add -A
git commit -m "test(status): verification fixes" || echo "nothing to commit"
```

---

## Self-Review notes

- **Spec coverage:** D1 registry → Task 1 + Task 5 hooks; D2 local-default/`--check` → Tasks 2–3 + flag in Task 5; D3 `--check` current-only → `cmd_status` passes `check` only for current (Task 5); D4 current-detail/others-oneliner → Task 4 + Task 5; D5 reuse → `status.py` imports `statusline`/`induct`; D6 `--json` → Task 5. Embed states never/idle/running/done/failed/interrupted → `_gather_embed` (Task 2) + renderers (Task 4). Corpus buckets → Task 2. Not-in-project & empty-registry edges → Task 5 tests.
- **Placeholder scan:** none — every step has concrete code/commands.
- **Type consistency:** snapshot dict keys (`name`/`path`/`qdrant_url`/`embed`/`corpus`/`check`) and embed/corpus sub-keys are identical across `gather_project_status` (Tasks 2–3), the renderers (Task 4), and the CLI (Task 5). `register_project(repo_root, name, qdrant_url, *, now)` / `load_registry()` signatures consistent between Task 1 and Task 5.
