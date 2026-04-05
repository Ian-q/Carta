import json
import sys
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

from carta.install.bootstrap import (
    run_bootstrap,
    _update_gitignore,
    _register_hooks,
)


def _mock_passing_preflight():
    """Return a context manager that mocks PreflightChecker with all passing results."""
    from carta.install.preflight import PreflightResult, PreflightCheck

    def create_passing_result():
        """Create a PreflightResult with all critical checks passing."""
        checks = [
            PreflightCheck("python_version", "pass", "Python 3.11.0 (supported)", "environment"),
            PreflightCheck("pip_availability", "pass", "pip available", "environment"),
            PreflightCheck("virtual_environment", "pass", "Running in virtual environment", "environment"),
            PreflightCheck("network_connectivity", "pass", "Network connectivity OK", "environment"),
            PreflightCheck("docker_installed", "pass", "Docker installed", "infrastructure"),
            PreflightCheck("docker_running", "pass", "Docker daemon running", "infrastructure"),
            PreflightCheck("qdrant_running", "pass", "Qdrant ready at http://localhost:6333", "infrastructure"),
            PreflightCheck("ollama_installed", "pass", "Ollama installed", "infrastructure"),
            PreflightCheck("ollama_running", "pass", "Ollama server running", "infrastructure"),
            PreflightCheck("ports_available", "pass", "Required ports available", "infrastructure"),
        ]
        return PreflightResult(checks)

    return patch("carta.install.preflight.PreflightChecker.run", return_value=create_passing_result())


def _mock_unavailable_qdrant_preflight():
    """Return a context manager that mocks PreflightChecker with Qdrant unavailable (warning, not blocking)."""
    from carta.install.preflight import PreflightResult, PreflightCheck

    def create_warning_result():
        """Create a PreflightResult with Qdrant unavailable as warning (not critical failure)."""
        checks = [
            PreflightCheck("python_version", "pass", "Python 3.11.0 (supported)", "environment"),
            PreflightCheck("pip_availability", "pass", "pip available", "environment"),
            PreflightCheck("virtual_environment", "pass", "Running in virtual environment", "environment"),
            PreflightCheck("network_connectivity", "pass", "Network connectivity OK", "environment"),
            PreflightCheck("docker_installed", "warn", "Docker not installed (optional but recommended)", "infrastructure", fixable=False),
            PreflightCheck("qdrant_running", "warn", "Qdrant not running", "infrastructure", fixable=False),
            PreflightCheck("ollama_installed", "warn", "Ollama not found (optional)", "infrastructure", fixable=False),
            PreflightCheck("ports_available", "pass", "Required ports available", "infrastructure"),
        ]
        return PreflightResult(checks)

    return patch("carta.install.preflight.PreflightChecker.run", return_value=create_warning_result())


# --- BOOT-01 ---

def test_boot01_residue_causes_exit(tmp_path):
    """run_bootstrap() calls sys.exit(1) when _remove_plugin_cache returns False."""
    with (
        _mock_passing_preflight(),
        patch("carta.install.bootstrap._write_config"),
        patch("carta.install.bootstrap._register_hooks"),
        patch("carta.install.bootstrap._remove_plugin_cache", return_value=False),
        patch("carta.install.bootstrap._create_qdrant_collections", return_value=True),
        patch("carta.install.bootstrap._update_gitignore"),
        patch("carta.install.bootstrap.shutil.copytree"),
        patch("carta.install.bootstrap._append_claude_md"),
    ):
        with pytest.raises(SystemExit) as exc_info:
            run_bootstrap(tmp_path)
        assert exc_info.value.code == 1


# --- BOOT-02 ---

def test_boot02_skips_when_parent_glob_carta_slash(tmp_path):
    """.carta/ in .gitignore suppresses all sub-entry additions."""
    gitignore = tmp_path / ".gitignore"
    gitignore.write_text(".carta/\n")
    _update_gitignore(tmp_path)
    assert gitignore.read_text() == ".carta/\n"


def test_boot02_skips_when_parent_glob_carta_star(tmp_path):
    """.carta/* in .gitignore suppresses all sub-entry additions."""
    gitignore = tmp_path / ".gitignore"
    gitignore.write_text(".carta/*\n")
    _update_gitignore(tmp_path)
    assert gitignore.read_text() == ".carta/*\n"


def test_boot02_adds_entries_without_parent_glob(tmp_path):
    """Without a parent glob, sub-entries are appended normally."""
    gitignore = tmp_path / ".gitignore"
    gitignore.write_text("node_modules/\n")
    _update_gitignore(tmp_path)
    content = gitignore.read_text()
    assert ".carta/scan-results.json" in content
    assert ".carta/carta/" in content
    assert ".carta/hooks/" in content


# --- BOOT-03 ---

def test_boot03_hook_scripts_copied_to_carta_hooks(tmp_path):
    """_register_hooks copies .sh files to .carta/hooks/ and does NOT write .claude/settings.json."""
    hooks_src = Path(__file__).parent.parent / "hooks"
    if not hooks_src.exists():
        pytest.skip("hooks/ source directory not present")

    _register_hooks(tmp_path)

    # Hook scripts should be copied
    hooks_dest = tmp_path / ".carta" / "hooks"
    assert hooks_dest.exists(), ".carta/hooks/ should be created"
    sh_files = list(hooks_src.glob("*.sh"))
    if sh_files:
        for script in sh_files:
            assert (hooks_dest / script.name).exists(), f"{script.name} should be copied"

    # .claude/settings.json must NOT be written (plugin-native handles this)
    claude_settings = tmp_path / ".claude" / "settings.json"
    assert not claude_settings.exists(), \
        "_register_hooks should not write .claude/settings.json (plugin-native handles hooks)"


# --- BOOT-05 ---

def test_bootstrap_does_not_write_claude_settings_hooks(tmp_path):
    """bootstrap should not mutate .claude/settings.json for hooks (plugin-native now handles this)."""
    project_root = tmp_path
    (project_root / ".git").mkdir()

    with _mock_passing_preflight(), \
         patch("carta.install.bootstrap._detect_project_name", return_value="test-proj"), \
         patch("carta.install.bootstrap._remove_plugin_cache", return_value=True), \
         patch("carta.install.bootstrap._create_qdrant_collections", return_value=True), \
         patch("carta.install.bootstrap._update_gitignore"), \
         patch("carta.install.bootstrap._create_mcp_configs"), \
         patch("carta.install.bootstrap._write_config"):
        run_bootstrap(project_root)

    claude_dir = project_root / ".claude"
    assert not claude_dir.exists() or not (claude_dir / "settings.json").exists(), \
        ".claude/settings.json should not be written by bootstrap (plugin-native handles hooks)"


# --- BOOT-04 ---

def test_bootstrap_continues_when_qdrant_unreachable(tmp_path):
    """bootstrap should not sys.exit when Qdrant is down — should warn and disable embed/search modules."""
    project_root = tmp_path
    (project_root / ".git").mkdir()

    with _mock_unavailable_qdrant_preflight(), \
         patch("carta.install.bootstrap._detect_project_name", return_value="test-proj"), \
         patch("carta.install.bootstrap._remove_plugin_cache", return_value=True), \
         patch("carta.install.bootstrap._create_qdrant_collections", return_value=True), \
         patch("carta.install.bootstrap._update_gitignore"), \
         patch("carta.install.bootstrap._create_mcp_configs"), \
         patch("carta.install.bootstrap._write_config"):
        # Should NOT raise SystemExit
        try:
            run_bootstrap(project_root)
        except SystemExit as e:
            raise AssertionError(f"bootstrap exited with code {e.code} when Qdrant was unreachable") from e
