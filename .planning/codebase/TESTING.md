# Testing Patterns

**Analysis Date:** 2026-03-25

## Test Framework

**Runner:**
- pytest >= 7.0 (defined in `pyproject.toml` under `[project.optional-dependencies]`)
- Config: No `pytest.ini` or `[tool.pytest]` section in pyproject.toml; uses pytest defaults

**Assertion Library:**
- pytest's built-in `assert` statements (no external assertion library like nose, unittest)

**Run Commands:**
```bash
pytest                              # Run all tests
pytest carta/tests/                 # Run specific directory
pytest carta/tests/test_cli.py      # Run specific file
pytest -v                           # Verbose output
pytest --tb=short                   # Short traceback format
```

**Coverage:**
- No coverage configuration detected
- No coverage requirements enforced
- Coverage tracking not integrated into CI/test runs

## Test File Organization

**Location:**
- Co-located with source: `carta/MODULE/tests/test_*.py` mirrors `carta/MODULE/`
- Example: source `carta/scanner/scanner.py` → test `carta/scanner/tests/test_scanner.py`

**Naming:**
- Test files: `test_*.py` (pytest convention)
- Test functions: `test_*()` (all tests discoverable by pytest)
- Test helpers: also in test files, prefixed with underscore: `_make_tree()`, `_minimal_cfg()`

**Structure:**
```
carta/
  scanner/
    scanner.py        # Implementation
    tests/
      __init__.py     # (usually empty)
      test_scanner.py # Tests for scanner.py
  embed/
    embed.py
    tests/
      test_embed.py
  tests/              # Root-level tests for CLI and integration
    __init__.py
    test_cli.py
    test_config.py
    test_version.py
```

## Test Structure

**Suite Organization:**
```python
# Tests organized by function/feature being tested
def test_parse_frontmatter_with_valid_frontmatter(tmp_path):
    """Test success case."""
    doc = write_doc(tmp_path, "test.md", """\
        ---
        related:
          - docs/CAN/TOPOLOGY.md
        last_reviewed: 2026-03-18
        ---
        # Doc content
        """)
    result = parse_frontmatter(doc)
    assert result == {
        "related": ["docs/CAN/TOPOLOGY.md"],
        "last_reviewed": "2026-03-18",
    }

def test_parse_frontmatter_no_frontmatter(tmp_path):
    """Test when no frontmatter present."""
    doc = write_doc(tmp_path, "test.md", "# Just a doc\nNo frontmatter here.\n")
    assert parse_frontmatter(doc) is None
```

**Patterns:**
- One test per logical case (success, missing field, invalid format, etc.)
- Test function names describe the scenario being tested
- Docstrings optional but used for clarity on non-obvious tests
- Comments with `# ---------------------------------------------------------------------------` separate logical groups

**Setup/Teardown:**
- pytest fixtures used for reusable setup (see `conftest.py`)
- No explicit teardown needed; pytest handles tmp_path fixture cleanup
- Fixtures passed as function parameters

**Assertion Pattern:**
```python
assert result == expected_dict
assert result is None
assert result.returncode == 0
assert "config" in result.stderr.lower() or "config" in result.stdout.lower()
```

## Mocking

**Framework:** `unittest.mock` (stdlib)

**Patterns:**
```python
from unittest.mock import MagicMock, patch

# Mock entire module
with patch('module.function'):
    # Code that uses module.function

# Mock specific return values
mock_obj = MagicMock()
mock_obj.return_value = some_value
```

**Example from `test_scanner.py`:**
```python
from unittest.mock import MagicMock, patch

# Can mock out imports or external calls
with patch('carta.scanner.scanner.check_homeless_docs'):
    result = run_scan(...)
```

**What to Mock:**
- External API calls (requests to Qdrant, Ollama)
- File system operations when testing logic (use tmp_path instead)
- Subprocess calls in integration tests

**What NOT to Mock:**
- Core application functions being tested
- Config loading/merging (test with real YAML files)
- Path/file operations (use pytest's tmp_path fixture)

## Fixtures and Factories

**Test Data:**
```python
@pytest.fixture
def minimal_cfg():
    """Canonical minimal config dict for tests across the carta package."""
    return {
        "project_name": "test-project",
        "qdrant_url": "http://localhost:6333",
        "docs_root": "docs/",
        "stale_threshold_days": 30,
        ...
    }

@pytest.fixture
def minimal_config_yaml():
    """Canonical minimal config as a YAML string for tests that need file content."""
    return (
        "project_name: test-project\n"
        "qdrant_url: http://localhost:6333\n"
    )
```

**Location:**
- `conftest.py` at package root: shared fixtures across all tests
- Local fixtures in test files: test-specific data factories

**Helper Functions (not fixtures):**
```python
def write_doc(tmp_path, name, content):
    """Create a markdown file in tmp_path."""
    p = tmp_path / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(content))
    return p

def _make_tree(tmp_path, files):
    """Create multiple files under tmp_path."""
    for f in files:
        p = tmp_path / f
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("# doc\n")
    return tmp_path

def _minimal_cfg(tmp_path, excluded_paths=None, stale_threshold_days=30):
    """Create minimal config dict for specific test scenario."""
    return {...}
```

## Coverage

**Requirements:** None enforced; coverage not tracked

**View Coverage:**
- Can run `pytest --cov=carta` if pytest-cov plugin installed (not required)
- No baseline or threshold configured

## Test Types

**Unit Tests:**
- Most tests in the suite (80%+)
- Test individual functions in isolation
- Example: `test_parse_frontmatter_*()` tests parse logic
- Example: `test_load_valid_config()` tests config loading
- Use minimal fixtures and real data (YAML dicts)

**Integration Tests:**
- CLI integration tests in `test_cli.py`
- Test subprocess invocation of `carta` commands
- Example: `test_runtime_cli_direct_execution()` simulates actual user workflow

```python
def test_runtime_cli_direct_execution(tmp_path):
    """Simulate what `carta init` does: copy runtime, run CLI directly."""
    dest = tmp_path / ".carta" / "carta"
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(CARTA_RUNTIME_SRC, dest)

    result = subprocess.run(
        [sys.executable, str(dest / "cli.py"), "--version"],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
        env=env,
    )
    assert result.returncode == 0, result.stderr
```

**E2E Tests:**
- Not present; integration tests cover most CLI workflows
- Would require running Qdrant/Ollama services

## Common Patterns

**Async Testing:**
- Not used; no async code in this codebase

**Error Testing:**
```python
def test_missing_project_name_raises(tmp_path):
    """Config without project_name should raise ConfigError."""
    bad = {k: v for k, v in MINIMAL_CONFIG.items() if k != "project_name"}
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(yaml.dump(bad))
    with pytest.raises(ConfigError, match="project_name"):
        load_config(cfg_path)

def test_unknown_command_exits_nonzero():
    """Invalid command should exit with non-zero status."""
    result = run_carta(["notacommand"])
    assert result.returncode != 0
```

**Subprocess Testing:**
```python
def run_carta(args: list[str], cwd: Path = None) -> subprocess.CompletedProcess:
    """Helper to run carta CLI in subprocess with proper PYTHONPATH."""
    repo_root = Path(__file__).resolve().parents[2]
    env = os.environ.copy()
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = str(repo_root) if not existing else f"{repo_root}{os.pathsep}{existing}"
    return subprocess.run(
        [sys.executable, "-m", "carta.cli"] + args,
        capture_output=True,
        text=True,
        cwd=str(cwd) if cwd else None,
        env=env,
    )

def test_version():
    """Version flag outputs correct version."""
    result = run_carta(["--version"])
    assert result.returncode == 0
    from carta import __version__
    assert __version__ in result.stdout
```

**File System Testing with tmp_path:**
```python
def test_scan_requires_config(tmp_path):
    """Running scan without .carta/config.yaml should fail."""
    result = run_carta(["scan"], cwd=tmp_path)
    assert result.returncode != 0
    assert "config" in result.stderr.lower() or "config" in result.stdout.lower()
```

---

*Testing analysis: 2026-03-25*
