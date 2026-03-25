# Phase 1: Pipeline Reliability + MCP Foundation - Discussion Log

> **Audit trail only.** Do not use as input to planning, research, or execution agents.
> Decisions are captured in CONTEXT.md — this log preserves the alternatives considered.

**Date:** 2026-03-25
**Phase:** 01-pipeline-reliability-mcp-foundation
**Areas discussed:** Plugin cache cleanup path (D), Sidecar auto-heal scope (C)

---

## Areas A & B — Not Discussed

User confirmed specs for MCP library choice and `.mcp.json` timing were solid with no major notes. Captured as Claude's Discretion in CONTEXT.md.

---

## C — Sidecar Auto-Heal Scope

| Option | Description | Selected |
|--------|-------------|----------|
| Heal only active files | Only fix `current_path` on sidecars being processed in the current `carta embed` run | |
| Full-repo scan | On every `carta embed`, scan ALL `*.embed-meta.yaml` files and heal any missing `current_path` | ✓ |

**User's choice:** Full-repo scan on `carta embed`

**Notes:** User described a broader vision for sidecars as first-class semantic artifacts — agent-enriched with project-specific notes (page pointers, quirks, cross-links), embedded as Qdrant chunks in their own right, forming a web of semantic memory that lets Claude navigate large documents precisely. Phase 1 scope is the mechanical `current_path` fix only; the enrichment vision is captured as a deferred idea for a future milestone.

---

## D — Plugin Cache Cleanup Path

| Option | Description | Selected |
|--------|-------------|----------|
| Old path only | Remove `~/.claude/plugins/carta/` as MCP-07 specifies | |
| Current path only | Remove `~/.claude/plugins/cache/carta-cc/carta-cc/{version}/` | |
| Both paths | Remove old v0.1.x path AND current cache path — full elimination | ✓ |

**User's choice:** Both paths

**Notes:** Full elimination ensures no two-registry situation where plugin cache and `.mcp.json` coexist. User agreed with the reasoning without hesitation.

---

## Claude's Discretion

- **MCP library choice:** Use official `mcp` Python SDK unless researcher finds strong reason to hand-roll JSON-RPC
- **`carta-mcp` entrypoint timing:** Planner to verify whether Phase 1 scaffold should include the `pyproject.toml` entrypoint so `.mcp.json` registers a runnable server

## Deferred Ideas

- Sidecar enrichment — agent-populated notes, sidecars as Qdrant chunks, cross-linking — future milestone
