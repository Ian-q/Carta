# Requirements: Carta v0.2

**Defined:** 2026-03-25
**Core Value:** Relevant project knowledge surfaces automatically when Claude is working — without manual recall and without context noise.

## v1 Requirements

### Pipeline Reliability

- [x] **PIPE-01**: Embed pipeline upserts Qdrant chunks in batches of 32, reducing HTTP round trips from O(N) to O(N/32)
- [x] **PIPE-02**: Per-file embed enforces a configurable timeout (default 300s); files exceeding the limit are skipped with a warning rather than hanging the pipeline
- [x] **PIPE-03**: Chunking overlap capped at 25% of take size and safety counter lowered to 2× word count, guaranteeing forward progress on dense single-paragraph documents
- [x] **PIPE-04**: Pipeline service functions (`run_embed`, `run_search`, `run_scan`) accept a `verbose=False` parameter that suppresses `print()` output when called from non-CLI contexts
- [x] **PIPE-05**: Sidecar `.embed-meta.yaml` files include `current_path` on creation; any sidecar missing the field is auto-healed on next embed run

### MCP Server

- [x] **MCP-01**: MCP server scaffolded in `carta/mcp/` with all logging directed to `stderr`; all tool handlers return structured error objects instead of raising exceptions
- [x] **MCP-02**: `carta_search` MCP tool queries Qdrant and returns scored, source-attributed results (score, source path, chunk excerpt)
- [x] **MCP-03**: `carta_embed` MCP tool embeds a single specified file with per-file timeout enforcement inherited from PIPE-02
- [x] **MCP-04**: `carta_scan` MCP tool returns structured scan results listing pending-embed and drift files
- [x] **MCP-05**: `carta-mcp` packaged as a separate `[project.scripts]` entrypoint in `pyproject.toml`, invokable as `carta-mcp`
- [x] **MCP-06**: `.mcp.json` added to project root as the sole Carta registration point for Claude Code; plugin cache registration removed entirely
- [x] **MCP-07**: `carta init` automatically removes stale `~/.claude/plugins/carta/` cache directories from v0.1.x installations; cleanup is verified with a post-deletion assertion

### Smart Hook

- [x] **HOOK-01**: `hooks/hook.sh` registered as `UserPromptSubmit` handler; extracts semantic query from prompt text and queries Qdrant via the shared search service
- [x] **HOOK-02**: Fast path: similarity score >0.85 → inject retrieved chunks immediately without calling Ollama
- [x] **HOOK-03**: Noise gate: similarity score <0.60 → discard candidates without injection
- [x] **HOOK-04**: Gray zone (0.60–0.85): `hooks/judge.py` calls Ollama (`qwen2.5:0.5b` default) for a binary relevance judgment; inject only on "yes"
- [x] **HOOK-05**: Hard 3-second wall-clock timeout wraps all Ollama judge calls; on timeout, hook fails open (no injection, prompt proceeds unblocked)
- [x] **HOOK-06**: Maximum 5 injected chunks per prompt enforced regardless of score band, preventing context flooding
- [x] **HOOK-07**: Similarity thresholds (high/low bounds) and judge model are configurable in `.carta/config.yaml`

### Embedding Improvements

- [x] **EMBED-01**: Markdown files (`.md`) processed by the embed pipeline alongside PDFs — text extracted directly, chunked with same logic, upserted to Qdrant with `file_type: markdown` in sidecar

### Bootstrap Hardening

- [x] **BOOT-01**: `_install_skills()` verifies stale cache deletion with a post-deletion assertion; prints a clear error if residue remains rather than silently continuing
- [x] **BOOT-02**: `_update_gitignore()` skips entries already covered by a parent directory glob already present in `.gitignore`
- [x] **BOOT-03**: Hook command string uses portable `exec "$(git rev-parse --show-toplevel)/..."` quoting pattern

## v2 Requirements

### Operator Visibility

- **OPS-01**: `carta status` CLI command prints Qdrant health, Ollama health, collection sizes, and pending file count in a single view
- **OPS-02**: `carta_status` MCP tool returns same structured health data as `carta status` for agent consumption
- **OPS-03**: `carta doctor` detects and reports cache conflicts between old plugin cache and new MCP registration

### Search Enhancement

- **SRCH-01**: Search supports hybrid semantic + keyword (BM25) scoring with configurable blend weight
- **SRCH-02**: Per-collection routing allows monorepo projects to scope search to a named sub-collection

## Out of Scope

| Feature | Reason |
|---------|--------|
| Plugin cache + MCP hybrid coexistence | Hybrid mode reproduces Issue #7 stale-version bug; hard cutover only |
| Claude-as-judge for relevance | Creates recursive tool call complexity inside the hook; Ollama judge is local and latency-bounded |
| Blind injection (inject on every prompt) | Produces context noise; three-zone filter is the intended design |
| Batched Ollama embedding requests | Ollama `/api/embeddings` does not support batch input natively |
| SSE transport for MCP server | Adds HTTP server process complexity with no benefit for a local-only tool |
| Multi-user or cloud-hosted Carta | Local tool only; no auth surface needed |
| Real-time file watching / auto-embed on save | Complexity without clear benefit; user-initiated `carta embed` is sufficient |

## Traceability

| Requirement | Phase | Status |
|-------------|-------|--------|
| PIPE-01 | Phase 1 | Complete |
| PIPE-02 | Phase 1 | Complete |
| PIPE-03 | Phase 1 | Complete |
| PIPE-04 | Phase 1 | Complete |
| PIPE-05 | Phase 1 | Complete |
| MCP-01 | Phase 1 | Complete |
| MCP-02 | Phase 2 | Complete |
| MCP-03 | Phase 2 | Complete |
| MCP-04 | Phase 2 | Complete |
| MCP-05 | Phase 2 | Complete |
| MCP-06 | Phase 1 | Complete |
| MCP-07 | Phase 1 | Complete |
| HOOK-01 | Phase 5 | Complete |
| HOOK-02 | Phase 5 | Complete |
| HOOK-03 | Phase 5 | Complete |
| HOOK-04 | Phase 5 | Complete |
| HOOK-05 | Phase 5 | Complete |
| HOOK-06 | Phase 5 | Complete |
| HOOK-07 | Phase 5 | Complete |
| EMBED-01 | Phase 6 | Complete |
| BOOT-01 | Phase 4 | Complete |
| BOOT-02 | Phase 4 | Complete |
| BOOT-03 | Phase 4 | Complete |

## Phase 999.3 Requirements

### Collection Scoping

- [ ] **SCOPE-01**: `get_search_collections(cfg, scope="repo")` returns only current project collections (`{project_name}_doc`, `_notes`, `_session`)
- [ ] **SCOPE-02**: `get_search_collections(cfg, scope="shared")` returns current project + permitted projects based on `cross_project_recall.project_filter`
- [ ] **SCOPE-03**: `get_search_collections(cfg, scope="global")` returns only `carta_global_*` collections
- [ ] **SCOPE-04**: Collection discovery queries Qdrant `/collections` endpoint and filters to Carta-related collections only
- [ ] **SCOPE-05**: `cross_project_recall.default_search_scope` config option controls default behavior ("repo" | "shared" | "global")

### Multi-Platform Support

- [ ] **PLATFORM-01**: `carta init` creates both `.mcp.json` (Claude Code) and `.opencode.json` (OpenCode) MCP configuration files
- [ ] **PLATFORM-02**: MCP tools (`carta_search`, `carta_embed`, `carta_scan`) work identically on both Claude Code and OpenCode
- [ ] **PLATFORM-03**: Documentation mentions both Claude Code and OpenCode as supported platforms

### Global Memory Pool (Future)

- [ ] **GLOBAL-01**: `carta_promote` MCP tool moves a chunk from project collection to `carta_global_*` (backlog)
- [ ] **GLOBAL-02**: Auto-promotion of high-relevance chunks based on `cross_project_recall.global_pool.auto_promote` setting (backlog)

**Coverage:**
- v1 requirements: 23 total
- v2/999.x requirements: 10 additional
- Mapped to phases: 23 + 8 pending
- Unmapped: 0

---
*Requirements defined: 2026-03-25*
*Last updated: 2026-04-01 after Phase 999.3 planning*
