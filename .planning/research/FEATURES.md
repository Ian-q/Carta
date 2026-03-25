# Feature Research

**Domain:** Semantic memory MCP server for AI coding agents (RAG over local docs)
**Researched:** 2026-03-25
**Confidence:** HIGH (MCP tool surface), MEDIUM (competitor analysis — based on public READMEs and docs)

---

## MCP Tool Surface: Specific Design

### carta_search

**Purpose:** Claude-initiated semantic retrieval from the embedded knowledge base.

```
Tool name: carta_search
Arguments:
  query        string   required   Natural language or code-term query
  limit        int      optional   Max chunks to return (default: 5, max: 20)
  min_score    float    optional   Minimum similarity threshold (default: 0.0 — let caller decide)
  collection   string   optional   Target Qdrant collection (default: project collection)

Returns (structuredContent):
  {
    "results": [
      {
        "content":    string,   // chunk text
        "score":      float,    // cosine similarity 0.0–1.0
        "source":     string,   // relative file path
        "chunk_index": int,     // position within source file
        "metadata":   object    // title, page, section if available
      }
    ],
    "query":          string,   // echoed for agent traceability
    "collection":     string,
    "total_embedded": int       // total chunks in collection
  }
```

Scores should be returned raw — the caller (hook or Claude) decides what to act on. Do not silently filter inside the tool.

---

### carta_embed

**Purpose:** Claude-initiated embedding of a specific file or directory path.

```
Tool name: carta_embed
Arguments:
  path         string   required   Absolute or project-relative path (file or directory)
  force        bool     optional   Re-embed even if already embedded (default: false)

Returns (structuredContent):
  {
    "embedded":  [string],   // files successfully embedded
    "skipped":   [string],   // files already current (hash match)
    "failed":    [string],   // files that errored
    "chunks_added": int
  }
```

Must propagate pipeline reliability fixes (batch upsert, per-file timeout) before this tool is exposed. An unreliable pipeline behind an MCP tool is worse than no tool — the agent will retry endlessly.

---

### carta_scan

**Purpose:** Discover embeddable files without embedding them. Lets Claude understand what's available before deciding what to embed.

```
Tool name: carta_scan
Arguments:
  path         string   optional   Directory to scan (default: project root)
  show_embedded bool    optional   Include already-embedded files (default: false)

Returns (structuredContent):
  {
    "pending":   [{ "path": string, "size_bytes": int, "type": string }],
    "embedded":  [{ "path": string, "chunks": int, "embedded_at": string }],
    "ignored":   [string]
  }
```

Scan-before-embed is a pattern Claude Code agents naturally follow: "what docs exist?" then "embed the relevant ones." Exposing scan separately enables this without forcing a full embed cycle.

---

### carta_status (P2 — add after core MCP is stable)

```
Tool name: carta_status
Arguments: none

Returns (structuredContent):
  {
    "qdrant":  { "healthy": bool, "collections": [{ "name": string, "vectors": int }] },
    "ollama":  { "healthy": bool, "model": string, "latency_ms": int },
    "project": { "collection": string, "total_chunks": int, "pending_files": int }
  }
```

---

## Feature Landscape

### Table Stakes (Users Expect These)

Features users assume exist. Missing these = product feels incomplete or untrustworthy.

| Feature | Why Expected | Complexity | Notes |
|---------|--------------|------------|-------|
| `carta_search` with scored results | Core retrieval — useless without it | MEDIUM | Scores must be returned, not hidden |
| Source attribution on every chunk | Agents need to cite/navigate to source | LOW | File path + chunk index is sufficient |
| Idempotent `carta_embed` | Agents call embed multiple times; must not duplicate | LOW | Hash-based skip already exists in CLI |
| Markdown file embedding | Most project docs are `.md`; absence is a gap | LOW | Scan already finds them; pipeline gap only |
| `carta_scan` for discovery | Agents need inventory before acting | LOW | Thin wrapper over scanner |
| MCP stdio transport | Claude Code native registration via `.mcp.json` | LOW | Replaces plugin cache entirely |
| Reliable embed pipeline | Hanging on dense PDFs kills agent workflows | HIGH | Batch upsert + per-file timeout are prereqs |
| `carta status` CLI command | Human operator needs health/diagnostic view | LOW | Qdrant + Ollama health + pending count |

### Differentiators (Competitive Advantage)

| Feature | Value Proposition | Complexity | Notes |
|---------|-------------------|------------|-------|
| Three-tier architecture (Hook + MCP + CLI) | Push (automatic injection) + pull (agent-initiated) covers both usage patterns; no other local tool does both | MEDIUM | Unique to Carta — competitors are pull-only |
| Ollama judge for gray-zone relevance | Binary yes/no from a 0.5–2B local model beats pure cosine threshold: fewer false positives injected, fewer false negatives discarded | MEDIUM | Only applies to hook path (0.6–0.85 band); fast path skips it |
| Similarity fast path (>0.85 inject, <0.6 discard) | Avoids Ollama round-trip for clear cases; hook adds <50ms on fast path vs ~300ms on judge path | LOW | Threshold values should be configurable in `.carta/config.yaml` |
| Project-scoped, local-only design | No cloud, no auth surface, no data leaving the machine; CI/air-gap friendly | LOW | Constraint that becomes a feature for security-conscious teams |
| Sidecar metadata per file | Enables incremental re-embed, audit of what's embedded, and future changed-since queries | LOW | Already exists; expose via `carta_scan` return shape |
| `carta_embed` with force flag | Agent can trigger re-embed after doc update without human CLI intervention | LOW | Closes the "stale knowledge" loop |

### Anti-Features (Deliberately NOT Building)

| Feature | Why Requested | Why Problematic | Alternative |
|---------|---------------|-----------------|-------------|
| Blind context injection (every prompt) | Seems like "always helpful" | Context noise degrades response quality; wastes tokens on irrelevant retrieval | Judge-filtered hook injects only when relevant |
| Hybrid plugin cache + MCP coexistence | Easier migration path | Two-registry problem is the root cause of version bugs; partial migration keeps the bug alive | Full cutover; `.mcp.json` is the only registration |
| Batched Ollama embedding calls | Performance optimization | Ollama `/api/embeddings` does not accept batch input natively; fake batching just serializes calls with extra overhead | Per-text calls remain; parallelize at the pipeline level instead |
| Cloud sync or multi-user sharing | Useful for teams | Requires auth surface, data egress, and operational complexity that contradicts the local-only constraint | Out of scope — local tool only |
| Claude-as-judge for gray-zone relevance | Higher accuracy for complex relevance | Claude is the consumer of the injected context — asking it to judge before injection adds a recursive tool call loop and significant latency | Small Ollama model (smollm2, qwen2.5:0.5b) is purpose-fit for binary yes/no |
| Arbitrary top-K cutoff without scores | Simple API | Hides quality signal from caller; agent can't distinguish "5 great results" from "5 mediocre results" | Return scores; let caller threshold |
| Real-time file watching / auto-embed | Convenient | Adds background process complexity, race conditions with embed pipeline, and unexpected Qdrant writes during active coding sessions | Manual `carta_embed` via MCP or CLI is explicit and predictable |

---

## Feature Dependencies

```
carta_search
    └──requires──> Qdrant collection (populated)
                       └──requires──> carta_embed (or CLI embed)
                                          └──requires──> embed pipeline reliability fixes
                                                             └──requires──> batch upsert + per-file timeout

Hook auto-injection
    └──requires──> carta_search (same retrieval path)
    └──requires──> Ollama judge model available (for gray zone)
    └──requires──> similarity fast path (threshold logic)

carta_scan
    └──requires──> scanner (already exists in CLI)
    └──enhances──> carta_embed (scan-then-embed workflow)

carta_status
    └──requires──> Qdrant health check
    └──requires──> Ollama health check
    └──enhances──> carta_scan (shows pending count)

MCP stdio server
    └──requires──> .mcp.json registration
    └──conflicts──> plugin cache (must be removed, not coexisted)
```

### Dependency Notes

- **Embed reliability before MCP exposure:** `carta_embed` behind an MCP tool is called by an AI agent that will retry on failure. An unreliable pipeline that hangs becomes an agent deadlock. Batch upsert and per-file timeout must ship first.
- **carta_scan enhances carta_embed:** The natural agent workflow is scan (inventory) → decide → embed (targeted). Exposing both enables this without forcing full-corpus embeds.
- **Hook requires same retrieval path as carta_search:** Unifying the retrieval logic means hook quality improvements (threshold tuning, judge accuracy) benefit agent-initiated search too.

---

## MVP Definition

### Launch With (v0.2 — this milestone)

- [ ] `carta_search` — core retrieval with scored, attributed results
- [ ] `carta_embed` — with force flag; reliability fixes (batch upsert, timeout) are prerequisites
- [ ] `carta_scan` — pending/embedded inventory
- [ ] MCP stdio server registered via `.mcp.json` (plugin cache eliminated)
- [ ] Markdown file embedding (closes the most common doc type gap)
- [ ] Smart hook: fast path thresholds + Ollama judge gray zone

### Add After Validation (v0.2.x)

- [ ] `carta_status` MCP tool — add when teams report confusion about health state
- [ ] `carta status` CLI command — human operator diagnostic; simpler than MCP tool
- [ ] Configurable thresholds in `.carta/config.yaml` — add when default values prove wrong in practice

### Future Consideration (v0.3+)

- [ ] Hybrid semantic + keyword search (BM25 boost for exact code terms) — adds complexity; validate pure semantic first
- [ ] Per-collection search (multiple named knowledge bases) — useful for monorepo with separate domains; defer until single-collection is proven

---

## Feature Prioritization Matrix

| Feature | User Value | Implementation Cost | Priority |
|---------|------------|---------------------|----------|
| `carta_search` (MCP) | HIGH | MEDIUM | P1 |
| `carta_embed` (MCP, reliability fixed) | HIGH | HIGH | P1 |
| MCP stdio + .mcp.json registration | HIGH | LOW | P1 |
| Plugin cache elimination | HIGH | LOW | P1 (blocker removal) |
| Markdown embedding | MEDIUM | LOW | P1 |
| `carta_scan` (MCP) | MEDIUM | LOW | P1 |
| Smart hook (fast path + judge) | HIGH | MEDIUM | P1 |
| `carta status` CLI | MEDIUM | LOW | P2 |
| `carta_status` MCP tool | LOW | LOW | P2 |
| Configurable thresholds | MEDIUM | LOW | P2 |
| Keyword boost / hybrid search | MEDIUM | HIGH | P3 |
| Per-collection routing | LOW | MEDIUM | P3 |

---

## Competitor Feature Analysis

| Feature | memory-bank-mcp | mcp-memory-service (doobidoo) | context7 | mcp-local-rag (shinpr) | Carta v0.2 |
|---------|-----------------|-------------------------------|----------|------------------------|------------|
| Transport | stdio | stdio | stdio | stdio | stdio |
| Storage | Markdown files | ChromaDB | Cloud (Upstash) | Local vector DB | Qdrant (local Docker) |
| Semantic search | No (tag-based) | Yes (sentence-transformers) | Yes (curated index) | Yes | Yes (Ollama embeddings) |
| Auto injection | No | No | No | No | Yes (hook with judge) |
| Agent-initiated embed | No | write_memory only | No | ingest_file | Yes (carta_embed) |
| Source attribution | Filename only | Partial | Library + version | File path | File path + chunk index + page |
| Local-only | Yes | Yes | No (cloud) | Yes | Yes |
| PDF support | No | No | No | Yes | Yes |
| Relevance filtering | None | Similarity score | Curated trust score | Quality gap filter | Fast path + Ollama judge |
| Health/status tool | No | No | No | status tool | carta_status (P2) |
| MCP registration | .mcp.json | .mcp.json | .mcp.json | .mcp.json | .mcp.json |

### What Carta Can Do Better

1. **Automatic push injection.** Every comparable tool is pull-only (Claude must call a tool). Carta's hook fires on `UserPromptSubmit` and injects without a tool call — zero friction for the agent.

2. **Ollama judge over binary threshold.** mcp-local-rag uses "relevance gap grouping" (quality-first cutoff). mcp-memory-service returns raw similarity scores with no filtering guidance. Carta's three-zone design (inject / judge / discard) gives better precision than a fixed cutoff without the latency cost of judging every retrieval.

3. **PDF + Markdown in one pipeline.** No comparable local tool handles both. Most handle only one format or plain text.

4. **Sidecar metadata enables incremental workflows.** No competitor tracks per-file embed state. Carta's `.embed-meta.yaml` sidecars enable `carta_scan` to show exactly what's pending, what's current, and what changed — enabling targeted re-embed rather than full corpus rebuild.

### What Carta Should Not Try to Beat Competitors On

- **Ease of install:** mcp-local-rag wins with zero Docker/Python setup. Carta requires Qdrant + Ollama. Don't try to eliminate this — it's the infra Carta already uses and the constraint is documented.
- **Breadth of file formats:** context7's curated library database and doobidoo's REST API cover more ground. Carta's scope is project docs (PDF, MD, text) — stay focused.

---

## Ollama Judge: Evaluation Against Alternatives

| Approach | Latency | Accuracy | Infra Cost | Verdict |
|----------|---------|----------|------------|---------|
| Pure cosine threshold (single cutoff) | ~0ms | Low (threshold is blunt) | None | Too many false positives in 0.6–0.85 band |
| Ollama judge on every result | ~300ms per call | High | Ollama already running | Too slow — blocks prompt submission for every query |
| **Ollama judge on gray zone only (0.6–0.85)** | ~0ms fast path, ~300ms gray zone | High where it matters | Ollama already running | Recommended: best precision/latency tradeoff |
| Claude-as-judge (recursive tool call) | ~2–5s | Highest | None (already in session) | Creates circular dependency; adds agent latency and tool-call nesting |
| Keyword filter post-similarity | ~1ms | Medium | None | Useful complement but doesn't replace semantic judgment |

**Recommendation:** Ollama gray-zone judge is correct. The 0.5B–2B binary yes/no task is well within small model capability, latency is acceptable because it only fires on ambiguous cases, and Ollama is already a required infra dependency. Claude-as-judge should be explicitly excluded as an anti-pattern for this hook because it adds recursive complexity and latency into the prompt submission path.

---

## Sources

- [memory-bank-mcp (alioshr)](https://github.com/alioshr/memory-bank-mcp) — file-based, tag search, no semantic
- [mcp-memory-service (doobidoo)](https://github.com/doobidoo/mcp-memory-service) — ChromaDB semantic, no auto-injection
- [context7 MCP](https://www.trevorlasn.com/blog/context7-mcp) — two-tool design (resolve + query), cloud-backed
- [mcp-local-rag (shinpr)](https://github.com/shinpr/mcp-local-rag) — semantic + keyword hybrid, quality-gap filter, zero setup
- [MCP Tools Specification 2025](https://modelcontextprotocol.io/specification/2025-06-18/server/tools) — structuredContent, outputSchema, backward compat text fallback
- [LLM-as-Judge guide (EvidentlyAI)](https://www.evidentlyai.com/llm-guide/llm-as-a-judge) — binary judgment viability, bias considerations
- [MCP spec structured tool output (Socket.dev)](https://socket.dev/blog/mcp-spec-updated-to-add-structured-tool-output-and-improved-oauth-2-1-compliance) — outputSchema field, structuredContent

---

*Feature research for: Carta v0.2 — MCP-first semantic memory sidecar for Claude Code*
*Researched: 2026-03-25*
