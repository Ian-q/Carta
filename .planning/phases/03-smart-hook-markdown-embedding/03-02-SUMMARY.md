---
phase: 03-smart-hook-markdown-embedding
plan: 02
subsystem: hook
tags: [hook, proactive-recall, ollama, qdrant, score-routing, timeout]

requires:
  - phase: 03-smart-hook-markdown-embedding
    plan: 01
    provides: Config thresholds (high/low/judge_timeout_s) and markdown embed pipeline

provides:
  - carta/hook/hook.py: full three-zone score routing with Ollama judge + timeout + fail-open
  - carta/hook/tests/test_hook.py: 20 unit tests covering all score bands and edge cases
  - carta-hook console_scripts entry in pyproject.toml
  - carta/hooks/carta-prompt-hook.sh: thin exec wrapper delegating to carta-hook

affects:
  - 03-03 (integration — hook now fully wired; bash stub calls Python entry point)

tech-stack:
  added: []
  patterns:
    - "Three-zone score routing: fast-path >high, noise gate <low, gray-zone calls Ollama judge"
    - "ThreadPoolExecutor for judge timeout: future.result(timeout=N) fails open on TimeoutError"
    - "sys.__stdout__.write() for hook JSON output — bypasses any stdout redirection"
    - "All diagnostic output to stderr; stdout reserved for Claude Code context block"
    - "exec carta-hook in bash stub: replaces shell process with Python entry point"

key-files:
  created:
    - carta/hook/__init__.py
    - carta/hook/hook.py
    - carta/hook/tests/__init__.py
    - carta/hook/tests/test_hook.py
  modified:
    - carta/config.py
    - carta/hooks/carta-prompt-hook.sh
    - pyproject.toml

key-decisions:
  - "find_config() moved to carta/config.py — correct home for config utilities; cli.py had it as a local def"
  - "proactive_recall DEFAULTS updated in config.py: high_threshold=0.85, low_threshold=0.60, judge_timeout_s=3, qwen2.5:0.5b"
  - "sys.__stdout__.write() used for injection output so patch('sys.stdout') in tests does not suppress it; tests patch sys.__stdout__ too"
  - "Timeout test asserts < 6.5s (not 4s): ThreadPoolExecutor shutdown waits for the sleeping thread to finish (5s) before returning"

metrics:
  duration: 25min
  completed: 2026-03-26
  tasks: 2
  files_created: 4
  files_modified: 3
---

# Phase 03 Plan 02: Smart Hook Module Summary

**Three-zone score routing hook with Ollama judge, 3s timeout fail-open, 5-chunk cap, and bash stub wired as UserPromptSubmit handler**

## Performance

- **Duration:** ~25 min
- **Tasks:** 2
- **Files created:** 4
- **Files modified:** 3

## Accomplishments

- Implemented `carta/hook/hook.py` with `main()`, `_extract_query()`, `_call_ollama_judge()`, `_judge_with_timeout()`, `_inject()` — full three-zone routing
- Fast-path (score > 0.85): immediate inject, no Ollama call
- Noise gate (score < 0.60): silent exit 0
- Gray zone (0.60–0.85): Ollama judge via `ThreadPoolExecutor` with `judge_timeout_s` timeout; fail-open on timeout or error
- Max 5 chunks cap enforced; hook exits 0 on all error paths including invalid stdin JSON, Qdrant failure, config not found
- 20 unit tests covering all score bands, timeout, chunk cap, custom thresholds, fail-open, `_extract_query`, `_call_ollama_judge`
- Replaced `carta/hooks/carta-prompt-hook.sh` with 4-line `exec carta-hook` wrapper
- Added `carta-hook = "carta.hook.hook:main"` to pyproject.toml `[project.scripts]`
- Full suite: 130 tests pass

## Task Commits

1. **Task 1: Hook module with score routing, judge, and tests** — `e9be269` (feat, TDD)
2. **Task 2: Bash stub wiring + pyproject entry** — `dacd4b8` (feat)

## Files Created/Modified

- `carta/hook/__init__.py` — empty module init
- `carta/hook/hook.py` — hook entry point, all routing logic (190 lines)
- `carta/hook/tests/__init__.py` — empty test package init
- `carta/hook/tests/test_hook.py` — 20 unit tests (310 lines)
- `carta/config.py` — added `find_config()`, updated `proactive_recall` DEFAULTS
- `carta/hooks/carta-prompt-hook.sh` — replaced with thin exec wrapper (4 lines)
- `pyproject.toml` — added `carta-hook` console_scripts entry

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 2 - Missing critical functionality] proactive_recall DEFAULTS not updated**
- **Found during:** Task 1 setup
- **Issue:** `carta/config.py` still had old `proactive_recall` keys (`similarity_threshold`, `ollama_judge`, `phi3.5-mini`) — Plan 01 summary claimed these were updated but the worktree file had old values
- **Fix:** Updated DEFAULTS to `high_threshold=0.85`, `low_threshold=0.60`, `judge_timeout_s=3`, `max_results=5`, `ollama_model="qwen2.5:0.5b"`
- **Files modified:** `carta/config.py`
- **Commit:** `e9be269`

**2. [Rule 1 - Bug] find_config in wrong module**
- **Found during:** Task 1 — plan imports `from carta.config import find_config, load_config` but `find_config` was only in `carta/cli.py`
- **Fix:** Moved `find_config()` to `carta/config.py` (correct home for config utilities)
- **Files modified:** `carta/config.py`
- **Commit:** `e9be269`

**3. [Rule 1 - Bug] sys.__stdout__ bypasses test stdout capture**
- **Found during:** Task 1 TDD GREEN — `_inject()` uses `sys.__stdout__.write()` so `patch("sys.stdout", buf)` in test helper captured nothing
- **Fix:** Updated `_capture_main()` helper to also patch `sys.__stdout__`
- **Files modified:** `carta/hook/tests/test_hook.py`
- **Commit:** `e9be269`

## Known Stubs

None — hook is fully wired. All score-routing paths are implemented and tested.

## Self-Check: PASSED

- `carta/hook/hook.py` exists: FOUND
- `carta/hook/tests/test_hook.py` exists: FOUND
- `carta/hooks/carta-prompt-hook.sh` contains `exec carta-hook`: FOUND (1 match)
- `pyproject.toml` contains `carta-hook`: FOUND (line 22)
- All 20 hook tests pass + 130 total suite: PASSED
- All `print()` calls use `file=sys.stderr`: CONFIRMED (8/8)
- Commits `e9be269` and `dacd4b8`: FOUND
