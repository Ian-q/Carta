---
phase: 3
slug: smart-hook-markdown-embedding
status: draft
nyquist_compliant: false
wave_0_complete: false
created: 2026-03-26
---

# Phase 3 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest 7.x |
| **Config file** | `pyproject.toml` (existing) |
| **Quick run command** | `python -m pytest carta/tests/ -x -q` |
| **Full suite command** | `python -m pytest carta/tests/ -v` |
| **Estimated runtime** | ~15 seconds |

---

## Sampling Rate

- **After every task commit:** Run `python -m pytest carta/tests/ -x -q`
- **After every plan wave:** Run `python -m pytest carta/tests/ -v`
- **Before `/gsd:verify-work`:** Full suite must be green
- **Max feedback latency:** 20 seconds

---

## Per-Task Verification Map

| Task ID | Plan | Wave | Requirement | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|-----------|-------------------|-------------|--------|
| 3-hook-smoke | TBD | 0 | HOOK-01 | manual | smoke test hook stdout format | ❌ W0 | ⬜ pending |
| 3-md-extractor | TBD | 1 | EMBED-01 | unit | `pytest carta/tests/test_markdown_embed.py -x -q` | ❌ W0 | ⬜ pending |
| 3-score-high | TBD | 1 | HOOK-02 | unit | `pytest carta/tests/test_hook.py::test_high_score_bypass -x -q` | ❌ W0 | ⬜ pending |
| 3-score-low | TBD | 1 | HOOK-03 | unit | `pytest carta/tests/test_hook.py::test_low_score_discard -x -q` | ❌ W0 | ⬜ pending |
| 3-judge-gray | TBD | 1 | HOOK-04 | unit | `pytest carta/tests/test_hook.py::test_gray_zone_judge -x -q` | ❌ W0 | ⬜ pending |
| 3-timeout | TBD | 1 | HOOK-05 | unit | `pytest carta/tests/test_hook.py::test_judge_timeout -x -q` | ❌ W0 | ⬜ pending |
| 3-chunk-cap | TBD | 1 | HOOK-06 | unit | `pytest carta/tests/test_hook.py::test_chunk_cap -x -q` | ❌ W0 | ⬜ pending |
| 3-config-thresholds | TBD | 1 | HOOK-07 | unit | `pytest carta/tests/test_hook.py::test_config_thresholds -x -q` | ❌ W0 | ⬜ pending |
| 3-sidecar-markdown | TBD | 1 | EMBED-01 | unit | `pytest carta/tests/test_markdown_embed.py::test_sidecar_file_type -x -q` | ❌ W0 | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

- [ ] `carta/tests/test_hook.py` — stubs for HOOK-01 through HOOK-07 (score band routing, chunk cap, timeout, config)
- [ ] `carta/tests/test_markdown_embed.py` — stubs for EMBED-01 (markdown extraction, sidecar file_type)
- [ ] `ollama pull qwen2.5:0.5b` — judge model not yet installed; Wave 0 must pull it
- [ ] Smoke test hook stdout format: `{"context": "..."}` vs `hookSpecificOutput.additionalContext` — verify which Claude Code accepts

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| Hook injects context into live Claude Code session | HOOK-01 | Requires real Claude Code session with UserPromptSubmit hook wired | 1. Install hook in `.claude/settings.json`. 2. Submit a prompt matching embedded doc. 3. Verify injected context appears in session. |
| Prompt proceeds unblocked when judge times out | HOOK-05 | Real Ollama timeout hard to trigger reliably in unit tests | 1. Set judge timeout to 0.1s in config. 2. Submit gray-zone prompt. 3. Verify prompt submitted within 1s (no hang). |

---

## Validation Sign-Off

- [ ] All tasks have `<automated>` verify or Wave 0 dependencies
- [ ] Sampling continuity: no 3 consecutive tasks without automated verify
- [ ] Wave 0 covers all MISSING references
- [ ] No watch-mode flags
- [ ] Feedback latency < 20s
- [ ] `nyquist_compliant: true` set in frontmatter

**Approval:** pending
