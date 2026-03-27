---
phase: 02-mcp-tools
verified: 2026-03-26T00:00:00Z
status: passed
score: 8/8 must-haves verified
re_verification: false
---

# Phase 02: MCP Tools Verification Report

**Phase Goal:** Implement carta_search, carta_embed, and carta_scan as FastMCP tool handlers; MCP server exposes Carta's knowledge retrieval and document management to Claude via three tools with structured error handling.
**Verified:** 2026-03-26
**Status:** passed
**Re-verification:** No — initial verification

## Goal Achievement

### Observable Truths

| # | Truth | Status | Evidence |
|---|-------|--------|----------|
| 1 | find_config() is importable from carta.config | VERIFIED | `carta/config.py:81: def find_config` |
| 2 | Sidecar .embed-meta.yaml files include file_mtime after embed | VERIFIED | `induct.py:64: "file_mtime": None` in stub; `pipeline.py:124: "file_mtime": os.path.getmtime(...)` in sidecar_updates |
| 3 | run_embed_file(path, cfg, force) embeds a single file and returns status dict | VERIFIED | `pipeline.py:159: def run_embed_file(path: Path, cfg: dict, force: bool = False, ...)` |
| 4 | Drift detection identifies files whose mtime exceeds sidecar file_mtime | VERIFIED | `scanner.py:431: def check_embed_drift(repo_root: Path, cfg: dict = None) -> list:` |
| 5 | Claude can call carta_search and receive scored results with source path and excerpt | VERIFIED | `server.py:54: def carta_search(query: str, top_k: int = 5)`; excerpt capped at `[:300]`; score rounded to 4dp |
| 6 | Claude can call carta_embed on a file path and it embeds with timeout enforcement | VERIFIED | `server.py:86: def carta_embed(path: str, force: bool = False)`; catches `concurrent.futures.TimeoutError` |
| 7 | Claude can call carta_scan and receive pending and drift arrays | VERIFIED | `server.py:126: def carta_scan()`; returns `{"pending": [...], "drift": [...]}` |
| 8 | All three tools return structured error dicts on failure, never raise exceptions | VERIFIED | All handlers catch FileNotFoundError, TimeoutError, RuntimeError, ConfigError, Exception — return typed error dicts with "error" and "detail" keys |

**Score:** 8/8 truths verified

### Required Artifacts

| Artifact | Expected | Status | Details |
|----------|----------|--------|---------|
| `carta/config.py` | find_config() moved from cli.py | VERIFIED | `def find_config` at line 81 |
| `carta/cli.py` | re-exports find_config from config | VERIFIED | `from carta.config import find_config` at line 20; no def find_config present |
| `carta/embed/pipeline.py` | run_embed_file single-file adapter with mtime skip | VERIFIED | `def run_embed_file` at line 159; mtime skip logic at line 193 |
| `carta/embed/induct.py` | file_mtime field in sidecar stub | VERIFIED | `"file_mtime": None` at line 64 |
| `carta/scanner/scanner.py` | check_embed_drift function for mtime-based drift | VERIFIED | `def check_embed_drift` at line 431 |
| `carta/mcp/server.py` | Three MCP tool handlers: carta_search, carta_embed, carta_scan | VERIFIED | 3 `@mcp_server.tool()` decorators confirmed |
| `carta/mcp/tests/test_server.py` | Unit tests for all tool handlers with mocked services | VERIFIED | 20 tests total: 6 search + 6 embed + 3 scan + 5 scaffold |

### Key Link Verification

| From | To | Via | Status | Details |
|------|----|-----|--------|---------|
| `carta/embed/pipeline.py` | `carta/embed/induct.py` | file_mtime stored in sidecar_updates | WIRED | `pipeline.py:124: "file_mtime": os.path.getmtime(str(file_path))` |
| `carta/config.py` | `carta/cli.py` | find_config moved to config, cli imports from config | WIRED | `cli.py:20: from carta.config import find_config` |
| `carta/mcp/server.py` | `carta/embed/pipeline.py` | run_search and run_embed_file delegation | WIRED | `server.py:17: from carta.embed.pipeline import run_search, run_embed_file, FILE_TIMEOUT_S` |
| `carta/mcp/server.py` | `carta/scanner/scanner.py` | check_embed_induction_needed and check_embed_drift for scan | WIRED | `server.py:18: from carta.scanner.scanner import check_embed_induction_needed, check_embed_drift` |
| `carta/mcp/server.py` | `carta/config.py` | _load_cfg helper using find_config + load_config | WIRED | `server.py:16: from carta.config import find_config, load_config, ConfigError` |

### Data-Flow Trace (Level 4)

These are MCP tool handlers — they delegate to service functions rather than rendering dynamic data to a UI. Data flow is through function call chains, verified via key links above. No hollow-prop or disconnected-source patterns applicable.

### Behavioral Spot-Checks

| Behavior | Command | Result | Status |
|----------|---------|--------|--------|
| Tool handlers importable | `python3 -c "from carta.mcp.server import carta_search, carta_embed, carta_scan; print('imports OK')"` | `imports OK` | PASS |
| Full test suite passes | `python3 -m pytest carta/ -x -q` | `144 passed, 5 warnings` | PASS |
| MCP server test suite passes | `python3 -m pytest carta/mcp/tests/test_server.py -x -q` | `31 passed` (includes service layer tests) | PASS |
| Three tools registered | `grep -c "@mcp_server.tool" carta/mcp/server.py` | `3` | PASS |
| carta-mcp entrypoint in pyproject.toml | `grep "carta-mcp" pyproject.toml` | `carta-mcp = "carta.mcp.server:main"` | PASS |

### Requirements Coverage

| Requirement | Source Plan | Description | Status | Evidence |
|-------------|------------|-------------|--------|----------|
| MCP-02 | 02-01, 02-02 | carta_search MCP tool queries Qdrant and returns scored, source-attributed results | SATISFIED | `carta_search` in server.py returns list with score, source, excerpt; tested with 6 unit tests |
| MCP-03 | 02-01, 02-02 | carta_embed MCP tool embeds a single specified file with per-file timeout enforcement | SATISFIED | `carta_embed` delegates to `run_embed_file`; catches `concurrent.futures.TimeoutError`; tested with 6 unit tests |
| MCP-04 | 02-01, 02-02 | carta_scan MCP tool returns structured scan results listing pending-embed and drift files | SATISFIED | `carta_scan` calls `check_embed_induction_needed` + `check_embed_drift`; returns `{"pending":[], "drift":[]}`; tested with 3 unit tests |
| MCP-05 | 02-02 | carta-mcp packaged as separate entrypoint in pyproject.toml | SATISFIED | `carta-mcp = "carta.mcp.server:main"` at line 23 of pyproject.toml |

Note: MCP-01 (server scaffold) was Phase 1's responsibility and is not re-verified here. MCP-06 and MCP-07 are not claimed by Phase 2 plans.

### Anti-Patterns Found

| File | Pattern | Severity | Impact |
|------|---------|----------|--------|
| None | — | — | — |

No TODOs, FIXMEs, placeholder returns, or stub patterns found in server.py. Wire-protocol discipline confirmed: lines 4 and 6 of server.py are comments documenting the constraint, not violations. No `print()` or `sys.exit()` calls in handler code.

### Human Verification Required

#### 1. Live MCP Registration

**Test:** Register `carta-mcp` in `.mcp.json`, open Claude Code in the project, and verify the three tools appear in the tool list.
**Expected:** carta_search, carta_embed, carta_scan visible and invokable.
**Why human:** Requires Claude Code running with an active MCP session; cannot verify programmatically.

#### 2. End-to-End carta_search with Qdrant Running

**Test:** With Qdrant and Ollama running and a collection populated, call `carta_search("some query")` via MCP and verify scored results are returned.
**Expected:** Non-empty list of `{score, source, excerpt}` dicts with real document excerpts.
**Why human:** Requires live Qdrant and Ollama services; tests mock these dependencies.

### Gaps Summary

No gaps. All 8 must-have truths verified. All artifacts exist, are substantive, and are wired. All four requirement IDs (MCP-02, MCP-03, MCP-04, MCP-05) are satisfied. The test suite passes at 144 tests with 0 failures (python3, where the `mcp` package is installed).

Note: the `python` (non-3) interpreter lacks the `mcp` package, causing import failure if tests are invoked as `python -m pytest`. This is an environment configuration detail, not a code defect — `python3` is the correct interpreter for this project.

---

_Verified: 2026-03-26_
_Verifier: Claude (gsd-verifier)_
