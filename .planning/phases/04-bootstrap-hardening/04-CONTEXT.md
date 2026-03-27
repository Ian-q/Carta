# Phase 4: Bootstrap Hardening - Context

**Gathered:** 2026-03-27
**Status:** Ready for planning

<domain>
## Phase Boundary

Make `carta init` defensively correct: abort clearly on plugin cache residue, skip gitignore entries already covered by a parent-directory glob, and use a portable `exec`-based quoting pattern for the hook command so it resolves the project root correctly when Claude Code is launched from a subdirectory.

All changes are in `carta/install/bootstrap.py` and the hook command string written by `_register_hooks()`.

</domain>

<decisions>
## Implementation Decisions

### BOOT-01: Cache residue failure behavior
- **D-01:** If `_remove_plugin_cache()` returns `False` (residue remains after deletion), `run_bootstrap()` must call `sys.exit(1)` with a clear error message — do not continue silently. Residue means the old plugin registration may still conflict with the new MCP server.

### BOOT-02: Gitignore parent-glob detection
- **D-02:** `_update_gitignore()` should use simple parent-directory detection only: if `.carta/` or `.carta/*` is already present in `.gitignore`, skip all `.carta/…` sub-entries. No full fnmatch simulation needed — the common case is all that matters.

### BOOT-03: Hook command quoting
- **D-03:** The hook command string written by `_register_hooks()` must use `exec` inside the bash wrapper and correct inner quoting:
  ```
  bash -c 'exec "$(git rev-parse --show-toplevel)/.carta/hooks/carta-prompt-hook.sh"'
  ```
  `exec` replaces the shell process; the double-quoted path handles directories with spaces. Apply this pattern to both `UserPromptSubmit` and `Stop` hook entries.

### Claude's Discretion
- Test coverage: add or update tests for all three behaviors as appropriate — Claude decides the test strategy (unit vs integration, mock vs real filesystem).

</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### Implementation target
- `carta/install/bootstrap.py` — all three requirements are implemented here; read the full file before planning

### Requirements
- `.planning/REQUIREMENTS.md` §Bootstrap Hardening — BOOT-01, BOOT-02, BOOT-03 (lines 42–44)

### Existing tests (for regression awareness)
- `carta/tests/test_bootstrap*.py` (if present) — check before adding new tests to avoid duplication

</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets
- `_remove_plugin_cache()` (bootstrap.py:143–173): Already has post-deletion assertion and returns `bool`. The fix is wiring up the return value in `run_bootstrap()` at line 40.
- `_update_gitignore()` (bootstrap.py:196–206): Currently does literal string match only (`if e not in existing_lines`). Needs parent-dir glob check added before the literal check.
- `_register_hooks()` (bootstrap.py:109–140): Writes the hook command at line 138. Single-line change to add `exec` and fix quoting.

### Established Patterns
- Errors go to `sys.stderr`; print confirmations go to stdout — maintain this throughout.
- `sys.exit(1)` used elsewhere in `run_bootstrap()` for fatal errors (e.g., Qdrant not reachable at line 22).

### Integration Points
- `run_bootstrap()` is the single orchestration function called by `cmd_init()` in `carta/cli.py` — changes are self-contained.

</code_context>

<specifics>
## Specific Ideas

- The `exec` quoting fix is the exact string shown in D-03 — both hook scripts get the same pattern update.
- The gitignore parent check should recognise `.carta/` (trailing slash) and `.carta/*` (glob) as covering all sub-entries. No need to handle `.carta/**` or other variants unless already present in real-world `.gitignore` files.

</specifics>

<deferred>
## Deferred Ideas

None — discussion stayed within phase scope.

</deferred>

---

*Phase: 04-bootstrap-hardening*
*Context gathered: 2026-03-27*
