# Agent Guidelines for Carta

## Build / Test / Lint Commands

```bash
# Install package in development mode
pip install -e ".[dev]"

# Run all tests
pytest

# Run a single test file
pytest carta/tests/test_config.py

# Run a single test
pytest carta/tests/test_config.py::test_load_valid_config

# Run with verbose output
pytest -v

# Run with coverage (if pytest-cov installed)
pytest --cov=carta

# Build package
python -m build

# Install locally
pip install -e .

# No linting tools configured - follow PEP 8 organically
```

## Project Structure

- Python 3.10+ semantic memory sidecar for Claude Code (Qdrant vectors, Ollama embeddings)
- **CLI** (`carta <cmd>`, or `python -m carta <cmd>`): `init`, `scan`, `embed` (`--visual` / `--repair`), `search`, `audit`, `doctor`, `eval`, `remember`, `status`, `statusline`, `export`, `import`, `update`
- **MCP** (`carta-mcp`, stdio): `carta_search`, `carta_embed`, `carta_scan`, `carta_remember`
- **Hook** (`carta-hook` + `carta/hooks/*.sh`): pre-prompt proactive recall, three-zone gate (high→inject, low→silent, gray→Ollama judge), fail-open
- **Modules** (`carta/`): `embed/` `search/` `scanner/` `audit/` `eval/` `vision/` `mcp/` `hook/` `memory/` `install/` `update/` `ui/`
- Tests in `carta/tests/` and `carta/*/tests/`
- **Which command?** `carta scan` / `/doc-audit` = doc structure; `carta audit` / `carta doctor` = embed-data integrity & environment; `carta eval` = retrieval quality (see the "Which audit command?" table in README)

## Code Style Guidelines

### Naming
- **Modules**: snake_case (`cli.py`, `embed.py`, `pipeline.py`)
- **Tests**: `test_*.py` (pytest convention)
- **Functions**: snake_case, verb-based (`run_embed()`, `chunk_text()`)
- **Private**: leading underscore (`_acquire_lock()`, `_deep_merge()`)
- **Constants**: UPPERCASE (`VECTOR_DIM`, `DEFAULTS`)
- **Type hints**: used on all function params and returns

### Imports
Organize in three sections with blank lines between:
1. stdlib (`argparse`, `pathlib`, `typing`)
2. third-party (`yaml`, `pytest`)
3. local (`from carta.config import ...`)

Use absolute imports from package root. Avoid relative imports.

### Formatting
- 4-space indentation
- Line length: ~100 chars (not strictly enforced)
- Python 3.10+ syntax (walrus operator acceptable)
- Path objects for file paths, not strings

### Error Handling
- Custom `ConfigError` for config issues
- Catch specific exceptions before generic ones
- `sys.exit(0)` for success, `sys.exit(1)` for errors
- `sys.exit(128 + signum)` for signal handlers
- Print errors to `sys.stderr`
- File ops use `missing_ok=True` for idempotency

### Logging
- Status to stdout: `print(f"Embedded: {summary['embedded']}")`
- Errors to stderr: `print(f"Error: {e}", file=sys.stderr)`
- Flush long operations: `print("...", flush=True)`
- No `logging` module used - direct print statements

### Functions
- Prefer explicit parameters over `**kwargs`
- Config dict passed as parameter, not globals
- Return early to reduce nesting
- Multiple returns acceptable (`None` on failure, data on success)

### Docstrings
- Public functions have docstrings explaining purpose, args, return
- Use natural language, not strict Google/NumPy format
- Type hints in signature preferred over docstring annotations

## Testing Patterns

```python
# Use fixtures from conftest.py
@pytest.fixture
def minimal_cfg():
    return {"project_name": "test", ...}

# Test structure: arrange, act, assert
def test_feature(tmp_path):
    # tmp_path is pytest built-in fixture
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(yaml.dump(MINIMAL_CONFIG))
    result = function_under_test(cfg_path)
    assert result == expected

# ConfigError testing
with pytest.raises(ConfigError, match="field_name"):
    load_config(bad_path)
```

## Architecture Notes

- CLI (`carta/cli.py`) dispatches to command handlers
- Config loaded via `find_config()` → `load_config()`
- Pipeline modules coordinate multi-step workflows
- Sidecar files: `.carta/sidecars/<source-path-relative-to-repo>.embed-meta.yaml` track embedding status (mirror the source tree — not colocated)
- Lock file: `.carta/embed.lock` for concurrency control
- Collections named: `{project_name}_{type}` (e.g., `myproject_doc`)

## Key Files

- `carta/cli.py` - CLI entry point and commands
- `carta/config.py` - Config loading, validation, defaults
- `carta/conftest.py` - Shared test fixtures
- `pyproject.toml` - Package metadata, dependencies, entry points

## Visual Embedding (ColPali) — Agent Guidance

ColPali/ColQwen2 visual embedding (`colpali_enabled: true`) loads a ~5–8 GB model and
runs on every PDF in the corpus. This is expensive and almost never appropriate corpus-wide.

**Always scope ColPali to the directories that actually contain visual-rich content:**

```yaml
embed:
  colpali_enabled: true
  colpali_scoped_paths:
    - "docs/reference/datasheets/"   # trailing slash = directory prefix
    - "docs/diagrams/**/*.pdf"       # ** glob = recursive match
```

An empty `colpali_scoped_paths: []` (the default) means no restriction — all PDFs
are processed. Only leave it empty if you have confirmed that the entire corpus is
visual-rich enough to justify the cost.

For the OCR/VLM text-extraction pipeline (separate from ColPali), use `vision_routing`
to override the per-page routing heuristic: `auto` (default) | `ocr` | `vision` | `off`.
Set `vision_call_timeout_s` if Ollama calls time out on dense pages (default: 300 s).

See the "Scoping heavy visual models" section in README.md for full config reference.

**Two-pass visual embedding:** image-heavy PDF pages are processed in two passes — run `carta embed` first (fast text; queues visual pages), then `carta embed --visual` (slow, resumable: OCR text + ColPali). Scope visual work with `colpali_scoped_paths`. The `--visual` pass requires the `[visual]` extra (`pip install 'carta-cc[visual]'`); if absent it exits cleanly with install guidance.
