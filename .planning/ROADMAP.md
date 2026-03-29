# Roadmap: Carta v0.2

**Project:** Carta v0.2 — MCP server + smart hook milestone
**Created:** 2026-03-25
**Granularity:** Standard
**Coverage:** 23/23 v1 requirements mapped

## Phases

- [ ] **Phase 1: Pipeline Reliability + MCP Foundation** - Reliable embed pipeline, MCP scaffolding, plugin cache migration
- [ ] **Phase 2: MCP Tools** - Full carta_search / carta_embed / carta_scan tool surface live in Claude Code
- [x] **Phase 3: Smart Hook + Markdown Embedding** - Automatic context injection with threshold routing and Ollama judge (completed 2026-03-27)
- [x] **Phase 4: Bootstrap Hardening** - Stale cache assertions, gitignore deduplication, portable hook quoting (completed 2026-03-27)
- [x] **Phase 5: Hook Wiring + Entry Point Fix** - Wire shell stub to Python module, register carta-hook entry point, fix HOOK-05 fail-open logic
- [x] **Phase 6: Phase 3 Verification + Housekeeping** - Write Phase 3 VERIFICATION.md, update stale ROADMAP progress entries (completed 2026-03-28)

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
**Plans:** 3 plans

Plans:
- [x] 01-01-PLAN.md — Pipeline reliability fixes (batch upsert, timeout, overlap cap, verbose, sidecar current_path)
- [x] 01-02-PLAN.md — MCP server scaffold, .mcp.json registration, plugin cache cleanup
- [ ] 01-03-PLAN.md — Integration verification + human checkpoint

### Phase 2: MCP Tools
**Goal**: Claude can invoke carta_search, carta_embed, and carta_scan as working MCP tools with structured, attributed responses
**Depends on**: Phase 1
**Requirements**: MCP-02, MCP-03, MCP-04, MCP-05
**Success Criteria** (what must be TRUE):
  1. Claude can call `carta_search` and receive scored results with source path and chunk excerpt for each hit
  2. Claude can call `carta_embed` on a specific file path and the file is embedded with per-file timeout enforcement
  3. Claude can call `carta_scan` and receive a structured list of pending-embed and drift files
  4. `carta-mcp` is invokable as a standalone entrypoint (registered in `pyproject.toml`); tool calls do not raise exceptions on Qdrant or Ollama failure — they return structured error objects
**Plans:** 1/2 plans executed

Plans:
- [x] 02-01-PLAN.md — Service layer prep (find_config to config.py, file_mtime sidecar, run_embed_file adapter, drift detection)
- [x] 02-02-PLAN.md — MCP tool handlers (carta_search, carta_embed, carta_scan) + test suite

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
**Plans:** 3/3 plans complete

Plans:
- [x] 03-01-PLAN.md — Config schema update + markdown embedding support
- [x] 03-02-PLAN.md — Smart hook module with three-zone score routing and Ollama judge
- [x] 03-03-PLAN.md — Integration verification + human smoke test checkpoint

### Phase 4: Bootstrap Hardening
**Goal**: carta init is defensively correct — stale cache deletion is verified, gitignore is idempotent, and the hook command string is portable across project subdirectories
**Depends on**: Phase 3
**Requirements**: BOOT-01, BOOT-02, BOOT-03
**Success Criteria** (what must be TRUE):
  1. If plugin cache deletion fails or leaves residue, `carta init` prints a clear error rather than silently continuing
  2. Running `carta init` twice on the same project does not add duplicate gitignore entries when a parent glob already covers the target
  3. The hook fires correctly when Claude Code is launched from a project subdirectory — the `exec` quoting pattern resolves the project root portably
**Plans:** 1/1 plans complete

Plans:
- [x] 04-01-PLAN.md — Bootstrap hardening: cache residue exit, gitignore parent-glob skip, portable exec hook quoting + tests

### Phase 5: Hook Wiring + Entry Point Fix
**Goal:** The smart hook is fully wired end-to-end — `carta-prompt-hook.sh` calls the Python module, `carta-hook` is a registered command, and the HOOK-05 fail-open timeout logic is corrected
**Depends on**: Phase 4
**Requirements**: HOOK-01, HOOK-02, HOOK-03, HOOK-04, HOOK-05, HOOK-06, HOOK-07
**Gap Closure:** Closes gaps from v0.2 audit — shell stub wiring, pyproject.toml entry point, HOOK-05 logic inversion
**Success Criteria** (what must be TRUE):
  1. `carta-prompt-hook.sh` invokes `carta-hook` after the enabled check — the Python hook module is reachable
  2. `carta-hook` exists on PATH after `pip install` (registered in `pyproject.toml [project.scripts]`)
  3. On `TimeoutError` in `_judge_with_timeout`, the hook returns `True` (inject / fail open)
  4. Flow C works end-to-end: Claude Code hook triggers → shell stub → Python hook → inject/discard
**Plans:** 1 plan (1 complete)

Plans:
- [x] 05-01-PLAN.md — Wire hook shell stub, register carta-hook entry point, fix HOOK-05 fail-open

### Phase 6: Phase 3 Verification + Housekeeping
**Goal:** Phase 3 has a VERIFICATION.md confirming all HOOK-* and EMBED-01 requirements; ROADMAP.md progress table reflects actual completion state
**Depends on**: Phase 5
**Requirements**: EMBED-01
**Gap Closure:** Closes EMBED-01 (code wired, no verification cert); fixes stale ROADMAP.md progress table
**Success Criteria** (what must be TRUE):
  1. `phases/03-smart-hook-markdown-embedding/` contains a VERIFICATION.md with SATISFIED status for all Phase 3 requirements
  2. ROADMAP.md progress table shows Phases 1, 2, 3, 4, 5 with accurate completion status
**Plans:** 1/1 plans complete

Plans:
- [x] 06-01-PLAN.md — Write Phase 03 VERIFICATION.md; update ROADMAP.md stale progress entries

## Progress

| Phase | Plans Complete | Status | Completed |
|-------|----------------|--------|-----------|
| 1. Pipeline Reliability + MCP Foundation | 3/3 | Complete | 2026-03-27 |
| 2. MCP Tools | 2/2 | Complete | 2026-03-27 |
| 3. Smart Hook + Markdown Embedding | 3/3 | Complete | 2026-03-27 |
| 4. Bootstrap Hardening | 1/1 | Complete | 2026-03-27 |
| 5. Hook Wiring + Entry Point Fix | 1/1 | Complete | 2026-03-27 |
| 6. Phase 3 Verification + Housekeeping | 1/1 | Complete   | 2026-03-28 |

## Backlog

### Phase 999.1: Document Versioning, Stale Memory Tracking & Chunk Lifecycle

**Goal:** Each embedded document has a content hash that tracks mutations. Chunks in Qdrant carry generation/staleness metadata. When documents change, the system marks them stale. When documents are deleted or superseded, chunks are orphaned and cleaned. Claude Code (via MCP) can autonomously trigger re-embedding.
**Requirements:** HASH-01, HASH-02, SIDECAR-01, PAYLOAD-01, LIFECYCLE-01, LIFECYCLE-02, LIFECYCLE-03, MCP-01, MCP-02, MCP-03, STALE-01
**Plans:** 5/1 plans complete

Plans:
- [ ] 999.1-01-PLAN.md — lifecycle.py: hash primitives, stale marking, orphan cleanup (TDD)
- [ ] 999.1-02-PLAN.md — sidecar stub + Qdrant payload schema expansion (TDD)
- [ ] 999.1-03-PLAN.md — pipeline.py mtime fast-path + stale alert wiring
- [ ] 999.1-04-PLAN.md — MCP carta_embed scope parameter (TDD)


---

### Phase 999.2: Vision Model Pipeline for Image-Embedded PDF Content (BACKLOG)

**Goal:** Detect image-heavy pages in PDFs via PyMuPDF, extract image bytes, pass to a local vision model (LLaVA or moondream2 via Ollama), and embed the text description. Enables extracting data from charts, plots, and diagrams (e.g. temperature response curves, register timing diagrams in datasheets) that have no text layer.
**Requirements:** VIS-01, VIS-02, VIS-03, VIS-04, VIS-05, VIS-06, VIS-07, VIS-08, VIS-09
**Plans:** 2 plans

Plans:
- [ ] 999.2-01-PLAN.md — Vision module (vision.py) with image extraction, Ollama calls, fail-open (TDD)
- [ ] 999.2-02-PLAN.md — Pipeline integration + sidecar schema extension

---
*Created: 2026-03-26*

### Phase 999.3: Qdrant Collection Scoping — Repo Isolation, Shared Pool, and Cross-Repo Recall (BACKLOG)

**Goal:** By default, Carta queries are scoped to the current repository's collections only — no cross-contamination between projects. Users can opt in to a shared/global memory pool for cross-project recall (e.g. a quirk solved in a UI project surfaces in a new backend project). Optionally, a common `carta_global` collection holds memories explicitly promoted as project-agnostic. Collection naming and access control logic lives in `config.py::collection_name()` and the search pipeline.
**Requirements:** TBD
**Plans:** 0 plans

Plans:
- [ ] TBD (promote with /gsd:review-backlog when ready)

Key design questions for researcher:
- Default: repo-scoped only (current behavior with named collections)
- Opt-in: `cross_project_recall.enabled: true` already scaffolded in config — expand this
- Global pool: `carta_global_*` collections for explicitly promoted memories
- Scope levels: `repo` | `shared` | `global` (configurable per search call or globally)
- Depends on: 999.1 (doc_type taxonomy, protected collection types)

---
*Created: 2026-03-28*
