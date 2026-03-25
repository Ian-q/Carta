# Phase 1: Pipeline Reliability + MCP Foundation - Research

**Researched:** 2026-03-25
**Domain:** Python embed pipeline reliability + MCP stdio server scaffolding
**Confidence:** HIGH

## Summary

Phase 1 fixes five discrete bugs in the embed pipeline (batch upserts, per-file timeout, chunking
overlap, verbose suppression, sidecar `current_path`) and establishes the MCP server scaffold with
correct wire-protocol discipline. All targets are already identified in existing source files — this
is a modification phase, not a greenfield one. The scope is tightly bounded: no new MCP tool
handlers, no new user-facing commands.

The MCP server uses the official `mcp` Python SDK (`mcp>=1.7.1`, which bundles FastMCP). The SDK
handles stdio framing, JSON-RPC encoding, and transport lifecycle. Phase 1 only needs the scaffold
— a running server with no tools registered, all log output on stderr, and a `carta-mcp` entry in
`pyproject.toml` so `.mcp.json` can register it. Plugin cache cleanup is the final cleanup step in
`bootstrap.py`.

**Primary recommendation:** Modify existing functions in-place preserving all call signatures;
add `carta/mcp/__init__.py` + `carta/mcp/server.py`; add `carta-mcp` entrypoint to
`pyproject.toml`; add `.mcp.json` at project root.

---

<user_constraints>
## User Constraints (from CONTEXT.md)

### Locked Decisions

- **D-01:** `upsert_chunks()` sends chunks to Qdrant in batches of 32 (single `client.upsert()`
  call per batch). Batch size is not configurable in Phase 1.
- **D-02:** Per-file embed enforces a 300s wall-clock timeout. Files exceeding the limit are
  skipped with a warning; pipeline continues.
- **D-03:** Chunking overlap capped at 25% of take size; safety counter lowered to 2× word count
  to guarantee forward progress on dense single-paragraph documents.
- **D-04:** `run_embed`, `run_search`, `run_scan` each accept a `verbose=False` parameter. When
  False, all `print()` output is suppressed.
- **D-05:** `write_sidecar()` writes `current_path` (relative path from repo root) on creation.
  On `carta embed`, a full-repo scan of all `*.embed-meta.yaml` files heals any missing the field.
- **D-06:** `carta/mcp/` directory with minimal stdio JSON-RPC server. Wire-protocol discipline
  only — no tool handlers in Phase 1.
- **D-07:** `.mcp.json` added to project root as sole Carta registration point. Plugin cache
  registration removed entirely — no hybrid.
- **D-08:** `carta init` removes BOTH plugin cache paths:
  - `~/.claude/plugins/carta/` (old v0.1.x)
  - `~/.claude/plugins/cache/carta-cc/carta-cc/{version}/` (current cache path)
  Post-deletion assertion verifies no residue; prints clear error rather than silently continuing.

### Claude's Discretion

- **MCP library choice:** Use official `mcp` Python SDK for the stdio server scaffold unless
  researcher finds strong reason to hand-roll JSON-RPC. SDK handles transport framing.
- **`carta-mcp` entrypoint timing:** Phase 1 scaffold should include the `carta-mcp` script entry
  in `pyproject.toml`. Verify whether a minimal server can be invoked without a formal entrypoint
  (it cannot — `carta-mcp` must be registered before `.mcp.json` can reference it).

### Deferred Ideas (OUT OF SCOPE)

- Sidecar enrichment / agent-populated notes
- `carta status` / `carta doctor` commands (OPS-01, OPS-02, OPS-03)
- Any MCP tool handlers (MCP-02, MCP-03, MCP-04) — these are Phase 2
</user_constraints>

---

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|------------------|
| PIPE-01 | Batch Qdrant upserts (32 per call) | See Architecture Patterns — Batch Upsert |
| PIPE-02 | Per-file 300s timeout; skip+warn on breach | See Architecture Patterns — Per-File Timeout |
| PIPE-03 | Overlap cap 25% of take; safety counter 2× word count | See Code Examples — Overlap Cap Fix |
| PIPE-04 | `verbose=False` on run_embed/run_search/run_scan | See Architecture Patterns — Verbose Suppression |
| PIPE-05 | `current_path` in sidecar; full-repo heal on embed | See Architecture Patterns — Sidecar Heal |
| MCP-01 | MCP scaffold in `carta/mcp/`; stderr logging; structured errors | See Standard Stack and MCP Scaffold section |
| MCP-06 | `.mcp.json` at project root; plugin cache removed | See .mcp.json Format section |
| MCP-07 | `carta init` removes stale cache dirs; post-deletion assertion | See Architecture Patterns — Cache Cleanup |
</phase_requirements>

---

## Standard Stack

### Core

| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| mcp | >=1.7.1 | Official MCP Python SDK — stdio server, JSON-RPC framing, FastMCP bundled | Authoritative SDK; FastMCP reduces scaffold surface to ~10 lines |
| qdrant-client | >=1.7 (already dep) | Vector DB client — `client.upsert()` accepts a list of PointStructs | Already in project; batch upsert is native API |
| threading / concurrent.futures | stdlib | Per-file timeout enforcement via `ThreadPoolExecutor` or `threading.Timer` | No new deps; timeout pattern is idiomatic Python |

### Supporting

| Library | Version | Purpose | When to Use |
|---------|---------|---------|-------------|
| logging | stdlib | Structured logging directed to stderr | MCP server must never print to stdout |
| signal / threading | stdlib | Timeout enforcement for per-file embed | Used inside pipeline.py |

### Alternatives Considered

| Instead of | Could Use | Tradeoff |
|------------|-----------|----------|
| `mcp` SDK (official) | Hand-rolled JSON-RPC | SDK handles framing edge cases, initialization handshake, capability negotiation; hand-rolling is high risk for a scaffold |
| `concurrent.futures.ThreadPoolExecutor` with `as_completed(timeout=)` | `signal.alarm` (SIGALRM) | SIGALRM is Unix-only; ThreadPoolExecutor works on all platforms and integrates cleanly with existing sync code |

**Installation:**

```bash
pip install "mcp>=1.7.1"
```

Add to `pyproject.toml` dependencies:

```toml
"mcp>=1.7.1",
```

**Version verification:** Latest confirmed on PyPI: `mcp 1.26.0` (as of 2026-03-25, verified via
`pip index versions mcp`). Minimum required per STATE.md decision: `>=1.7.1`. Recommending `>=1.7.1`
in `pyproject.toml` to allow upgrades; pin to `>=1.7.1,<2.0` if stability is a concern.

---

## Architecture Patterns

### Recommended Project Structure

```
carta/
├── mcp/
│   ├── __init__.py       # empty or re-export server
│   └── server.py         # FastMCP scaffold, stderr logging config
├── embed/
│   ├── embed.py          # PIPE-01: batch upsert
│   ├── pipeline.py       # PIPE-02: timeout; PIPE-04: verbose param
│   ├── induct.py         # PIPE-05: current_path in write_sidecar
│   └── parse.py          # PIPE-03: overlap cap
└── install/
    └── bootstrap.py      # MCP-07: cache cleanup + assertion
.mcp.json                 # MCP-06: sole registration point
pyproject.toml            # carta-mcp entrypoint added
```

### Pattern 1: Batch Upsert (PIPE-01)

**What:** Accumulate `PointStruct` objects into a list; call `client.upsert()` once per batch of 32
rather than once per chunk.

**When to use:** Always in `upsert_chunks()`.

**Current code (embed.py line 76–91):** iterates chunks, calls `client.upsert(points=[point])`
per chunk — O(N) HTTP round trips.

**Fix:**

```python
# Source: D-01 decision; qdrant-client API (client.upsert accepts list)
BATCH_SIZE = 32

def upsert_chunks(chunks: list[dict], cfg: dict, client: QdrantClient = None) -> int:
    ...
    batch: list[PointStruct] = []
    upserted = 0
    for chunk in chunks:
        chunk_id = f"{chunk.get('slug', '?')}[{chunk.get('chunk_index', '?')}]"
        try:
            vec = get_embedding(chunk["text"], ollama_url=ollama_url, model=model)
            payload = {k: v for k, v in chunk.items() if k != "text"}
            payload["text"] = chunk["text"]
            batch.append(PointStruct(
                id=_point_id(chunk["slug"], chunk["chunk_index"]),
                vector=vec,
                payload=payload,
            ))
            if len(batch) >= BATCH_SIZE:
                client.upsert(collection_name=coll_name, points=batch)
                upserted += len(batch)
                batch = []
        except Exception as e:
            print(f"Warning: skipping chunk {chunk_id} — {e}")
    if batch:
        client.upsert(collection_name=coll_name, points=batch)
        upserted += len(batch)
    return upserted
```

**Note:** The existing per-chunk error handling (skip bad chunk, continue) changes slightly: if
embedding fails for one chunk, it is not added to the batch, so the rest of the batch is
unaffected. The batch is flushed after the loop completes. This preserves the existing
fault-isolation guarantee.

### Pattern 2: Per-File Timeout (PIPE-02)

**What:** Wrap each file's embed work in a `concurrent.futures.ThreadPoolExecutor` future with
`future.result(timeout=300)`. On `TimeoutError`, mark file skipped with a warning.

**When to use:** Around the per-file processing block in `run_embed()` (pipeline.py lines 122–151).

```python
# Source: D-02 decision; concurrent.futures stdlib
import concurrent.futures

FILE_TIMEOUT_S = 300

def _embed_one_file(file_info, cfg, client, repo_root, max_tokens, overlap_fraction):
    """Extract, chunk, embed one file. Returns (count, sidecar_updates)."""
    ...

# Inside run_embed loop:
with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
    future = executor.submit(_embed_one_file, file_info, cfg, client, repo_root,
                             max_tokens, overlap_fraction)
    try:
        count, updates = future.result(timeout=FILE_TIMEOUT_S)
        ...
    except concurrent.futures.TimeoutError:
        print(f"  [{idx}/{total}] TIMEOUT: {file_path.name} exceeded {FILE_TIMEOUT_S}s — skipping",
              file=sys.stderr, flush=True)
        summary["skipped"] += 1
```

**Alternative:** A simpler approach uses `threading.Timer` to set a flag; the thread checks it.
ThreadPoolExecutor is cleaner because `future.result(timeout=)` blocks the calling thread and
cancellation is automatic.

### Pattern 3: Overlap Cap (PIPE-03)

**What:** In `chunk_text()` (parse.py), the overlap carried into the next chunk must not exceed
25% of the `take` size. Additionally, the safety counter that prevents infinite loops should be
`2 * original_words_len` rather than `max(10_000, original_words_len * 50)`.

**Current safety counter (parse.py line 130):**
```python
if safety_iters > max(10_000, original_words_len * 50):
```

**Fix:**
```python
# Source: D-03 decision
if safety_iters > max(10, original_words_len * 2):
```

**Overlap cap fix (parse.py line 149–153):**
```python
# Current:
overlap_len = min(overlap_words, len(take) - 1)
# Fix — cap at 25% of take size:
overlap_cap = max(0, len(take) // 4)
overlap_len = min(overlap_words, overlap_cap)
```

### Pattern 4: Verbose Suppression (PIPE-04)

**What:** Add `verbose: bool = False` parameter to `run_embed`, `run_search`, `run_scan`. When
`False`, all `print()` calls inside those functions are suppressed.

**Simplest implementation:** Pass `verbose` through; replace each `print(...)` with
`if verbose: print(...)`. Do NOT suppress `sys.stderr` prints — those are error signals.

**Call site compatibility:** `carta/cli.py::cmd_embed()` calls `run_embed(repo_root, cfg)` — this
call continues to work unchanged because `verbose=False` is the default. To get progress output in
the CLI, the call becomes `run_embed(repo_root, cfg, verbose=True)`.

```python
# pipeline.py
def run_embed(repo_root: Path, cfg: dict, verbose: bool = False) -> dict:
    ...
    if verbose:
        print("carta embed: checking Qdrant connectivity...", flush=True)
```

**Important:** `cli.py::cmd_embed()` must be updated to pass `verbose=True` so existing CLI
behavior is preserved. This is a call-site update, not just a function signature change.

### Pattern 5: Sidecar `current_path` + Heal (PIPE-05)

**What:** `write_sidecar()` in `induct.py` must include `current_path` (relative path from repo
root) in the stub. A full-repo heal pass in `run_embed()` reads every `*.embed-meta.yaml` and
writes `current_path` if missing.

**`generate_sidecar_stub()` change (induct.py):**
```python
# Add current_path to stub — requires repo_root; already a parameter
stub = {
    "slug": slug,
    "doc_type": doc_type,
    "current_path": str(rel_path),   # ADD THIS
    "status": "pending",
    ...
}
```

**Heal pass (run_embed in pipeline.py — runs before processing pending files):**
```python
def _heal_sidecar_current_paths(repo_root: Path) -> int:
    """Add current_path to sidecars missing the field. Returns count healed."""
    healed = 0
    for sidecar_path in repo_root.rglob("*.embed-meta.yaml"):
        data = read_sidecar(sidecar_path)
        if data is None or "current_path" in data:
            continue
        # Infer current_path from sidecar location
        stem = sidecar_path.name.replace(".embed-meta.yaml", "")
        for ext in _SUPPORTED_EXTENSIONS:
            candidate = sidecar_path.parent / f"{stem}{ext}"
            if candidate.exists():
                data["current_path"] = str(candidate.relative_to(repo_root))
                with open(sidecar_path, "w") as f:
                    yaml.dump(data, f, default_flow_style=False, sort_keys=False)
                healed += 1
                break
    return healed
```

### Pattern 6: MCP Server Scaffold (MCP-01)

**What:** Minimal FastMCP server in `carta/mcp/server.py`. No tool handlers. All logging via
Python `logging` module directed to stderr. Server runs via `mcp.run()` with stdio transport.

```python
# carta/mcp/server.py
# Source: mcp SDK (mcp>=1.7.1, FastMCP bundled); confirmed pattern from STATE.md
import logging
import sys
from mcp.server.fastmcp import FastMCP

# Direct ALL log output to stderr — stdout is reserved for JSON-RPC framing
logging.basicConfig(
    stream=sys.stderr,
    level=logging.WARNING,
    format="%(levelname)s [carta-mcp] %(message)s",
)

mcp = FastMCP("carta")

def main() -> None:
    mcp.run()   # stdio transport by default

if __name__ == "__main__":
    main()
```

**Key discipline rules:**
1. Never call `print()` in MCP server code — use `logging.error()` / `logging.warning()` instead
2. Never call `sys.exit()` — raise exceptions; FastMCP returns structured error responses
3. Tool handlers (Phase 2) must catch all exceptions and return structured error dicts, not raise

### Pattern 7: `.mcp.json` Format (MCP-06)

**What:** Project-root `.mcp.json` registers `carta-mcp` as the sole Carta MCP server.

```json
{
  "mcpServers": {
    "carta": {
      "command": "carta-mcp",
      "args": []
    }
  }
}
```

**Verified format:** Confirmed against real `.mcp.json` at `/Users/ian/School/Elementrailer/petsense/.mcp.json`
(format: `{"mcpServers": {"name": {"command": "...", "args": [...]}}}`)

### Pattern 8: `pyproject.toml` Entrypoint

**What:** Add `carta-mcp` script entry alongside existing `carta`:

```toml
[project.scripts]
carta = "carta.cli:main"
carta-mcp = "carta.mcp.server:main"
```

### Pattern 9: Plugin Cache Cleanup (MCP-07)

**What:** `_install_skills()` in `bootstrap.py` currently only cleans stale version dirs under
`~/.claude/plugins/cache/carta-cc/carta-cc/`. D-08 requires removing BOTH:
- `~/.claude/plugins/carta/` (old v0.1.x path)
- `~/.claude/plugins/cache/carta-cc/` (entire tree, not just version subdirs)

With post-deletion assertion:

```python
def _remove_plugin_cache() -> None:
    """Remove all Carta plugin cache directories. Prints error if residue remains."""
    paths_to_remove = [
        Path.home() / ".claude/plugins/carta",
        Path.home() / ".claude/plugins/cache/carta-cc",
    ]
    for p in paths_to_remove:
        if p.exists():
            shutil.rmtree(p)
            print(f"  Removed stale plugin cache: {p}")

    # Post-deletion assertion
    residue = [p for p in paths_to_remove if p.exists()]
    if residue:
        print(
            f"  ERROR: plugin cache residue remains after cleanup: {residue}\n"
            f"  Remove manually before using carta-mcp.",
            file=sys.stderr,
        )
```

This replaces the version-dir-only cleanup logic in the current `_install_skills()`.

### Anti-Patterns to Avoid

- **stdout in MCP server:** Any `print()` call in `carta/mcp/` corrupts the JSON-RPC stream silently. Claude Code will fail to parse responses without any visible error.
- **sys.exit() in MCP server:** Terminates the transport without a proper shutdown; callers receive EOF instead of a structured error.
- **Hybrid plugin cache + .mcp.json:** The two-registry problem (Issue #7) — Claude Code can load stale skills from the cache and ignore `.mcp.json`, or load both and fail. Hard cutover only.
- **Overlap growing unbounded:** The current safety counter `max(10_000, N*50)` allows ~50× the word count as iterations. On a dense 1-page PDF with a single paragraph, this can run for many seconds. The 2× counter forces forward progress.

---

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| JSON-RPC framing for stdio | Custom framing/parser | `mcp` SDK FastMCP | Handles content-length framing, initialization handshake, capability negotiation — all edge cases handled |
| Per-file timeout | Custom SIGALRM handler | `concurrent.futures.ThreadPoolExecutor` with `future.result(timeout=)` | SIGALRM is Unix-only; futures work cross-platform and compose cleanly |
| Batch accumulation | Custom flush logic | Accumulate list + flush at N | Already how qdrant-client batch upsert works; no helper library needed |

---

## Common Pitfalls

### Pitfall 1: stdout Pollution Breaks MCP Silently

**What goes wrong:** Any `print()` in the MCP server process corrupts the JSON-RPC byte stream. Claude Code will see malformed responses or silently drop the server.

**Why it happens:** MCP stdio transport uses stdout exclusively for framing; any non-JSON-RPC output (including progress prints, warnings) breaks the parser.

**How to avoid:** Configure `logging.basicConfig(stream=sys.stderr)` at the top of `server.py`. Never import `carta.cli` from MCP code (cli.py has prints at import time in some flows). Run `verbose=False` for all pipeline calls from MCP handlers.

**Warning signs:** `carta-mcp` produces output but no tool responses appear in Claude Code.

### Pitfall 2: verbose=False Default Breaks CLI Progress Output

**What goes wrong:** Adding `verbose=False` as default to `run_embed` and not updating `cli.py::cmd_embed()` call site means `carta embed` runs silently — no progress output.

**Why it happens:** Call site uses positional args `run_embed(repo_root, cfg)` — the default `verbose=False` takes effect.

**How to avoid:** Update `cmd_embed()` to call `run_embed(repo_root, cfg, verbose=True)`.

**Warning signs:** `carta embed` produces no output at all — no "checking Qdrant", no per-file progress.

### Pitfall 3: Batch Upsert Swallows Per-Chunk Errors

**What goes wrong:** If embedding fails for one chunk in a batch, the batch never gets flushed, dropping all previously accumulated good points.

**Why it happens:** Error in `get_embedding()` raises before the chunk is appended to the batch — so it's naturally excluded. But if the error occurs in `client.upsert()`, the whole batch is lost.

**How to avoid:** Only call `get_embedding()` inside the try block before appending to batch. The `client.upsert()` call with the full batch should be its own try block so batch-level upsert failures are logged but don't lose the individual point counts.

### Pitfall 4: Sidecar Heal Pass Overwrites `current_path` for Moved Files

**What goes wrong:** The heal pass infers `current_path` from the sidecar's current filesystem location. If the sidecar exists but the source file does not (e.g., file was moved or deleted), the heal pass should skip that sidecar.

**Why it happens:** The heal pass only adds `current_path` when missing — it should also verify the source file exists before writing the path.

**How to avoid:** In `_heal_sidecar_current_paths()`, only write `current_path` when the candidate source file exists (already shown in the example above — the `candidate.exists()` check guards this).

### Pitfall 5: Plugin Cache Residue After `shutil.rmtree`

**What goes wrong:** On macOS with SIP or unusual permissions, `shutil.rmtree` can leave `.DS_Store` files or fail silently on read-only entries.

**Why it happens:** macOS adds `.DS_Store` files automatically; if a subdirectory is read-only, rmtree can raise.

**How to avoid:** Wrap `shutil.rmtree(p)` with `ignore_errors=False` and catch `OSError`. The post-deletion assertion prints a clear error to stderr if any path under the target still exists.

---

## Code Examples

### Verified: `client.upsert()` accepts a list of PointStructs

```python
# Source: qdrant-client Python SDK public API — list[PointStruct] is standard
client.upsert(
    collection_name="myproject_doc",
    points=[point1, point2, ...],  # up to 32 in Phase 1
)
```

### Verified: `.mcp.json` format

```json
{
  "mcpServers": {
    "carta": {
      "command": "carta-mcp",
      "args": []
    }
  }
}
```

Source: confirmed against `/Users/ian/School/Elementrailer/petsense/.mcp.json` (real project file
on this machine).

### Verified: `concurrent.futures` timeout pattern

```python
# Source: Python stdlib docs — concurrent.futures.ThreadPoolExecutor
import concurrent.futures

with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
    future = executor.submit(fn, *args)
    try:
        result = future.result(timeout=300)
    except concurrent.futures.TimeoutError:
        # future is still running in background thread — executor.shutdown(wait=False)
        # is called automatically on context exit; thread may run to completion
        pass
```

**Note:** On timeout, the thread cannot be hard-killed in Python. The thread will run to completion
in the background. For embed workloads (Ollama HTTP calls), the thread will eventually finish or
hit the `requests` timeout (60s per call, per existing `get_embedding()`). This is acceptable
behavior — the pipeline moves on, and the thread cleans up on its own.

---

## Environment Availability

| Dependency | Required By | Available | Version | Fallback |
|------------|-------------|-----------|---------|----------|
| Python 3.10+ | All code | ✓ | 3.14.3 | — |
| pytest | Test suite | ✓ | 9.0.2 | — |
| qdrant-client | Pipeline tests (mocked) | ✓ (in project deps) | >=1.7 | Mock in tests |
| mcp SDK | MCP scaffold | ✗ | not installed | Must add to pyproject.toml + install |
| Docker | Qdrant container | ✓ | 28.0.4 | — |
| `~/.claude/plugins/cache/carta-cc/` | Plugin cache cleanup target | ✓ | carta-cc present | — |

**Missing dependencies with no fallback:**

- `mcp>=1.7.1` — not installed in current environment. Must be added to `pyproject.toml` as a
  runtime dependency and installed (`pip install -e ".[dev]"` or equivalent) before the MCP
  scaffold can be tested.

---

## Validation Architecture

### Test Framework

| Property | Value |
|----------|-------|
| Framework | pytest 9.0.2 |
| Config file | none (uses pyproject.toml or default discovery) |
| Quick run command | `python3 -m pytest carta/embed/tests/ carta/install/tests/ -x -q` |
| Full suite command | `python3 -m pytest -x -q` |

### Phase Requirements → Test Map

| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| PIPE-01 | `upsert_chunks()` calls `client.upsert()` with list of ≤32 points | unit | `python3 -m pytest carta/embed/tests/test_embed.py -x -q -k "batch"` | ❌ Wave 0 |
| PIPE-02 | File exceeding 300s is skipped with warning; pipeline continues | unit | `python3 -m pytest carta/embed/tests/test_embed.py -x -q -k "timeout"` | ❌ Wave 0 |
| PIPE-03 | `chunk_text()` with overlap=0.5 terminates on dense single-para doc | unit | `python3 -m pytest carta/embed/tests/test_embed.py -x -q -k "overlap"` | ✅ (test_chunk_text_pathological_long_token_with_overlap_terminates) |
| PIPE-04 | `run_embed(verbose=False)` produces no stdout | unit | `python3 -m pytest carta/embed/tests/test_embed.py -x -q -k "verbose"` | ❌ Wave 0 |
| PIPE-05 | `write_sidecar()` includes `current_path`; heal pass adds field to old sidecars | unit | `python3 -m pytest carta/embed/tests/test_embed.py -x -q -k "current_path or heal"` | ❌ Wave 0 |
| MCP-01 | `carta-mcp` process produces no stdout before tool calls; stderr has log output | smoke | `python3 -m pytest carta/mcp/tests/test_server.py -x -q` | ❌ Wave 0 |
| MCP-06 | `.mcp.json` exists at project root with correct mcpServers entry | unit | `python3 -m pytest tests/test_mcp_registration.py -x -q` | ❌ Wave 0 |
| MCP-07 | `carta init` removes both cache paths; post-deletion assertion fires | unit | `python3 -m pytest carta/install/tests/test_bootstrap.py -x -q -k "cache"` | ❌ Wave 0 |

### Sampling Rate

- **Per task commit:** `python3 -m pytest carta/embed/tests/ carta/install/tests/ -x -q`
- **Per wave merge:** `python3 -m pytest -x -q`
- **Phase gate:** Full suite green before `/gsd:verify-work`

### Wave 0 Gaps

- [ ] `carta/embed/tests/test_embed.py` — add tests for PIPE-01 (batch), PIPE-02 (timeout), PIPE-04 (verbose), PIPE-05 (current_path + heal)
- [ ] `carta/mcp/tests/__init__.py` + `carta/mcp/tests/test_server.py` — MCP-01 smoke test
- [ ] `tests/test_mcp_registration.py` — MCP-06 .mcp.json file presence test
- [ ] `carta/install/tests/test_bootstrap.py` — add tests for MCP-07 (cache cleanup + assertion)

---

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| Plugin cache (`~/.claude/plugins/`) | `.mcp.json` project-scoped registration | v0.2 migration (this phase) | Eliminates two-registry stale-version bug |
| One `client.upsert()` per chunk | Batched upsert (32 per call) | PIPE-01 fix | O(N/32) HTTP round trips vs O(N) |
| No per-file timeout | 300s wall-clock timeout | PIPE-02 fix | Dense PDFs can no longer hang the pipeline indefinitely |

**Deprecated/outdated:**

- `_install_skills()` plugin cache logic: entire function replaced by `_remove_plugin_cache()` +
  the `.mcp.json` approach. Skills are no longer distributed via plugin cache in v0.2.

---

## Open Questions

1. **`_install_skills()` replacement scope**
   - What we know: D-08 removes both cache paths; `.mcp.json` is the new registration point
   - What's unclear: Does `_install_skills()` need to survive at all (for skill `.md` files), or is the entire function replaced? The function also copies `SKILL.md` files into the cache — if skills are no longer cache-distributed, this is dead code.
   - Recommendation: Treat `_install_skills()` as fully replaced by `_remove_plugin_cache()`. SKILL.md files remain in the package for reference but are not installed to any cache location in v0.2.

2. **`run_scan` verbose param — where is `run_scan` defined?**
   - What we know: PIPE-04 requires `run_scan` to accept `verbose=False`; REQUIREMENTS.md lists it
   - What's unclear: `run_scan` is not visible in `pipeline.py` — it may live in `carta/scanner/scanner.py`
   - Recommendation: Planner should verify location of `run_scan` before writing that task.

3. **MCP SDK `mcp.run()` exact signature**
   - What we know: STATE.md says `mcp>=1.7.1`, FastMCP bundled, `mcp.run()` for stdio transport
   - What's unclear: Web search unavailable; exact import path and `run()` signature not verified against installed package (package not installed in env)
   - Recommendation: After installing `mcp>=1.7.1`, verify with `python3 -c "from mcp.server.fastmcp import FastMCP; help(FastMCP.run)"`. Expected: `FastMCP("name").run()` with no required args for stdio mode.

---

## Project Constraints (from CLAUDE.md)

- Python 3.10+ syntax only (project is on 3.14.3 — all stdlib patterns are valid)
- No new infra: `mcp` SDK is explicitly permitted (`mcp>=1.7.1` in STATE.md); it uses Ollama + Qdrant already present
- All embeddings use 768-dimensional vectors (VECTOR_DIM = 768)
- Config dict (`cfg`) passed as parameter, not global — MCP handlers must follow same pattern
- Errors to stderr, progress to stdout — MCP server INVERTS this (everything to stderr, JSON-RPC to stdout only)
- `sys.exit()` not used in MCP server (use structured error returns)
- No `__all__` — all public functions importable
- CLI (`carta/cli.py`) is never imported by MCP tier

---

## Sources

### Primary (HIGH confidence)

- Direct source code inspection — `carta/embed/embed.py`, `pipeline.py`, `induct.py`, `parse.py`, `install/bootstrap.py`, `conftest.py`, `carta/embed/tests/test_embed.py`, `carta/install/tests/test_bootstrap.py`
- `pyproject.toml` — current scripts and deps
- `~/.claude/plugins/installed_plugins.json` — confirmed `carta-cc@carta-cc` in plugin cache
- `/Users/ian/School/Elementrailer/petsense/.mcp.json` — confirmed `.mcp.json` schema on this machine
- `pip index versions mcp` — confirmed `mcp 1.26.0` latest; `1.7.1` minimum per STATE.md decision

### Secondary (MEDIUM confidence)

- `STATE.md` implementation notes: `mcp>=1.7.1`, FastMCP bundled, `mcp.run()` for stdio — recorded from previous research session

### Tertiary (LOW confidence)

- MCP SDK `FastMCP` API details (import paths, `run()` signature) — based on training knowledge and STATE.md note; web search unavailable; verify after install

---

## Metadata

**Confidence breakdown:**

- Standard stack: HIGH — versions verified via `pip index versions`; decision recorded in STATE.md
- Architecture (pipeline fixes): HIGH — all target functions read in full; fixes are mechanical
- Architecture (MCP scaffold): MEDIUM — FastMCP import path and `run()` signature not verified against installed package (not installed); verify after `pip install mcp>=1.7.1`
- Pitfalls: HIGH — derived from direct source analysis and known MCP stdout-pollution failure mode

**Research date:** 2026-03-25
**Valid until:** 2026-04-25 (MCP SDK moves fast; re-check if scaffold has issues)
