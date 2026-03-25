# Roadmap: Carta v0.2

**Project:** Carta v0.2 — MCP server + smart hook milestone
**Created:** 2026-03-25
**Granularity:** Standard
**Coverage:** 23/23 v1 requirements mapped

## Phases

- [ ] **Phase 1: Pipeline Reliability + MCP Foundation** - Reliable embed pipeline, MCP scaffolding, plugin cache migration
- [ ] **Phase 2: MCP Tools** - Full carta_search / carta_embed / carta_scan tool surface live in Claude Code
- [ ] **Phase 3: Smart Hook + Markdown Embedding** - Automatic context injection with threshold routing and Ollama judge
- [ ] **Phase 4: Bootstrap Hardening** - Stale cache assertions, gitignore deduplication, portable hook quoting

## Phase Details

### Phase 1: Pipeline Reliability + MCP Foundation
**Goal**: The embed pipeline is reliable and the MCP server scaffold is in place with correct wire-protocol discipline — no stdout pollution, no unhandled exceptions, no plugin cache conflicts
**Depends on**: Nothing (first phase)
**Requirements**: PIPE-01, PIPE-02, PIPE-03, PIPE-04, PIPE-05, MCP-01, MCP-06, MCP-07
**Success Criteria** (what must be TRUE):
  1. Running `carta embed` on a dense PDF completes without hanging — batched Qdrant upserts and per-file timeout fire as expected
  2. Running `carta-mcp` produces a clean JSON-RPC stream on stdout with all log output on stderr only
  3. Running `carta init` on a machine with a v0.1.x plugin cache removes the stale cache directory and prints confirmation
  4. `.mcp.json` is present at project root and is the sole Carta registration point; no plugin cache entry exists
  5. Sidecar files written or re-embedded include `current_path`; sidecars missing the field are healed automatically
**Plans**: TBD

### Phase 2: MCP Tools
**Goal**: Claude can invoke carta_search, carta_embed, and carta_scan as working MCP tools with structured, attributed responses
**Depends on**: Phase 1
**Requirements**: MCP-02, MCP-03, MCP-04, MCP-05
**Success Criteria** (what must be TRUE):
  1. Claude can call `carta_search` and receive scored results with source path and chunk excerpt for each hit
  2. Claude can call `carta_embed` on a specific file path and the file is embedded with per-file timeout enforcement
  3. Claude can call `carta_scan` and receive a structured list of pending-embed and drift files
  4. `carta-mcp` is invokable as a standalone entrypoint (registered in `pyproject.toml`); tool calls do not raise exceptions on Qdrant or Ollama failure — they return structured error objects
**Plans**: TBD
**UI hint**: no

### Phase 3: Smart Hook + Markdown Embedding
**Goal**: Relevant documentation surfaces automatically on UserPromptSubmit without context noise; markdown files are embeddable alongside PDFs
**Depends on**: Phase 2
**Requirements**: HOOK-01, HOOK-02, HOOK-03, HOOK-04, HOOK-05, HOOK-06, HOOK-07, EMBED-01
**Success Criteria** (what must be TRUE):
  1. On a high-similarity prompt (score >0.85), the hook injects matching chunks into the session without calling Ollama
  2. On a low-similarity prompt (score <0.60), the hook discards candidates and the prompt proceeds with no injection
  3. On a gray-zone prompt (0.60–0.85), the hook calls the Ollama judge and injects only on a "yes" verdict; if the judge call exceeds 3 seconds the prompt proceeds unblocked
  4. No more than 5 chunks are ever injected in a single prompt regardless of score band
  5. Threshold values (high/low bounds) and judge model are readable from `.carta/config.yaml`; running `carta embed` on a `.md` file embeds it to Qdrant with `file_type: markdown` in the sidecar
**Plans**: TBD

### Phase 4: Bootstrap Hardening
**Goal**: carta init is defensively correct — stale cache deletion is verified, gitignore is idempotent, and the hook command string is portable across project subdirectories
**Depends on**: Phase 3
**Requirements**: BOOT-01, BOOT-02, BOOT-03
**Success Criteria** (what must be TRUE):
  1. If plugin cache deletion fails or leaves residue, `carta init` prints a clear error rather than silently continuing
  2. Running `carta init` twice on the same project does not add duplicate gitignore entries when a parent glob already covers the target
  3. The hook fires correctly when Claude Code is launched from a project subdirectory — the `exec` quoting pattern resolves the project root portably
**Plans**: TBD

## Progress

| Phase | Plans Complete | Status | Completed |
|-------|----------------|--------|-----------|
| 1. Pipeline Reliability + MCP Foundation | 0/? | Not started | - |
| 2. MCP Tools | 0/? | Not started | - |
| 3. Smart Hook + Markdown Embedding | 0/? | Not started | - |
| 4. Bootstrap Hardening | 0/? | Not started | - |

---
*Created: 2026-03-25*
