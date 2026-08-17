## Project

**Carta**

Carta is a semantic memory sidecar for Claude Code that gives agents automatic access to project documentation and session memory. It chains Qdrant vector storage, Ollama embeddings, and a smart context injection hook so relevant knowledge surfaces when Claude is working — without manual recall. v0.2 migrates from a fragile plugin cache architecture to a three-tier design: MCP server for Claude-initiated operations, a smart hook with Ollama-judge filtering for automatic injection, and a CLI for human-initiated setup and batch work.

**Core Value:** Relevant project knowledge surfaces automatically when Claude is working — without manual recall and without context noise.

### Constraints

- **Tech stack:** Python 3.10+, Qdrant client, Ollama HTTP API, MCP stdio server — no new infra
- **Compatibility:** Embed pipeline fixes must not regress existing sidecar state or Qdrant collections
- **Sequencing:** MCP server wraps the same embed pipeline — reliability fixes (batch upsert, timeout) are prerequisites before exposing `carta_embed` via MCP
- **Local only:** All judging runs on local Ollama. The **proactive-recall hook** blocks prompt submission, so its judge stays a small ≤2B model (default `qwen3.5:0.8b`) to keep latency acceptable. The **stale-scan / claude-md supersession judge** runs pre-push / on-demand (not on prompt submission), so it deliberately uses a larger, higher-precision model (default `qwen3.5:9b`) — #84 validated it rejects all known real-world false positives; its per-host latency is a separate tuning concern (#86).

## Technology Stack

## Languages
- Python 3.10+ - Core application, CLI, scanning, embedding pipeline
- Bash - Installation script, shell integration hooks
- YAML - Configuration files, frontmatter in markdown docs
## Runtime
- Python 3.10 or later (specified in `pyproject.toml`)
- pip / setuptools (modern Python packaging)
- Lockfile: Not detected (uses direct `pyproject.toml` dependencies)
## Frameworks
- argparse (stdlib) - CLI argument parsing in `carta/cli.py`
- pathlib (stdlib) - File system operations
- pytest 7.0+ - Test runner and framework (dev dependency)
- unittest.mock (stdlib) - Mocking in tests
- setuptools 61.0+ - Package building and installation
- shutil, subprocess (stdlib) - File operations and process management
## Key Dependencies
- qdrant-client 1.7+ - Vector database client for Qdrant integration (`carta/embed/embed.py`, `carta/embed/pipeline.py`)
- PyMUPDF 1.23+ (pymupdf) - PDF text extraction
- requests 2.31+ - HTTP client library
- PyYAML 6.0+ - YAML parsing for config and frontmatter
## Configuration
- `CARTA_QDRANT_URL` / `CARTA_OLLAMA_URL` - **Seed values, not runtime overrides.** Read only by `carta/install/bootstrap.py` (at `carta init`, to write the URL *into* the config) and `carta/install/preflight.py` (so a remote backend isn't reported as down). `load_config` never consults them, so setting one at runtime does **not** redirect `carta search`, the MCP tools, or the hook — those follow `qdrant_url` / `embed.ollama_url` in `.carta/config.yaml`. To point an existing project elsewhere, edit the config.
- `PYTHONPATH` - Set during tests in `carta/tests/test_cli.py`
- `pyproject.toml` - Single source of truth for package metadata and dependencies
## Platform Requirements
- Python 3.10+ (command: `python3 --version`)
- Git (for project detection in bootstrap)
- Docker (strongly recommended for running Qdrant and Ollama containers)
- **Qdrant** - Vector database service (Docker: `docker run -p 6333:6333 qdrant/qdrant`)
- **Ollama** - Local LLM embedding service (https://ollama.ai)
- Docker environment or system with Python 3.10+
- Network access to Qdrant API (default port 6333)
- Network access to Ollama API (default port 11434)
- No cloud services required - all runs locally
## Special Considerations
- Package installed via `pipx`, `pip`, or `uvx` exposes `carta` command
- Symlink checks in `carta/cli.py:_platformio_carta_paths_on_path()` to detect PlatformIO conflicts
- Runtime copied to `.carta/carta/` during `carta init` for self-contained execution
- All embeddings use 768-dimensional vectors (nomic-embed-text standard)
- Defined in `carta/embed/embed.py:17` and `carta/install/bootstrap.py:12`

## Conventions

## Naming Patterns
- Module files use snake_case: `cli.py`, `embed.py`, `parse.py`, `scanner.py`, `bootstrap.py`
- Test files follow pytest convention: `test_*.py` (e.g., `test_cli.py`, `test_config.py`)
- Package initialization: `__init__.py` (often empty or minimal)
- Private/internal functions use leading underscore: `_embed_lock_read_pid()`, `_check_qdrant()`, `_deep_merge()`, `_estimate_tokens()`
- Public functions use verb-based snake_case: `find_config()`, `parse_frontmatter()`, `chunk_text()`, `is_excluded()`, `run_embed()`, `run_scan()`
- Helper functions follow pattern: `is_*()`, `get_*()`, `check_*()`, `run_*()`
- Local variables and parameters use snake_case: `repo_root`, `cfg_path`, `chunk_index`, `max_tokens`
- Class variables and constants use UPPERCASE: `REQUIRED_FIELDS`, `DEFAULTS`, `VECTOR_DIM`, `DEFAULT_HOMELESS_ROOT_WHITELIST`, `CARTA_RUNTIME_SRC`
- Collection names use pattern: `{project_name}_{type_}` (e.g., `test-project_doc`)
- Type hints used throughout for function parameters and returns: `def load_config(path: Path) -> dict:`
- Optional types: `Optional[dict]`, `Optional[str]`
- Generic collections: `list[dict]`, `list[str]`, `dict[str, int]`
## Code Style
- Python 3.10+ syntax (modern type hints, walrus operator acceptable)
- Line length: not strictly enforced but tends toward ~100 chars
- Spaces: 4-space indentation (PEP 8 standard)
- Imports: organized in sections (stdlib, third-party, local)
- No explicit linter configured (no .pylintrc, ruff.toml, or black config)
- Code follows PEP 8 conventions organically
- Type hints are used but not strictly validated with mypy
## Import Organization
- No aliases configured; absolute imports from package root (`from carta.config import ...`)
- Relative imports avoided in favor of explicit package paths
## Error Handling
- Custom exception class for config errors: `ConfigError` in `carta/config.py`
- Try-except blocks catch specific exceptions before generic ones
- File operations use `missing_ok=True` on `Path.unlink()` for idempotency
- OSError caught broadly for file system operations that may fail intermittently
- `sys.exit(0)` for success
- `sys.exit(1)` for generic errors
- `sys.exit(128 + signum)` for signal handlers
- Error messages printed to `sys.stderr` before exit
## Logging
- Status messages to stdout: `print(f"Initialising Carta for project: {project_name}")`
- Errors to stderr: `print(f"Error: {e}", file=sys.stderr)`
- Flush often for long-running operations: `print("...", flush=True)`
- Progress/summary output: `print(f"Embedded: {summary['embedded']}, Skipped: {summary['skipped']}")`
- No logging module (logging.getLogger) used
- No log levels (DEBUG, INFO, WARNING)
- Messages are human-readable and immediate
## Comments
- Docstrings on public functions explain purpose, arguments, return value
- Inline comments explain non-obvious logic (e.g., FT-5 comment in `cli.py` line 113)
- Heuristic explanations: "Token estimate. Uses max of word-count and char-count..." in `parse.py`
- Complex sections marked with comment blocks: `# ---------------------------------------------------------------------------`
- Args/Returns documented in natural language
- Type hints in function signature preferred over docstring type annotations
## Function Design
- Prefer explicit parameters over **kwargs
- Use Path objects for file paths, not strings
- Config dict (cfg) passed as parameter rather than globals
- Optional parameters have defaults and type hints
- Functions return early to reduce nesting (e.g., line 82-90 in `parse.py`)
- Multiple return types acceptable: `None` on failure, dict/list on success
- Tuples or dicts used to return multiple values
## Module Design
- No __all__ defined; all public functions are importable
- Modules are cohesive: `embed.py` handles embedding/upserting, `parse.py` handles PDF extraction, `scanner.py` handles doc structure checking
- Package `__init__.py` files are minimal (often empty except version in `carta/__init__.py`)
- No re-exports of submodule contents

## Development Workflow (Superpowers)

This project uses the **Superpowers** skill flow. Invoke skills before acting (see `using-superpowers`), and follow this progression for any non-trivial change:

- **`brainstorming`** — turn an idea/issue into an approved design before touching code. Mandatory before creative work.
- **`writing-plans`** — convert the approved spec into a phased implementation plan. Specs live in `docs/superpowers/specs/`, plans in `docs/superpowers/plans/`.
- **`test-driven-development`** — write the failing test before the implementation, for every feature and bugfix.
- **`executing-plans` / `subagent-driven-development`** — execute the plan with review checkpoints.
- **`systematic-debugging`** — for any bug, test failure, or unexpected behaviour, before proposing a fix.
- **`verification-before-completion`** — run the verification commands and confirm output before claiming anything is done.
- **`finishing-a-development-branch`** — decide merge/PR/cleanup once work is complete.

Retrieval-quality changes are validated against the ET-embed eval corpus — see the eval workflow in auto-memory (`project_et-embed-eval-workflow`).

## Carta surface — authoritative reference

> Hand-maintained and authoritative for the current Carta surface.

### CLI — `carta <command>` (or `python -m carta <command>`)

| Command | Purpose |
|---------|---------|
| `init` | Bootstrap Carta in a repo (config, collections, skills, hook) |
| `scan` | Structural doc scan → `.carta/scan-results.json` (no LLM) |
| `embed` | Extract/chunk/embed pending docs → Qdrant. `--visual` drains image-heavy pages (two-pass); `--repair` re-embeds damaged points. `.xlsx`/`.csv` sources embed text-bearing cells only (frame names + notes; numerics stay out), mirrored to `.carta/companions/` |
| `search` | Hybrid (BM25 + dense, RRF) semantic search. `--trace <substring>` reports per-stage ranks and scores (bm25 rank / dense rank + raw cosine / intra-RRF / fused / final) for matching paths — which stage lost a result |
| `focus` | Deep retrieval scoped to **one file**: page-anchored passages, an outline (omit query), and table/figure pages as images. Two-step partner to `search` (locate → go deep) |
| `audit` | Embed-pipeline **data integrity** check → JSON |
| `doctor` | Diagnose environment (Qdrant/Ollama/models); `--fix` auto-installs |
| `eval` | Score retrieval quality against an eval set |
| `remember` | Capture a curated note (quirk / bug-note / helpful-note); `--about <file>` associates it with a source file |
| `status` | System-wide status across registered projects (`~/.carta/registry.json`) |
| `statusline` | Status-line widget output (embed progress) |
| `hook` | Install/run the stale-reference git hook (`hook install`, `hook check`; pre-push default) |
| `claude-md` | Reconcile CLAUDE.md against the docs graph: `check` reports superseded sections (JSON), `record` stamps the sync sidecar. Pairs with the `/claude-md-sync` skill |
| `export` / `import` | Share / restore embeddings + sidecars |
| `update` | Self-update the installed package |

### MCP — `carta-mcp` (stdio, `carta/mcp/server.py`)

Claude-initiated tools: `carta_search`, `carta_focus`, `carta_embed`, `carta_scan`, `carta_remember`.

### Hook — `carta-hook` (+ `carta/hooks/*.sh`)

Pre-prompt-submit proactive recall with a three-zone gate on the **top hit**.
On the hybrid path it reads two different signals — **lane rank for agreement,
the dense lane's raw cosine for relevance** — and never thresholds the fused
RRF score, whose scale depends on `rrf_k` and lane count:

1. `dense_score` present and **< `low_threshold`** → **silent** (the only route
   to silent: measured irrelevance). A top hit with no `dense_score` — e.g.
   BM25-only — is never silenced.
2. else top hit within **`agree_rank`** (default 3, 0-indexed) in **both** lanes
   → **inject**, no Ollama call.
3. else → small **Ollama judge**; inject on yes, silent on no or timeout.

Non-hybrid (plain cosine, no lane ranks) collections fall back to the legacy
`low_threshold` / `high_threshold` gate, which is still calibrated for them.
All paths exit 0 (fail-open). Logic in `carta/hook/hook.py`.

Each invocation appends one JSONL calibration record to
`~/.carta/traces/<project_name>/hook-YYYY-MM.jsonl` — machine-level state
outside every repo, because the record holds the derived query (the prompt
verbatim for prompts ≤500 chars). Disable with `proactive_recall.trace: false`.

A second, opt-in hook — `carta hook` — installs a managed git `pre-push` (or
`pre-commit`) shim that scans changed docs and warns when a section has been
superseded by an authoritative doc in the graph. Warn-only by default; fail-open.
Core in `carta/hook/stale_scan.py`; shim install/removal in `carta/hook/git_hook.py`;
shared yes/no judge in `carta/hook/judge.py`. Run on demand as a whole-branch pre-PR
audit with `carta hook check --diff [range]` (default range `<default-branch>...HEAD`;
`--fail-on-stale` to exit non-zero).

After a doc change, `carta hook check` also nudges toward `/claude-md-sync` when CLAUDE.md may
have drifted from the docs. The sync itself runs via the `/claude-md-sync` skill (detect →
agent drafts → human approves → `carta claude-md record`); metadata lives out-of-band in
`.carta/sidecars/CLAUDE.md.sync.yaml`.

### Sidecars

Embedding state lives at `.carta/sidecars/<source-path-relative-to-repo>.embed-meta.yaml`
— mirrors the source tree, **not** colocated beside the file (`carta/embed/induct.py::sidecar_path`).

Spreadsheet sources (`.csv`/`.xlsx`) use extension-preserving sidecar names
(`data.csv.embed-meta.yaml`) so same-stem files never collide; `.md`/`.pdf`
keep the legacy extension-stripped names.

### Module map (`carta/`)

`embed/` extract→chunk→embed→upsert + sidecar lifecycle · `search/` hybrid
retrieval/RRF/rerank/related-graph · `scanner/` structural scan · `audit/`
embed-integrity scan · `eval/` retrieval eval · `vision/` PDF routing/OCR/ColPali ·
`mcp/` MCP server · `hook/` recall judge (`hooks/` = shell entry) · `memory/` note
capture · `install/` bootstrap/preflight/auto-fix · `update/` self-update · `ui/`
status rendering.

### Which command? (full table in README / AGENTS.md)

`carta scan` / `/doc-audit` = doc structure · `carta audit` / `carta doctor` =
embed-data integrity & environment · `carta eval` = retrieval quality.

<!-- Carta is active. Collections: doc-audit-cc_doc, doc-audit-cc_session, doc-audit-cc_notes -->
