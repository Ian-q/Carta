---
gsd_state_version: 1.0
milestone: v0.2
milestone_name: milestone
status: In progress
last_updated: "2026-04-02T00:00:00.000Z"
progress:
  total_phases: 10
  completed_phases: 8
  total_plans: 21
  completed_plans: 21
  total_plans_pending: 4
---

# Carta v0.2 — Project State

**Last updated:** 2026-04-02
**Milestone:** v0.2 — MCP server + smart hook + multi-platform

## Project Reference

**Core value:** Relevant project knowledge surfaces automatically when Claude is working — without manual recall and without context noise.

**Current focus:** Phase 999.4 — GLM-OCR intelligent PDF extraction

## Current Position

Phase: 999.4
Plan: 05 (completed)
Status: **COMPLETE** - Phase 999.4 finished

## Performance Metrics

| Metric | Value |
|--------|-------|
| Phases total | 10 |
| Phases complete | 9 |
| Phases in planning | 0 |
| Requirements total | 33 |
| Requirements complete | 23 |
| Total plans | 21 |
| Completed plans | 21 |
| Active phase | — |
| Active plan | — |

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
- **Phase 999.3: Multi-platform support** — Carta supports both Claude Code (`.mcp.json`) and OpenCode (`.opencode.json`) equally
- **Phase 999.3: Collection scoping** — Three scope levels (repo/shared/global) with repo as secure default
- **Phase 999.3: Cross-project opt-in** — `cross_project_recall.enabled` controls shared scope access
- **Phase 999.4: GLM-OCR integration** — Intelligent content classification routes text pages to GLM-OCR, visual pages to LLaVA
- **Phase 999.4: Dual extraction** — Hybrid approach preserves table structure while maintaining visual context

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
- [x] Phase 999.3-01: collection scoping module (`carta/search/scoped.py`)
- [x] Phase 999.3-02: update `carta_search` MCP tool with scope parameter
- [x] Phase 999.3-03: OpenCode support (`.opencode.json` generation) — INCLUDED in 999.3-01 bootstrap update
- [x] Phase 999.4-01: content classification module (GLM-OCR routing) — **COMPLETED**
- [x] Phase 999.4-02: dual extraction pipeline (GLM-OCR + LLaVA) — **COMPLETED**
- [x] Phase 999.4-03: structured chunking (table preservation) — **COMPLETED**
- [x] Phase 999.4-04: sidecar schema updates (extraction provenance) — **COMPLETED**
- [x] Phase 999.4-05: integration & validation — **COMPLETED**

### Post-v0.2 Backlog (Non-blocking)

- [ ] Issue #1-Followup-01: Page classifier integration for selective ColPali embedding
- [ ] Issue #1-Followup-02: Visual collection lifecycle management (mark stale on re-embed)
- [ ] Issue #1-Followup-03: Comprehensive ColPali embedding tests

**Details:** See `.planning/BACKLOG.md` for full specifications.

### Blockers

None.

## Session Continuity

**Last session:** 2026-04-02T00:00:00Z

**To resume:** Phase 999.4 planning complete. Design spec and Plan 999.4-01 created. GLM-OCR model confirmed available locally. Ready to execute content classification module.

---
*Initialized: 2026-03-25*
