# Phase 1: Pipeline Reliability + MCP Foundation - Context

**Gathered:** 2026-03-25
**Status:** Ready for planning

<domain>
## Phase Boundary

Fix the embed pipeline's hanging/slowness bugs (batch upserts, per-file timeout, overlap cap, verbose param, sidecar current_path), scaffold the MCP server with correct wire-protocol discipline (stderr-only logging, structured errors), and fully migrate away from the plugin cache to `.mcp.json` as the sole Carta registration point.

New capabilities (tool handlers, search/embed/scan MCP tools) belong in Phase 2.

</domain>

<decisions>
## Implementation Decisions

### Pipeline Reliability

- **D-01:** `upsert_chunks()` sends chunks to Qdrant in batches of 32 (single `client.upsert()` call per batch, not one call per chunk). Batch size is the PIPE-01 spec — not configurable in Phase 1.
- **D-02:** Per-file embed enforces a 300s wall-clock timeout (PIPE-02 spec). Files exceeding the limit are skipped with a warning; the pipeline continues with remaining files.
- **D-03:** Chunking overlap capped at 25% of take size; safety counter lowered to 2× word count to guarantee forward progress on dense single-paragraph documents (PIPE-03 spec).
- **D-04:** `run_embed`, `run_search`, `run_scan` each accept a `verbose=False` parameter. When False, all `print()` output is suppressed — callers (MCP handlers, tests) can run these without stdout pollution.
- **D-05:** `write_sidecar()` writes `current_path` (relative path from repo root) on creation. On `carta embed`, a full-repo scan of all `*.embed-meta.yaml` files heals any that are missing the field — not just files being actively processed in that run.

### MCP Server Scaffold

- **D-06:** `carta/mcp/` directory created with a minimal stdio JSON-RPC server. Phase 1 delivers wire-protocol discipline only (correct stderr logging, structured error returns, no unhandled exceptions, no stdout pollution) — no tool handlers yet; those are Phase 2.
- **D-07:** `.mcp.json` added to project root as the sole Carta registration point. Plugin cache registration is removed entirely — no hybrid coexistence.

### Plugin Cache Migration

- **D-08:** `carta init` removes **both** plugin cache paths:
  - `~/.claude/plugins/carta/` (old v0.1.x path per MCP-07 spec)
  - `~/.claude/plugins/cache/carta-cc/carta-cc/{version}/` (current cache path found in codebase)

  Both are cleaned to guarantee complete elimination of the two-registry problem. Post-deletion assertion verifies no residue remains; prints a clear error if residue is found rather than silently continuing.

### Claude's Discretion

- **MCP library choice:** Use the official `mcp` Python SDK for the stdio server scaffold unless the researcher finds a strong reason to hand-roll JSON-RPC. SDK handles transport framing and reduces implementation surface.
- **`carta-mcp` entrypoint timing:** Phase 1 scaffold should include the `carta-mcp` script entry in `pyproject.toml` so `.mcp.json` can register a runnable (even if tool-less) server. Planner should verify whether this pulls MCP-05 partially into Phase 1 or whether a minimal server can be invoked without the formal entrypoint.

</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### Requirements
- `.planning/REQUIREMENTS.md` — PIPE-01 through PIPE-05, MCP-01, MCP-06, MCP-07 with exact acceptance criteria
- `.planning/ROADMAP.md` — Phase 1 success criteria (5 items); sequencing constraints

### Existing Implementation
- `carta/embed/embed.py` — current `upsert_chunks()` sequential implementation (target of PIPE-01 batch fix)
- `carta/embed/pipeline.py` — `run_embed()` pipeline flow; no timeout currently (target of PIPE-02)
- `carta/embed/parse.py` — `chunk_text()` overlap logic (target of PIPE-03)
- `carta/embed/induct.py` — `write_sidecar()` missing `current_path` (target of PIPE-05)
- `carta/install/bootstrap.py` — `_install_skills()` plugin cache management (target of MCP-07)
- `pyproject.toml` — current `[project.scripts]` (single `carta` entry; `carta-mcp` to be added)

</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets
- `carta/embed/embed.py::upsert_chunks()`: Modify in-place to batch — function signature stays the same, internal loop changes to accumulate points and flush at 32
- `carta/embed/induct.py::write_sidecar()` / `_update_sidecar()`: Add `current_path` field here; auto-heal pass can reuse same YAML read/write pattern
- `carta/config.py::collection_name()`: Reusable for MCP server to resolve collection names
- Existing `requests`-based Ollama HTTP calls: Pattern to follow for MCP scaffold's service calls

### Established Patterns
- Errors to stderr, progress to stdout — MCP server inverts this (everything to stderr, JSON-RPC to stdout only)
- Config dict passed as parameter, not global — MCP handlers should follow same pattern
- `sys.exit(1)` on error, `sys.exit(0)` on success — MCP server should NOT call sys.exit; return structured errors instead

### Integration Points
- `carta/cli.py::cmd_embed()` calls `run_embed()` — PIPE-04 verbose param must not break this call site
- `carta/install/bootstrap.py::cmd_init()` — MCP-07 cache cleanup hooks in here
- `pyproject.toml` — `carta-mcp` entrypoint added alongside existing `carta` entry

</code_context>

<specifics>
## Specific Ideas

### Sidecar Enrichment Vision (Deferred)
The user has a clear long-term vision for sidecars as first-class semantic artifacts: on initial embedding, an agent reviews the source document and populates the sidecar with project-specific notes (e.g., precise page/table/section pointers, project-relevant quirks). The sidecar itself lives in Qdrant as an embeddable chunk. When the smart hook fires, surfacing the sidecar gives Claude a precise pointer to the exact location in a large document without scanning the whole thing. Sidecars cross-link to related documents, forming a web of enriched semantic memory.

This is a meaningful architectural direction. Phase 1 lays the mechanical foundation (`current_path`, full-repo heal). The enrichment layer is a future milestone capability.

</specifics>

<deferred>
## Deferred Ideas

- **Sidecar enrichment / agent-populated notes:** Agent reviews source documents on embed and writes project-specific annotations into sidecar files; sidecars embedded as first-class Qdrant chunks; cross-linking between related sidecars. Future milestone — not v0.2 scope.
- **`carta status` / `carta doctor` commands:** Operator visibility (OPS-01, OPS-02, OPS-03) — v2 requirements, not this phase.

</deferred>

---

*Phase: 01-pipeline-reliability-mcp-foundation*
*Context gathered: 2026-03-25*
