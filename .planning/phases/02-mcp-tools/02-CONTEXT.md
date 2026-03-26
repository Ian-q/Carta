# Phase 2: MCP Tools - Context

**Gathered:** 2026-03-25
**Status:** Ready for planning

<domain>
## Phase Boundary

Implement `carta_search`, `carta_embed`, and `carta_scan` as working FastMCP tool handlers in the existing scaffold. These tools delegate to the shared service layer (`run_search`, `run_embed`, `run_scan`). Phase 2 delivers the tool surface only — the smart hook (Ollama judge, automatic injection) belongs in Phase 3.

Note: MCP-05 (`carta-mcp` entrypoint in `pyproject.toml`) is already complete from Phase 1.

</domain>

<decisions>
## Implementation Decisions

### carta_search

- **D-01:** Tool signature: `carta_search(query: str, top_k: int = 5)` — simple default, caller controls result count when needed. No doc_type filter in Phase 2.
- **D-02:** Success response: list of result dicts — `[{"score": float, "source": str, "excerpt": str}]`. Excerpt capped at ~300 characters (~2-3 sentences). No extra fields (doc_type, chunk_index, collection) — they add tokens without helping Claude reason.

### carta_embed

- **D-03:** Tool signature: `carta_embed(path: str, force: bool = False)` — skip if already embedded and file mtime unchanged; re-embed regardless when `force=True`.
- **D-04:** Change detection uses file mtime stored in the sidecar `.embed-meta.yaml`. If mtime matches, skip. If mtime is newer, treat as drift and proceed. No git integration in Phase 2.
- **D-05:** Success response: `{"status": "ok", "chunks": N}` — chunk count confirms the embed ran. On skip (already current): `{"status": "skipped", "reason": "already embedded, file unchanged"}`.

### carta_scan

- **D-06:** Tool signature: `carta_scan()` — no parameters; scans the full project as configured.
- **D-07:** Success response: `{"pending": ["path/a.pdf", ...], "drift": ["path/b.pdf", ...]}` — flat path arrays. No metadata per file (last-embedded date, chunk count, etc.) in Phase 2; deeper per-file inspection happens on demand via `carta_embed`.
- **D-08:** "Pending" = no sidecar exists. "Drift" = sidecar exists but file mtime is newer than `embedded_at` in sidecar. Planner to verify this logic is present in `run_scan()` or add it.

### Error Handling

- **D-09:** All tools return structured error dicts on failure — never raise exceptions. Uniform shape: `{"error": "<type>", "detail": "<human message>"}`.
- **D-10:** Error types to distinguish across all tools:
  - `service_unavailable` — Qdrant or Ollama unreachable
  - `file_not_found` — path does not exist (carta_embed)
  - `timeout` — per-file timeout exceeded (carta_embed, inherited from PIPE-02)
  - `collection_not_found` — project not initialized

### Claude's Discretion

- FastMCP decorator pattern (`@mcp_server.tool()`) for handler registration — standard SDK usage, no surprises expected.
- Whether `run_scan()` already tracks drift via mtime or needs that logic added — planner to check and implement as needed.

</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### Requirements
- `.planning/REQUIREMENTS.md` — MCP-02, MCP-03, MCP-04, MCP-05 with exact acceptance criteria
- `.planning/ROADMAP.md` — Phase 2 success criteria (4 items); phase dependency on Phase 1

### Existing Implementation
- `carta/mcp/server.py` — FastMCP scaffold; tool handlers attach here via `@mcp_server.tool()`
- `carta/embed/pipeline.py` — `run_embed()`, `run_search()`, `run_scan()` with `verbose=False` — primary delegation targets
- `carta/embed/embed.py` — `upsert_chunks()` batch implementation (Phase 1); referenced by `run_embed()`
- `carta/embed/induct.py` — sidecar read/write; `embedded_at` and `current_path` fields; mtime drift check goes here
- `carta/scanner/scanner.py` — scan logic; verify/add pending vs drift distinction
- `pyproject.toml` — `carta-mcp` entrypoint already registered (MCP-05 complete)

</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets
- `mcp_server = FastMCP("carta")` in `server.py` — tool handlers register with `@mcp_server.tool()` decorator, no boilerplate needed
- `run_search(cfg, query, verbose=False)` — callable directly from tool handler; returns Qdrant results
- `run_embed(cfg, path, verbose=False)` — single-file embed with timeout enforcement (PIPE-02); reuse as-is
- `run_scan(cfg, verbose=False)` — scanner pipeline; may need drift detection added

### Established Patterns
- Wire-protocol discipline from Phase 1: stderr logging only, no `print()`, no `sys.exit()`, no unhandled exceptions
- Config dict passed as parameter — tool handlers load config via `load_config()` the same way CLI commands do
- Structured error returns already established as the pattern in the scaffold docstring

### Integration Points
- `server.py` is the only file that changes in `carta/mcp/` — add tool handler functions decorated with `@mcp_server.tool()`
- `induct.py::write_sidecar()` / `_update_sidecar()` — mtime field may need adding for drift detection
- No CLI layer involvement — tool handlers bypass `carta/cli.py` entirely

</code_context>

<specifics>
## Specific Ideas

- Deeper search with Ollama judge integration (Phase 3): user wants an option where the judge has access to more excerpt context for in-depth relevance checking. Not Phase 2 scope but should be designed into Phase 3.
- Judge model flexibility: the Ollama judge does not have to be Ollama-only — API-driven or Claude sub-agent alternatives should be discussed as a Phase 3 design decision.

</specifics>

<deferred>
## Deferred Ideas

- **Git-hash / commit-linked embed tracking:** Link each embed to the git commit hash at time of embedding; use content hash for deduplication across versions; schedule re-embeds on change detection. Meaningful architectural direction — defer to backlog (beyond v0.2 scope).
- **Deeper search mode with judge:** A `carta_search` variant that passes more context to the Ollama judge for deeper relevance filtering. Phase 3 design decision — judge lives in the hook phase.
- **Judge model alternatives (API/Claude sub-agent):** The relevance judge doesn't have to be Ollama — explore API-driven or Claude Code sub-agent options. Phase 3 design decision.

</deferred>

---

*Phase: 02-mcp-tools*
*Context gathered: 2026-03-25*
