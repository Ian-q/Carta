---
phase: 1
slug: pipeline-reliability-mcp-foundation
status: draft
nyquist_compliant: false
wave_0_complete: false
created: 2026-03-25
---

# Phase 1 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest 7.x |
| **Config file** | pyproject.toml (pytest section) |
| **Quick run command** | `python -m pytest carta/tests/ -x -q` |
| **Full suite command** | `python -m pytest carta/tests/ -v` |
| **Estimated runtime** | ~10 seconds |

---

## Sampling Rate

- **After every task commit:** Run `python -m pytest carta/tests/ -x -q`
- **After every plan wave:** Run `python -m pytest carta/tests/ -v`
- **Before `/gsd:verify-work`:** Full suite must be green
- **Max feedback latency:** 15 seconds

---

## Per-Task Verification Map

| Task ID | Plan | Wave | Requirement | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|-----------|-------------------|-------------|--------|
| 1-01-01 | 01 | 1 | PIPE-01 | unit | `python -m pytest carta/tests/test_embed.py -k upsert -x -q` | ❌ W0 | ⬜ pending |
| 1-01-02 | 01 | 1 | PIPE-02 | unit | `python -m pytest carta/tests/test_pipeline.py -k timeout -x -q` | ❌ W0 | ⬜ pending |
| 1-01-03 | 01 | 1 | PIPE-03 | unit | `python -m pytest carta/tests/test_parse.py -k overlap -x -q` | ❌ W0 | ⬜ pending |
| 1-01-04 | 01 | 1 | PIPE-04 | unit | `python -m pytest carta/tests/test_pipeline.py -k verbose -x -q` | ❌ W0 | ⬜ pending |
| 1-01-05 | 01 | 1 | PIPE-05 | unit | `python -m pytest carta/tests/test_induct.py -k current_path -x -q` | ❌ W0 | ⬜ pending |
| 1-02-01 | 02 | 2 | MCP-01 | manual | see Manual-Only | N/A | ⬜ pending |
| 1-02-02 | 02 | 2 | MCP-06 | unit | `python -m pytest carta/tests/test_mcp.py -k mcp_json -x -q` | ❌ W0 | ⬜ pending |
| 1-02-03 | 02 | 2 | MCP-07 | unit | `python -m pytest carta/tests/test_bootstrap.py -k plugin_cache -x -q` | ❌ W0 | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

- [ ] `carta/tests/test_embed.py` — stubs for PIPE-01 (batch upsert unit tests)
- [ ] `carta/tests/test_pipeline.py` — stubs for PIPE-02 (timeout), PIPE-04 (verbose)
- [ ] `carta/tests/test_parse.py` — stubs for PIPE-03 (overlap cap)
- [ ] `carta/tests/test_induct.py` — stubs for PIPE-05 (current_path sidecar field)
- [ ] `carta/tests/test_mcp.py` — stubs for MCP-06 (.mcp.json validation)
- [ ] `carta/tests/test_bootstrap.py` — stubs for MCP-07 (plugin cache removal)
- [ ] `pip install "mcp>=1.7.1"` — MCP SDK not currently installed; required before MCP scaffold can be tested

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| `carta-mcp` produces clean JSON-RPC stream on stdout, all logs on stderr | MCP-01 | Requires live MCP server process with stdio inspection | Run `carta-mcp` and verify: stdout is JSON-RPC only, stderr has log lines, no unhandled exceptions |

---

## Validation Sign-Off

- [ ] All tasks have `<automated>` verify or Wave 0 dependencies
- [ ] Sampling continuity: no 3 consecutive tasks without automated verify
- [ ] Wave 0 covers all MISSING references
- [ ] No watch-mode flags
- [ ] Feedback latency < 15s
- [ ] `nyquist_compliant: true` set in frontmatter

**Approval:** pending
