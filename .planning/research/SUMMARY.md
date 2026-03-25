# Project Research Summary

**Project:** Carta v0.2 — MCP server + smart hook milestone
**Domain:** Semantic memory sidecar for AI coding agents (local RAG over project docs)
**Researched:** 2026-03-25
**Confidence:** HIGH

## Executive Summary

Carta v0.2 is a three-tier semantic memory system for Claude Code: a CLI for human-driven batch operations, an MCP stdio server for Claude-initiated retrieval and embedding, and a UserPromptSubmit hook for automatic context injection. The existing Python stack (Qdrant, Ollama, PyMuPDF, PyYAML) requires only one new dependency — the official `mcp>=1.7.1` package, which bundles FastMCP. The architecture is a thin-entry-point pattern: CLI, MCP server, and hook all delegate to the same shared `carta/embed/pipeline.py` and `carta/scanner/scanner.py` service layer, with no subprocess boundaries or logic duplication.

The recommended build order is strict: pipeline reliability fixes (batch upsert, per-file timeout) must land before the MCP server is exposed, and the MCP server must be stable before the hook is built. This order is not preference — it is a hard dependency chain. An unreliable embed pipeline behind an MCP tool creates agent deadlocks; a hook built before the search API stabilizes requires rework when MCP tool development aligns the interface.

The dominant risks are implementation-level, not architectural: stdout pollution in the MCP server process corrupts the JSON-RPC stream silently, Ollama cold-start can freeze the synchronous hook for 13–46 seconds without a timeout fallback, and stale v0.1.x plugin cache entries will conflict with MCP registration if cache cleanup is not automated. All three risks have clear mitigations and must be addressed in Phase 1 scaffolding, not deferred to integration testing.

## Key Findings

### Recommended Stack

The existing stack is retained without change. The only addition is `mcp>=1.7.1` (official Anthropic/MCP SDK), which provides `FastMCP`, the `@mcp.tool()` decorator, and `mcp.run(transport="stdio")` in a single package. The standalone `fastmcp` PyPI package (by jlowin) is a community fork and must not be used — the official bundled version is the correct choice.

The Ollama judge reuses the existing `requests` library against `/api/generate`. No additional Ollama client package is needed. stdio transport is correct for a local-only tool; SSE adds HTTP server complexity with no benefit.

**Core technologies:**
- `mcp>=1.7.1`: MCP server + stdio transport — official SDK, FastMCP bundled, zero-schema tool registration via type hints
- `requests` (existing): Ollama judge calls — already present, covers `/api/generate` without new dependency
- `qdrant-client` (existing): Vector search — shared across CLI, MCP, and hook via `pipeline.py`
- `qwen2.5:0.5b`: Ollama judge model — 0.5B parameters keeps gray-zone judge latency under 500ms; make configurable in `.carta/config.yaml`

### Expected Features

The v0.2 MVP is three MCP tools plus the smart hook. All features are well-defined; the dependency chain between them is strict.

**Must have (table stakes):**
- `carta_search` with scored, attributed results — core retrieval; scores must be returned raw, not hidden
- `carta_embed` with idempotency and reliability fixes — agents retry on failure; batch upsert and per-file timeout are hard prerequisites
- `carta_scan` for inventory — enables scan-then-embed agent workflow without forcing full corpus embed
- MCP stdio server registered via `.mcp.json` — plugin cache eliminated completely; no hybrid coexistence
- Markdown file embedding — most project docs are `.md`; absence is a functional gap
- Smart hook: fast-path thresholds (>0.85 inject, <0.60 discard) + Ollama gray-zone judge (0.60–0.85)

**Should have (competitive advantage):**
- Three-tier push+pull architecture — unique; no comparable local tool does automatic injection
- Ollama gray-zone judge — better precision than a fixed cosine threshold; avoids context pollution from false positives
- Sidecar metadata exposed via `carta_scan` — enables targeted re-embed rather than full corpus rebuild

**Defer to v0.2.x:**
- `carta_status` MCP tool and `carta status` CLI — useful diagnostic but not blocking for initial release
- Configurable thresholds in config — add after default values are validated in practice

**Defer to v0.3+:**
- Hybrid semantic + keyword search (BM25 boost)
- Per-collection routing for monorepo use cases

### Architecture Approach

Three entry points (CLI, MCP server, hook) sit atop a shared service layer (`pipeline.py`, `scanner.py`, `config.py`). Each entry point is a thin adapter that translates its input format and delegates to service layer functions with no business logic in the entry point itself. The MCP server lives in `carta/mcp/` as a new subpackage; hook scripts live in `hooks/` outside the package (shell entry point is not a Python module). The CLI (`carta/cli.py`) is never imported by MCP or hook tiers, enforcing clean isolation.

**Major components:**
1. `carta/mcp/server.py` + `carta/mcp/tools.py` — FastMCP instance, tool handlers delegating to service layer; all logging to stderr
2. `hooks/hook.sh` + `hooks/judge.py` — shell entry point for UserPromptSubmit, threshold routing, Ollama judge call with hard timeout
3. `carta/embed/pipeline.py` — shared embed/search core; must accept `verbose=False` to suppress print output when called from MCP context
4. `.mcp.json` — single registration source; replaces plugin cache entirely

### Critical Pitfalls

1. **stdout pollution breaks MCP JSON-RPC** — Any `print()` to stdout in the server process corrupts the wire protocol. Set `logging.basicConfig(stream=sys.stderr)` before any imports; add `verbose=False` parameter to pipeline functions. Address in Phase 1 before writing any tool handler.

2. **Ollama cold-start freezes the synchronous hook** — Without a model resident in memory, the first judge call takes 13–46 seconds. Implement a hard wall-clock timeout (3s) around the judge call with fail-open behavior (pass through without injection on timeout). Set `OLLAMA_KEEP_ALIVE=-1` as a required config step. Address in Phase 2 before wiring any blocking Ollama call.

3. **Plugin cache residue conflicts with MCP registration** — Stale `~/.claude/plugins/carta/` entries from v0.1.x cause the old skill to win over the new MCP tool. Cache cleanup must be automated in `carta init` migration path, not documentation-only. Address in Phase 1 (migration bootstrap).

4. **MCP server crash silently leaves tool calls failing** — No automatic restart mechanism. Wrap all tool handlers in `try/except`; return structured errors on Qdrant/Ollama failure rather than raising. Address in Phase 1 scaffolding.

5. **Hook subdirectory trigger bug** — Confirmed Claude Code bugs (#8810, #17277) where UserPromptSubmit hooks may not fire when launched from a project subdirectory or on the first prompt. The MCP pull path (`carta_search`) is the reliable fallback; do not design the UX assuming hook injection is 100% reliable.

## Implications for Roadmap

Based on research, the build order is determined by hard dependency chains, not preference. Four phases map directly to the architecture's dependency graph.

### Phase 1: Foundation — Pipeline Reliability + MCP Scaffolding + Migration

**Rationale:** Everything else depends on a stable embed/search API and a clean MCP wire protocol. Fixing the pipeline before exposing it via MCP prevents agent deadlocks. Establishing stdout discipline and error handling before writing tool handlers avoids retrofitting these cross-cutting concerns later. Plugin cache cleanup must happen before MCP tools are tested to avoid false negatives from cache conflicts.

**Delivers:** Reliable `pipeline.py` (batch upsert, per-file timeout, `verbose=False`), MCP server scaffolding with correct logging/error handling, automated plugin cache migration, `.mcp.json` registration.

**Addresses:** `carta_search`, `carta_embed`, `carta_scan` (tool handler shells), Markdown embedding support.

**Avoids:** stdout pollution pitfall, MCP crash pitfall, plugin cache conflict pitfall.

### Phase 2: MCP Tools — Full Tool Surface

**Rationale:** With a reliable service layer and correct MCP scaffolding, tool handlers are straightforward wrappers. `carta_scan` is the thinnest tool (scanner already exists); `carta_embed` requires the reliability fixes from Phase 1; `carta_search` is the core retrieval path that the hook will also use.

**Delivers:** `carta_search`, `carta_embed`, `carta_scan` as working MCP tools with structured responses; `.mcp.json` tested in Claude Code; plugin cache fully eliminated.

**Uses:** `mcp>=1.7.1` FastMCP, shared `pipeline.py` and `scanner.py`, `verbose=False` discipline established in Phase 1.

**Implements:** `carta/mcp/` subpackage; `.mcp.json` as sole registration mechanism.

### Phase 3: Smart Hook — Auto-Injection

**Rationale:** The hook uses the same `run_search()` interface as `carta_search`. Building the hook after the MCP search tool means the interface is already stable — the hook becomes a thin caller of the same function rather than a parallel implementation. Fast-path threshold logic ships first; Ollama judge is added once fast-path behavior is verified.

**Delivers:** `hooks/hook.sh` + `hooks/judge.py`; fast-path inject/discard; gray-zone Ollama judge with timeout fallback; chunk cap enforcement (3–5 max per prompt); `OLLAMA_KEEP_ALIVE` documented as required.

**Avoids:** Ollama cold-start freeze (timeout fallback), context over-injection (chunk cap + judge filter), hook subdirectory bug (MCP pull path as documented fallback).

### Phase 4: Operator UX — Status + Diagnostics

**Rationale:** `carta status` CLI and `carta_status` MCP tool are independent of the core tiers. They add operator visibility but do not unblock any other work. Ship after Phase 3 is validated in real use.

**Delivers:** `carta status` CLI command (Qdrant + Ollama health, pending file count), `carta_status` MCP tool, `carta doctor` cache conflict detection.

**Addresses:** P2 features from FEATURES.md priority matrix.

### Phase Ordering Rationale

- Phase 1 before Phase 2: The MCP `carta_embed` tool wraps the embed pipeline. An unreliable pipeline produces an unreliable tool that agents retry endlessly — the pipeline must be fixed first.
- Phase 2 before Phase 3: Hook and MCP server share `run_search()`. Aligning that interface once during Phase 2 avoids rework when Phase 3 is built.
- Hard cutover on plugin cache: Hybrid cache + MCP coexistence reproduces the stale-version bug that motivated this migration. No gradual migration — full cutover in Phase 1.
- Phase 4 last: Diagnostic tooling has no dependencies on Phase 3 but also does not unblock anything; defer until the core tiers are stable.

### Research Flags

Phases with well-documented patterns (research-phase not needed):
- **Phase 1:** Pipeline reliability patterns (batch upsert, timeout) are standard; MCP SDK patterns confirmed via official docs.
- **Phase 2:** MCP tool registration and FastMCP usage are well-documented; no unknowns.

Phases that may need validation during implementation:
- **Phase 3:** Hook threshold values (0.85/0.60) are design choices, not empirically validated defaults. The roadmap should flag threshold calibration as a post-ship tuning task, not a pre-ship blocker. The Ollama judge timeout value (3s) should be empirically verified against the target hardware profile.

## Confidence Assessment

| Area | Confidence | Notes |
|------|------------|-------|
| Stack | HIGH | `mcp>=1.7.1` confirmed on PyPI; FastMCP bundling confirmed via official GitHub README; all other deps are existing validated stack |
| Features | HIGH | MCP tool surface is fully specified with schema; feature dependency chain is explicit; competitor analysis is MEDIUM (public READMEs only) |
| Architecture | HIGH | Existing codebase is known; MCP SDK patterns verified via official docs; Qdrant latency figures from official benchmarks |
| Pitfalls | MEDIUM-HIGH | stdout and cold-start pitfalls confirmed via official docs and issue tracker; hook subdirectory bug confirmed via two Claude Code issues; Ollama keep-alive behavior from community sources |

**Overall confidence:** HIGH

### Gaps to Address

- **Threshold calibration (0.85/0.60):** These values are design starting points, not empirically derived. Validate against real project corpora during Phase 3 integration testing. Make configurable in `.carta/config.yaml` from day one.
- **Hook timing on CPU-only machines:** Gray-zone latency estimates (700ms–2s warm, 1.5–3.5s cold) are based on community benchmarks. Actual performance on CPU-only developer machines may be higher. The async hook option (`"async": true` in `.mcp.json`) is the documented fallback if synchronous injection proves too slow.
- **Markdown embedding gap:** Research confirms `.md` files are not currently embedded. The fix is a pipeline-level change (parser support) rather than an architectural change — low risk but must be verified against existing sidecar hash logic.

## Sources

### Primary (HIGH confidence)
- MCP Python SDK (GitHub): https://github.com/modelcontextprotocol/python-sdk — SDK architecture, FastMCP bundling
- MCP build-server docs: https://modelcontextprotocol.io/docs/develop/build-server — stdio transport, stdout discipline
- Claude Code hooks reference: https://code.claude.ai/docs/en/hooks — UserPromptSubmit behavior, 60s timeout, async option
- Qdrant latency benchmarks: https://qdrant.tech/benchmarks/single-node-speed-benchmark/ — 3–10ms local Docker estimates
- Ollama API: https://github.com/ollama/ollama/blob/main/docs/api.md — `/api/generate` and `/api/embeddings` endpoints
- MCP Python SDK (PyPI): https://pypi.org/project/mcp/1.7.1/ — version confirmation

### Secondary (MEDIUM confidence)
- Claude Code issue #8810 — UserPromptSubmit subdirectory trigger bug
- Claude Code issue #17277 — UserPromptSubmit first-prompt inconsistency
- Ollama keep-alive optimization: https://markaicode.com/ollama-inference-speed-optimization/ — `OLLAMA_KEEP_ALIVE` behavior
- FastMCP stdio logging pitfall: https://nearform.com/digital-community/implementing-model-context-protocol-mcp-tips-tricks-and-pitfalls/ — stdout contamination confirmed
- Competitor READMEs: memory-bank-mcp, mcp-memory-service, context7, mcp-local-rag — feature comparison

### Tertiary (LOW confidence)
- Ollama cold-start latency: https://acecloud.ai/blog/cold-start-latency-llm-inference/ — 13–46s model load time (community benchmark; hardware-dependent)

---
*Research completed: 2026-03-25*
*Ready for roadmap: yes*
