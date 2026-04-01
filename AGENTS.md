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

- Python 3.10+ semantic memory sidecar for Claude Code
- CLI commands: `init`, `scan`, `embed`, `search`
- Uses Qdrant for vector storage, Ollama for embeddings
- Tests in `carta/tests/` and `carta/*/tests/`

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
- Sidecar files: `*.embed-meta.yaml` track embedding status
- Lock file: `.carta/embed.lock` for concurrency control
- Collections named: `{project_name}_{type}` (e.g., `myproject_doc`)

## Key Files

- `carta/cli.py` - CLI entry point and commands
- `carta/config.py` - Config loading, validation, defaults
- `carta/conftest.py` - Shared test fixtures
- `pyproject.toml` - Package metadata, dependencies, entry points
