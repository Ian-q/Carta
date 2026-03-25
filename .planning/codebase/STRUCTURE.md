# Codebase Structure

**Analysis Date:** 2026-03-25

## Directory Layout

```
doc-audit-cc/
├── carta/                      # Main Python package
│   ├── __init__.py             # Version definition
│   ├── __main__.py             # Entry point (python -m carta)
│   ├── cli.py                  # Command-line interface and dispatching
│   ├── config.py               # Configuration loading and validation
│   ├── embed/                  # Document embedding subsystem
│   │   ├── __init__.py
│   │   ├── embed.py            # Ollama API and Qdrant integration
│   │   ├── parse.py            # PDF extraction and token estimation
│   │   ├── pipeline.py         # Orchestration of embed/search workflows
│   │   ├── induct.py           # Sidecar stub generation and slug creation
│   │   └── tests/              # Unit tests for embed subsystem
│   ├── scanner/                # Documentation structure scanner
│   │   ├── __init__.py
│   │   ├── scanner.py          # Issue detection (homeless docs, frontmatter)
│   │   └── tests/              # Unit tests for scanner
│   ├── install/                # Bootstrap and initialization
│   │   ├── __init__.py
│   │   ├── bootstrap.py        # Project initialization (config, Qdrant setup)
│   │   └── tests/              # Unit tests for bootstrap
│   ├── hooks/                  # Git hooks for automation
│   │   ├── carta-prompt-hook.sh
│   │   └── carta-stop-hook.sh
│   ├── tests/                  # Package-level tests
│   │   ├── test_cli.py
│   │   ├── test_config.py
│   │   ├── test_version.py
│   │   └── conftest.py         # Pytest fixtures and configuration
│   └── conftest.py             # Shared pytest configuration
├── skills/                     # Claude skills (user-facing commands)
│   ├── doc-audit/
│   ├── doc-embed/
│   ├── doc-search/
│   └── carta-init/
├── .planning/                  # GSD analysis and planning docs
│   └── codebase/               # Codebase documentation (ARCHITECTURE.md, etc.)
├── .claude-plugin              # Claude plugin metadata
├── docs/                       # Project documentation
│   ├── testing/                # Test documentation and archives
│   └── superpowers/            # Feature guides
├── .github/                    # GitHub CI/CD workflows
├── pyproject.toml              # Python package metadata
└── [config files]              # pytest.ini, setup.cfg, etc.
```

## Directory Purposes

**carta/ (Main Package):**
- Purpose: Implementation of carta CLI tool and all subsystems
- Contains: Python modules for embedding, scanning, configuration, installation
- Key files: `cli.py` (entry point), `config.py` (configuration), subsystem modules

**carta/embed/:**
- Purpose: Document embedding and semantic search functionality
- Contains: PDF parsing, chunking, Ollama integration, Qdrant persistence
- Key files: `embed.py` (vector operations), `pipeline.py` (workflow orchestration), `parse.py` (text extraction)

**carta/scanner/:**
- Purpose: Documentation structure analysis and quality auditing
- Contains: Structural checks, frontmatter parsing, file discovery
- Key files: `scanner.py` (main logic)

**carta/install/:**
- Purpose: Project initialization and bootstrap
- Contains: Configuration generation, Qdrant collection creation, Git hook registration
- Key files: `bootstrap.py` (main logic)

**carta/hooks/:**
- Purpose: Git hooks for automated carte operations
- Contains: Shell scripts for prompt management and session cleanup
- Key files: `carta-prompt-hook.sh`, `carta-stop-hook.sh`

**carta/tests/:**
- Purpose: Unit and integration tests for the package
- Contains: Test modules for each subsystem
- Key files: `conftest.py` (pytest configuration and fixtures)

**skills/:**
- Purpose: Claude skill definitions for user-facing commands
- Contains: Declarative skill metadata (doc-audit, doc-embed, doc-search, carta-init)
- Note: Mirrors subsystem structure for consistency

**.planning/codebase/:**
- Purpose: GSD-generated codebase analysis documents
- Contains: ARCHITECTURE.md, STRUCTURE.md, CONVENTIONS.md, TESTING.md, CONCERNS.md
- Note: Not committed to version control; generated for planning purposes

## Key File Locations

**Entry Points:**
- `carta/__main__.py`: Package entry point when running `python -m carta`
- `carta/cli.py`: Main CLI dispatcher; defines all subcommands (init, scan, embed, search)

**Configuration:**
- `carta/config.py`: Configuration schema, loading, and validation
- `.carta/config.yaml`: User project configuration (created by `carta init`)

**Core Logic:**
- `carta/scanner/scanner.py`: Documentation auditing (checks for homeless docs, frontmatter validation)
- `carta/embed/pipeline.py`: Workflow orchestration (orchestrates embed, search operations)
- `carta/embed/embed.py`: Vector operations (Ollama embedding, Qdrant upsert)
- `carta/embed/parse.py`: Text extraction (PDF parsing, chunking logic)
- `carta/embed/induct.py`: Metadata management (sidecar generation, slug creation)
- `carta/install/bootstrap.py`: Project initialization (Qdrant setup, hook registration)

**Testing:**
- `carta/conftest.py`: Shared pytest fixtures
- `carta/tests/test_cli.py`: CLI command tests
- `carta/tests/test_config.py`: Configuration loading tests
- `carta/embed/tests/test_embed.py`: Embedding pipeline tests
- `carta/scanner/tests/test_scanner.py`: Scanner logic tests
- `carta/install/tests/test_bootstrap.py`: Bootstrap initialization tests

## Naming Conventions

**Files:**
- Module files: snake_case (e.g., `embed.py`, `scanner.py`, `bootstrap.py`)
- Test files: `test_*.py` (e.g., `test_cli.py`, `test_embed.py`)
- Configuration: `config.yaml` in `.carta/` directory; `*.embed-meta.yaml` for sidecars
- Auxiliary shells scripts: kebab-case (e.g., `carta-prompt-hook.sh`)

**Directories:**
- Package directories: lowercase (e.g., `embed`, `scanner`, `install`, `hooks`)
- Test subdirectories: `tests/` within each subsystem
- Hidden directories: `.carta/` (project state), `.planning/` (analysis), `.github/` (CI/CD)

**Python Symbols:**
- Classes: PascalCase (e.g., `ConfigError`)
- Functions: snake_case (e.g., `load_config`, `run_embed`, `chunk_text`)
- Constants: UPPER_SNAKE_CASE (e.g., `VECTOR_DIM`, `REQUIRED_FIELDS`)
- Private functions: Leading underscore (e.g., `_embed_lock_read_pid`, `_deep_merge`)

## Where to Add New Code

**New Feature (e.g., new CLI command):**
- Command logic: `carta/cli.py` (add `cmd_*` function and dispatch entry)
- Subsystem: Create new module in `carta/` (e.g., `carta/newfeature/module.py`)
- Tests: `carta/newfeature/tests/test_module.py`
- Skill: `skills/newfeature/skill.yaml`

**New Subsystem Module:**
- Implementation: `carta/subsystem/module.py`
- Tests: `carta/subsystem/tests/test_module.py`
- Entry in dispatcher: `carta/cli.py` for public CLI operations

**Utility Functions:**
- Shared helpers used across subsystems: Define in existing modules (e.g., `parse.py` for text utilities)
- If widely used across multiple subsystems, consider creating `carta/utils.py` or `carta/common.py`

**Configuration Additions:**
- New config fields: Add to `DEFAULTS` dict in `carta/config.py`
- Validation rules: Add to `load_config()` validation logic
- Documentation: Update config schema in bootstrap help text

**Tests:**
- Unit tests: Colocate with module under `tests/` subdirectory
- Integration tests: `carta/tests/` for cross-subsystem workflows
- Fixtures: Define in `conftest.py` at appropriate level (local or package-wide)

## Special Directories

**`.carta/` (Project State):**
- Purpose: Holds project-specific configuration and runtime files
- Generated: Yes (created by `carta init`)
- Committed: No (.gitignore'd)
- Contains:
  - `config.yaml` — User project configuration
  - `scan-results.json` — Output of `carta scan`
  - `embed.lock` — PID file for embedding concurrency control
  - `carta/` — Copy of the runtime (installer copies this on init)

**`.planning/codebase/` (Analysis Docs):**
- Purpose: GSD codebase mapping documents
- Generated: Yes (created by /gsd:map-codebase)
- Committed: No (for planning/reference only)
- Contains: ARCHITECTURE.md, STRUCTURE.md, CONVENTIONS.md, TESTING.md, CONCERNS.md

**`skills/` (Claude Skills):**
- Purpose: User-facing command definitions for Claude interaction
- Generated: No (manually maintained)
- Committed: Yes
- Pattern: One subdirectory per skill, mirroring `carta/` subsystem structure

**`docs/` (Documentation):**
- Purpose: User and developer documentation
- Generated: Partially (archives may be generated)
- Committed: Yes
- Contains: Testing documentation, feature guides, reference material

**`.github/workflows/` (CI/CD):**
- Purpose: GitHub Actions automation
- Generated: No
- Committed: Yes
- Contains: Test runners, release automation, deployment pipelines

## Module Dependencies

```
cli.py
├── config.py
├── scanner/scanner.py
│   └── config.py
├── embed/pipeline.py
│   ├── config.py
│   ├── embed/embed.py
│   │   ├── config.py
│   │   └── requests (Ollama)
│   │   └── qdrant-client (Qdrant)
│   ├── embed/parse.py
│   │   └── fitz (PyMuPDF)
│   └── embed/induct.py
│       └── config.py
└── install/bootstrap.py
    ├── config.py
    └── requests (Qdrant/Ollama checks)
```

---

*Structure analysis: 2026-03-25*
