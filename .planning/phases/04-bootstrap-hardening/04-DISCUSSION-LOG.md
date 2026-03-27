# Phase 4: Bootstrap Hardening - Discussion Log

> **Audit trail only.** Do not use as input to planning, research, or execution agents.
> Decisions are captured in CONTEXT.md — this log preserves the alternatives considered.

**Date:** 2026-03-27
**Phase:** 04-bootstrap-hardening
**Areas discussed:** Cache residue behavior, Gitignore glob detection, Hook quoting

---

## Area A: Cache residue behavior (BOOT-01)

**Question:** If plugin cache residue remains after deletion, what should `carta init` do?

| Option | Description |
|--------|-------------|
| **Abort with sys.exit(1)** ✓ | Print a clear error to stderr and exit non-zero — residue means an old plugin registration may still conflict with the MCP server |
| Warn and continue | Print a prominent error but let init proceed — user can clean up manually |

**Selected:** Abort with sys.exit(1)

---

## Area B: Gitignore parent-glob detection (BOOT-02)

**Question:** How broad should the parent-glob detection be for gitignore deduplication?

| Option | Description |
|--------|-------------|
| **Simple parent dir** ✓ | Only skip if `.carta/` or `.carta/*` already exists — covers the common case |
| Full fnmatch | Simulate full gitignore glob matching — catches edge cases but adds complexity |
| You decide | Claude picks the sophistication level |

**Selected:** Simple parent dir

---

## Area C: Hook quoting (BOOT-03)

**Question:** What should the portable hook command pattern look like?

| Option | Preview |
|--------|---------|
| **Add exec, fix quoting** ✓ | `bash -c 'exec "$(git rev-parse --show-toplevel)/.carta/hooks/carta-prompt-hook.sh"'` |
| Fix quoting only | `bash -c '"$(git rev-parse --show-toplevel)/.carta/hooks/carta-prompt-hook.sh"'` |

**Selected:** Add exec + fix quoting
