---
phase: 02-mcp-tools
plan: "01"
subsystem: service-layer
tags: [config, embed-pipeline, scanner, mcp, sidecar, drift-detection]
dependency_graph:
  requires: []
  provides: [find_config-in-config, run_embed_file, check_embed_drift, file_mtime-sidecar]
  affects: [carta/config.py, carta/cli.py, carta/embed/pipeline.py, carta/embed/induct.py, carta/scanner/scanner.py]
tech_stack:
  added: []
  patterns: [single-file-embed-adapter, mtime-skip-logic, drift-detection]
key_files:
  created:
    - carta/mcp/__init__.py
    - carta/mcp/tests/__init__.py
    - carta/mcp/tests/test_server.py
  modified:
    - carta/config.py
    - carta/cli.py
    - carta/embed/pipeline.py
    - carta/embed/induct.py
    - carta/scanner/scanner.py
decisions:
  - find_config moved to config.py so MCP handlers never import cli.py
  - _embed_one_file extracted from run_embed inline loop for reuse by run_embed_file
  - file_mtime stored as os.path.getmtime() float in sidecar after each embed
metrics:
  duration: ~10 min
  completed: "2026-03-26"
  tasks: 2
  files_changed: 8
---

# Phase 02 Plan 01: Service Layer Primitives for MCP Tool Handlers Summary

Service layer preparation for MCP: moved `find_config` to `config.py`, added `file_mtime` to sidecar schema, extracted `_embed_one_file` helper, created `run_embed_file` single-file adapter with mtime skip logic, and added `check_embed_drift` to scanner for mtime-based drift detection.

## What Was Built

### Task 1: find_config + file_mtime + run_embed_file

- **`carta/config.py`** — `find_config(start: Path = None) -> Path` moved here from `cli.py`. MCP handlers can now call `from carta.config import find_config` without importing the CLI module.
- **`carta/cli.py`** — `find_config` definition removed; replaced with `from carta.config import find_config` at top. Existing CLI code is unaffected.
- **`carta/embed/induct.py`** — `generate_sidecar_stub()` now includes `"file_mtime": None` in the stub dict.
- **`carta/embed/pipeline.py`** — Three additions:
  - `import os` added
  - `FILE_TIMEOUT_S = 300` constant
  - `_embed_one_file()` helper extracted from `run_embed` inline loop; stores `"file_mtime": os.path.getmtime(str(file_path))` in sidecar_updates
  - `run_embed_file(path, cfg, force, verbose)` single-file adapter with mtime skip and force override

### Task 2: check_embed_drift

- **`carta/scanner/scanner.py`** — `check_embed_drift(repo_root, cfg)` added after `check_embed_induction_needed`. Iterates embeddable files in scan dirs, reads sidecars with `status=embedded` and a `file_mtime` field, compares against `os.path.getmtime()`, returns list of `embed_drift` issue dicts. Skips pending sidecars and legacy sidecars without `file_mtime`.

### Test Coverage

- **`carta/mcp/tests/test_server.py`** — 12 new tests (plus package `__init__.py` files):
  - `find_config` importable from `carta.config`, walks directories, raises `FileNotFoundError`
  - `generate_sidecar_stub` includes `file_mtime: None`
  - `run_embed_file` raises `FileNotFoundError` for missing path
  - `run_embed_file` skips when mtime matches sidecar
  - `run_embed_file` force-re-embeds even when mtime matches
  - `run_embed_file` returns `{"status": "ok", "chunks": N}` on success
  - `check_embed_drift` returns empty list with no drift
  - `check_embed_drift` detects modified files
  - `check_embed_drift` skips pending sidecars
  - `check_embed_drift` skips legacy sidecars without file_mtime

## Verification

```
python -m pytest carta/ -x -q
122 passed, 5 warnings in 1.11s
```

All plan acceptance criteria met:
- `grep "def find_config" carta/config.py` — present
- `grep "from carta.config import find_config" carta/cli.py` — present
- `grep "def find_config" carta/cli.py` — absent
- `grep "file_mtime" carta/embed/induct.py` — present
- `grep "def run_embed_file" carta/embed/pipeline.py` — present
- `grep "file_mtime" carta/embed/pipeline.py` — present in _embed_one_file sidecar_updates
- `grep "import os" carta/embed/pipeline.py` — present
- `grep "def check_embed_drift" carta/scanner/scanner.py` — present

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Refactor] Extracted _embed_one_file from run_embed inline loop**

- **Found during:** Task 1 implementation
- **Issue:** `_embed_one_file` referenced in plan interfaces but did not exist — `run_embed` had all embed logic inline
- **Fix:** Extracted inline embed logic into `_embed_one_file()` helper; `run_embed` now delegates to it. Behavior unchanged.
- **Files modified:** `carta/embed/pipeline.py`
- **Commit:** 046c744

## Known Stubs

None — all new functions are fully implemented, not placeholder stubs.

## Self-Check: PASSED

- `carta/config.py` — FOUND
- `carta/cli.py` — FOUND
- `carta/embed/pipeline.py` — FOUND
- `carta/embed/induct.py` — FOUND
- `carta/scanner/scanner.py` — FOUND
- `carta/mcp/tests/test_server.py` — FOUND
- Commit 046c744 — FOUND
