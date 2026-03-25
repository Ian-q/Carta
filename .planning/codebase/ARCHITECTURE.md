# Architecture

**Analysis Date:** 2026-03-25

## Pattern Overview

**Overall:** Modular CLI application with pluggable subsystems for documentation scanning, embedding, and search.

**Key Characteristics:**
- Command-driven entry point with subcommands for distinct workflows
- Layered architecture: CLI → Pipeline → Core subsystems → External services
- Configuration-driven initialization and multi-module feature gating
- Vector database integration (Qdrant) for semantic search over embedded documents
- File-based state management via sidecar metadata files

## Layers

**Presentation (CLI):**
- Purpose: Command-line interface and user-facing operations
- Location: `carta/cli.py`
- Contains: Argument parsing, command dispatch, process locking for concurrency control
- Depends on: Config loading, pipeline orchestration modules
- Used by: Direct user invocation via `carta` command

**Configuration:**
- Purpose: Load, validate, and provide access to project settings
- Location: `carta/config.py`
- Contains: Configuration schema with defaults, YAML parsing, field validation
- Depends on: PyYAML for parsing
- Used by: All subsystems for accessing settings

**Orchestration/Pipeline:**
- Purpose: Coordinate multi-step workflows and resource management
- Location: `carta/embed/pipeline.py`, `carta/scanner/scanner.py`
- Contains: High-level workflow execution, error aggregation, state transitions
- Depends on: Core subsystems (embed, parse, search), configuration
- Used by: CLI commands

**Core Processing Subsystems:**

1. **Scanner** (`carta/scanner/`):
   - Purpose: Analyze documentation structure and identify quality issues
   - Location: `carta/scanner/scanner.py`
   - Contains: Structural checks (homeless docs), frontmatter parsing, file discovery
   - Depends on: Configuration for exclusion rules and validation patterns
   - Used by: `cmd_scan` to audit documentation

2. **Embed** (`carta/embed/`):
   - Purpose: Extract text from documents, chunk for embedding, and persist vectors
   - Location: `carta/embed/embed.py`, `carta/embed/parse.py`, `carta/embed/induct.py`
   - Contains: PDF extraction, token estimation, chunking logic, Ollama API integration
   - Depends on: PyMuPDF for PDF parsing, requests for Ollama API, Qdrant client
   - Used by: `cmd_embed` to index documents

3. **Search** (`carta/embed/pipeline.py::run_search`):
   - Purpose: Query embedded documents using semantic similarity
   - Location: `carta/embed/pipeline.py`
   - Contains: Query embedding, Qdrant filtering, result ranking
   - Depends on: Ollama embeddings, Qdrant collections
   - Used by: `cmd_search` for knowledge retrieval

**Installation/Bootstrap:**
- Purpose: Initialize projects with carta configuration and infrastructure
- Location: `carta/install/bootstrap.py`
- Contains: Environment detection, Git integration, runtime installation
- Depends on: External service checks (Qdrant, Ollama), Git subprocess calls
- Used by: `cmd_init` during project setup

**External Integration Layer:**
- Qdrant vector database: Persistent storage of document embeddings and metadata
- Ollama: Local LLM inference for generating embeddings (nomic-embed-text model)
- Git: Project detection, hook registration

## Data Flow

**Scan Workflow:**

1. User invokes: `carta scan`
2. CLI finds `.carta/config.yaml` by traversing up directory tree
3. Config loaded and validated (project_name, qdrant_url required)
4. Scanner walks file tree, skips excluded paths via fnmatch patterns
5. For each markdown file: parses YAML frontmatter, checks for structural issues
6. Issues aggregated and written to `.carta/scan-results.json`
7. Summary printed to stdout

**Embed Workflow:**

1. User invokes: `carta embed`
2. CLI acquires atomic lock via `embed.lock` file to prevent concurrent embedding
3. Qdrant connectivity verified (short 5s timeout)
4. Scanner discovers all `.embed-meta.yaml` sidecars with `status: pending`
5. For each pending file:
   - Extract text from PDF (PyMuPDF)
   - Estimate tokens using char/word heuristic (3 chars/token for technical content)
   - Split into chunks respecting max_tokens (default 800) and overlap (default 15%)
   - Get embedding vector from Ollama for each chunk
   - Upsert to Qdrant with deterministic UUID from slug+chunk_index
   - Update sidecar status to `indexed_at` timestamp, chunk_count
6. Summary (embedded/skipped counts) printed; lock removed on exit (atexit + signal handlers)

**Search Workflow:**

1. User invokes: `carta search "query terms"`
2. Config loaded, doc_search module verified enabled
3. Query joined from arguments, embedded via Ollama
4. Qdrant search hits with top_n=5 (configurable)
5. Results printed as `[score] source — excerpt`

**State Management:**
- YAML configuration: `.carta/config.yaml` — loaded once per command
- Sidecar metadata: `*.embed-meta.yaml` — tracks embedding status per document
- Lock file: `.carta/embed.lock` — PID-based concurrency control
- Scan results: `.carta/scan-results.json` — output of audit
- Qdrant collections: Named per project (e.g., `myproject_doc`, `myproject_session`, `myproject_quirk`)

## Key Abstractions

**Configuration Object:**
- Purpose: Single source of truth for all settings
- Examples: `carta/config.py::load_config()`
- Pattern: Deep merge of defaults + user YAML; dictionary-based access with get() fallback

**Sidecar Metadata:**
- Purpose: Track embedding lifecycle without modifying source documents
- Examples: `carta/embed/induct.py::write_sidecar()`
- Pattern: Colocated `.embed-meta.yaml` file with same stem as document; YAML serialization

**Chunk Dictionary:**
- Purpose: Atomic unit for embedding and storage
- Structure: `{"slug", "text", "chunk_index", "doc_type", ...metadata}`
- Used by: `upsert_chunks()` to generate vectors and store in Qdrant

**Collection Name Convention:**
- Purpose: Namespace vectors by project and document type
- Pattern: `{project_name}_{type}` (e.g., `myproject_doc`, `myproject_session`, `myproject_quirk`)
- Location: `carta/config.py::collection_name()`

## Entry Points

**carta command (CLI):**
- Location: `carta/__main__.py` → `carta/cli.py::main()`
- Triggers: User executes `carta {init|scan|embed|search|--version}`
- Responsibilities: Parse arguments, dispatch to command handler, catch exceptions, manage exit codes

**cmd_init (Bootstrap):**
- Location: `carta/cli.py::cmd_init()`
- Triggers: `carta init` in any project subdirectory
- Responsibilities: Check PATH conflicts, run bootstrap (config creation, Qdrant collection setup)

**cmd_scan (Documentation Audit):**
- Location: `carta/cli.py::cmd_scan()`
- Triggers: `carta scan`
- Responsibilities: Load config, run structural scanner, output issues JSON

**cmd_embed (Document Indexing):**
- Location: `carta/cli.py::cmd_embed()`
- Triggers: `carta embed`
- Responsibilities: Acquire lock, discover pending files, extract/chunk/embed, upsert to Qdrant

**cmd_search (Semantic Query):**
- Location: `carta/cli.py::cmd_search()`
- Triggers: `carta search "query"`
- Responsibilities: Embed query, search Qdrant, format and display results

## Error Handling

**Strategy:** Fail fast with clear error messages; accumulate non-fatal errors for summary reporting.

**Patterns:**

1. **Configuration errors** (`ConfigError`):
   - Raised in `config.py::load_config()` for missing/invalid YAML
   - Caught in CLI; printed to stderr with exit code 1

2. **Service connectivity errors**:
   - Qdrant: Pre-flight check in `cmd_embed()` with 5s timeout; prints Docker start instructions
   - Ollama: Checked during bootstrap; prints warning but allows continuation
   - Failures propagate as RuntimeError with context (URL, detail)

3. **File I/O errors**:
   - Sidecar read/write: Wrapped in optional try-catch; return None on failure
   - PDF extraction: Exceptions bubble up; caught at pipeline level for error accumulation

4. **Concurrency errors**:
   - Embed lock collision: Detects stale PIDs via `os.kill(pid, 0)`; removes and retries
   - Prints helpful message directing user to remove `.carta/embed.lock` if truly stale

5. **Embedded workflow accumulation**:
   - Pipeline collects errors in `summary["errors"]` list
   - Returns non-zero exit code if errors present
   - Allows partial success (some files embedded despite failures)

## Cross-Cutting Concerns

**Logging:** Printf-style via Python `print()` to stdout/stderr; no logging framework. Key operations annotated with print statements for user visibility.

**Validation:**
- Configuration: Schema validation in `load_config()` with type checking
- Files: Exclusion patterns via `fnmatch`; YAML frontmatter optional but parsed safely
- Inputs: CLI arguments parsed by argparse; query terms joined from positional args

**Authentication:** Not applicable — local CLI tool. Qdrant and Ollama assumed accessible on localhost.

**Process Management:**
- Signal handlers in `cmd_embed()` to clean up lock file on SIGTERM/SIGINT
- atexit handler for guaranteed lock removal
- PID-based lock detection for cleanup of abandoned processes

**Path Resolution:**
- Config lookup: Walk parent directories until `.carta/config.yaml` found
- File discovery: Use `rglob()` with exclusion filtering
- Relative path computation: Store relative-to-repo for sidecar metadata and scan results

---

*Architecture analysis: 2026-03-25*
