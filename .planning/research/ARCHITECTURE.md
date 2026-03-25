# Architecture Research

**Domain:** Python CLI + MCP stdio server + smart hook (three-tier semantic memory sidecar)
**Researched:** 2026-03-25
**Confidence:** HIGH (existing codebase is known; MCP SDK patterns verified via official docs)

## Standard Architecture

### System Overview

```
┌─────────────────────────────────────────────────────────────────────┐
│                        ENTRY POINTS                                  │
├──────────────────┬──────────────────────┬───────────────────────────┤
│   Hook Tier      │    MCP Tier           │    CLI Tier               │
│ (auto, push)     │  (Claude-initiated)   │  (human, batch)           │
│                  │                       │                           │
│ hooks/           │ carta/mcp/            │ carta/cli.py              │
│   hook.sh        │   server.py           │                           │
│   judge.py       │   tools.py            │                           │
├──────────────────┴──────────────────────┴───────────────────────────┤
│                        SERVICE LAYER (shared)                        │
│  carta/embed/pipeline.py   carta/scanner/scanner.py   carta/config.py│
├─────────────────────────────────────────────────────────────────────┤
│                        EXTERNAL SERVICES                             │
│         Qdrant (localhost:6333)      Ollama (localhost:11434)        │
│         .carta/config.yaml           *.embed-meta.yaml sidecars      │
└─────────────────────────────────────────────────────────────────────┘
```

### Component Responsibilities

| Component | Responsibility | Boundary |
|-----------|----------------|----------|
| `hooks/hook.sh` | Shell entry point; receives UserPromptSubmit JSON; invokes `hooks/judge.py`; writes inject/discard decision to stdout | No carta imports; pure I/O bridge |
| `hooks/judge.py` | Embeds prompt via Ollama; queries Qdrant; applies threshold logic; calls Ollama judge for gray zone; returns formatted injection or empty string | Imports from `carta.embed.pipeline` (search only), `carta.config` |
| `carta/mcp/server.py` | FastMCP server definition; transport=stdio; registers `carta_search`, `carta_embed`, `carta_scan` tools | No CLI logic; no argparse |
| `carta/mcp/tools.py` | Tool handler functions; delegates to service layer; formats MCP responses | Imports from `carta.embed.pipeline`, `carta.scanner.scanner`, `carta.config` |
| `carta/embed/pipeline.py` | Embed workflow, search, batch upsert, per-file timeout — the shared core | No awareness of MCP or hook callers |
| `carta/scanner/scanner.py` | File discovery, frontmatter audit, scan-results.json | No awareness of MCP or hook callers |
| `carta/config.py` | Load `.carta/config.yaml`; provide `collection_name()` | Used by all tiers |
| `carta/cli.py` | Argument parsing; command dispatch; human-facing UX | Imports service layer only; not imported by MCP or hook |

## Recommended Project Structure

```
carta/
├── cli.py                  # CLI entry point — unchanged
├── config.py               # Config loading — shared by all tiers
├── embed/
│   ├── pipeline.py         # Core embed/search logic — shared
│   ├── embed.py
│   ├── parse.py
│   └── induct.py
├── scanner/
│   └── scanner.py          # Scan logic — shared
├── install/
│   └── bootstrap.py        # Init/bootstrap — CLI only
└── mcp/                    # New: MCP server
    ├── __init__.py
    ├── server.py            # FastMCP instance + mcp.run(transport="stdio")
    └── tools.py             # @mcp.tool() handlers delegating to service layer

hooks/                      # New: hook scripts (outside carta/ package)
├── hook.sh                 # Shell wrapper registered in .mcp.json hooks block
└── judge.py                # Python hook logic; imports carta package

.mcp.json                   # Claude Code MCP registration (replaces plugin cache)
```

### Structure Rationale

- **`carta/mcp/` as a subpackage:** MCP server is a new entry point, not a new domain. It sits alongside `carta/embed/` and `carta/scanner/` and delegates to them rather than reimplementing logic.
- **`hooks/` outside package:** Shell hook entry point is not a Python module. Keeping it in a top-level `hooks/` dir makes registration in `.mcp.json` unambiguous and avoids confusion with package imports.
- **`carta/cli.py` untouched:** The CLI is not imported by MCP or hook tiers. Changes to it cannot break the other tiers. This is the correct isolation boundary.
- **`carta/config.py` as shared foundation:** All three tiers call `load_config()`. It is the only module that touches the filesystem for configuration.

## Architectural Patterns

### Pattern 1: Thin Entry Point, Fat Service Layer

**What:** Each tier (CLI, MCP, hook) is a thin adapter that translates its input format and delegates to the same `pipeline.py` and `scanner.py` functions. No business logic lives in the entry point.

**When to use:** Whenever multiple callers need the same operation. This is the correct pattern here because search, embed, and scan are called from three different contexts.

**Trade-offs:** Requires that the service layer be callable without side effects (no argparse, no `sys.exit`, no print-only outputs). The existing `pipeline.py` is mostly clean; `cmd_embed` in `cli.py` holds argparse concerns and should not be called directly by MCP tools.

**Example:**
```python
# carta/mcp/tools.py
from carta.embed.pipeline import run_search, run_embed_pipeline
from carta.config import load_config

@mcp.tool()
def carta_search(query: str, top_n: int = 5) -> list[dict]:
    """Search embedded documentation."""
    config = load_config()
    return run_search(config, query, top_n=top_n)
```

### Pattern 2: Hook Fast Path / Gray Zone Split

**What:** The hook makes a Qdrant query first (fast, ~5ms). Similarity score determines the path:
- `>0.85` → inject immediately, skip Ollama judge
- `<0.60` → discard immediately, skip Ollama judge
- `0.60–0.85` → call Ollama small model (≤2B) for binary relevance judgment

**When to use:** Always for the hook. The fast path handles the common cases (clear hit, clear miss) without incurring the Ollama round-trip cost. Only the ambiguous minority pays the full latency cost.

**Trade-offs:** The 0.60/0.85 thresholds are configurable but need empirical calibration. Wrong thresholds push too many queries into the gray zone, negating the fast path benefit.

**Example:**
```python
# hooks/judge.py — threshold routing
score = results[0].score if results else 0.0
if score > HIGH_THRESHOLD:
    inject(results)
elif score < LOW_THRESHOLD:
    sys.exit(0)  # discard — no injection
else:
    verdict = ollama_judge(prompt, results[0].payload["text"])
    if verdict == "relevant":
        inject(results)
```

### Pattern 3: MCP stdio Server — stdout Reserved for Protocol

**What:** The FastMCP server communicates exclusively via stdout (JSON-RPC). Any `print()` statement in server code contaminates the protocol stream. All logging must go to stderr or a file.

**When to use:** Always with stdio transport. This is non-negotiable — stray stdout breaks the MCP client.

**Trade-offs:** The existing codebase uses `print()` throughout the service layer for user feedback. These prints are safe when called from the CLI (user sees them) but become protocol pollution when called from MCP tools. MCP tool handlers must suppress or redirect print output from the service layer, or the service layer functions must accept a `silent=True` parameter.

**Example:**
```python
# carta/mcp/server.py
import sys
import logging

logging.basicConfig(stream=sys.stderr, level=logging.INFO)

mcp = FastMCP("carta")

if __name__ == "__main__":
    mcp.run(transport="stdio")
```

## Data Flow

### Hook Flow (UserPromptSubmit)

```
Claude Code → UserPromptSubmit event
    ↓
hooks/hook.sh  (receives JSON on stdin)
    ↓
hooks/judge.py (subprocess or direct call)
    ↓ embed prompt text via Ollama (~50–200ms first call, ~10ms warm)
    ↓ query Qdrant top-3 (~3–10ms local Docker)
    ↓
[score check]
    ├── >0.85 → format inject block → write to stdout → Claude sees context
    ├── <0.60 → exit 0 → no injection
    └── 0.60–0.85 → Ollama judge call (~500–2000ms for 1B–2B model)
                       ├── "relevant" → inject
                       └── "not relevant" → discard
```

### MCP Tool Flow

```
Claude Code → mcp.json → stdio spawn → carta/mcp/server.py
    ↓ JSON-RPC tool call (e.g., carta_search)
carta/mcp/tools.py handler
    ↓ load_config()
    ↓ carta.embed.pipeline.run_search()
    ↓ Ollama embed query (~10ms warm)
    ↓ Qdrant search (~5ms)
    ↓ return structured result list
JSON-RPC response → Claude
```

### .mcp.json Registration Flow

```
.mcp.json (project root)
  ├── mcpServers.carta.command = "python -m carta.mcp.server"
  ├── mcpServers.carta.transport = "stdio"
  └── hooks.UserPromptSubmit[0].command = "hooks/hook.sh"

Claude Code reads .mcp.json at startup
  ├── spawns carta MCP server process (stdio)
  └── registers hook for UserPromptSubmit lifecycle
```

No plugin cache involved. Claude Code resolves tools natively from `.mcp.json`.

## Hook Latency Budget

The UserPromptSubmit hook blocks prompt processing. Target: under 3 seconds for the fast path, under 5 seconds for the gray zone path. Both are well within the 60-second default hook timeout.

| Stage | Estimated Latency | Notes |
|-------|-------------------|-------|
| Shell script startup (`hook.sh`) | ~20ms | bash/sh process spawn |
| Python interpreter startup (`judge.py`) | ~100–200ms | Cold start; warm if kept alive |
| Config load + import | ~50ms | YAML parse, module imports |
| Ollama embed query (warm) | ~10–50ms | `nomic-embed-text` warm, GPU |
| Ollama embed query (cold) | ~200–500ms | First call after idle; model reload |
| Qdrant search (local Docker) | ~3–10ms | Small collection, HNSW index |
| **Fast path total (warm)** | **~200–300ms** | No Ollama judge |
| **Fast path total (cold)** | **~500–800ms** | Includes model load |
| Ollama judge call (1B model, warm) | ~500–1500ms | Binary classification prompt |
| Ollama judge call (2B model, warm) | ~1000–2500ms | More reliable but slower |
| **Gray zone total (warm)** | **~700ms–2s** | Acceptable |
| **Gray zone total (cold)** | **~1.5–3.5s** | Acceptable; borderline on slow hardware |

**Key risk:** Cold Ollama model load on the judge path on CPU-only machines can spike to 5–8 seconds. Mitigation: use `OLLAMA_KEEP_ALIVE=24h` to keep the model resident; document this as a required config step.

**Async hook option:** The hook can be marked `"async": true` in `.mcp.json` if injection becomes too slow in practice. This makes injection best-effort (Claude may start responding before injection completes). Synchronous is preferred for reliable injection; async is the fallback.

## Build Order

Dependencies flow strictly in one direction. The service layer must be stable before the entry points can be reliable.

```
Phase 1: Service layer reliability (prerequisite for all)
  - Batch Qdrant upsert (32/batch) in pipeline.py
  - Per-file timeout (300s) in pipeline.py
  - current_path written on sidecar creation
  → Stable embed and search APIs that MCP and hook can depend on

Phase 2: MCP server
  - carta/mcp/server.py + tools.py
  - .mcp.json registration (replaces plugin cache)
  - Verify carta_search, carta_embed, carta_scan via Claude Code
  → Replaces plugin cache architecture entirely

Phase 3: Smart hook
  - hooks/hook.sh + hooks/judge.py
  - Fast path (threshold-only) first, then Ollama judge
  - .mcp.json hook registration
  → Automatic injection via UserPromptSubmit

Phase 4: CLI additions
  - carta status command
  - Markdown embedding support
  → Incremental; no dependencies on Phase 2/3
```

**Rationale for this order:**
- MCP server built on a flaky embed pipeline will produce unreliable `carta_embed` calls. Fix the pipeline first.
- Hook built before MCP server is wasted effort if search API changes during MCP development. MCP tools and hook share the same `run_search()` interface — align it once, not twice.
- Plugin cache elimination happens at Phase 2 completion. Do not run hybrid (cache + MCP) — the stale-version problem is the reason for this migration.

## Anti-Patterns

### Anti-Pattern 1: Duplicating Search Logic in the Hook

**What people do:** Copy `run_search()` logic directly into `judge.py` to avoid importing the carta package.

**Why it's wrong:** Two codebases diverge. When the Qdrant collection schema or embedding model changes, the hook breaks silently while tests pass on the main path.

**Do this instead:** `hooks/judge.py` imports `from carta.embed.pipeline import run_search`. The package is installed in the same virtualenv. The hook is a thin caller, not a reimplementation.

### Anti-Pattern 2: Injecting on Every Prompt

**What people do:** Skip the threshold logic and always inject the top Qdrant results regardless of relevance score.

**Why it's wrong:** Context noise accumulates. Claude sees irrelevant documentation on every prompt, degrading response quality and consuming context window.

**Do this instead:** Enforce the 0.85/0.60 thresholds. Let the fast-path discard handle the majority of prompts. Tune thresholds empirically after running for a few sessions.

### Anti-Pattern 3: print() in MCP Tool Handlers

**What people do:** Call `pipeline.run_embed_pipeline(config)` from an MCP tool handler without suppressing the pipeline's print statements.

**Why it's wrong:** Every `print("Embedding: doc.pdf...")` writes to stdout, corrupting the JSON-RPC stream. Claude Code sees malformed MCP responses and either errors or disconnects.

**Do this instead:** Add a `verbose=False` parameter to pipeline functions. MCP callers pass `verbose=False`; CLI callers pass `verbose=True`. Route all MCP-tier logging to `sys.stderr`.

### Anti-Pattern 4: Hybrid Plugin Cache + MCP

**What people do:** Keep the plugin cache skills alongside the new MCP tools during migration.

**Why it's wrong:** Reproduces the stale-version problem. Lexicographically earlier cache entries win over MCP tools. The entire value of the migration is eliminating the cache.

**Do this instead:** Remove plugin cache registration completely in Phase 2. Ship MCP tools as the only mechanism. Hard cutover, not gradual migration.

## Integration Points

### External Services

| Service | Integration Pattern | Notes |
|---------|---------------------|-------|
| Qdrant (localhost:6333) | HTTP client via `qdrant-client`; pre-flight 5s timeout check | Same in all three tiers via `pipeline.py` |
| Ollama (localhost:11434) | HTTP POST to `/api/embeddings` and `/api/generate`; per-request | Hook uses both; MCP uses embed only; set `OLLAMA_KEEP_ALIVE=24h` |
| `.carta/config.yaml` | `load_config()` called once per invocation; walk parent dirs | Hook must resolve config relative to project root, not hook script location |

### Internal Boundaries

| Boundary | Communication | Notes |
|----------|---------------|-------|
| `carta/mcp/tools.py` → `carta/embed/pipeline.py` | Direct Python import | MCP tools call `run_search()`, `run_embed_pipeline()` with `verbose=False` |
| `hooks/judge.py` → `carta/embed/pipeline.py` | Direct Python import | Hook calls `run_search()` only; never triggers embed during hook execution |
| `carta/cli.py` → `carta/embed/pipeline.py` | Direct Python import | Unchanged from v0.1.x |
| `.mcp.json` → `carta/mcp/server.py` | stdio process spawn by Claude Code | `python -m carta.mcp.server`; inherits project virtualenv |
| `.mcp.json` → `hooks/hook.sh` | Process spawn by Claude Code | Shell must activate virtualenv or use absolute python path |

## Sources

- [MCP Python SDK — official docs](https://github.com/modelcontextprotocol/python-sdk) — HIGH confidence
- [MCP stdio transport guide](https://modelcontextprotocol.io/docs/develop/build-server) — HIGH confidence
- [Qdrant local latency benchmarks](https://qdrant.tech/benchmarks/single-node-speed-benchmark/) — HIGH confidence (3–10ms typical for small collections)
- [Claude Code hooks reference](https://code.claude.com/docs/en/hooks) — HIGH confidence (60s default timeout, UserPromptSubmit blocking behavior)
- [Ollama keep-alive optimization](https://markaicode.com/ollama-inference-speed-optimization/) — MEDIUM confidence (community source; keep-alive behavior is well-documented by Ollama)
- [FastMCP stdio logging pitfall](https://nearform.com/digital-community/implementing-model-context-protocol-mcp-tips-tricks-and-pitfalls/) — HIGH confidence (stdout contamination is a protocol-level constraint)
- Existing codebase analysis: `.planning/codebase/ARCHITECTURE.md` — HIGH confidence

---
*Architecture research for: Carta v0.2 — MCP server + smart hook on existing Python CLI*
*Researched: 2026-03-25*
