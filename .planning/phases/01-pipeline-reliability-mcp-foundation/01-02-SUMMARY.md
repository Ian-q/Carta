---
phase: 01-pipeline-reliability-mcp-foundation
plan: 02
subsystem: mcp-foundation
tags: [mcp, bootstrap, plugin-cache, stdio, fastmcp]
dependency_graph:
  requires: []
  provides: [carta-mcp-entrypoint, mcp-server-scaffold, plugin-cache-cleanup]
  affects: [carta/install/bootstrap.py, pyproject.toml, .mcp.json]
tech_stack:
  added: ["mcp>=1.7.1 (FastMCP bundled)"]
  patterns: ["stdio JSON-RPC server", "stderr-only logging", "AST-based test assertions"]
key_files:
  created:
    - carta/mcp/__init__.py
    - carta/mcp/server.py
    - carta/mcp/tests/__init__.py
    - carta/mcp/tests/test_server.py
    - .mcp.json
  modified:
    - pyproject.toml
    - carta/install/bootstrap.py
    - carta/install/tests/test_bootstrap.py
decisions:
  - "Use AST walk (not string search) for sys.exit and print() detection in tests — avoids docstring false positives"
  - "mcp>=1.7.1 installed with --break-system-packages on Python 3.14 (Homebrew managed)"
  - "test_install_skills_copies_skill_markdown removed along with _install_skills() — test had no function to test"
metrics:
  duration_minutes: 12
  completed_date: "2026-03-25"
  tasks_completed: 2
  files_created: 5
  files_modified: 3
---

# Phase 01 Plan 02: MCP Server Scaffold and Plugin Cache Cleanup Summary

**One-liner:** FastMCP stdio server scaffold with stderr-only logging, .mcp.json registration, and plugin cache replaced by cleanup-with-assertion routine.

## Tasks Completed

| Task | Name | Commit | Files |
|------|------|--------|-------|
| 1 | MCP server scaffold, entrypoint, .mcp.json | 14be85e | carta/mcp/server.py, .mcp.json, pyproject.toml |
| 2 | Plugin cache cleanup with post-deletion assertion | b452206 | carta/install/bootstrap.py, test_bootstrap.py |

## What Was Built

**Task 1 — MCP server scaffold:**
- `carta/mcp/server.py`: FastMCP("carta") instance, `logging.basicConfig(stream=sys.stderr)`, `def main()` calling `mcp_server.run()`. Zero `print()` calls, zero `sys.exit()` calls.
- `.mcp.json`: Single MCP registration at project root — `mcpServers.carta.command = "carta-mcp"`.
- `pyproject.toml`: Added `mcp>=1.7.1` to dependencies; added `carta-mcp = "carta.mcp.server:main"` script entry.
- 5 tests covering wire-protocol discipline (AST-based checks) and registration validity.

**Task 2 — Plugin cache cleanup:**
- `_install_skills()` function (66 lines) removed entirely — plugin cache approach abandoned.
- `_remove_plugin_cache()` added: removes `~/.claude/plugins/carta/` and `~/.claude/plugins/cache/carta-cc/`, prints error to stderr if residue remains, returns `bool`.
- `run_bootstrap()` updated to call `_remove_plugin_cache()` instead.
- 4 new tests: removes both paths, noop when absent, returns False on residue, asserts `_install_skills` gone.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] Fixed sys.exit test false positive from docstring**
- **Found during:** Task 1, GREEN phase
- **Issue:** `test_server_module_has_no_sys_exit` used simple string match `"sys.exit" not in source`, which matched the docstring comment "Never call sys.exit()"
- **Fix:** Replaced string match with AST walk checking for actual `sys.exit()` call nodes
- **Files modified:** `carta/mcp/tests/test_server.py`
- **Commit:** 14be85e

**2. [Rule 3 - Blocking] mcp package not installed for Python 3.14**
- **Found during:** Task 1, GREEN phase
- **Issue:** `pip install` installed mcp into Python 3.12 (system), but tests ran under Python 3.14 (Homebrew)
- **Fix:** `python3 -m pip install "mcp>=1.7.1" --break-system-packages` targeting the active interpreter
- **Files modified:** None (runtime environment)

## Known Stubs

None. No placeholder data or unconnected components — this plan creates pure scaffold with no UI rendering path.

## Self-Check

Files created/modified:
- `/Users/ian/dev/doc-audit-cc/.claude/worktrees/agent-abfb88af/carta/mcp/__init__.py` — exists
- `/Users/ian/dev/doc-audit-cc/.claude/worktrees/agent-abfb88af/carta/mcp/server.py` — exists
- `/Users/ian/dev/doc-audit-cc/.claude/worktrees/agent-abfb88af/carta/mcp/tests/__init__.py` — exists
- `/Users/ian/dev/doc-audit-cc/.claude/worktrees/agent-abfb88af/carta/mcp/tests/test_server.py` — exists
- `/Users/ian/dev/doc-audit-cc/.claude/worktrees/agent-abfb88af/.mcp.json` — exists
- `/Users/ian/dev/doc-audit-cc/.claude/worktrees/agent-abfb88af/pyproject.toml` — modified
- `/Users/ian/dev/doc-audit-cc/.claude/worktrees/agent-abfb88af/carta/install/bootstrap.py` — modified

Commits verified: 7b0ea4a, 14be85e, 84b5e8e, b452206

## Self-Check: PASSED
