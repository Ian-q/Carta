# Coding Conventions

**Analysis Date:** 2026-03-25

## Naming Patterns

**Files:**
- Module files use snake_case: `cli.py`, `embed.py`, `parse.py`, `scanner.py`, `bootstrap.py`
- Test files follow pytest convention: `test_*.py` (e.g., `test_cli.py`, `test_config.py`)
- Package initialization: `__init__.py` (often empty or minimal)

**Functions:**
- Private/internal functions use leading underscore: `_embed_lock_read_pid()`, `_check_qdrant()`, `_deep_merge()`, `_estimate_tokens()`
- Public functions use verb-based snake_case: `find_config()`, `parse_frontmatter()`, `chunk_text()`, `is_excluded()`, `run_embed()`, `run_scan()`
- Helper functions follow pattern: `is_*()`, `get_*()`, `check_*()`, `run_*()`

**Variables:**
- Local variables and parameters use snake_case: `repo_root`, `cfg_path`, `chunk_index`, `max_tokens`
- Class variables and constants use UPPERCASE: `REQUIRED_FIELDS`, `DEFAULTS`, `VECTOR_DIM`, `DEFAULT_HOMELESS_ROOT_WHITELIST`, `CARTA_RUNTIME_SRC`
- Collection names use pattern: `{project_name}_{type_}` (e.g., `test-project_doc`)

**Types:**
- Type hints used throughout for function parameters and returns: `def load_config(path: Path) -> dict:`
- Optional types: `Optional[dict]`, `Optional[str]`
- Generic collections: `list[dict]`, `list[str]`, `dict[str, int]`

## Code Style

**Formatting:**
- Python 3.10+ syntax (modern type hints, walrus operator acceptable)
- Line length: not strictly enforced but tends toward ~100 chars
- Spaces: 4-space indentation (PEP 8 standard)
- Imports: organized in sections (stdlib, third-party, local)

**Linting:**
- No explicit linter configured (no .pylintrc, ruff.toml, or black config)
- Code follows PEP 8 conventions organically
- Type hints are used but not strictly validated with mypy

## Import Organization

**Order:**
1. Standard library imports (os, sys, pathlib, json, yaml, etc.)
2. Third-party imports (requests, qdrant_client, pymupdf/fitz, pytest)
3. Local imports (from carta.* modules)

**Pattern in practice:**
```python
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

import requests
import yaml
from qdrant_client import QdrantClient

from carta.config import load_config, collection_name
from carta.scanner.scanner import run_scan
```

**Path Aliases:**
- No aliases configured; absolute imports from package root (`from carta.config import ...`)
- Relative imports avoided in favor of explicit package paths

## Error Handling

**Patterns:**
- Custom exception class for config errors: `ConfigError` in `carta/config.py`
- Try-except blocks catch specific exceptions before generic ones
- File operations use `missing_ok=True` on `Path.unlink()` for idempotency
- OSError caught broadly for file system operations that may fail intermittently

**Example from `cli.py`:**
```python
try:
    return int(lock_path.read_text().strip())
except (ValueError, OSError):
    return None
```

**Example from `config.py`:**
```python
try:
    raw = yaml.safe_load(f) or {}
except yaml.YAMLError as e:
    raise ConfigError(f"Invalid YAML in {path}: {e}") from e
```

**Exit patterns:**
- `sys.exit(0)` for success
- `sys.exit(1)` for generic errors
- `sys.exit(128 + signum)` for signal handlers
- Error messages printed to `sys.stderr` before exit

## Logging

**Framework:** `print()` to stdout/stderr (no logger library)

**Patterns:**
- Status messages to stdout: `print(f"Initialising Carta for project: {project_name}")`
- Errors to stderr: `print(f"Error: {e}", file=sys.stderr)`
- Flush often for long-running operations: `print("...", flush=True)`
- Progress/summary output: `print(f"Embedded: {summary['embedded']}, Skipped: {summary['skipped']}")`

**No structured logging:**
- No logging module (logging.getLogger) used
- No log levels (DEBUG, INFO, WARNING)
- Messages are human-readable and immediate

## Comments

**When to Comment:**
- Docstrings on public functions explain purpose, arguments, return value
- Inline comments explain non-obvious logic (e.g., FT-5 comment in `cli.py` line 113)
- Heuristic explanations: "Token estimate. Uses max of word-count and char-count..." in `parse.py`
- Complex sections marked with comment blocks: `# ---------------------------------------------------------------------------`

**Docstring Style:**
```python
def upsert_chunks(chunks: list[dict], cfg: dict, client: QdrantClient = None) -> int:
    """Embed and upsert chunks to Qdrant using settings from cfg.

    Args:
        chunks: list of chunk dicts with at minimum keys: "slug", "text", "chunk_index".
        cfg: carta config dict (must contain qdrant_url, embed.ollama_url, ...).
        client: optional QdrantClient instance. If None, a new client is created.

    Returns:
        Number of points upserted.
    """
```

**No Type Docstring Tags:**
- Args/Returns documented in natural language
- Type hints in function signature preferred over docstring type annotations

## Function Design

**Size:** Functions are typically 20-80 lines; longer functions (100+) are intentionally complex (e.g., `chunk_text()` handles text splitting logic)

**Parameters:**
- Prefer explicit parameters over **kwargs
- Use Path objects for file paths, not strings
- Config dict (cfg) passed as parameter rather than globals
- Optional parameters have defaults and type hints

**Return Values:**
- Functions return early to reduce nesting (e.g., line 82-90 in `parse.py`)
- Multiple return types acceptable: `None` on failure, dict/list on success
- Tuples or dicts used to return multiple values

**Example of early return:**
```python
if _estimate_tokens(text) <= max_tokens:
    chunks.append({...})
    chunk_index += 1
    continue  # Skip complex splitting
```

## Module Design

**Exports:**
- No __all__ defined; all public functions are importable
- Modules are cohesive: `embed.py` handles embedding/upserting, `parse.py` handles PDF extraction, `scanner.py` handles doc structure checking

**Barrel Files:**
- Package `__init__.py` files are minimal (often empty except version in `carta/__init__.py`)
- No re-exports of submodule contents

**Module Structure Pattern:**
1. Module docstring
2. Imports
3. Constants/defaults
4. Helper/private functions (prefixed with _)
5. Public functions
6. Main entry point (if applicable)

**Example from `scanner.py`:**
```
"""Carta documentation structural scanner."""

import fnmatch
import json
...

# Standard root-level markdown / convention files...
DEFAULT_HOMELESS_ROOT_WHITELIST = frozenset({...})

def _anchor_basenames(cfg: dict) -> set[str]:
    """..."""
    ...

def check_homeless_docs(repo_root: Path, cfg: dict) -> list:
    """..."""
    ...
```

---

*Convention analysis: 2026-03-25*
