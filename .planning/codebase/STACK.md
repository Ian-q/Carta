# Technology Stack

**Analysis Date:** 2026-03-25

## Languages

**Primary:**
- Python 3.10+ - Core application, CLI, scanning, embedding pipeline

**Secondary:**
- Bash - Installation script, shell integration hooks
- YAML - Configuration files, frontmatter in markdown docs

## Runtime

**Environment:**
- Python 3.10 or later (specified in `pyproject.toml`)

**Package Manager:**
- pip / setuptools (modern Python packaging)
- Lockfile: Not detected (uses direct `pyproject.toml` dependencies)

## Frameworks

**Core:**
- argparse (stdlib) - CLI argument parsing in `carta/cli.py`
- pathlib (stdlib) - File system operations

**Testing:**
- pytest 7.0+ - Test runner and framework (dev dependency)
- unittest.mock (stdlib) - Mocking in tests

**Build/Dev:**
- setuptools 61.0+ - Package building and installation
- shutil, subprocess (stdlib) - File operations and process management

## Key Dependencies

**Critical:**
- qdrant-client 1.7+ - Vector database client for Qdrant integration (`carta/embed/embed.py`, `carta/embed/pipeline.py`)
  - Used for semantic search over embedded documents
  - Manages collection creation, upsert, and vector operations
  - Dependency: `QdrantClient` class in `carta/embed/embed.py:7`

**Data & Content:**
- PyMUPDF 1.23+ (pymupdf) - PDF text extraction
  - Used in `carta/embed/parse.py` for extracting text from reference documents
  - Critical for embedding pipeline

**HTTP & Communication:**
- requests 2.31+ - HTTP client library
  - Used for Ollama API calls in `carta/embed/embed.py:27` (embeddings endpoint)
  - Used for Qdrant health checks in `carta/install/bootstrap.py`

**Configuration & Serialization:**
- PyYAML 6.0+ - YAML parsing for config and frontmatter
  - Config loading in `carta/config.py:2`
  - Sidecar metadata reading in `carta/embed/induct.py:7`
  - Frontmatter parsing in `carta/scanner/scanner.py`

## Configuration

**Environment Variables:**
- `CARTA_QDRANT_URL` - Override Qdrant URL (default: `http://localhost:6333`)
- `CARTA_OLLAMA_URL` - Override Ollama URL (default: `http://localhost:11434`)
- `PYTHONPATH` - Set during tests in `carta/tests/test_cli.py`

**Build Configuration:**
- `pyproject.toml` - Single source of truth for package metadata and dependencies
  - Dynamic version from `carta.__version__` attribute
  - Entry point: `carta = "carta.cli:main"` - makes `carta` command available
  - Packages: `carta*` (all modules under `carta/`)
  - Package data: Hooks (`.sh` files) and skill definitions (`SKILL.md`)

## Platform Requirements

**Development:**
- Python 3.10+ (command: `python3 --version`)
- Git (for project detection in bootstrap)
- Docker (strongly recommended for running Qdrant and Ollama containers)

**Runtime Dependencies (not in pip):**
- **Qdrant** - Vector database service (Docker: `docker run -p 6333:6333 qdrant/qdrant`)
  - Optional if embedding disabled via config
  - Connected at `cfg["qdrant_url"]` (default: `http://localhost:6333`)
  - Health check in `carta/install/bootstrap.py:_check_qdrant()`

- **Ollama** - Local LLM embedding service (https://ollama.ai)
  - Requires model: `nomic-embed-text:latest` (768-dimensional embeddings)
  - Optional if embedding disabled via config
  - Connected at `cfg["embed"]["ollama_url"]` (default: `http://localhost:11434`)
  - Alternative model: `phi3.5-mini` for proactive recall judge (optional)

**Production:**
- Docker environment or system with Python 3.10+
- Network access to Qdrant API (default port 6333)
- Network access to Ollama API (default port 11434)
- No cloud services required - all runs locally

## Special Considerations

**CLI Installation:**
- Package installed via `pipx`, `pip`, or `uvx` exposes `carta` command
- Symlink checks in `carta/cli.py:_platformio_carta_paths_on_path()` to detect PlatformIO conflicts
- Runtime copied to `.carta/carta/` during `carta init` for self-contained execution

**Vector Dimensions:**
- All embeddings use 768-dimensional vectors (nomic-embed-text standard)
- Defined in `carta/embed/embed.py:17` and `carta/install/bootstrap.py:12`

---

*Stack analysis: 2026-03-25*
