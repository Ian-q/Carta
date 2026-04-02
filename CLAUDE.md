<!-- GSD:project-start source:PROJECT.md -->
## Project

**Carta**

Carta is a semantic memory sidecar for Claude Code that gives agents automatic access to project documentation and session memory. It chains Qdrant vector storage, Ollama embeddings, and a smart context injection hook so relevant knowledge surfaces when Claude is working — without manual recall. v0.2 migrates from a fragile plugin cache architecture to a three-tier design: MCP server for Claude-initiated operations, a smart hook with Ollama-judge filtering for automatic injection, and a CLI for human-initiated setup and batch work.

**Core Value:** Relevant project knowledge surfaces automatically when Claude is working — without manual recall and without context noise.

### Constraints

- **Tech stack:** Python 3.10+, Qdrant client, Ollama HTTP API, MCP stdio server — no new infra
- **Compatibility:** Embed pipeline fixes must not regress existing sidecar state or Qdrant collections
- **Sequencing:** MCP server wraps the same embed pipeline — reliability fixes (batch upsert, timeout) are prerequisites before exposing `carta_embed` via MCP
- **Local only:** Ollama judge must be a small model (≤2B params) to keep hook latency acceptable; hook blocks prompt submission
<!-- GSD:project-end -->

<!-- GSD:stack-start source:codebase/STACK.md -->
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
- `CARTA_QDRANT_URL` - Override Qdrant URL (default: `http://localhost:6333`)
- `CARTA_OLLAMA_URL` - Override Ollama URL (default: `http://localhost:11434`)
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
<!-- GSD:stack-end -->

<!-- GSD:conventions-start source:CONVENTIONS.md -->
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
<!-- GSD:conventions-end -->

<!-- GSD:architecture-start source:ARCHITECTURE.md -->
## Architecture

## Pattern Overview
- Command-driven entry point with subcommands for distinct workflows
- Layered architecture: CLI → Pipeline → Core subsystems → External services
- Configuration-driven initialization and multi-module feature gating
- Vector database integration (Qdrant) for semantic search over embedded documents
- File-based state management via sidecar metadata files
## Layers
- Purpose: Command-line interface and user-facing operations
- Location: `carta/cli.py`
- Contains: Argument parsing, command dispatch, process locking for concurrency control
- Depends on: Config loading, pipeline orchestration modules
- Used by: Direct user invocation via `carta` command
- Purpose: Load, validate, and provide access to project settings
- Location: `carta/config.py`
- Contains: Configuration schema with defaults, YAML parsing, field validation
- Depends on: PyYAML for parsing
- Used by: All subsystems for accessing settings
- Purpose: Coordinate multi-step workflows and resource management
- Location: `carta/embed/pipeline.py`, `carta/scanner/scanner.py`
- Contains: High-level workflow execution, error aggregation, state transitions
- Depends on: Core subsystems (embed, parse, search), configuration
- Used by: CLI commands
- Purpose: Initialize projects with carta configuration and infrastructure
- Location: `carta/install/bootstrap.py`
- Contains: Environment detection, Git integration, runtime installation
- Depends on: External service checks (Qdrant, Ollama), Git subprocess calls
- Used by: `cmd_init` during project setup
- Qdrant vector database: Persistent storage of document embeddings and metadata
- Ollama: Local LLM inference for generating embeddings (nomic-embed-text model)
- Git: Project detection, hook registration
## Data Flow
- YAML configuration: `.carta/config.yaml` — loaded once per command
- Sidecar metadata: `*.embed-meta.yaml` — tracks embedding status per document
- Lock file: `.carta/embed.lock` — PID-based concurrency control
- Scan results: `.carta/scan-results.json` — output of audit
- Qdrant collections: Named per project (e.g., `myproject_doc`, `myproject_session`, `myproject_quirk`)
## Key Abstractions
- Purpose: Single source of truth for all settings
- Examples: `carta/config.py::load_config()`
- Pattern: Deep merge of defaults + user YAML; dictionary-based access with get() fallback
- Purpose: Track embedding lifecycle without modifying source documents
- Examples: `carta/embed/induct.py::write_sidecar()`
- Pattern: Colocated `.embed-meta.yaml` file with same stem as document; YAML serialization
- Purpose: Atomic unit for embedding and storage
- Structure: `{"slug", "text", "chunk_index", "doc_type", ...metadata}`
- Used by: `upsert_chunks()` to generate vectors and store in Qdrant
- Purpose: Namespace vectors by project and document type
- Pattern: `{project_name}_{type}` (e.g., `myproject_doc`, `myproject_session`, `myproject_quirk`)
- Location: `carta/config.py::collection_name()`
## Entry Points
- Location: `carta/__main__.py` → `carta/cli.py::main()`
- Triggers: User executes `carta {init|scan|embed|search|--version}`
- Responsibilities: Parse arguments, dispatch to command handler, catch exceptions, manage exit codes
- Location: `carta/cli.py::cmd_init()`
- Triggers: `carta init` in any project subdirectory
- Responsibilities: Check PATH conflicts, run bootstrap (config creation, Qdrant collection setup)
- Location: `carta/cli.py::cmd_scan()`
- Triggers: `carta scan`
- Responsibilities: Load config, run structural scanner, output issues JSON
- Location: `carta/cli.py::cmd_embed()`
- Triggers: `carta embed`
- Responsibilities: Acquire lock, discover pending files, extract/chunk/embed, upsert to Qdrant
- Location: `carta/cli.py::cmd_search()`
- Triggers: `carta search "query"`
- Responsibilities: Embed query, search Qdrant, format and display results
## Error Handling
## Cross-Cutting Concerns
- Configuration: Schema validation in `load_config()` with type checking
- Files: Exclusion patterns via `fnmatch`; YAML frontmatter optional but parsed safely
- Inputs: CLI arguments parsed by argparse; query terms joined from positional args
- Signal handlers in `cmd_embed()` to clean up lock file on SIGTERM/SIGINT
- atexit handler for guaranteed lock removal
- PID-based lock detection for cleanup of abandoned processes
- Config lookup: Walk parent directories until `.carta/config.yaml` found
- File discovery: Use `rglob()` with exclusion filtering
- Relative path computation: Store relative-to-repo for sidecar metadata and scan results
<!-- GSD:architecture-end -->

<!-- GSD:workflow-start source:GSD defaults -->
## GSD Workflow Enforcement

Before using Edit, Write, or other file-changing tools, start work through a GSD command so planning artifacts and execution context stay in sync.

Use these entry points:
- `/gsd:quick` for small fixes, doc updates, and ad-hoc tasks
- `/gsd:debug` for investigation and bug fixing
- `/gsd:execute-phase` for planned phase work

Do not make direct repo edits outside a GSD workflow unless the user explicitly asks to bypass it.
<!-- GSD:workflow-end -->



<!-- GSD:profile-start -->
## Developer Profile

> Profile not yet configured. Run `/gsd:profile-user` to generate your developer profile.
> This section is managed by `generate-claude-profile` -- do not edit manually.
<!-- GSD:profile-end -->

<!-- Carta is active. Collections: doc-audit-cc_doc, doc-audit-cc_session, doc-audit-cc_quirk -->
