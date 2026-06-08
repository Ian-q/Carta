# Carta status-line progress widget — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Show live `carta embed` progress as a compact segment in Claude Code's status line, fed by a status file the embed pipeline writes and read by a fast `carta statusline` command.

**Architecture:** `carta embed` atomically writes `.carta/embed-status.json` as it works (Components 1–2). A new `carta statusline` subcommand reads that file, resolves liveness (PID + finish-flash window), and prints a one-segment widget (Component 3). `carta init` / `carta statusline --install` wires a marker-guarded snippet into the user's existing status-line script (Component 4). The three pieces are decoupled entirely through the on-disk file.

**Tech Stack:** Python 3.10+, stdlib only (`json`, `os`, `socket`, `time`, `shlex`, `pathlib`), pytest + `unittest.mock`. Follows existing carta patterns (best-effort writes like `_write_perf_log_entry`, argparse subcommands, `DEFAULTS` deep-merge config).

**Spec:** `docs/superpowers/specs/2026-06-06-statusline-progress-widget-design.md`

---

## File Structure

- **Create `carta/embed/status.py`** — `StatusWriter` class: owns the `.carta/embed-status.json` lifecycle (atomic writes, counters, best-effort).
- **Create `carta/statusline.py`** — the reader/printer + installer. Pure helpers (`resolve_state`, `format_segment`, formatters), IO (`read_status`, `_pid_alive`, `print_segment`), and wiring (`find_statusline_script`, `install_into_script`, `uninstall_from_script`, `offer_install`).
- **Modify `carta/config.py`** — add `embed.status_file: True` default.
- **Modify `carta/embed/pipeline.py`** — instantiate `StatusWriter` in `run_embed` and call it at the lifecycle points.
- **Modify `carta/cli.py`** — register `statusline` subcommand (`--install`/`--uninstall`), add `cmd_statusline`, wire into dispatch.
- **Modify `carta/cli.py::cmd_init`** — offer status-line wiring after bootstrap.
- **Create tests:** `carta/tests/test_status.py`, `carta/tests/test_statusline.py`, `carta/tests/test_statusline_install.py`.

Why this split: the writer (embed-side) and the reader (status-line-side) never share a process, so they live in separate modules. `statusline.py` keeps pure rendering separate from IO and from file-editing so each is independently testable.

---

## Task 1: Config flag `embed.status_file`

**Files:**
- Modify: `carta/config.py` (the `DEFAULTS["embed"]` block, near `"file_timeout_s": 600,`)
- Test: `carta/tests/test_config.py`

- [ ] **Step 1: Write the failing test**

Add to `carta/tests/test_config.py`:

```python
def test_status_file_default_enabled():
    from carta.config import DEFAULTS
    assert DEFAULTS["embed"]["status_file"] is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest carta/tests/test_config.py::test_status_file_default_enabled -v`
Expected: FAIL with `KeyError: 'status_file'`.

- [ ] **Step 3: Add the default**

In `carta/config.py`, inside the `"embed": { ... }` dict, immediately after the line `"file_timeout_s": 600,  # seconds allowed per file; raise for large/dense PDFs` add:

```python
        "status_file": True,  # write .carta/embed-status.json for the status-line widget
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest carta/tests/test_config.py::test_status_file_default_enabled -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add carta/config.py carta/tests/test_config.py
git commit -m "feat(embed): add status_file config default for status-line widget"
```

---

## Task 2: `StatusWriter` (status file lifecycle)

**Files:**
- Create: `carta/embed/status.py`
- Test: `carta/tests/test_status.py`

- [ ] **Step 1: Write the failing tests**

Create `carta/tests/test_status.py`:

```python
import json
import os
import socket

from carta.embed.status import StatusWriter, STATUS_FILENAME


def _read(repo_root):
    p = repo_root / ".carta" / STATUS_FILENAME
    return json.loads(p.read_text())


def test_start_writes_running_with_identity(tmp_path):
    (tmp_path / ".carta").mkdir()
    sw = StatusWriter(tmp_path, enabled=True)
    sw.start(total=3)
    data = _read(tmp_path)
    assert data["schema"] == 1
    assert data["phase"] == "running"
    assert data["total"] == 3
    assert data["pid"] == os.getpid()
    assert data["host"] == socket.gethostname()
    assert data["embedded"] == 0 and data["chunks"] == 0
    assert data["finished_at"] is None


def test_file_start_sets_current(tmp_path):
    (tmp_path / ".carta").mkdir()
    sw = StatusWriter(tmp_path, enabled=True)
    sw.start(total=2)
    sw.file_start(1, "big.pdf")
    data = _read(tmp_path)
    assert data["current_idx"] == 1
    assert data["current_file"] == "big.pdf"
    assert isinstance(data["current_file_started_at"], float)


def test_file_done_accumulates_counters(tmp_path):
    (tmp_path / ".carta").mkdir()
    sw = StatusWriter(tmp_path, enabled=True)
    sw.start(total=2)
    sw.file_start(1, "a.md")
    sw.file_done(embedded=1, chunks=10)
    sw.file_start(2, "b.md")
    sw.file_done(skipped=1)
    data = _read(tmp_path)
    assert data["embedded"] == 1
    assert data["skipped"] == 1
    assert data["chunks"] == 10


def test_finish_sets_phase_and_finished_at(tmp_path):
    (tmp_path / ".carta").mkdir()
    sw = StatusWriter(tmp_path, enabled=True)
    sw.start(total=1)
    sw.finish("done")
    data = _read(tmp_path)
    assert data["phase"] == "done"
    assert isinstance(data["finished_at"], float)
    assert data["current_file"] is None


def test_disabled_writes_nothing(tmp_path):
    (tmp_path / ".carta").mkdir()
    sw = StatusWriter(tmp_path, enabled=False)
    sw.start(total=1)
    sw.file_start(1, "x.md")
    sw.finish("done")
    assert not (tmp_path / ".carta" / STATUS_FILENAME).exists()


def test_write_failure_is_swallowed(tmp_path):
    # .carta missing -> parent dir doesn't exist; must not raise
    sw = StatusWriter(tmp_path, enabled=True)
    sw.start(total=1)  # should not raise even though .carta/ is absent
    sw.finish("done")


def test_no_tmp_file_left_behind(tmp_path):
    (tmp_path / ".carta").mkdir()
    sw = StatusWriter(tmp_path, enabled=True)
    sw.start(total=1)
    sw.finish("done")
    leftovers = list((tmp_path / ".carta").glob("*.tmp"))
    assert leftovers == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest carta/tests/test_status.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'carta.embed.status'`.

- [ ] **Step 3: Implement `StatusWriter`**

Create `carta/embed/status.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest carta/tests/test_status.py -v`
Expected: PASS (all 7).

- [ ] **Step 5: Commit**

```bash
git add carta/embed/status.py carta/tests/test_status.py
git commit -m "feat(embed): StatusWriter for atomic embed-status.json lifecycle"
```

---

## Task 3: Wire `StatusWriter` into `run_embed`

**Files:**
- Modify: `carta/embed/pipeline.py` (`run_embed`, the block from `perf_log_path = _resolve_perf_log_path(repo_root)` through `return summary`)
- Test: `carta/tests/test_pipeline.py`

Context: `run_embed` already computes `perf_log_path`/`perf_context`, then `pending`, `total`, then loops with four terminal branches (LFS skip, timeout, error, ok), then emits a stale alert and `return summary`. We add `StatusWriter` calls mirroring the perf-log calls, and wrap the loop so a crash still finalizes the file.

- [ ] **Step 1: Write the failing test**

Add to `carta/tests/test_pipeline.py`:

```python
def test_run_embed_writes_status_done(tmp_path, monkeypatch):
    """run_embed should leave a phase=done status file when status_file is on."""
    import json
    from carta.embed import pipeline
    from carta.embed.status import STATUS_FILENAME

    (tmp_path / ".carta").mkdir()

    # Stub out everything run_embed does except the status lifecycle.
    monkeypatch.setattr(pipeline, "migrate_sidecars", lambda *a, **k: None)
    monkeypatch.setattr(pipeline, "_heal_sidecar_current_paths", lambda *a, **k: None)
    monkeypatch.setattr(pipeline, "detect_orphaned_sidecars", lambda *a, **k: [])
    monkeypatch.setattr(pipeline, "discover_pending_files", lambda *a, **k: [])
    monkeypatch.setattr(pipeline, "discover_stale_files", lambda *a, **k: [])
    monkeypatch.setattr(pipeline, "ensure_collection", lambda *a, **k: None)
    monkeypatch.setattr(pipeline, "collection_name", lambda *a, **k: "t_doc")

    class _FakeClient:
        def __init__(self, *a, **k): pass
        def get_collections(self): return None
    monkeypatch.setattr(pipeline, "QdrantClient", _FakeClient)

    cfg = {"qdrant_url": "http://x", "embed": {"status_file": True},
           "modules": {}, "docs_root": "docs/"}
    pipeline.run_embed(tmp_path, cfg, verbose=False, progress=None)

    data = json.loads((tmp_path / ".carta" / STATUS_FILENAME).read_text())
    assert data["phase"] == "done"
    assert data["total"] == 0


def test_run_embed_status_disabled(tmp_path, monkeypatch):
    from carta.embed import pipeline
    from carta.embed.status import STATUS_FILENAME

    (tmp_path / ".carta").mkdir()
    monkeypatch.setattr(pipeline, "migrate_sidecars", lambda *a, **k: None)
    monkeypatch.setattr(pipeline, "_heal_sidecar_current_paths", lambda *a, **k: None)
    monkeypatch.setattr(pipeline, "detect_orphaned_sidecars", lambda *a, **k: [])
    monkeypatch.setattr(pipeline, "discover_pending_files", lambda *a, **k: [])
    monkeypatch.setattr(pipeline, "discover_stale_files", lambda *a, **k: [])
    monkeypatch.setattr(pipeline, "ensure_collection", lambda *a, **k: None)
    monkeypatch.setattr(pipeline, "collection_name", lambda *a, **k: "t_doc")

    class _FakeClient:
        def __init__(self, *a, **k): pass
        def get_collections(self): return None
    monkeypatch.setattr(pipeline, "QdrantClient", _FakeClient)

    cfg = {"qdrant_url": "http://x", "embed": {"status_file": False},
           "modules": {}, "docs_root": "docs/"}
    pipeline.run_embed(tmp_path, cfg, verbose=False, progress=None)
    assert not (tmp_path / ".carta" / STATUS_FILENAME).exists()
```

> Note: if `test_pipeline.py` already stubs these symbols via a shared fixture, reuse it instead of re-stubbing — check the top of the file first. The names above match the imports in `carta/embed/pipeline.py`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest carta/tests/test_pipeline.py::test_run_embed_writes_status_done carta/tests/test_pipeline.py::test_run_embed_status_disabled -v`
Expected: FAIL — no status file is written (`FileNotFoundError` / assertion error).

- [ ] **Step 3: Add the import**

At the top of `carta/embed/pipeline.py`, in the local-imports group (after the line `from carta.embed.induct import generate_sidecar_stub, read_sidecar, write_sidecar, sidecar_path`) add:

```python
from carta.embed.status import StatusWriter
```

- [ ] **Step 4: Instantiate and start the writer**

In `run_embed`, find:

```python
    perf_log_path = _resolve_perf_log_path(repo_root)
    perf_context = _build_perf_context(cfg)

    pending = discover_pending_files(repo_root)
    total = len(pending)
    if progress is not None:
        progress.set_total(total)
    if verbose:
        print(f"carta embed: {total} file(s) pending.", flush=True)
```

Replace it with:

```python
    perf_log_path = _resolve_perf_log_path(repo_root)
    perf_context = _build_perf_context(cfg)

    status = StatusWriter(
        repo_root, enabled=cfg.get("embed", {}).get("status_file", True)
    )

    pending = discover_pending_files(repo_root)
    total = len(pending)
    if progress is not None:
        progress.set_total(total)
    if verbose:
        print(f"carta embed: {total} file(s) pending.", flush=True)
    status.start(total)
```

- [ ] **Step 5: Wrap the loop and finalize**

The current code is:

```python
    for idx, file_info in enumerate(pending, start=1):
        file_path: Path = file_info["file_path"]
        ...
                "vision_strategies": _summarize_vision_strategies(vision_events),
            })

    # Emit stale alert after embed loop
    stale_count = len(discover_stale_files(repo_root))
```

Wrap only the `for` loop in `try/except` so a crash finalizes the file as `failed`. Change the `for` line and add the `except` block right before the `# Emit stale alert` comment:

```python
    try:
        for idx, file_info in enumerate(pending, start=1):
            file_path: Path = file_info["file_path"]
            ...                       # (loop body unchanged except the file_start/file_done calls below)
                    "vision_strategies": _summarize_vision_strategies(vision_events),
                })
    except BaseException:
        status.finish("failed")
        raise

    # Emit stale alert after embed loop
    stale_count = len(discover_stale_files(repo_root))
```

(Indent the existing loop body one level to sit under `try:`.)

Then change the final lines of `run_embed` from:

```python
    if alert_msg:
        print(alert_msg, flush=True)

    return summary
```

to:

```python
    if alert_msg:
        print(alert_msg, flush=True)

    status.finish("done")
    return summary
```

- [ ] **Step 6: Add `file_start` / `file_done` calls in the loop**

Inside the loop body (now under `try:`), make these four edits:

(a) Add `file_start` once, right after `rel_file` is computed. Find:

```python
        rel_file = str(file_path.relative_to(repo_root)) if file_path.is_relative_to(repo_root) else str(file_path)

        # LFS guard
        if is_lfs_pointer(file_path):
```

Insert the `status.file_start` line:

```python
        rel_file = str(file_path.relative_to(repo_root)) if file_path.is_relative_to(repo_root) else str(file_path)

        status.file_start(idx, file_path.name)

        # LFS guard
        if is_lfs_pointer(file_path):
```

(b) LFS branch — find the `_write_perf_log_entry(... "skip_reason": "lfs_pointer" ...)` call and add `status.file_done` right before its `continue`:

```python
            _write_perf_log_entry(perf_log_path, {
                **perf_context, "file": rel_file, "status": "skip",
                "skip_reason": "lfs_pointer", "chunks": 0, "elapsed_s": 0.0,
            })
            status.file_done(skipped=1)
            continue
```

(c) Timeout branch — after the `_write_perf_log_entry(... "status": "timeout" ...)` call, add:

```python
            _write_perf_log_entry(perf_log_path, {
                **perf_context, "file": rel_file, "status": "timeout",
                "chunks": 0, "elapsed_s": round(elapsed, 2),
                "timeout_s": file_timeout_s,
            })
            status.file_done(skipped=1)
```

(d) Error branch — after the `_write_perf_log_entry(... "status": "error" ...)` call, add:

```python
            _write_perf_log_entry(perf_log_path, {
                **perf_context, "file": rel_file, "status": "error",
                "chunks": 0, "elapsed_s": round(elapsed, 2),
                "error": str(exc)[:200],
            })
            status.file_done(errors=1)
```

(e) OK branch — after the `_write_perf_log_entry(... "status": "ok" ...)` call (the last `_write_perf_log_entry` in the loop), add:

```python
            _write_perf_log_entry(perf_log_path, {
                **perf_context, "file": rel_file, "status": "ok",
                "chunks": count, "elapsed_s": round(elapsed, 2),
                "vision_strategies": _summarize_vision_strategies(vision_events),
            })
            status.file_done(embedded=1, chunks=count)
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `python -m pytest carta/tests/test_pipeline.py -v`
Expected: PASS, including the two new tests. (Confirm no pre-existing pipeline tests broke.)

- [ ] **Step 8: Commit**

```bash
git add carta/embed/pipeline.py carta/tests/test_pipeline.py
git commit -m "feat(embed): write live embed-status.json from run_embed"
```

---

## Task 4: status-line render logic (pure)

**Files:**
- Create: `carta/statusline.py`
- Test: `carta/tests/test_statusline.py`

This task implements only the pure, IO-free functions so they're trivially testable. IO and install come in Tasks 5–6.

- [ ] **Step 1: Write the failing tests**

Create `carta/tests/test_statusline.py`:

```python
import re

from carta import statusline as sl

_STRIP = re.compile(r"\x1b\[[0-9;]*m")


def _plain(s):
    return _STRIP.sub("", s)


def _running_status(**over):
    base = {
        "schema": 1, "phase": "running", "host": "h", "pid": 123,
        "total": 47, "current_idx": 24, "current_file": "EN_UM_N32WB03x.pdf",
        "current_file_started_at": 1000.0, "updated_at": 1000.0,
        "started_at": 0.0, "finished_at": None,
        "embedded": 23, "skipped": 1, "errors": 0, "chunks": 2334,
    }
    base.update(over)
    return base


def test_fmt_elapsed():
    assert sl._fmt_elapsed(4) == "4s"
    assert sl._fmt_elapsed(125) == "2m"
    assert sl._fmt_elapsed(3 * 3600 + 5 * 60) == "3h5m"
    assert sl._fmt_elapsed(-3) == "0s"


def test_fmt_chunks():
    assert sl._fmt_chunks(0) == "0"
    assert sl._fmt_chunks(999) == "999"
    assert sl._fmt_chunks(2334) == "2.3k"


def test_resolve_running_alive():
    st = _running_status()
    assert sl.resolve_state(st, now=1000.0, hostname="h",
                            pid_alive_fn=lambda p: True) == "running"


def test_resolve_running_dead_pid_is_idle():
    st = _running_status()
    assert sl.resolve_state(st, now=1000.0, hostname="h",
                            pid_alive_fn=lambda p: False) == "idle"


def test_resolve_running_other_host_uses_updated_at():
    st = _running_status(host="other", updated_at=1000.0)
    # within window -> running; pid_alive_fn must NOT be consulted for other host
    assert sl.resolve_state(st, now=1030.0, hostname="h",
                            pid_alive_fn=lambda p: (_ for _ in ()).throw(AssertionError())) == "running"
    assert sl.resolve_state(st, now=2000.0, hostname="h",
                            pid_alive_fn=lambda p: False) == "idle"


def test_resolve_done_flash_then_idle():
    st = _running_status(phase="done", finished_at=1000.0)
    assert sl.resolve_state(st, now=1010.0, hostname="h", pid_alive_fn=lambda p: True) == "done"
    assert sl.resolve_state(st, now=1100.0, hostname="h", pid_alive_fn=lambda p: True) == "idle"


def test_resolve_failed_flash():
    st = _running_status(phase="failed", finished_at=1000.0)
    assert sl.resolve_state(st, now=1010.0, hostname="h", pid_alive_fn=lambda p: True) == "failed"


def test_format_running_plain():
    st = _running_status()
    out = sl.format_segment(st, "running", now=1000.0 + 19 * 60, color=False)
    assert out == "⠋ carta 24/47  EN_UM_N32WB03x.pdf  19m" or out.startswith("⠋ carta 24/47")
    # spinner frame varies with now; assert the stable parts:
    assert "carta 24/47" in out
    assert "EN_UM_N32WB03x.pdf" in out
    assert "19m" in out


def test_format_long_filename_truncated():
    st = _running_status(current_file="a-really-extremely-long-document-name-that-overflows.pdf")
    out = sl.format_segment(st, "running", now=1000.0, color=False)
    assert "…" in out


def test_format_done_plain():
    st = _running_status(phase="done", embedded=45, skipped=1, chunks=2334)
    out = sl.format_segment(st, "done", now=1.0, color=False)
    assert out == "✓ carta 46 files · 2.3k chunks"


def test_format_failed_plain():
    st = _running_status(phase="failed", current_idx=12, total=47, errors=3)
    out = sl.format_segment(st, "failed", now=1.0, color=False)
    assert out == "✗ carta 12/47 · 3 errors"


def test_format_idle_is_empty():
    assert sl.format_segment(_running_status(), "idle", now=1.0, color=False) == ""


def test_format_color_includes_ansi():
    st = _running_status()
    out = sl.format_segment(st, "running", now=1000.0, color=True)
    assert "\x1b[" in out
    assert "carta 24/47" in _plain(out)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest carta/tests/test_statusline.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'carta.statusline'`.

- [ ] **Step 3: Implement the pure functions**

Create `carta/statusline.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest carta/tests/test_statusline.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add carta/statusline.py carta/tests/test_statusline.py
git commit -m "feat(statusline): pure render + state-resolution logic"
```

---

## Task 5: status-line IO (`read_status`, `_pid_alive`, `print_segment`)

**Files:**
- Modify: `carta/statusline.py`
- Test: `carta/tests/test_statusline.py`

- [ ] **Step 1: Write the failing tests**

Append to `carta/tests/test_statusline.py`:

```python
import io
import json as _json


def test_read_status_walks_up(tmp_path):
    (tmp_path / ".carta").mkdir()
    (tmp_path / ".carta" / "embed-status.json").write_text('{"phase": "running"}')
    sub = tmp_path / "a" / "b"
    sub.mkdir(parents=True)
    assert sl.read_status(sub) == {"phase": "running"}


def test_read_status_missing_returns_none(tmp_path):
    assert sl.read_status(tmp_path) is None


def test_read_status_corrupt_returns_none(tmp_path):
    (tmp_path / ".carta").mkdir()
    (tmp_path / ".carta" / "embed-status.json").write_text("{not json")
    assert sl.read_status(tmp_path) is None


def test_pid_alive_self_true():
    assert sl._pid_alive(os.getpid()) is True


def test_pid_alive_dead_false():
    # PID 2**31-1 is essentially never a live process
    assert sl._pid_alive(2**31 - 1) is False


def test_print_segment_running(tmp_path, monkeypatch, capsys):
    (tmp_path / ".carta").mkdir()
    status = {
        "schema": 1, "phase": "running", "host": socket.gethostname(),
        "pid": os.getpid(), "total": 47, "current_idx": 24,
        "current_file": "big.pdf", "current_file_started_at": 0.0,
        "updated_at": 0.0, "finished_at": None, "embedded": 0,
        "skipped": 0, "errors": 0, "chunks": 0,
    }
    (tmp_path / ".carta" / "embed-status.json").write_text(_json.dumps(status))
    stdin = io.StringIO(_json.dumps({"workspace": {"current_dir": str(tmp_path)}}))
    monkeypatch.setattr(sys, "stdin", stdin)
    sl.print_segment()
    assert "carta 24/47" in _STRIP.sub("", capsys.readouterr().out)


def test_print_segment_no_status_is_empty(tmp_path, monkeypatch, capsys):
    stdin = io.StringIO(_json.dumps({"cwd": str(tmp_path)}))
    monkeypatch.setattr(sys, "stdin", stdin)
    sl.print_segment()
    assert capsys.readouterr().out == ""


def test_print_segment_never_raises_on_garbage(monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", io.StringIO("this is not json"))
    sl.print_segment()  # must not raise
    assert capsys.readouterr().out == ""
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest carta/tests/test_statusline.py -k "read_status or pid_alive or print_segment" -v`
Expected: FAIL with `AttributeError: module 'carta.statusline' has no attribute 'read_status'`.

- [ ] **Step 3: Implement the IO functions**

Append to `carta/statusline.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest carta/tests/test_statusline.py -v`
Expected: PASS (Task 4 + Task 5 tests).

- [ ] **Step 5: Commit**

```bash
git add carta/statusline.py carta/tests/test_statusline.py
git commit -m "feat(statusline): read_status + pid liveness + print_segment IO"
```

---

## Task 6: status-line wiring (install / uninstall)

**Files:**
- Modify: `carta/statusline.py`
- Test: `carta/tests/test_statusline_install.py`

- [ ] **Step 1: Write the failing tests**

Create `carta/tests/test_statusline_install.py`:

```python
import json

from carta import statusline as sl

SAMPLE_SCRIPT = """#!/usr/bin/env bash
input=$(cat)
parts="user:dir"
parts="$parts │ branch"
echo -e "$parts"
"""


def _write_script(tmp_path, body=SAMPLE_SCRIPT):
    p = tmp_path / "statusline-command.sh"
    p.write_text(body)
    return p


def test_install_inserts_block_and_backup(tmp_path):
    p = _write_script(tmp_path)
    result = sl.install_into_script(p, confirm=lambda msg: True)
    assert result == "installed"
    text = p.read_text()
    assert sl.MARKER_START in text and sl.MARKER_END in text
    # block sits BEFORE the echo line
    assert text.index(sl.MARKER_START) < text.index('echo -e "$parts"')
    # backup preserved original
    assert (tmp_path / "statusline-command.sh.bak").read_text() == SAMPLE_SCRIPT


def test_install_is_idempotent(tmp_path):
    p = _write_script(tmp_path)
    sl.install_into_script(p, confirm=lambda msg: True)
    once = p.read_text()
    result = sl.install_into_script(p, confirm=lambda msg: True)
    assert result == "already"
    assert p.read_text() == once  # unchanged on second run


def test_install_declined_changes_nothing(tmp_path):
    p = _write_script(tmp_path)
    result = sl.install_into_script(p, confirm=lambda msg: False)
    assert result == "declined"
    assert p.read_text() == SAMPLE_SCRIPT
    assert not (tmp_path / "statusline-command.sh.bak").exists()


def test_install_unsupported_script_refused(tmp_path):
    # No `parts` variable / no echo of parts -> cannot safely wire
    p = _write_script(tmp_path, body="#!/usr/bin/env bash\necho hello\n")
    result = sl.install_into_script(p, confirm=lambda msg: True)
    assert result == "unsupported"
    assert "carta statusline" not in p.read_text()


def test_uninstall_removes_block(tmp_path):
    p = _write_script(tmp_path)
    sl.install_into_script(p, confirm=lambda msg: True)
    result = sl.uninstall_from_script(p)
    assert result == "removed"
    text = p.read_text()
    assert sl.MARKER_START not in text and sl.MARKER_END not in text
    assert 'echo -e "$parts"' in text  # rest intact


def test_uninstall_absent_is_noop(tmp_path):
    p = _write_script(tmp_path)
    result = sl.uninstall_from_script(p)
    assert result == "absent"
    assert p.read_text() == SAMPLE_SCRIPT


def test_find_statusline_script(tmp_path):
    script = _write_script(tmp_path)
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps(
        {"statusLine": {"type": "command", "command": f"bash {script}"}}
    ))
    assert sl.find_statusline_script(settings) == script


def test_find_statusline_script_inline_returns_none(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps(
        {"statusLine": {"type": "command", "command": "echo hi"}}
    ))
    assert sl.find_statusline_script(settings) is None


def test_find_statusline_script_missing_key_returns_none(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({}))
    assert sl.find_statusline_script(settings) is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest carta/tests/test_statusline_install.py -v`
Expected: FAIL with `AttributeError: module 'carta.statusline' has no attribute 'MARKER_START'`.

- [ ] **Step 3: Implement wiring functions**

Append to `carta/statusline.py` (add `import re` and `import shlex` to the import block at the top of the file):

```python
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
    if "$input" not in text or "parts" not in text:
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest carta/tests/test_statusline_install.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add carta/statusline.py carta/tests/test_statusline_install.py
git commit -m "feat(statusline): idempotent install/uninstall wiring helpers"
```

---

## Task 7: CLI `statusline` subcommand

**Files:**
- Modify: `carta/cli.py` (add `cmd_statusline`, register subparser, add to `dispatch`)
- Test: `carta/tests/test_cli.py`

- [ ] **Step 1: Write the failing test**

Add to `carta/tests/test_cli.py`:

```python
def test_statusline_print_segment_smoke(tmp_path, monkeypatch, capsys):
    """`carta statusline` (no flags) prints the segment for cwd, never errors."""
    import io, json, os, socket
    from carta import cli

    (tmp_path / ".carta").mkdir()
    status = {
        "schema": 1, "phase": "running", "host": socket.gethostname(),
        "pid": os.getpid(), "total": 5, "current_idx": 2,
        "current_file": "x.md", "current_file_started_at": 0.0,
        "updated_at": 0.0, "finished_at": None, "embedded": 1,
        "skipped": 0, "errors": 0, "chunks": 3,
    }
    (tmp_path / ".carta" / "embed-status.json").write_text(json.dumps(status))
    monkeypatch.setattr(__import__("sys"), "stdin",
                        io.StringIO(json.dumps({"cwd": str(tmp_path)})))

    args = type("A", (), {"install": False, "uninstall": False})()
    cli.cmd_statusline(args)
    out = capsys.readouterr().out
    assert "carta 2/5" in out.replace("\x1b", "")  # ANSI-tolerant
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest carta/tests/test_cli.py::test_statusline_print_segment_smoke -v`
Expected: FAIL with `AttributeError: module 'carta.cli' has no attribute 'cmd_statusline'`.

- [ ] **Step 3: Add `cmd_statusline`**

In `carta/cli.py`, add this function next to the other `cmd_*` functions (e.g. after `cmd_doctor`):

```python
def cmd_statusline(args):
    """Print the embed-progress status-line segment, or install/uninstall wiring."""
    from carta import statusline
    from pathlib import Path

    if getattr(args, "install", False) or getattr(args, "uninstall", False):
        settings_path = Path.home() / ".claude" / "settings.json"
        script = statusline.find_statusline_script(settings_path)
        if script is None:
            print(
                "carta statusline: no wireable status-line script found in "
                f"{settings_path}.\n"
                "Add this to your status-line script, before it prints $parts:\n"
                '  seg=$(command -v carta >/dev/null && carta statusline <<<"$input" 2>/dev/null)\n'
                '  [ -n "$seg" ] && parts="$parts │ $seg"',
                file=sys.stderr,
            )
            sys.exit(1)
        if getattr(args, "uninstall", False):
            result = statusline.uninstall_from_script(script)
            print(f"carta statusline: {result} ({script})")
            sys.exit(0)
        result = statusline.install_into_script(
            script, confirm=lambda msg: input(f"{msg} [y/N] ").strip().lower() == "y"
        )
        print(f"carta statusline: {result} ({script})")
        if result == "installed":
            print(f"  backup: {script}.bak")
        sys.exit(0)

    # Default: print the segment for the current working directory.
    statusline.print_segment()
    sys.exit(0)
```

- [ ] **Step 4: Register the subparser and dispatch entry**

In `main()`, after the `update_p` subparser block and before `args = parser.parse_args()`, add:

```python
    statusline_p = sub.add_parser(
        "statusline",
        help="Print the embed-progress status-line segment (or --install/--uninstall wiring)",
    )
    statusline_p.add_argument(
        "--install", action="store_true",
        help="Wire the carta segment into your Claude Code status-line script",
    )
    statusline_p.add_argument(
        "--uninstall", action="store_true",
        help="Remove the carta segment from your status-line script",
    )
```

Then add `"statusline": cmd_statusline,` to the `dispatch` dict.

- [ ] **Step 5: Run test to verify it passes**

Run: `python -m pytest carta/tests/test_cli.py::test_statusline_print_segment_smoke -v`
Expected: PASS.

- [ ] **Step 6: Manual smoke check**

Run: `echo '{"cwd": "."}' | python -m carta statusline`
Expected: prints nothing (no embed running here) and exits 0.

- [ ] **Step 7: Commit**

```bash
git add carta/cli.py carta/tests/test_cli.py
git commit -m "feat(cli): carta statusline subcommand (segment + install/uninstall)"
```

---

## Task 8: Offer wiring during `carta init` + docs/gitignore

**Files:**
- Modify: `carta/cli.py::cmd_init`
- Modify: `carta/statusline.py` (add `offer_install` helper)
- Modify: `README.md` (document the widget; mention `.carta/embed-status.json` is gitignored)
- Test: `carta/tests/test_statusline_install.py`

- [ ] **Step 1: Write the failing test**

Add to `carta/tests/test_statusline_install.py`:

```python
def test_offer_install_declined_noop(tmp_path, monkeypatch):
    script = _write_script(tmp_path)
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps(
        {"statusLine": {"type": "command", "command": f"bash {script}"}}
    ))
    # auto-decline the prompt
    monkeypatch.setattr("builtins.input", lambda *a, **k: "n")
    result = sl.offer_install(settings_path=settings, interactive=True)
    assert result == "declined"
    assert sl.MARKER_START not in script.read_text()


def test_offer_install_no_script_returns_unavailable(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({}))
    assert sl.offer_install(settings_path=settings, interactive=True) == "unavailable"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest carta/tests/test_statusline_install.py -k offer_install -v`
Expected: FAIL with `AttributeError: module 'carta.statusline' has no attribute 'offer_install'`.

- [ ] **Step 3: Implement `offer_install`**

Append to `carta/statusline.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest carta/tests/test_statusline_install.py -k offer_install -v`
Expected: PASS.

- [ ] **Step 5: Call it from `cmd_init`**

In `carta/cli.py::cmd_init`, change:

```python
def cmd_init(args):
    _check_path_conflict()
    from carta.install.bootstrap import run_bootstrap
    run_bootstrap(Path.cwd(), skip_skills=getattr(args, "skip_skills", False))
    _notify_if_update()
```

to:

```python
def cmd_init(args):
    _check_path_conflict()
    from carta.install.bootstrap import run_bootstrap
    run_bootstrap(Path.cwd(), skip_skills=getattr(args, "skip_skills", False))

    # Offer to wire the embed-progress segment into the user's status line.
    try:
        from carta import statusline
        result = statusline.offer_install(interactive=sys.stdin.isatty())
        if result == "installed":
            print("✓ Wired carta embed-progress into your status line.")
        elif result == "unsupported":
            print(
                "Note: couldn't auto-wire the status line; add this before your "
                'script prints $parts:\n'
                '  seg=$(command -v carta >/dev/null && carta statusline <<<"$input" 2>/dev/null)\n'
                '  [ -n "$seg" ] && parts="$parts │ $seg"'
            )
    except Exception:
        pass  # status-line wiring is a convenience, never block init

    _notify_if_update()
```

- [ ] **Step 6: Run the init test suite**

Run: `python -m pytest carta/tests/test_cli.py -k init -v`
Expected: PASS (no regression; wiring is guarded and `isatty()` is False under pytest so it won't prompt).

- [ ] **Step 7: Documentation + gitignore note**

In `README.md`, add a short "Status-line progress widget" section describing: what it shows (`⠹ carta 24/47  big.pdf  19m`), that `carta init` offers to wire it, the manual snippet, and `carta statusline --install/--uninstall`. Note that `.carta/embed-status.json` is regenerated each run and should be gitignored.

In the repo's `.gitignore` (or the `.carta`-ignore guidance the bootstrap writes), ensure `.carta/embed-status.json` is covered (the existing `.carta/` ignore patterns likely already cover it — verify with `git check-ignore .carta/embed-status.json`; only add an explicit line if it is NOT already ignored).

- [ ] **Step 8: Commit**

```bash
git add carta/statusline.py carta/cli.py carta/tests/test_statusline_install.py README.md .gitignore
git commit -m "feat(init): offer status-line wiring on carta init; document widget"
```

---

## Final verification

- [ ] **Run the full test suite**

Run: `python -m pytest carta/tests/ -q`
Expected: all pass (new modules + no regressions).

- [ ] **End-to-end manual check**

In a repo with carta initialized and pending files, in one terminal run `carta embed`; in another run `cat .carta/embed-status.json` (should show `phase: running`, advancing `current_idx`). Pipe a fake session into the segment printer:
`echo "{\"cwd\": \"$PWD\"}" | carta statusline` → prints `⠹ carta N/M  <file>  <elapsed>`. After the run finishes, the same command shows the `✓ carta … files · …k chunks` flash for ~30s, then empties.

- [ ] **Final commit (if any docs/cleanup remain)**

```bash
git add -A
git commit -m "docs: finalize status-line widget notes"
```

---

## Self-review notes (author)

- **Spec coverage:** Component 1 (status file) → Task 2; Component 2 (pipeline writes) → Task 3; Component 3 (segment printer: render + IO) → Tasks 4–5 + CLI Task 7; Component 4 (auto-wiring) → Tasks 6–8; config flag (D-gate) → Task 1; edges/staleness (D4) → Task 4 `resolve_state` tests; testing section → covered across `test_status.py`/`test_statusline.py`/`test_statusline_install.py`.
- **Open spec item resolved:** the "how does the snippet find `$input`/`parts`" question is implemented conservatively in `install_into_script` — it requires both `$input` and `parts` to be present and inserts before the last `echo|printf … parts` line, else returns `"unsupported"` and the caller prints the manual snippet.
- **Type/name consistency:** `STATUS_FILENAME`/`SCHEMA` from `status.py` reused in `statusline.py` and tests; `resolve_state`/`format_segment`/`read_status`/`print_segment`/`install_into_script`/`uninstall_from_script`/`find_statusline_script`/`offer_install` names used consistently across tasks and tests.
