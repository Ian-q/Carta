---
phase: 05-hook-wiring-entry-point-fix
verified: 2026-03-27T00:00:00Z
status: passed
score: 4/4 must-haves verified
re_verification: false
---

# Phase 05: Hook Wiring and Entry Point Fix — Verification Report

**Phase Goal:** Close the three wiring gaps identified in the v0.2 milestone audit that block HOOK-01 through HOOK-07 — wire the shell stub to invoke the Python module, register the carta-hook console script entry point, and fix the HOOK-05 fail-open inversion.
**Verified:** 2026-03-27
**Status:** PASSED
**Re-verification:** No — initial verification

## Goal Achievement

### Observable Truths

| # | Truth | Status | Evidence |
|---|-------|--------|----------|
| 1 | carta-prompt-hook.sh invokes carta-hook after the enabled check — the Python module is reachable | VERIFIED | Line 19: `exec carta-hook`; two legitimate `exit 0` guards remain (config-not-found, disabled) |
| 2 | carta-hook command exists on PATH after pip install | VERIFIED | `which carta-hook` → `/Library/Frameworks/Python.framework/Versions/3.12/bin/carta-hook` |
| 3 | TimeoutError in _judge_with_timeout returns True (inject / fail open), not False | VERIFIED | hook.py line 199: `return True` inside `except concurrent.futures.TimeoutError`; docstring updated to match |
| 4 | Flow C is end-to-end: Claude Code hook triggers → shell stub → Python hook → inject or discard | VERIFIED | Shell stub delegates via `exec carta-hook`; carta-hook is registered entry point pointing to `carta.hook.hook:main`; hook.py implements full score-band routing |

**Score:** 4/4 truths verified

### Required Artifacts

| Artifact | Expected | Status | Details |
|----------|----------|--------|---------|
| `carta/hooks/carta-prompt-hook.sh` | Shell stub delegating to carta-hook after enabled check | VERIFIED | Line 19 `exec carta-hook`; "Plan 2" placeholder comment removed; enabled-check guards intact |
| `pyproject.toml` | carta-hook registered as console script | VERIFIED | Line 24: `carta-hook = "carta.hook.hook:main"` present in `[project.scripts]` |
| `carta/hook/hook.py` | Corrected fail-open timeout logic (return True on TimeoutError) | VERIFIED | Line 199: `return True`; line 202: `return False` for non-timeout exceptions (correct); docstring line 189 updated |
| `carta/hook/tests/test_hook.py` | Tests covering HOOK-05 inversion fix and timeout behavior | VERIFIED | 5 test functions found: `test_judge_timeout_fails_open` (updated), `test_judge_timeout_returns_true`, `test_judge_exception_returns_false`, `test_judge_yes_returns_true`, `test_judge_no_returns_false` |

### Key Link Verification

| From | To | Via | Status | Details |
|------|----|-----|--------|---------|
| `carta/hooks/carta-prompt-hook.sh` | `carta-hook` (console script) | `exec carta-hook` | WIRED | Grep confirms `exec carta-hook` at line 19 |
| `pyproject.toml [project.scripts]` | `carta.hook.hook:main` | pip install entry point | WIRED | `carta-hook = "carta.hook.hook:main"` confirmed; `which carta-hook` returns valid path |
| `carta/hook/hook.py _judge_with_timeout` | `return True` on TimeoutError | `concurrent.futures.TimeoutError` handler | WIRED | Line 194-199: `except concurrent.futures.TimeoutError:` → `return True` |

### Data-Flow Trace (Level 4)

Not applicable — this phase modifies wiring infrastructure (shell stub, entry point, timeout logic), not data-rendering components.

### Behavioral Spot-Checks

| Behavior | Command | Result | Status |
|----------|---------|--------|--------|
| All hook tests pass including HOOK-05 timeout tests | `python -m pytest carta/hook/tests/test_hook.py -v -q` | 24 passed in 5.52s | PASS |
| carta-hook is on PATH | `which carta-hook` | `/Library/Frameworks/Python.framework/Versions/3.12/bin/carta-hook` | PASS |

### Requirements Coverage

| Requirement | Source Plan | Description | Status | Evidence |
|-------------|------------|-------------|--------|----------|
| HOOK-01 | 05-01-PLAN.md | Shell hook registered as UserPromptSubmit handler; extracts semantic query, queries Qdrant | SATISFIED | `carta-prompt-hook.sh` wired via `exec carta-hook`; `hook.py` main() runs search pipeline |
| HOOK-02 | 05-01-PLAN.md | Fast path: similarity score >0.85 → inject without Ollama | SATISFIED | `hook.py` line 91: `if hits[0]["score"] > high_threshold:` → `_inject(hits)` (default 0.85) |
| HOOK-03 | 05-01-PLAN.md | Noise gate: similarity score <0.60 → discard without injection | SATISFIED | `hook.py` line 87: `if not hits or hits[0]["score"] < low_threshold:` → silent exit (default 0.60) |
| HOOK-04 | 05-01-PLAN.md | Gray zone (0.60-0.85): Ollama judge for binary relevance; inject on "yes" | SATISFIED | `hook.py` lines 95-98: gray zone calls `_judge_with_timeout`; injects on `True` verdict |
| HOOK-05 | 05-01-PLAN.md | 3-second wall-clock timeout on Ollama judge; on timeout fail-open | SATISFIED | `hook.py` line 199: `return True` on `TimeoutError`; default `judge_timeout_s=3`; 24 tests pass including `test_judge_timeout_returns_true` |
| HOOK-06 | 05-01-PLAN.md | Maximum 5 injected chunks per prompt enforced | SATISFIED | `hook.py` line 84: `hits = hits[:max_results]` (default `max_results=5`) |
| HOOK-07 | 05-01-PLAN.md | Thresholds and judge model configurable in `.carta/config.yaml` | SATISFIED | `hook.py` lines 68-71: reads `high_threshold`, `low_threshold`, `max_results`, `judge_timeout_s` from config with defaults |

**Note on REQUIREMENTS.md tracking:** All seven HOOK requirements remain marked "Pending" in `.planning/REQUIREMENTS.md`. This is a documentation tracking gap — the implementation satisfies all seven requirements but the tracking table was not updated. Not a blocker; flag for the next planning pass.

### Anti-Patterns Found

| File | Line | Pattern | Severity | Impact |
|------|------|---------|----------|--------|
| `carta/hooks/carta-prompt-hook.sh` | 7, 14 | `exit 0` | Info | Both are legitimate guard exits (carta not initialised; proactive_recall disabled) — not stub patterns |

No blockers or warnings found. The two `exit 0` occurrences are intentional fast-exit guards, not leftover stubs.

### Human Verification Required

#### 1. End-to-end hook invocation in a live Claude Code session

**Test:** Configure a project with `proactive_recall: true`, embed at least one document, then submit a prompt. Verify the hook is triggered and context is injected (or correctly suppressed) based on score.
**Expected:** Qdrant query executes; matching chunks above threshold appear in context injection output; no "command not found" or Python import errors in hook stderr.
**Why human:** Requires live Claude Code environment with Qdrant and Ollama running.

### Gaps Summary

No gaps. All four observable truths verified. All three wiring fixes confirmed in code. Test suite passes (24/24). The only open item is a REQUIREMENTS.md tracking update (documentation, not a code gap).

---

_Verified: 2026-03-27_
_Verifier: Claude (gsd-verifier)_
