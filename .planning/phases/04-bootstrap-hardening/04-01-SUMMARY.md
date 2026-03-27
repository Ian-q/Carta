---
phase: 04-bootstrap-hardening
plan: 01
subsystem: bootstrap
tags: [bootstrap, hardening, gitignore, hooks, plugin-cache]
dependency_graph:
  requires: []
  provides: [hardened-bootstrap]
  affects: [carta-init, hook-registration, gitignore-management]
tech_stack:
  added: []
  patterns: [return-value-guard, parent-glob-check, exec-quoting]
key_files:
  created:
    - carta/tests/test_bootstrap.py
  modified:
    - carta/install/bootstrap.py
decisions:
  - "_remove_plugin_cache() added to bootstrap.py — was referenced in plan but missing from codebase; added with post-deletion assertion returning bool"
  - "BOOT-01 wired after _register_hooks() call, before _install_skills() — matches logical flow (cleanup before install)"
metrics:
  duration: 12m
  completed: "2026-03-27"
  tasks: 2
  files: 2
---

# Phase 04 Plan 01: Bootstrap Hardening Summary

**One-liner:** Hardened `carta init` with residue-abort on plugin cache failure, parent-glob gitignore deduplication, and portable `exec`-based hook quoting.

## Tasks Completed

| # | Name | Commit | Files |
|---|------|--------|-------|
| 1 | Implement BOOT-01, BOOT-02, BOOT-03 in bootstrap.py | 6019795 | carta/install/bootstrap.py |
| 2 | Write unit tests + add _remove_plugin_cache() | 73109ac | carta/tests/test_bootstrap.py, carta/install/bootstrap.py |

## What Was Built

Three surgical fixes to `carta/install/bootstrap.py`:

**BOOT-01 — Abort on residue:** `run_bootstrap()` now checks the return value of `_remove_plugin_cache()`. If it returns False (residue remains after deletion attempt), a clear stderr message is printed and `sys.exit(1)` is called. `carta init` no longer silently continues with a broken plugin cache state.

**BOOT-02 — Gitignore parent-glob guard:** `_update_gitignore()` now checks for `.carta/` or `.carta/*` in existing `.gitignore` lines before appending sub-entries. If either parent glob is present, all three `.carta/…` sub-entries are skipped entirely, preventing duplicate/redundant entries on re-runs.

**BOOT-03 — Portable exec hook quoting:** The hook command string in `_register_hooks()` now uses `exec` inside the bash wrapper with double-quoted inner path: `bash -c 'exec "$(git rev-parse --show-toplevel)/.carta/hooks/{script_name}"'`. This handles directories with spaces and resolves the project root correctly when Claude Code is launched from a subdirectory.

**_remove_plugin_cache() added:** The plan referenced this function as pre-existing at line 143, but it was absent from the codebase. Added with rmtree deletion, post-deletion existence check, and bool return value matching the plan spec.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 - Blocking] Added missing _remove_plugin_cache() function**
- **Found during:** Task 2 (test run — AttributeError: module has no attribute '_remove_plugin_cache')
- **Issue:** Plan spec referenced `_remove_plugin_cache()` at bootstrap.py:143 as pre-existing, but function was absent from codebase. BOOT-01 edit added the call without the definition.
- **Fix:** Added `_remove_plugin_cache()` implementing rmtree of `~/.claude/plugins/cache/carta-cc/carta-cc`, post-deletion existence check, stderr error on residue, bool return.
- **Files modified:** carta/install/bootstrap.py
- **Commit:** 73109ac

## Test Results

16/16 tests pass (5 new + 11 existing, no regressions):
- `test_boot01_residue_causes_exit` — PASSED
- `test_boot02_skips_when_parent_glob_carta_slash` — PASSED
- `test_boot02_skips_when_parent_glob_carta_star` — PASSED
- `test_boot02_adds_entries_without_parent_glob` — PASSED
- `test_boot03_hook_cmd_uses_exec_quoting` — PASSED

## Known Stubs

None.

## Self-Check: PASSED
