# Phase 2: MCP Tools - Discussion Log

> **Audit trail only.** Do not use as input to planning, research, or execution agents.
> Decisions are captured in CONTEXT.md — this log preserves the alternatives considered.

**Date:** 2026-03-25
**Phase:** 02-mcp-tools
**Areas discussed:** Tool input contracts, Error response shape, carta_search result format, carta_scan output fields

---

## Tool Input Contracts

| Option | Description | Selected |
|--------|-------------|----------|
| `carta_search(query)` only | Fixed top_k at config default | |
| `carta_search(query, top_k=5)` | Caller controls result count | ✓ |
| `carta_search(query, top_k=5, doc_type=None)` | Full filter control | |
| `carta_embed` force always | Always re-embed | |
| `carta_embed(path, force=False)` | Skip if unchanged, force flag available | ✓ |

**User's choice:** `carta_search(query, top_k=5)` — simple default, optional count control. `carta_embed(path, force=False)` with mtime-based change detection.

**Notes:** User wanted simplicity to avoid context pollution while preserving control. Raised git-hash/commit-linked drift tracking as a longer-term idea — deferred to backlog. Mtime-based drift via sidecar is the Phase 2 approach.

---

## Error Response Shape

| Option | Description | Selected |
|--------|-------------|----------|
| Flat `{"error": "msg"}` | Minimal, no type info | |
| `{"error": "type", "detail": "msg"}` | Typed errors, flat structure | ✓ |
| Status envelope `{"status": "ok/error", ...}` | Uniform wrapper for all responses | |
| Raise McpError natively | FastMCP native exception handling | |

**User's choice:** Option 1 (flat typed errors) — `{"error": "<type>", "detail": "<message>"}`.

**Notes:** User agreed that success responses should stay lean (no wrapper), and that typed errors give Claude enough to distinguish failure modes.

---

## carta_search Result Format

| Option | Description | Selected |
|--------|-------------|----------|
| Minimal `{score, source, excerpt}` | 3 fields, ~300 char excerpt | ✓ |
| With extras `{score, source, excerpt, doc_type, chunk_index}` | Additional metadata fields | |

**User's choice:** Minimal — `score`, `source`, `excerpt` only. Excerpt capped at ~300 characters.

**Notes:** User agreed minimal is better for context hygiene. Raised idea of a deeper search mode integrated with the Ollama judge — noted as a Phase 3 design decision. Also flagged that the judge model doesn't have to be Ollama-only (API or Claude sub-agent alternatives worth exploring in Phase 3).

---

## carta_scan Output Fields

| Option | Description | Selected |
|--------|-------------|----------|
| Minimal `{pending: [...], drift: [...]}` | Flat path arrays only | ✓ |
| With metadata per file | Last-embedded date, chunk count, file size | |

**User's choice:** Minimal flat arrays — deeper per-file inspection happens on demand via `carta_embed`.

**Notes:** User noted that deeper scanning can happen per-file, so scan output just needs to surface which files need attention.

---

## Claude's Discretion

- FastMCP decorator pattern for handler registration
- Whether `run_scan()` already tracks drift via mtime or needs it added

## Deferred Ideas

- Git-hash / commit-linked embed tracking — backlog
- Deeper search mode with Ollama judge — Phase 3
- Judge model alternatives (API/Claude sub-agent) — Phase 3 design decision
