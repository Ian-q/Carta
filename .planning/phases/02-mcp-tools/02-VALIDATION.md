---
phase: 2
slug: mcp-tools
status: draft
nyquist_compliant: false
wave_0_complete: false
created: 2026-03-26
---

# Phase 2 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest 7.x |
| **Config file** | `pyproject.toml` (pytest section) |
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
| 2-01-01 | 01 | 1 | MCP-02 | unit | `python -m pytest carta/tests/test_mcp_search.py -x -q` | ❌ W0 | ⬜ pending |
| 2-01-02 | 01 | 1 | MCP-03 | unit | `python -m pytest carta/tests/test_mcp_embed.py -x -q` | ❌ W0 | ⬜ pending |
| 2-01-03 | 01 | 1 | MCP-04 | unit | `python -m pytest carta/tests/test_mcp_scan.py -x -q` | ❌ W0 | ⬜ pending |
| 2-01-04 | 01 | 2 | MCP-05 | integration | `python -m pytest carta/tests/test_mcp_server.py -x -q` | ❌ W0 | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

- [ ] `carta/tests/test_mcp_search.py` — stubs for MCP-02 (carta_search tool)
- [ ] `carta/tests/test_mcp_embed.py` — stubs for MCP-03 (carta_embed tool)
- [ ] `carta/tests/test_mcp_scan.py` — stubs for MCP-04 (carta_scan tool)
- [ ] `carta/tests/test_mcp_server.py` — stubs for MCP-05 (server entrypoint + error handling)

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| MCP tool registered in Claude Code | MCP-05 | Requires live Claude Code session | Run `carta-mcp`, register in `.claude/settings.json`, confirm tools appear in session |

---

## Validation Sign-Off

- [ ] All tasks have `<automated>` verify or Wave 0 dependencies
- [ ] Sampling continuity: no 3 consecutive tasks without automated verify
- [ ] Wave 0 covers all MISSING references
- [ ] No watch-mode flags
- [ ] Feedback latency < 15s
- [ ] `nyquist_compliant: true` set in frontmatter

**Approval:** pending
