# CLI + MCP Hybrid Migration and Stability Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a stable CLI-first Carta release that fixes known reliability bugs and adds an optional MCP server interface without regressing existing workflows.

**Architecture:** Keep the CLI (`init`, `scan`, `embed`, `search`) as the primary local interface and expose the same core functions through a new stdio MCP server. Complete reliability and schema fixes first so MCP wraps stable behavior, then document hybrid operations as the new default integration model.

**Tech Stack:** Python 3.10+, `pytest`, Qdrant client, Ollama HTTP API, MCP stdio server, Markdown docs.

---

## File Structure and Responsibilities

- `carta/embed/embed.py`: chunk embedding/upsert throughput, batching, progress, timeout behavior.
- `carta/embed/pipeline.py`: per-file embed orchestration and sidecar healing, per-file timeout enforcement.
- `carta/embed/parse.py`: PDF text extraction and chunking logic (overlap capping, forward-progress guarantees).
- `carta/embed/induct.py`: sidecar stub generation (`current_path` consistency).
- `carta/scanner/scanner.py`: sidecar validation fallback and issue severity behavior.
- `carta/install/bootstrap.py`: stale skill cache handling, hook command generation, `.gitignore` updates.
- `carta/cli.py`: user-facing warning semantics (PATH/interpreter conflicts).
- `carta/mcp/server.py` (new): MCP tool registration and delegation to existing core functions.
- `pyproject.toml`: MCP entrypoint/dependency wiring.
- `carta/embed/tests/test_embed.py`: embed batching/progress regression tests.
- `carta/embed/tests/test_parse.py`: chunking overlap and safety-counter behavior tests.
- `carta/scanner/tests/test_scanner.py`: sidecar/current_path behavior tests.
- `carta/install/tests/test_bootstrap.py`: bootstrap/gitignore/hook robustness tests.
- `carta/tests/test_cli.py`: PATH warning behavior tests.
- `README.md`, `docs/install.md`, `docs/testing/install-test-guide.md`: hybrid usage and troubleshooting guidance.

---

## Task 1: Embed Throughput and Hang-Resistance

**Files:**

- Modify: `carta/embed/embed.py`
- Modify: `carta/embed/parse.py`
- Modify: `carta/embed/pipeline.py`
- Test: `carta/embed/tests/test_embed.py`
- Test: `carta/embed/tests/test_parse.py`

### Task 1A: Fix Qdrant Batch Upsert and Progress Visibility

- [ ] **Step 1: Write failing tests for batched upsert behavior**

```python
def test_upsert_chunks_batches_points(mock_qdrant_client, cfg):
    chunks = [make_chunk(i) for i in range(65)]
    cfg["embed"]["qdrant_batch_size"] = 32
    upserted = upsert_chunks(chunks, cfg, client=mock_qdrant_client)
    assert upserted == 65
    assert mock_qdrant_client.upsert.call_count == 3  # 32 + 32 + 1
```

- [ ] **Step 2: Run tests to confirm failure before implementation**

Run: `pytest carta/embed/tests/test_embed.py::test_upsert_chunks_batches_points -v`  
Expected: FAIL on upsert call count assertions (currently 65 individual upserts).

- [ ] **Step 3: Implement Qdrant batch upsert + periodic progress logging**

In `carta/embed/embed.py`:

```python
batch_size = max(1, int(cfg["embed"].get("qdrant_batch_size", 32)))
for i in range(0, len(points), batch_size):
    batch = points[i:i + batch_size]
    client.upsert(collection_name=coll_name, points=batch)
    print(f"upserted {i + len(batch)}/{len(points)} chunks", flush=True)
```

- [ ] **Step 4: Verify batching implementation**

Run: `pytest carta/embed/tests/test_embed.py::test_upsert_chunks_batches_points -v`  
Expected: PASS with exactly 3 upsert calls.

### Task 1B: Fix Chunking Overlap Loop and Forward-Progress Guarantees

**Context:** `parse.py`'s `chunk_text()` re-injects `overlap_words` on every iteration. When PDFs have dense single-block paragraphs (e.g., register tables in datasheets), the word-by-word splitter takes very small chunks and the overlap can exceed the `take` size, causing near-zero forward progress. The safety counter is set too high (`original_words_len * 50`) and takes 20+ minutes to trip.

- [ ] **Step 1: Write failing test for pathological chunking case**

In `carta/embed/tests/test_parse.py`:

```python
def test_chunk_text_handles_dense_single_paragraph(cfg):
    """Test that chunking completes in reasonable time on dense text blocks."""
    # Simulate a 2000-word dense paragraph (no natural breaks)
    dense_text = " ".join([f"word{i}" for i in range(2000)])
    cfg["embed"]["chunk_token_budget"] = 200  # tight budget
    
    import time
    start = time.time()
    chunks = chunk_text(dense_text, cfg)
    elapsed = time.time() - start
    
    assert len(chunks) > 0, "should produce chunks"
    assert elapsed < 5.0, f"chunking took {elapsed:.1f}s, expected <5s"
```

- [ ] **Step 2: Run test to confirm pathological behavior**

Run: `pytest carta/embed/tests/test_parse.py::test_chunk_text_handles_dense_single_paragraph -v -s`  
Expected: FAIL with timeout or excessive duration (>20s).

- [ ] **Step 3: Implement overlap capping and lowered safety counter**

In `carta/embed/parse.py`, within `chunk_text()`:

```python
# Cap overlap at 25% of the take size to guarantee forward progress
max_overlap = max(10, len(take) // 4)  # at least 10 words, max 25% of take
overlap_words = words[max(0, i - max_overlap):i]

# ... (rest of chunking logic)

# Lower safety counter to fail fast (2x instead of 50x)
if iteration_count > len(original_words) * 2:
    print(f"WARNING: chunking safety limit hit for dense text block", file=sys.stderr)
    break  # fail fast instead of spinning
```

- [ ] **Step 4: Verify chunking completes quickly**

Run: `pytest carta/embed/tests/test_parse.py::test_chunk_text_handles_dense_single_paragraph -v -s`  
Expected: PASS with completion time <5s.

### Task 1C: Add Per-File Timeout Enforcement in Pipeline

**Context:** Even with chunking and upsert fixes, if a file hangs during embedding (Ollama stall, network issue), the entire `carta embed` process blocks indefinitely. Add per-file timeout with graceful degradation.

- [ ] **Step 1: Write test for per-file timeout enforcement**

In `carta/embed/tests/test_embed.py`:

```python
def test_embed_file_enforces_timeout(tmp_path, cfg, monkeypatch):
    """Test that embed_file respects per-file timeout and continues processing."""
    def slow_embed(*args, **kwargs):
        time.sleep(10)  # simulate hang
        return []
    
    monkeypatch.setattr("carta.embed.embed.embed_chunks", slow_embed)
    cfg["embed"]["per_file_timeout_seconds"] = 2
    
    pdf = tmp_path / "test.pdf"
    pdf.write_bytes(b"%PDF-1.4\ntest")
    
    result = embed_file(pdf, tmp_path, cfg)
    assert result["status"] == "timeout"
```

- [ ] **Step 2: Implement timeout wrapper in pipeline.py**

In `carta/embed/pipeline.py`:

```python
import signal
import contextlib

@contextlib.contextmanager
def timeout_context(seconds):
    def timeout_handler(signum, frame):
        raise TimeoutError(f"Operation exceeded {seconds}s timeout")
    
    old_handler = signal.signal(signal.SIGALRM, timeout_handler)
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old_handler)

def embed_file(file_path, repo_root, cfg):
    timeout = int(cfg["embed"].get("per_file_timeout_seconds", 300))  # 5min default
    try:
        with timeout_context(timeout):
            # ... existing embed logic
    except TimeoutError as e:
        print(f"TIMEOUT: {file_path.name} exceeded {timeout}s, skipping", file=sys.stderr)
        return {"status": "timeout", "file": str(file_path)}
```

- [ ] **Step 3: Verify timeout behavior**

Run: `pytest carta/embed/tests/test_embed.py::test_embed_file_enforces_timeout -v`  
Expected: PASS with timeout triggering and graceful continuation.

- [ ] **Step 4: Commit all Task 1 changes**

```bash
git add carta/embed/embed.py carta/embed/parse.py carta/embed/pipeline.py carta/embed/tests/test_embed.py carta/embed/tests/test_parse.py
git commit -m "fix: batch qdrant upserts, cap chunking overlap, add per-file timeouts"
```

---

## Task 2: Sidecar `current_path` Schema Alignment and Auto-Heal

**Files:**

- Modify: `carta/embed/induct.py`
- Modify: `carta/embed/pipeline.py`
- Modify: `carta/scanner/scanner.py`
- Test: `carta/scanner/tests/test_scanner.py`
- Test: `carta/embed/tests/test_embed.py`

- [ ] **Step 1: Write failing tests for sidecar stubs missing `current_path`**

```python
def test_generate_sidecar_stub_includes_current_path(tmp_path, cfg):
    pdf = tmp_path / "docs/reference/a.pdf"
    pdf.parent.mkdir(parents=True)
    pdf.write_bytes(b"%PDF")
    stub = generate_sidecar_stub(pdf, tmp_path, cfg)
    assert stub["current_path"] == "docs/reference/a.pdf"
```

- [ ] **Step 2: Run tests to confirm failure before implementation**

Run: `pytest carta/embed/tests/test_embed.py carta/scanner/tests/test_scanner.py -q`  
Expected: FAIL due to missing field / scanner behavior mismatch.

- [ ] **Step 3: Implement schema write + embed-time healing**

```python
stub["current_path"] = str(file_path.relative_to(repo_root))
updates.setdefault("current_path", str(file_path.relative_to(repo_root)))
```

- [ ] **Step 4: Add scanner fallback for co-located sidecar/pdf when missing field**

Run: `pytest carta/embed/tests/test_embed.py carta/scanner/tests/test_scanner.py -q`  
Expected: PASS and reduced false-positive sidecar drift findings.

- [ ] **Step 5: Commit**

```bash
git add carta/embed/induct.py carta/embed/pipeline.py carta/scanner/scanner.py carta/embed/tests/test_embed.py carta/scanner/tests/test_scanner.py
git commit -m "fix: align sidecar current_path schema across embed and scanner"
```

---

## Task 3: Bootstrap Hardening (Skill Cache, `.gitignore`, Hook Quoting, PATH Warning)

**Files:**

- Modify: `carta/install/bootstrap.py`
- Modify: `carta/cli.py`
- Test: `carta/install/tests/test_bootstrap.py`
- Test: `carta/tests/test_cli.py`

- [ ] **Step 1: Write failing tests for stale cache verification and gitignore dedupe**

```python
def test_update_gitignore_skips_duplicate_entries(tmp_path):
    gi = tmp_path / ".gitignore"
    gi.write_text(".carta/\n")
    _update_gitignore(tmp_path)
    assert gi.read_text().count(".carta/scan-results.json") == 0

def test_install_skills_verifies_stale_cache_deletion(tmp_path):
    """Test that bootstrap asserts no stale version dirs remain after cleanup."""
    # setup: create v0.1.6 and v0.1.11 dirs
    # call _install_skills
    # assert only v0.1.11 remains
    ...
```

- [ ] **Step 2: Run targeted test suite to capture failures**

Run: `pytest carta/install/tests/test_bootstrap.py carta/tests/test_cli.py -q`  
Expected: FAIL on dedupe/warning semantics before implementation.

- [ ] **Step 3: Implement hardened install behavior**

```python
# Snapshot iterator to avoid modification-during-iteration issues
stale_dirs = [entry for entry in version_parent.iterdir() 
              if entry.is_dir() and entry.name != version]

for entry in stale_dirs:
    print(f"Removing stale skill cache: {entry.name}")
    shutil.rmtree(entry)

# Verify deletion succeeded
remaining = [e for e in version_parent.iterdir() 
             if e.is_dir() and e.name != version]
if remaining:
    raise RuntimeError(
        f"ERROR: stale cache dirs still present after cleanup: {[e.name for e in remaining]}"
    )
```

- [ ] **Step 4: Implement safer hook command string and precise PATH warning text**

Run: `pytest carta/install/tests/test_bootstrap.py carta/tests/test_cli.py -q`  
Expected: PASS with stable bootstrap idempotency and clearer warnings.

- [ ] **Step 5: Commit**

```bash
git add carta/install/bootstrap.py carta/cli.py carta/install/tests/test_bootstrap.py carta/tests/test_cli.py
git commit -m "fix: harden bootstrap cache handling and init warning clarity"
```

---

## Task 4: Add Optional MCP Server (Hybrid Interface)

**Files:**

- Create: `carta/mcp/server.py`
- Modify: `pyproject.toml`
- Test: `carta/tests/test_mcp_server.py`

- [ ] **Step 1: Write failing tests for MCP tool routing to core functions**

```python
def test_mcp_search_tool_calls_run_search(monkeypatch):
    # assert tool dispatch delegates to existing run_search path
    ...

def test_mcp_embed_tool_enforces_timeout(monkeypatch):
    """Test that MCP carta_embed enforces timeout before delegating to pipeline."""
    # monkeypatch pipeline to simulate slow embed
    # call carta_embed tool with timeout config
    # assert timeout is respected and error returned gracefully
    ...
```

- [ ] **Step 2: Run tests to verify missing MCP implementation fails**

Run: `pytest carta/tests/test_mcp_server.py -q`  
Expected: FAIL because MCP module/tool handlers do not yet exist.

- [ ] **Step 3: Implement minimal stdio MCP server with stable tool surface**

In `carta/mcp/server.py`:

```python
import sys
from mcp.server.stdio import stdio_server
from mcp.server import Server
from mcp import types

TOOLS = ["carta_scan", "carta_search", "carta_embed"]

@server.call_tool()
async def handle_call_tool(name: str, arguments: dict):
    if name == "carta_scan":
        # delegate to run_scan with shared config loading
        ...
    elif name == "carta_search":
        # delegate to run_search
        ...
    elif name == "carta_embed":
        # CRITICAL: enforce timeout wrapper before calling pipeline
        timeout = arguments.get("timeout_seconds", 300)
        try:
            with timeout_context(timeout):
                # delegate to run_embed
                ...
        except TimeoutError:
            return types.TextContent(type="text", text=f"ERROR: embed exceeded {timeout}s timeout")
    else:
        raise ValueError(f"Unknown tool: {name}")
```

**Key requirement:** `carta_embed` MCP tool MUST enforce per-file timeout so hanging embeds don't block the MCP client indefinitely.

- [ ] **Step 4: Add packaging entrypoint and smoke-test MCP startup**

In `pyproject.toml`:

```toml
[project.scripts]
carta-mcp = "carta.mcp.server:main"
```

Run: `pytest carta/tests/test_mcp_server.py -q`  
Run: `carta-mcp --help` (or `python -m carta.mcp.server`)  
Expected: tests PASS and server process starts cleanly.

- [ ] **Step 5: Commit**

```bash
git add carta/mcp/server.py pyproject.toml carta/tests/test_mcp_server.py
git commit -m "feat: add optional MCP server with timeout-enforcing carta_embed tool"
```

---

## Task 5: Docs and Operational Handoff for Hybrid Model

**Files:**

- Modify: `README.md`
- Modify: `docs/install.md`
- Modify: `docs/testing/install-test-guide.md`
- Modify: `skills/carta-init/SKILL.md`
- Modify: `skills/doc-embed/SKILL.md`
- Modify: `skills/doc-search/SKILL.md`

- [ ] **Step 1: Write failing doc checks (or checklist) for missing hybrid guidance**

```text
Checklist:
- CLI-first guidance present
- MCP setup documented (including .mcp.json config snippet)
- MCP timeout behavior documented for carta_embed
- Sidecar current_path behavior documented
- Troubleshooting for Qdrant/Ollama/path warnings updated
- Chunking overlap and timeout guardrails explained
```

- [ ] **Step 2: Run doc/test checks**

Run: `pytest -q`  
Expected: Existing tests remain green; no new regressions introduced.

- [ ] **Step 3: Update docs and skill usage notes to match shipped behavior**

Example additions to `README.md`:

```markdown
## Using Carta

### CLI (Local Bulk Operations)

Use the CLI for bulk local embedding and search:

```bash
carta init
carta embed  # batched upsert, per-file timeout protection
carta search "query"
```

### MCP Server (Agent-Driven Workflows)

Add to your `.mcp.json`:

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

MCP tools:
- `carta_scan` - audit docs for drift/contradictions
- `carta_search` - semantic search over embedded docs
- `carta_embed` - embed specific files (enforces per-file timeout)

**Timeout behavior:** `carta_embed` via MCP enforces a configurable per-file timeout (default 300s) to prevent indefinite hangs. Files exceeding the timeout are skipped with a warning.
```

- [ ] **Step 4: Validate end-to-end walkthrough from clean environment**

Run:

- `carta init`
- `carta embed` (with a test PDF known to have dense paragraphs)
- `carta search "query"`
- MCP tool smoke call for `carta_search`
- MCP `carta_embed` with short timeout to verify timeout enforcement

Expected: all workflows succeed with documented commands, chunking completes in <5s/file, timeout protection works.

- [ ] **Step 5: Commit**

```bash
git add README.md docs/install.md docs/testing/install-test-guide.md skills/carta-init/SKILL.md skills/doc-embed/SKILL.md skills/doc-search/SKILL.md
git commit -m "docs: publish CLI+MCP hybrid workflow with timeout and chunking fixes"
```

---

## Future Work (Not in This Release)

**These are deferred to avoid scope creep but should be tracked:**

### FW-1: Markdown File Embedding

**Context:** `.md` files in `docs/` are scanned and audited but never embedded, so `/doc-search` can't find them.

**Proposed fix:** Extend `carta/embed/pipeline.py` to handle `.md` files:
- Extract text directly (no PDF parsing)
- Generate sidecars with `file_type: "markdown"`
- Use same chunking logic as PDF text extraction

**Tracking:** Create issue `#8` for markdown embedding support.

### FW-2: `carta status` Command

**Context:** Operators have no visibility into Qdrant/Ollama health, collection sizes, or pending files without manually querying services.

**Proposed implementation:**

```bash
$ carta status

Qdrant: ✓ running (localhost:6333)
Ollama: ✓ running (localhost:11434)
Collection: carta_docs (1,234 chunks, 85 files)
Pending embeds: 3 files
Last scan: 2026-03-24 18:42:11
```

**Tracking:** Create issue `#9` for status command.

### FW-3: MCP Server Becomes Primary Architecture

**Context:** The skills/plugin-cache approach is fragile (version resolution, cache staleness). An MCP-native architecture eliminates the plugin cache entirely.

**Long-term vision:**
- Claude Code resolves MCP tools natively via `.mcp.json`
- No skill registration or plugin cache needed
- Skills become simple usage documentation, not code artifacts
- Carta becomes a pure CLI + MCP server package

**Tracking:** Create issue `#10` for MCP-first architecture migration (post-v1.0 stability).

---

## Dependency Order and Critical Path

1. **Task 1** (embed reliability: batching, chunking, timeout) - CRITICAL - required before exposing MCP embed confidently.
2. **Task 2** (sidecar schema alignment) - prevents scanner noise and data mismatch.
3. **Task 3** (bootstrap hardening) - installation/runtime trust.
4. **Task 4** (MCP server) - architecture expansion on stable core. **CRITICAL: MCP embed tool must enforce timeout.**
5. **Task 5** (docs handoff) - ship-ready guidance and operational consistency.

Critical path: **Task 1 (all subtasks) -> Task 4 -> Task 5**.

**Blocker note:** Task 4 cannot proceed until Task 1C (per-file timeout) is complete, as MCP `carta_embed` must wrap the timeout-protected pipeline.

---

## Definition of Done

- CLI workflows remain stable and documented.
- **No high-severity embed hang/stall regressions** in tests and large-file smoke runs:
  - Qdrant upserts are batched (32 at a time)
  - Chunking overlap capped at 25% of take size
  - Chunking safety counter lowered to 2x (fails fast)
  - Per-file timeout enforced in pipeline (default 300s)
- Sidecar `current_path` is consistently written/healed and scanner false positives are reduced.
- Bootstrap no longer silently tolerates stale skill cache directories.
- Optional MCP server is available and delegates to shared core logic.
- **MCP `carta_embed` tool enforces timeout** and returns graceful errors on timeout.
- Hybrid docs are updated and reproducible by a fresh operator.
- All tests pass: `pytest -q` shows green across embed, parse, scanner, bootstrap, CLI, and MCP modules.