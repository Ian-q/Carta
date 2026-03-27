---
gsd_state_version: 1.0
milestone: v0.2
milestone_name: milestone
status: Phase complete — ready for verification
last_updated: "2026-03-27T00:18:46.013Z"
progress:
  total_phases: 5
  completed_phases: 2
  total_plans: 5
  completed_plans: 5
---

# Carta v0.2 — Project State

**Last updated:** 2026-03-25
**Milestone:** v0.2 — MCP server + smart hook

## Project Reference

**Core value:** Relevant project knowledge surfaces automatically when Claude is working — without manual recall and without context noise.

**Current focus:** Phase 02 — mcp-tools

## Current Position

Phase: 02 (mcp-tools) — EXECUTING
Plan: 2 of 2

## Performance Metrics

| Metric | Value |
|--------|-------|
| Phases total | 4 |
| Phases complete | 0 |
| Requirements total | 23 |
| Requirements complete | 0 |
| Phase 01 P02 | 12 | 2 tasks | 8 files |
| Phase 01-pipeline-reliability-mcp-foundation P01 | 8 | 2 tasks | 7 files |
| Phase 02 P01 | 10 | 2 tasks | 8 files |
| Phase 02 P02 | 15 | 2 tasks | 3 files |

## Accumulated Context

### Key Decisions

- MCP-first (no hybrid): Plugin cache eliminated in Phase 1; `.mcp.json` is sole registration
- Three-tier architecture: Hook (push) + MCP (pull) + CLI (human batch)
- Embed reliability before MCP exposure: pipeline fixes are Phase 1 prerequisites
- Ollama judge for gray-zone relevance: `qwen2.5:0.5b`, 3s hard timeout, fail-open
- Use AST walk for print/sys.exit detection in MCP tests — avoids docstring false positives (01-02)
- Plugin cache replaced by cleanup-with-assertion routine; .mcp.json is sole registration (01-02)

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

- [ ] Begin Phase 1: batch Qdrant upsert in pipeline.py
- [ ] Phase 1: per-file timeout in pipeline.py
- [ ] Phase 1: verbose=False parameter on all pipeline service functions
- [x] Phase 1: MCP server scaffold in carta/mcp/ with stderr logging
- [x] Phase 1: .mcp.json at project root
- [x] Phase 1: carta init plugin cache cleanup with post-deletion assertion

### Blockers

None.

## Session Continuity

**Last session:** 2026-03-27T00:18:46.011Z

**To resume:** Read `.planning/ROADMAP.md` for phase goals and success criteria. Current phase is Phase 1, Plan 2 of 3 complete. Next: 01-03-PLAN.md.

---
*Initialized: 2026-03-25*
