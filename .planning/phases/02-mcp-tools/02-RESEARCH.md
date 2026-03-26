# Phase 2: MCP Tools — Research

**Researched:** 2026-03-26
**Domain:** FastMCP tool handler implementation, sidecar mtime drift detection, single-file embed adapter
**Confidence:** HIGH

## Summary

Phase 2 adds three tool handlers (`carta_search`, `carta_embed`, `carta_scan`) to the existing FastMCP scaffold in `carta/mcp/server.py`. The scaffold is clean and ready — all wire-protocol constraints (stderr-only logging, no `print()`, no `sys.exit()`, structured error returns) are already enforced and tested.

The primary implementation gap is that `run_embed` in `pipeline.py` operates on all pending sidecar files, not a single specified path. `carta_embed(path)` requires either a new single-file wrapper function or a targeted invocation of `_embed_one_file` with constructed metadata. Additionally, `carta_scan` must extract pending/drift distinctions from the existing `run_scan` output (or call lower-level scanner helpers directly), and sidecar files do not currently store file mtime — that field must be added to enable `carta_embed` drift detection per D-04.

`run_search` is the cleanest integration: it already returns `[{"score", "source", "excerpt"}]` and only needs a try/except wrapper to convert `RuntimeError` into structured error dicts. `carta-mcp` is already registered in `pyproject.toml` (MCP-05 complete from Phase 1).

**Primary recommendation:** Implement all three tool handlers in `server.py` only; add `file_mtime` to sidecar schema in `induct.py`; add a `run_embed_file(path, cfg, force)` function to `pipeline.py` for single-file embed with mtime skip logic.

---

<user_constraints>
## User Constraints (from CONTEXT.md)

### Locked Decisions

#### carta_search
- **D-01:** Tool signature: `carta_search(query: str, top_k: int = 5)` — simple default, caller controls result count when needed. No doc_type filter in Phase 2.
- **D-02:** Success response: list of result dicts — `[{"score": float, "source": str, "excerpt": str}]`. Excerpt capped at ~300 characters (~2-3 sentences). No extra fields (doc_type, chunk_index, collection) — they add tokens without helping Claude reason.

#### carta_embed
- **D-03:** Tool signature: `carta_embed(path: str, force: bool = False)` — skip if already embedded and file mtime unchanged; re-embed regardless when `force=True`.
- **D-04:** Change detection uses file mtime stored in the sidecar `.embed-meta.yaml`. If mtime matches, skip. If mtime is newer, treat as drift and proceed. No git integration in Phase 2.
- **D-05:** Success response: `{"status": "ok", "chunks": N}` — chunk count confirms the embed ran. On skip (already current): `{"status": "skipped", "reason": "already embedded, file unchanged"}`.

#### carta_scan
- **D-06:** Tool signature: `carta_scan()` — no parameters; scans the full project as configured.
- **D-07:** Success response: `{"pending": ["path/a.pdf", ...], "drift": ["path/b.pdf", ...]}` — flat path arrays. No metadata per file in Phase 2.
- **D-08:** "Pending" = no sidecar exists. "Drift" = sidecar exists but file mtime is newer than `embedded_at` in sidecar. Planner to verify this logic is present in `run_scan()` or add it.

#### Error Handling
- **D-09:** All tools return structured error dicts on failure — never raise exceptions. Uniform shape: `{"error": "<type>", "detail": "<human message>"}`.
- **D-10:** Error types:
  - `service_unavailable` — Qdrant or Ollama unreachable
  - `file_not_found` — path does not exist (carta_embed)
  - `timeout` — per-file timeout exceeded (carta_embed, inherited from PIPE-02)
  - `collection_not_found` — project not initialized

### Claude's Discretion

- FastMCP decorator pattern (`@mcp_server.tool()`) for handler registration — standard SDK usage, no surprises expected.
- Whether `run_scan()` already tracks drift via mtime or needs that logic added — planner to check and implement as needed.

### Deferred Ideas (OUT OF SCOPE)

- **Git-hash / commit-linked embed tracking:** Link each embed to the git commit hash at time of embedding; use content hash for deduplication across versions; schedule re-embeds on change detection. Meaningful architectural direction — defer to backlog (beyond v0.2 scope).
- **Deeper search mode with judge:** A `carta_search` variant that passes more context to the Ollama judge for deeper relevance filtering. Phase 3 design decision — judge lives in the hook phase.
- **Judge model alternatives (API/Claude sub-agent):** The relevance judge doesn't have to be Ollama — explore API-driven or Claude Code sub-agent options. Phase 3 design decision.
</user_constraints>

---

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|------------------|
| MCP-02 | `carta_search` MCP tool queries Qdrant and returns scored, source-attributed results (score, source path, chunk excerpt) | `run_search()` already returns the right shape; only needs try/except wrapper for structured error returns |
| MCP-03 | `carta_embed` MCP tool embeds a single specified file with per-file timeout enforcement inherited from PIPE-02 | `run_embed()` is batch-only; new `run_embed_file()` adapter needed in `pipeline.py`; sidecar needs `file_mtime` field for D-04 skip logic |
| MCP-04 | `carta_scan` MCP tool returns structured scan results listing pending-embed and drift files | `run_scan()` returns full audit dict; extraction of pending/drift arrays plus mtime-based drift requires new logic or direct scanner helper calls |
| MCP-05 | `carta-mcp` packaged as a separate entrypoint in `pyproject.toml`, invokable as `carta-mcp` | **Already complete from Phase 1** — `carta-mcp = "carta.mcp.server:main"` confirmed in `pyproject.toml` |
</phase_requirements>

---

## Standard Stack

### Core

| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| mcp (FastMCP) | 1.26.0 (installed) | MCP server, tool registration | Official Anthropic SDK; already in use in scaffold |
| qdrant-client | 1.7+ (project dep) | Vector search backend | Project standard; used by `run_search` |
| PyYAML | 6.0+ (project dep) | Sidecar read/write | Project standard; used by induct.py |

### No new dependencies required

All Phase 2 work uses libraries already installed. No `pip install` needed.

## Architecture Patterns

### Recommended Project Structure

No new files or directories needed. All changes are surgical:

```
carta/
├── mcp/
│   └── server.py            # ADD: 3 tool handler functions
├── embed/
│   ├── pipeline.py          # ADD: run_embed_file() single-file adapter
│   └── induct.py            # ADD: file_mtime field to sidecar stub
└── scanner/
    └── scanner.py           # VERIFY/ADD: mtime-based drift distinction
```

### Pattern 1: FastMCP Tool Handler Registration

**What:** Decorate a function with `@mcp_server.tool()` to register it as an MCP tool. FastMCP infers the tool name from the function name. Type annotations on parameters become the tool's JSON schema.

**When to use:** All three tool handlers follow this pattern.

```python
# Source: mcp v1.26.0 installed SDK, FastMCP.tool docstring
@mcp_server.tool()
def carta_search(query: str, top_k: int = 5) -> list[dict]:
    """Search embedded project documentation for chunks relevant to query."""
    try:
        cfg = _load_cfg()
    except Exception as e:
        return {"error": "service_unavailable", "detail": str(e)}
    try:
        results = run_search(query, cfg, verbose=False)
    except RuntimeError as e:
        return {"error": "service_unavailable", "detail": str(e)}
    return [
        {"score": r["score"], "source": r["source"], "excerpt": r["excerpt"][:300]}
        for r in results[:top_k]
    ]
```

**Wire-protocol discipline (already established, must be maintained):**
- No `print()` calls in `server.py` — stdout is JSON-RPC
- All logging via `logging.getLogger(__name__).warning(...)` to stderr
- No `sys.exit()` — return error dicts
- No unhandled exceptions — all tool handlers wrapped in try/except

### Pattern 2: Config Loading in MCP Handler

**What:** MCP server process starts in an unspecified cwd. `find_config()` walks up from cwd looking for `.carta/config.yaml`. A helper `_load_cfg()` in `server.py` centralizes this and converts `ConfigError` into a structured error.

```python
# Confirmed pattern from carta/cli.py cmd_search
from carta.config import find_config, load_config, ConfigError

def _load_cfg() -> dict:
    """Load carta config; raises ConfigError if not found."""
    return load_config(find_config())
```

**Pitfall:** If the MCP server starts from a directory with no `.carta/` ancestor, `find_config()` will raise. The tool handler must catch this and return `{"error": "service_unavailable", "detail": "..."}`.

### Pattern 3: Single-File Embed Adapter (new function needed)

**What:** `run_embed(repo_root, cfg)` processes ALL pending sidecar files. `carta_embed(path)` needs a single-file variant. The right approach is a new `run_embed_file(path, cfg, force)` function in `pipeline.py` that:

1. Resolves the path (absolute or relative to repo root)
2. Checks existence → `file_not_found` error
3. Reads the sidecar if it exists
4. Compares file mtime to sidecar `file_mtime` field → skip if unchanged and `force=False`
5. Calls `_embed_one_file(...)` with the ThreadPoolExecutor timeout pattern (reuse from `run_embed`)
6. Writes updated sidecar with new `file_mtime`
7. Returns `{"status": "ok", "chunks": N}` or `{"status": "skipped", "reason": "..."}`

**Why not reuse `run_embed` directly:** `run_embed` iterates all pending sidecars — it cannot target a specific file path without modifying its discovery logic.

### Pattern 4: Sidecar mtime Field

**What:** `induct.py::generate_sidecar_stub()` currently writes: `slug`, `doc_type`, `current_path`, `status`, `indexed_at`, `chunk_count`, `collection`, `spec_summary`, `notes`. It does NOT write `file_mtime`. Per D-04, mtime-based skip/drift detection requires storing mtime at embed time.

**Action required:** Add `file_mtime` to the sidecar stub in `generate_sidecar_stub()` and update it in `_embed_one_file`'s `sidecar_updates` dict after successful embed.

```python
# In _embed_one_file sidecar_updates (pipeline.py line ~104):
sidecar_updates = {
    "status": "embedded",
    "indexed_at": datetime.now(timezone.utc).isoformat(),
    "chunk_count": count,
    "file_mtime": os.path.getmtime(file_path),  # ADD THIS
}
```

### Pattern 5: carta_scan Pending/Drift Extraction

**What:** `run_scan()` returns a full audit dict with `issues[]` list. The `check_embed_induction_needed()` function produces issues with `type: "embed_induction_needed"` for files with no sidecar OR with `status: pending`. This covers "pending" (D-08).

**Drift detection gap:** No existing scanner function checks whether `file mtime > sidecar.embedded_at`. The D-08 drift condition (sidecar exists but file mtime is newer than `embedded_at`) is not implemented. The `carta_scan` tool handler must implement this check, or a new `check_embed_drift()` function must be added to `scanner.py`.

**Recommended approach for carta_scan tool handler:**

```python
@mcp_server.tool()
def carta_scan() -> dict:
    """Scan project for files pending embed or drifted since last embed."""
    try:
        cfg = _load_cfg()
        repo_root = find_repo_root(cfg)  # from find_config() parent
    except Exception as e:
        return {"error": "service_unavailable", "detail": str(e)}

    pending = []
    drift = []

    # Use check_embed_induction_needed for pending (no sidecar or status=pending)
    from carta.scanner.scanner import check_embed_induction_needed, _get_embed_scan_dirs, parse_sidecar, _EMBED_EXTENSIONS
    for issue in check_embed_induction_needed(repo_root, cfg):
        pending.append(issue["doc"])

    # Drift: sidecar exists + embedded + file mtime > sidecar file_mtime
    # (new logic needed — iterate sidecars and compare mtime)
    ...
    return {"pending": pending, "drift": drift}
```

### Anti-Patterns to Avoid

- **Importing `carta.cli`:** The CLI layer must never be imported by MCP handlers. Use `carta.config`, `carta.embed.pipeline`, `carta.scanner.scanner` directly.
- **Calling `run_embed(repo_root, cfg)` for single-file embed:** This scans all pending sidecars and ignores the `path` argument. Use the new `run_embed_file()` instead.
- **Raising exceptions from tool handlers:** Any unhandled exception terminates the MCP JSON-RPC session silently from the caller's perspective. All exceptions must be caught and returned as error dicts.
- **Using `print()` in server.py:** Stdout is reserved for JSON-RPC framing. The existing AST-walk test will catch this.

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Tool registration | Manual JSON-RPC dispatch | `@mcp_server.tool()` FastMCP decorator | SDK handles schema generation, parameter validation, transport |
| Embedding + upsert | Custom Qdrant client calls | `_embed_one_file()` + `upsert_chunks()` from pipeline.py | Already handles chunking, LFS guard, batch upsert, timeout |
| Config loading | Manual YAML parsing | `find_config()` + `load_config()` from config.py | Handles cwd-walk, deep merge, required field validation |
| Sidecar read/write | Direct YAML open | `read_sidecar()` / `_update_sidecar()` from pipeline.py / induct.py | Handles OSError, yaml.YAMLError, missing_ok patterns |
| Pending file discovery | Filesystem scan | `check_embed_induction_needed()` from scanner.py | Already implements sidecar-absence and status=pending check |

## Common Pitfalls

### Pitfall 1: run_embed is Batch-Only

**What goes wrong:** Calling `run_embed(repo_root, cfg)` from `carta_embed(path)` ignores the `path` parameter and embeds all pending files, not just the requested one.
**Why it happens:** `run_embed` discovers files via `discover_pending_files(repo_root)` which reads all `.embed-meta.yaml` sidecars with `status: pending`.
**How to avoid:** Implement `run_embed_file(path, cfg, force)` in `pipeline.py` as a targeted single-file variant.
**Warning signs:** Calling `carta_embed("docs/spec.pdf")` and seeing multiple files embedded in the Qdrant collection.

### Pitfall 2: Missing file_mtime in Sidecar

**What goes wrong:** `carta_embed(path, force=False)` cannot detect "already embedded, file unchanged" because `file_mtime` is not stored in the sidecar. Every call will re-embed regardless.
**Why it happens:** Current `generate_sidecar_stub()` and `_embed_one_file` sidecar_updates do not include `file_mtime`.
**How to avoid:** Add `file_mtime: float` (from `os.path.getmtime()`) to sidecar_updates in `_embed_one_file` and to the stub schema in `induct.py`.
**Warning signs:** `carta_embed` always returns `{"status": "ok", "chunks": N}` even when file hasn't changed.

### Pitfall 3: Config Not Found When MCP Server Starts Outside Project

**What goes wrong:** `find_config()` walks up from cwd to find `.carta/config.yaml`. If `carta-mcp` is started from a directory with no carta project ancestor, it raises `ConfigError`.
**Why it happens:** `carta-mcp` is invoked by Claude Code from its own process context, which may not be the project root.
**How to avoid:** Tool handlers wrap `_load_cfg()` in try/except and return `{"error": "service_unavailable", "detail": "..."}`. Test by running `carta-mcp` from `/tmp`.
**Warning signs:** MCP session connection errors in Claude Code when `.carta/config.yaml` is not in the cwd ancestry.

### Pitfall 4: run_scan Returns Full Audit Dict, Not pending/drift Arrays

**What goes wrong:** Passing `run_scan()` output directly as `carta_scan` response returns hundreds of fields including all structural doc issues, not the `{"pending": [...], "drift": [...]}` shape required by D-07.
**Why it happens:** `run_scan` was designed for the full structural audit, not the embed-focused subset.
**How to avoid:** Extract from `run_scan()` issues list or call lower-level scanner helpers (`check_embed_induction_needed`) directly. Implement drift check separately.

### Pitfall 5: Excerpt Length Not Enforced

**What goes wrong:** `run_search` returns `excerpt` from the raw payload text, which can be thousands of characters. If not capped, `carta_search` floods Claude's context.
**Why it happens:** `run_search` returns `payload.get("text", "")` without truncation.
**How to avoid:** Cap excerpt in the `carta_search` handler: `r["excerpt"][:300]` per D-02.

## Code Examples

### Tool Handler Registration (FastMCP v1.26.0)

```python
# Source: mcp v1.26.0 installed SDK — FastMCP.tool docstring (verified locally)
from mcp.server.fastmcp import FastMCP

mcp_server = FastMCP("carta")

@mcp_server.tool()
def carta_search(query: str, top_k: int = 5) -> list[dict] | dict:
    """Search embedded project documentation."""
    ...
```

### Structured Error Return Pattern

```python
# Source: CONTEXT.md D-09, D-10 (locked decisions)
# All tools follow this uniform error shape — never raise
return {"error": "service_unavailable", "detail": "Qdrant unreachable at http://localhost:6333"}
return {"error": "file_not_found", "detail": f"Path does not exist: {path}"}
return {"error": "timeout", "detail": f"Embed exceeded {FILE_TIMEOUT_S}s timeout"}
return {"error": "collection_not_found", "detail": "Run `carta init` to initialize this project"}
```

### Sidecar mtime Update (to add to pipeline.py)

```python
# In _embed_one_file sidecar_updates dict — add file_mtime field
import os
sidecar_updates = {
    "status": "embedded",
    "indexed_at": datetime.now(timezone.utc).isoformat(),
    "chunk_count": count,
    "file_mtime": os.path.getmtime(str(file_path)),  # float: seconds since epoch
}
```

## Environment Availability

| Dependency | Required By | Available | Version | Fallback |
|------------|------------|-----------|---------|----------|
| mcp (FastMCP) | All tool handlers | Yes | 1.26.0 | — |
| qdrant-client | carta_search, carta_embed | Yes (project dep) | 1.7+ | Return service_unavailable |
| Qdrant service | carta_search, carta_embed | Runtime check | — | Structured error return |
| Ollama service | carta_search, carta_embed | Runtime check | — | Structured error return |
| Python 3.10+ | All | Yes (3.12 confirmed) | 3.12 | — |

**Missing dependencies with no fallback:** None — all libraries installed.

**Runtime services (Qdrant, Ollama) are optional at startup** — tools check connectivity on each call and return structured errors if unreachable.

## Validation Architecture

### Test Framework

| Property | Value |
|----------|-------|
| Framework | pytest 7.0+ |
| Config file | `pyproject.toml` (pytest section) |
| Quick run command | `pytest carta/mcp/tests/ -x -q` |
| Full suite command | `pytest carta/ -x -q` |

### Phase Requirements → Test Map

| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| MCP-02 | `carta_search` returns scored, source-attributed results | unit (mock Qdrant) | `pytest carta/mcp/tests/test_server.py::test_carta_search -x` | No — Wave 0 |
| MCP-02 | `carta_search` returns error dict on Qdrant failure | unit | `pytest carta/mcp/tests/test_server.py::test_carta_search_qdrant_unavailable -x` | No — Wave 0 |
| MCP-03 | `carta_embed` skips unchanged file (mtime match) | unit (mock embed) | `pytest carta/mcp/tests/test_server.py::test_carta_embed_skip -x` | No — Wave 0 |
| MCP-03 | `carta_embed` returns error dict on file not found | unit | `pytest carta/mcp/tests/test_server.py::test_carta_embed_file_not_found -x` | No — Wave 0 |
| MCP-03 | `carta_embed` returns error dict on timeout | unit (mock timeout) | `pytest carta/mcp/tests/test_server.py::test_carta_embed_timeout -x` | No — Wave 0 |
| MCP-04 | `carta_scan` returns pending/drift arrays | unit (mock scanner) | `pytest carta/mcp/tests/test_server.py::test_carta_scan -x` | No — Wave 0 |
| MCP-05 | `carta-mcp` entrypoint registered and callable | unit (existing) | `pytest carta/mcp/tests/test_server.py::test_server_main_is_callable -x` | Yes |
| MCP-01 | `server.py` has no print() or sys.exit() | AST static (existing) | `pytest carta/mcp/tests/test_server.py -k "no_print or no_sys_exit" -x` | Yes |

### Sampling Rate

- **Per task commit:** `pytest carta/mcp/tests/ -x -q`
- **Per wave merge:** `pytest carta/ -x -q`
- **Phase gate:** Full suite green before `/gsd:verify-work`

### Wave 0 Gaps

- [ ] `carta/mcp/tests/test_server.py` — add tool handler tests (MCP-02, MCP-03, MCP-04); file exists but only has scaffold tests
- [ ] `carta/embed/tests/test_embed.py` — add test for `run_embed_file()` single-file adapter and mtime skip logic

## Sources

### Primary (HIGH confidence)

- Installed SDK: `mcp==1.26.0` — `FastMCP.tool` decorator signature verified via `help()` locally
- Source code: `carta/mcp/server.py` — scaffold and wire-protocol constraints confirmed
- Source code: `carta/embed/pipeline.py` — `run_embed`, `run_search`, `_embed_one_file` signatures and return shapes confirmed
- Source code: `carta/embed/induct.py` — sidecar schema fields confirmed (no `file_mtime` field)
- Source code: `carta/scanner/scanner.py` — `run_scan` return shape, `check_embed_induction_needed` logic confirmed
- Source code: `pyproject.toml` — `carta-mcp` entrypoint confirmed present

### Secondary (MEDIUM confidence)

- CONTEXT.md locked decisions (D-01 through D-10) — response shapes, error types, tool signatures all specified by user

## Project Constraints (from CLAUDE.md)

- **Tech stack:** Python 3.10+, Qdrant client, Ollama HTTP API, MCP stdio — no new infra
- **MCP SDK:** Use `mcp>=1.7.1` (official SDK, FastMCP bundled) — do NOT use standalone `fastmcp` PyPI package
- **Wire-protocol discipline:** stdout reserved for JSON-RPC; all logging to stderr; no `print()`; no `sys.exit()`; no unhandled exceptions from tool handlers
- **No CLI dependency:** MCP tool handlers must not import or call `carta/cli.py`
- **Shared service layer:** All three tiers (CLI, MCP, hook) delegate to `pipeline.py` + `scanner.py` — no duplicate logic in `server.py`
- **Naming:** Functions use snake_case; private helpers use leading underscore (`_load_cfg`)
- **Error handling:** Return early with structured dicts; never raise from tool handler boundary

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH — installed versions confirmed locally
- Architecture patterns: HIGH — existing code read directly, patterns confirmed
- Pitfalls: HIGH — derived from actual code gaps found in source (missing file_mtime, batch-only run_embed)
- Drift detection gap: HIGH — verified by reading full induct.py sidecar schema (no mtime field present)

**Research date:** 2026-03-26
**Valid until:** 2026-04-25 (stable domain, no fast-moving dependencies)
