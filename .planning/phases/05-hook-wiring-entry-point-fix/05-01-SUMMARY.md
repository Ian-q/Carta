---
phase: 05-hook-wiring-entry-point-fix
plan: "01"
subsystem: hook
tags: [hook, entry-point, fail-open, HOOK-05, wiring]
dependency_graph:
  requires: []
  provides: [carta-hook-entry-point, hook-wiring, HOOK-05-fix]
  affects: [carta/hooks/carta-prompt-hook.sh, pyproject.toml, carta/hook/hook.py]
tech_stack:
  added: []
  patterns: [TDD, exec-delegation, fail-open-timeout]
key_files:
  created: []
  modified:
    - carta/hook/hook.py
    - carta/hook/tests/test_hook.py
    - carta/hooks/carta-prompt-hook.sh
    - pyproject.toml
decisions:
  - "HOOK-05 fail-open means inject on timeout (return True), not discard (return False)"
  - "exec carta-hook replaces shell process — avoids subprocess wrapper, ensures direct stdout"
  - "Corrected pre-existing test_judge_timeout_fails_open which encoded the bug (asserted no output on timeout)"
metrics:
  duration_minutes: 15
  completed_date: "2026-03-27"
  tasks_completed: 3
  files_modified: 4
---

# Phase 05 Plan 01: Hook Wiring and Entry Point Fix Summary

**One-liner:** Wired shell stub to `exec carta-hook`, registered `carta-hook` console script, and fixed HOOK-05 fail-open inversion (`return True` on timeout).

## Tasks Completed

| Task | Name | Commit | Files |
|------|------|--------|-------|
| 1 (RED) | Add failing HOOK-05 timeout tests | b5c161a | carta/hook/tests/test_hook.py |
| 1 (GREEN) | Fix fail-open inversion + update stale test | b7d390c | carta/hook/hook.py, carta/hook/tests/test_hook.py |
| 2 | Register carta-hook entry point | d89e84a | pyproject.toml |
| 3 | Wire shell stub to exec carta-hook | da80a57 | carta/hooks/carta-prompt-hook.sh |

## Decisions Made

- **HOOK-05 is fail-open = inject:** `TimeoutError` in `_judge_with_timeout` must return `True`. The original `return False` would silently discard context when Ollama was slow — the opposite of fail-open. Fixed to `return True`.
- **Pre-existing test corrected:** `test_judge_timeout_fails_open` was written against the buggy behavior (asserting no output on timeout). Updated to assert injection on timeout per HOOK-05.
- **`exec` pattern for shell stub:** Using `exec carta-hook` replaces the shell process rather than spawning a subprocess. This is the correct pattern for Claude Code hooks — stdout is the direct output of the Python module with no wrapper overhead.

## Verification Results

- `python -m pytest carta/hook/tests/test_hook.py -v` — 24 passed
- `which carta-hook` — `/Library/Frameworks/Python.framework/Versions/3.12/bin/carta-hook`
- `grep "carta-hook" carta/hooks/carta-prompt-hook.sh` — `exec carta-hook` present
- `grep -c "exit 0" carta/hooks/carta-prompt-hook.sh` — 2 (config-not-found + module-disabled, not old stub)
- Full suite: 102 passed, 1 pre-existing failure (out of scope — see deferred-items.md)

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] Corrected pre-existing test encoding the bug**
- **Found during:** Task 1 GREEN phase
- **Issue:** `test_judge_timeout_fails_open` asserted `out.strip() == ""` on timeout — this encoded the old incorrect fail-closed behavior. After fixing the bug, this test failed.
- **Fix:** Updated assertion to expect injection on timeout per HOOK-05. The test name (`fails_open`) was always correct; only the assertion was wrong.
- **Files modified:** `carta/hook/tests/test_hook.py`
- **Commit:** b7d390c

### Pre-existing Failures (Out of Scope)

**test_bootstrap.py::test_install_skills_removed** — Phase 04 added `_install_skills()` but a test asserts it must not exist. Inter-phase contradiction, not caused by this plan. Logged in `deferred-items.md`.

## Known Stubs

None — all three wiring fixes are complete and functional.

## Self-Check: PASSED
