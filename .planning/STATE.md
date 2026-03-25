# Carta v0.2 — Project State

**Last updated:** 2026-03-25
**Milestone:** v0.2 — MCP server + smart hook

## Project Reference

**Core value:** Relevant project knowledge surfaces automatically when Claude is working — without manual recall and without context noise.

**Current focus:** Phase 1 — Pipeline Reliability + MCP Foundation

## Current Position

| Field | Value |
|-------|-------|
| Phase | 1 |
| Plan | None (not started) |
| Status | Not started |
| Phase goal | Reliable embed pipeline + MCP scaffold with wire-protocol discipline |

**Progress:**
```
[Phase 1] [ ] Pipeline Reliability + MCP Foundation
[Phase 2] [ ] MCP Tools
[Phase 3] [ ] Smart Hook + Markdown Embedding
[Phase 4] [ ] Bootstrap Hardening
```

## Performance Metrics

| Metric | Value |
|--------|-------|
| Phases total | 4 |
| Phases complete | 0 |
| Requirements total | 23 |
| Requirements complete | 0 |

## Accumulated Context

### Key Decisions

- MCP-first (no hybrid): Plugin cache eliminated in Phase 1; `.mcp.json` is sole registration
- Three-tier architecture: Hook (push) + MCP (pull) + CLI (human batch)
- Embed reliability before MCP exposure: pipeline fixes are Phase 1 prerequisites
- Ollama judge for gray-zone relevance: `qwen2.5:0.5b`, 3s hard timeout, fail-open

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
- [ ] Phase 1: MCP server scaffold in carta/mcp/ with stderr logging
- [ ] Phase 1: .mcp.json at project root
- [ ] Phase 1: carta init plugin cache cleanup with post-deletion assertion

### Blockers

None.

## Session Continuity

**To resume:** Read `.planning/ROADMAP.md` for phase goals and success criteria. Current phase is Phase 1. No plans have been created yet — run `/gsd:plan-phase 1` to begin.

---
*Initialized: 2026-03-25*
