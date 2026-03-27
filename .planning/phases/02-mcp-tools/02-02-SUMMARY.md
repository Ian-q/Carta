---
phase: 02-mcp-tools
plan: "02"
subsystem: mcp-tool-handlers
tags: [mcp, fastmcp, carta_search, carta_embed, carta_scan, error-handling, tdd]
dependency_graph:
  requires: [find_config-in-config, run_embed_file, check_embed_drift, file_mtime-sidecar]
  provides: [carta_search-tool, carta_embed-tool, carta_scan-tool]
  affects: [carta/mcp/server.py, carta/mcp/tests/test_server.py, carta/embed/pipeline.py]
tech_stack:
  added: [mcp>=1.7.1]
  patterns: [structured-error-returns, wire-protocol-discipline, tdd-red-green]
key_files:
  created: []
  modified:
    - carta/mcp/server.py
    - carta/mcp/tests/test_server.py
    - carta/embed/pipeline.py
decisions:
  - All tool handlers return structured error dicts on failure, never raise exceptions
  - _load_cfg and _repo_root_from_cfg helpers isolate config loading for testability
  - pipeline.py merged Plan 01 (run_embed_file, file_mtime) with main (_heal_sidecar_current_paths, concurrent.futures)
metrics:
  duration: ~15 min
  completed: "2026-03-26"
  tasks: 2
  files_changed: 3
---

# Phase 02 Plan 02: MCP Tool Handlers Summary

Three FastMCP tool handlers implemented in server.py: carta_search with excerpt truncation and score rounding, carta_embed with mtime skip logic and timeout handling, and carta_scan returning flat pending/drift path arrays — all returning structured error dicts on failure per wire-protocol discipline.

## What Was Built

### Task 1: Three MCP Tool Handlers in server.py

- **`carta/mcp/server.py`** — Added three `@mcp_server.tool()` handlers:
  - `carta_search(query, top_k=5)` — delegates to `run_search`, caps excerpts at 300 chars, rounds scores to 4dp, returns `list[dict]` or error dict
  - `carta_embed(path, force=False)` — resolves relative paths via repo root, delegates to `run_embed_file`, catches `FileNotFoundError`, `TimeoutError`, `RuntimeError` into typed error dicts
  - `carta_scan()` — calls `check_embed_induction_needed` + `check_embed_drift`, extracts `doc` field from each issue, returns `{"pending": [...], "drift": [...]}`
  - `_load_cfg()` and `_repo_root_from_cfg()` config helpers for clean testability
  - Wire-protocol discipline maintained: no `print()`, no `sys.exit()`, stderr-only logging via `_logger`

### Task 2: Comprehensive Test Suite (20 tests total)

- **`carta/mcp/tests/test_server.py`** — 15 new tests + 5 existing scaffold tests:
  - `carta_search`: happy path, excerpt truncation, top_k limiting, score rounding, RuntimeError→service_unavailable, config not found
  - `carta_embed`: success, skipped, file_not_found, timeout, service_unavailable, force=True forwarding
  - `carta_scan`: pending+drift extraction, empty arrays, config not found
  - All tests mock external dependencies via `unittest.mock.patch`

### Deviation: Pipeline.py Merge

- **`carta/embed/pipeline.py`** — Merged Plan 01 additions (`run_embed_file`, `file_mtime` in `_embed_one_file`, `find_config` import) with main branch additions (`_heal_sidecar_current_paths`, `concurrent.futures` timeout in `run_embed`, `verbose` parameter on `run_search`). The worktree merge conflict took Plan 01's version which was missing `_heal_sidecar_current_paths`, causing test_embed.py import failures.

## Verification

```
python3 -m pytest carta/mcp/tests/test_server.py -v  →  20 passed
python3 -m pytest carta/ -x -q                        →  144 passed, 5 warnings
grep -c "@mcp_server.tool" carta/mcp/server.py        →  3
python3 -c "from carta.mcp.server import carta_search, carta_embed, carta_scan; print('OK')"  →  OK
```

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 - Blocking] Merged pipeline.py conflict between Plan 01 worktree and main**

- **Found during:** Task 1 GREEN phase (full suite run)
- **Issue:** Worktree merge resolution took Plan 01's pipeline.py (`--theirs`) which was missing `_heal_sidecar_current_paths` used by `test_embed.py`. Main's pipeline.py had the function but lacked `run_embed_file` and `file_mtime`.
- **Fix:** Wrote merged pipeline.py containing all additions from both branches: `run_embed_file`, `file_mtime` in sidecar_updates, `_heal_sidecar_current_paths`, `concurrent.futures` timeout in `run_embed`, `verbose` param on `run_search`.
- **Files modified:** `carta/embed/pipeline.py`
- **Commit:** ce57f3d

## Known Stubs

None — all tool handlers are fully implemented and delegate to real service layer functions.

## Self-Check: PASSED

- `carta/mcp/server.py` — FOUND, contains 3 `@mcp_server.tool()` decorators
- `carta/mcp/tests/test_server.py` — FOUND, 20 tests
- `carta/embed/pipeline.py` — FOUND, merged version with all functions
- Commit ce57f3d — FOUND
