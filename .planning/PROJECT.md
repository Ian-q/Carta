# Carta

## What This Is

Carta is a semantic memory sidecar for Claude Code that gives agents automatic access to project documentation and session memory. It chains Qdrant vector storage, Ollama embeddings, and a smart context injection hook so relevant knowledge surfaces when Claude is working — without manual recall. v0.2 migrates from a fragile plugin cache architecture to a three-tier design: MCP server for Claude-initiated operations, a smart hook with Ollama-judge filtering for automatic injection, and a CLI for human-initiated setup and batch work.

## Core Value

Relevant project knowledge surfaces automatically when Claude is working — without manual recall and without context noise.

## Requirements

### Validated

- ✓ CLI with init/scan/embed/search commands — v0.1.x
- ✓ Qdrant vector storage for semantic search — v0.1.x
- ✓ PDF text extraction and chunking pipeline — v0.1.x
- ✓ Sidecar metadata tracking (`.embed-meta.yaml`) — v0.1.x
- ✓ UserPromptSubmit/Stop hook infrastructure — v0.1.x
- ✓ Skill registration via plugin cache — v0.1.x (being replaced)
- ✓ Embed pipeline reliability: batch Qdrant upsert (32/batch), overlap cap at 25% of take, per-file timeout (300s default) — Validated in Phase 01
- ✓ Sidecar `current_path` written on creation and auto-healed on embed — Validated in Phase 01
- ✓ Plugin cache elimination — `.mcp.json` is the sole registration; `_remove_plugin_cache()` cleans legacy paths — Validated in Phase 01
- ✓ MCP server scaffold (`carta-mcp`) with FastMCP stdio transport, stderr-only logging, no stdout pollution — Validated in Phase 01

### Active

- ✓ MCP server exposing `carta_search`, `carta_embed`, `carta_scan` tools via stdio transport — Validated in Phase 02
- [ ] Smart hook: similarity threshold fast path (>0.85 inject, <0.6 discard) + Ollama judge for gray zone (0.6–0.85)
- [ ] Markdown file embedding support (`.md` files scanned but not currently embeddable)
- [ ] `carta status` command (Qdrant/Ollama health, collection sizes, pending files)

### Out of Scope

- Multi-user or cloud-hosted Carta — local tool only; no auth surface needed
- Hybrid plugin cache + MCP — full migration, not coexistence; cache fragility is the root problem
- Blind context injection (injecting on every prompt) — replaced by judge-filtered injection to prevent context noise
- Batched Ollama embedding API — Ollama `/api/embeddings` does not support batch input natively; per-text calls remain

## Context

- **Current state (Phase 02 complete):** Three MCP tools live in `carta/mcp/server.py`: `carta_search` (scored, excerpt-capped results), `carta_embed` (single-file with mtime skip and timeout enforcement), `carta_scan` (pending + drift arrays). Service layer extended: `find_config` moved to `config.py`, `file_mtime` in sidecar schema, `run_embed_file` adapter, `check_embed_drift` in scanner. 144 tests passing. Next: smart hook with Ollama-judge filtering (Phase 03).
- **Architecture map:** Modular layered CLI — `carta/cli.py` → `carta/embed/pipeline.py` + `carta/scanner/scanner.py` → Qdrant + Ollama. Config via `.carta/config.yaml`, state via sidecar `.embed-meta.yaml` files.
- **Plugin cache problem:** Carta manages its own skill cache install rather than using native Claude Code plugin flow. This creates a two-registry problem where lexicographically earlier stale versions win. MCP tools are resolved natively by Claude Code via `.mcp.json` — no cache involved.
- **Automatic injection design:** Hook fires on UserPromptSubmit, extracts semantic query from prompt, retrieves Qdrant candidates. Fast path: similarity >0.85 → inject immediately. Noise gate: similarity <0.6 → discard. Gray zone: 0.6–0.85 → Ollama lightweight model (0.5B–2B) makes relevance judgment. This keeps context injection demand-driven and noise-free.
- **Infra:** Qdrant (Docker, localhost:6333) + Ollama (localhost:11434) — same stack, no changes.

## Constraints

- **Tech stack:** Python 3.10+, Qdrant client, Ollama HTTP API, MCP stdio server — no new infra
- **Compatibility:** Embed pipeline fixes must not regress existing sidecar state or Qdrant collections
- **Sequencing:** MCP server wraps the same embed pipeline — reliability fixes (batch upsert, timeout) are prerequisites before exposing `carta_embed` via MCP
- **Local only:** Ollama judge must be a small model (≤2B params) to keep hook latency acceptable; hook blocks prompt submission

## Key Decisions

| Decision | Rationale | Outcome |
|----------|-----------|---------|
| MCP-first over hybrid CLI+MCP | Plugin cache is root cause of Issue #7; MCP tools resolve natively via `.mcp.json`, eliminating entire version-resolution problem class | — Active |
| Three-tier architecture (Hook + MCP + CLI) | Hook owns automatic injection (push); MCP owns Claude-initiated ops (pull); CLI owns human batch/setup | — Active |
| Ollama judge for gray-zone relevance | Binary relevance judgment is a simple task for a small local model; avoids context noise without sacrificing recall | — Active |
| Similarity threshold fast path | Avoids Ollama round-trip for clear-hit and clear-miss cases; only gray zone pays the latency cost | — Active |
| Embed reliability before MCP exposure | Unreliable pipeline → unreliable MCP tool; fixes are prerequisites not parallel work | ✓ Validated in Phase 01 |

## Evolution

This document evolves at phase transitions and milestone boundaries.

**After each phase transition** (via `/gsd:transition`):
1. Requirements invalidated? → Move to Out of Scope with reason
2. Requirements validated? → Move to Validated with phase reference
3. New requirements emerged? → Add to Active
4. Decisions to log? → Add to Key Decisions
5. "What This Is" still accurate? → Update if drifted

**After each milestone** (via `/gsd:complete-milestone`):
1. Full review of all sections
2. Core Value check — still the right priority?
3. Audit Out of Scope — reasons still valid?
4. Update Context with current state

---
*Last updated: 2026-03-26 after Phase 02 completion*
