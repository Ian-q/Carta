---
gsd_state_version: 1.0
milestone: v0.2
milestone_name: milestone
status: Ready to plan
last_updated: "2026-03-31T22:10:00.000Z"
progress:
  total_phases: 9
  completed_phases: 8
  total_plans: 18
  completed_plans: 18
---

# Carta v0.2 — Project State

**Last updated:** 2026-03-31
**Milestone:** v0.2 — MCP server + smart hook

## Project Reference

**Core value:** Relevant project knowledge surfaces automatically when Claude is working — without manual recall and without context noise.

**Current focus:** Phase 999.3 — Qdrant collection scoping

## Current Position

Phase: 999.3
Plan: Not started

## Performance Metrics

| Metric | Value |
|--------|-------|
| Phases total | 9 |
| Phases complete | 8 |
| Requirements total | 23 |
| Requirements complete | 23 |
| Total plans | 18 |
| Completed plans | 18 |
| Active phase | 999.3 |
| Active plan | Not started |

## Accumulated Context

### Key Decisions

- MCP-first (no hybrid): Plugin cache eliminated in Phase 1; `.mcp.json` is sole registration
- Three-tier architecture: Hook (push) + MCP (pull) + CLI (human batch)
- Embed reliability before MCP exposure: pipeline fixes are Phase 1 prerequisites
- Ollama judge for gray-zone relevance: `qwen2.5:0.5b`, 3s hard timeout, fail-open
- Use AST walk for print/sys.exit detection in MCP tests — avoids docstring false positives (01-02)
- Plugin cache replaced by cleanup-with-assertion routine; .mcp.json is sole registration (01-02)
- Hook files restored from git objects (worktree artifact) — no content change; all 20 hook tests pass (03-03)
- HOOK-05 fail-open = inject on timeout (return True), not discard — pre-existing inversion fixed (05-01)
- carta-hook registered as console script; shell stub uses exec delegation pattern (05-01)
- Lifecycle primitives as pure stdlib leaf module: no Qdrant imports, fully testable in isolation (999.1-01a)
- Lazy import of extract_image_descriptions inside pdf if-block — keeps vision optional for non-PDF flows (999.2-02)
- chunk_index offset = len(raw_chunks) + position — prevents _point_id UUID collision between text and image chunks (999.2-02)
- status always 'embedded' even when image_chunks=0 per D-14 — zero signals model unavailability, not failure (999.2-02)

### Pitfalls to Avoid

- stdout pollution breaks MCP JSON-RPC silently — all logging must go to stderr
- Plugin cache cleanup must precede any MCP tool testing
- Ollama cold-start can freeze hook synchronously — 3s timeout + fail-open is mandatory
- Hook subdirectory trigger bug (Claude Code issues #8810, #17277) — MCP pull path is reliable fallback

### Implementation Notes

- `mcp>=1.7.1` (official SDK, FastMCP bundled) — do NOT use standalone `fastmcp` PyPI package
- Shared service layer: all three tiers delegate to `pipeline.py` + `scanner.py`
- CLI (`carta/cli.py`) is never imported by MCP or hook tiers
- `OLLAMA_KEEP_ALIVE=-1` must be set as a required config step before hook use

### Todos

- [x] Begin Phase 1: batch Qdrant upsert in pipeline.py
- [x] Phase 1: per-file timeout in pipeline.py
- [x] Phase 1: verbose=False parameter on all pipeline service functions
- [x] Phase 1: MCP server scaffold in carta/mcp/ with stderr logging
- [x] Phase 1: .mcp.json at project root
- [x] Phase 1: carta init plugin cache cleanup with post-deletion assertion
- [x] Phase 999.1-01a: compute_file_hash and needs_rehash — stdlib primitives
- [x] Phase 999.1-01b: Qdrant lifecycle ops (mark_stale, cleanup_orphans)
- [x] Phase 999.1-02: sidecar schema + chunk payload with lifecycle fields
- [x] Phase 999.1-03: pipeline lifecycle integration (mtime/hash/generation + stale alert)
- [x] Phase 999.1-04: MCP `carta_embed` scope support + stale discovery
- [x] Phase 999.2-01: vision module (PyMuPDF extraction + Ollama call path)
- [x] Phase 999.2-02: pipeline integration + sidecar image fields + tests

### Blockers

None.

## Session Continuity

**Last session:** 2026-03-31T22:10:00Z

**To resume:** Reconciliation complete across STATE/ROADMAP/999.2 verification docs. Latest full suite run: 75 passing tests. Next planning target is Phase 999.3.

---
*Initialized: 2026-03-25*
