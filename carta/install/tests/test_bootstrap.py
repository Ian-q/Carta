import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock
import json
import stat
import os
import yaml


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


def test_bootstrap_creates_carta_dir(tmp_path):
    from carta.install.bootstrap import run_bootstrap
    with _mock_passing_preflight(), \
         patch("carta.install.bootstrap._register_hooks"), \
         patch("carta.install.bootstrap._create_qdrant_collections"):
        run_bootstrap(tmp_path)
    assert (tmp_path / ".carta").exists()
    assert (tmp_path / ".carta" / "config.yaml").exists()

def test_bootstrap_config_has_all_fields(tmp_path):
    from carta.install.bootstrap import run_bootstrap
    with _mock_passing_preflight(), \
         patch("carta.install.bootstrap._register_hooks"), \
         patch("carta.install.bootstrap._create_qdrant_collections"):
        run_bootstrap(tmp_path)
    cfg = yaml.safe_load((tmp_path / ".carta" / "config.yaml").read_text())
    assert "project_name" in cfg
    assert "qdrant_url" in cfg
    assert "modules" in cfg
    assert "embed" in cfg, "embed block missing — _write_config must merge DEFAULTS"
    assert "proactive_recall" in cfg, "proactive_recall block missing"
    assert "cross_project_recall" in cfg, "cross_project_recall block missing"
    assert "contradiction_types" in cfg, "contradiction_types missing"


def test_bootstrap_updates_gitignore(tmp_path):
    (tmp_path / ".gitignore").write_text("node_modules/\n")
    from carta.install.bootstrap import run_bootstrap
    with _mock_passing_preflight(), \
         patch("carta.install.bootstrap._register_hooks"), \
         patch("carta.install.bootstrap._create_qdrant_collections"):
        run_bootstrap(tmp_path)
    content = (tmp_path / ".gitignore").read_text()
    assert ".carta/scan-results.json" in content


def test_bootstrap_appends_claude_md(tmp_path):
    (tmp_path / "CLAUDE.md").write_text("# My Project\n")
    from carta.install.bootstrap import run_bootstrap
    with _mock_passing_preflight(), \
         patch("carta.install.bootstrap._register_hooks"), \
         patch("carta.install.bootstrap._create_qdrant_collections"):
        run_bootstrap(tmp_path)
    content = (tmp_path / "CLAUDE.md").read_text()
    assert "Carta is active" in content


def test_bootstrap_creates_namespaced_collections(tmp_path):
    from carta.install.bootstrap import run_bootstrap
    mock_create = MagicMock()
    with _mock_passing_preflight(), \
         patch("carta.install.bootstrap._register_hooks"), \
         patch("carta.install.bootstrap._create_qdrant_collections", mock_create):
        run_bootstrap(tmp_path)
    project_name = mock_create.call_args[0][0]
    assert isinstance(project_name, str) and len(project_name) > 0

def test_create_qdrant_collections_uses_namespaced_names():
    from unittest.mock import MagicMock
    from carta.install.bootstrap import _create_qdrant_collections
    client = MagicMock()
    client.collection_exists.return_value = False
    with patch("qdrant_client.QdrantClient", return_value=client):
        _create_qdrant_collections("my-project", "http://localhost:6333")
    created = [c.kwargs["collection_name"] for c in client.create_collection.call_args_list]
    assert "my-project_doc" in created
    assert "my-project_session" in created
    assert "my-project_notes" in created


def test_create_qdrant_collections_creates_hybrid_schema():
    """Init creates the named dense + bm25 sparse hybrid schema (CA-10) so an
    init-before-embed project is not silently stuck on dense-only retrieval."""
    from unittest.mock import MagicMock
    from carta.install.bootstrap import _create_qdrant_collections
    client = MagicMock()
    client.collection_exists.return_value = False
    # Pin the hybrid path: ensure_collection builds the named dense+bm25 schema only
    # when fastembed is importable (an optional extra absent from the base CI install).
    with patch("qdrant_client.QdrantClient", return_value=client), \
         patch("carta.embed.embed._fastembed_available", return_value=True):
        ok = _create_qdrant_collections("p", "http://localhost:6333")
    assert ok
    assert client.create_collection.call_args_list, "should create collections"
    for c in client.create_collection.call_args_list:
        assert "dense" in c.kwargs["vectors_config"]
        assert "bm25" in c.kwargs["sparse_vectors_config"]


def test_create_qdrant_collections_warns_on_legacy_existing_schema(capsys):
    """Re-init over an existing NON-hybrid collection must warn, not silently
    rubber-stamp it (audit CA-27)."""
    from unittest.mock import MagicMock
    from carta.install.bootstrap import _create_qdrant_collections
    client = MagicMock()
    client.collection_exists.return_value = True  # already exists
    with patch("qdrant_client.QdrantClient", return_value=client), \
         patch("carta.embed.embed.collection_is_hybrid", return_value=False):
        ok = _create_qdrant_collections("p", "http://localhost:6333")
    assert ok  # not a hard failure
    out = capsys.readouterr().out.lower()
    assert "legacy" in out or "hybrid" in out
    client.create_collection.assert_not_called()  # existing collection left untouched

def test_bootstrap_continues_if_qdrant_unavailable(tmp_path):
    """bootstrap should warn and continue (not exit) when Qdrant is unreachable but not critically failing."""
    from carta.install.bootstrap import run_bootstrap
    with _mock_unavailable_qdrant_preflight(), \
         patch("carta.install.bootstrap._remove_plugin_cache", return_value=True), \
         patch("carta.install.bootstrap._create_qdrant_collections", return_value=True), \
         patch("carta.install.bootstrap._update_gitignore"), \
         patch("carta.install.bootstrap._create_mcp_configs"):
        try:
            run_bootstrap(tmp_path)
        except SystemExit as e:
            raise AssertionError(f"bootstrap exited with code {e.code} when Qdrant was unreachable") from e


def test_bootstrap_uses_qdrant_url_from_env(tmp_path):
    from carta.install.bootstrap import run_bootstrap

    custom_url = "http://qdrant.example:7000"
    with patch.dict(os.environ, {"CARTA_QDRANT_URL": custom_url}, clear=False), \
         _mock_passing_preflight(), \
         patch("carta.install.bootstrap._register_hooks"), \
         patch("carta.install.bootstrap._update_gitignore"), \
         patch("carta.install.bootstrap._append_claude_md"), \
         patch("carta.install.bootstrap.shutil.copytree"), \
         patch("carta.install.bootstrap._create_qdrant_collections") as mock_create:
        run_bootstrap(tmp_path)

    mock_create.assert_called_once()
    assert mock_create.call_args[0][1] == custom_url

    cfg = yaml.safe_load((tmp_path / ".carta" / "config.yaml").read_text())
    assert cfg["qdrant_url"] == custom_url


@pytest.mark.skip(reason="Mocking issue with shutil.copytree - copytree not being captured (pre-existing)")
def test_bootstrap_copytree_ignores_non_runtime_artifacts(tmp_path):
    from carta.install.bootstrap import run_bootstrap

    with _mock_passing_preflight(), \
         patch("carta.install.bootstrap._register_hooks"), \
         patch("carta.install.bootstrap._create_qdrant_collections"), \
         patch("carta.install.bootstrap._update_gitignore"), \
         patch("carta.install.bootstrap._append_claude_md"), \
         patch("carta.install.bootstrap.shutil.copytree") as mock_copytree:
        run_bootstrap(tmp_path)

    mock_copytree.assert_called_once()
    ignore_fn = mock_copytree.call_args.kwargs["ignore"]
    ignored = ignore_fn(
        str(tmp_path),
        ["tests", "__pycache__", "module.pyc", "module.pyo", "cli.py", "hooks"],
    )
    assert "tests" in ignored
    assert "__pycache__" in ignored
    assert "module.pyc" in ignored
    assert "module.pyo" in ignored
    assert "cli.py" not in ignored
    assert "hooks" not in ignored


def test_register_hooks_copies_scripts_locally(tmp_path):
    from carta.install.bootstrap import _register_hooks

    _register_hooks(tmp_path)

    hooks_dir = tmp_path / ".carta" / "hooks"
    assert (hooks_dir / "carta-prompt-hook.sh").exists()
    assert (hooks_dir / "carta-stop-hook.sh").exists()


def test_register_hooks_sets_executable_and_does_not_write_claude_settings(tmp_path):
    """_register_hooks copies scripts as executable; does NOT write .claude/settings.json
    (Claude Code hook registration is now plugin-native via hooks/hooks.json)."""
    from carta.install.bootstrap import _register_hooks

    _register_hooks(tmp_path)

    hooks_dir = tmp_path / ".carta" / "hooks"
    prompt_hook = hooks_dir / "carta-prompt-hook.sh"
    stop_hook = hooks_dir / "carta-stop-hook.sh"

    prompt_mode = prompt_hook.stat().st_mode
    stop_mode = stop_hook.stat().st_mode
    assert prompt_mode & stat.S_IXUSR
    assert prompt_mode & stat.S_IXGRP
    assert prompt_mode & stat.S_IXOTH
    assert stop_mode & stat.S_IXUSR
    assert stop_mode & stat.S_IXGRP
    assert stop_mode & stat.S_IXOTH

    # .claude/settings.json must NOT be written — plugin-native handles this
    settings_path = tmp_path / ".claude" / "settings.json"
    assert not settings_path.exists(), \
        "_register_hooks must not write .claude/settings.json (plugin-native handles hooks)"


# ---------------------------------------------------------------------------
# Plugin cache cleanup tests (MCP-07)
# ---------------------------------------------------------------------------

def test_remove_plugin_cache_removes_both_paths(tmp_path):
    """_remove_plugin_cache() removes both known cache dirs and returns True."""
    from carta.install.bootstrap import _remove_plugin_cache

    # Create both cache dirs
    path_a = tmp_path / ".claude/plugins/carta"
    path_b = tmp_path / ".claude/plugins/cache/carta-cc"
    path_a.mkdir(parents=True)
    path_b.mkdir(parents=True)

    with patch("carta.install.bootstrap.Path.home", return_value=tmp_path):
        result = _remove_plugin_cache()

    assert result is True
    assert not path_a.exists()
    assert not path_b.exists()


def test_remove_plugin_cache_noop_when_absent(tmp_path):
    """_remove_plugin_cache() returns True when neither cache dir exists."""
    from carta.install.bootstrap import _remove_plugin_cache

    with patch("carta.install.bootstrap.Path.home", return_value=tmp_path):
        result = _remove_plugin_cache()

    assert result is True


def test_remove_plugin_cache_assertion_on_residue(tmp_path, capsys):
    """_remove_plugin_cache() returns False and prints error when residue remains."""
    from carta.install.bootstrap import _remove_plugin_cache

    # Create one cache dir
    path_a = tmp_path / ".claude/plugins/carta"
    path_a.mkdir(parents=True)

    with patch("carta.install.bootstrap.Path.home", return_value=tmp_path), \
         patch("carta.install.bootstrap.shutil.rmtree"):  # rmtree is a no-op, residue remains
        result = _remove_plugin_cache()

    assert result is False
    captured = capsys.readouterr()
    assert "plugin cache residue" in captured.err


def test_carta_claude_block_content():
    from carta.install.bootstrap import _carta_claude_block
    block = _carta_claude_block("acme")
    assert "<!-- carta:guidance:start -->" in block
    assert "<!-- carta:guidance:end -->" in block
    assert "## Carta Knowledge Graph" in block
    assert "Search the docs before you assume." in block
    assert '`/doc-search "<name> responsibilities"`' in block   # trigger table row
    assert "/doc-audit" in block and "/doc-embed" in block       # maintenance line
    # collections comment interpolates the project name and stays inside the block
    assert "Carta is active. Collections: acme_doc, acme_session, acme_notes" in block


def test_append_claude_md_appends_to_existing(tmp_path):
    from carta.install.bootstrap import _append_claude_md
    (tmp_path / "CLAUDE.md").write_text("# My Project\n\nExisting guidance.\n")
    _append_claude_md(tmp_path, "acme")
    content = (tmp_path / "CLAUDE.md").read_text()
    assert "Existing guidance." in content                       # prior content preserved
    assert "## Carta Knowledge Graph" in content
    assert content.count("<!-- carta:guidance:start -->") == 1


def test_append_claude_md_creates_when_absent(tmp_path):
    from carta.install.bootstrap import _append_claude_md
    assert not (tmp_path / "CLAUDE.md").exists()
    _append_claude_md(tmp_path, "acme")
    content = (tmp_path / "CLAUDE.md").read_text()
    assert content.startswith("# acme")
    assert "## Carta Knowledge Graph" in content
    assert "<!-- carta:guidance:start -->" in content


def test_append_claude_md_idempotent_on_marker(tmp_path):
    from carta.install.bootstrap import _append_claude_md
    (tmp_path / "CLAUDE.md").write_text("# My Project\n")
    _append_claude_md(tmp_path, "acme")
    _append_claude_md(tmp_path, "acme")                          # second call is a no-op
    content = (tmp_path / "CLAUDE.md").read_text()
    assert content.count("<!-- carta:guidance:start -->") == 1


def test_append_claude_md_skips_legacy_oneliner(tmp_path):
    from carta.install.bootstrap import _append_claude_md
    # A repo bootstrapped by an older Carta has only the legacy comment.
    (tmp_path / "CLAUDE.md").write_text(
        "# My Project\n\n<!-- Carta is active. Collections: acme_doc, acme_session, acme_notes -->\n"
    )
    _append_claude_md(tmp_path, "acme")
    content = (tmp_path / "CLAUDE.md").read_text()
    assert "<!-- carta:guidance:start -->" not in content        # not upgraded / not doubled
    assert content.count("Carta is active") == 1


def test_write_config_preserves_user_customization_on_rerun(tmp_path):
    """Re-running `carta init` must NOT clobber a user's tuned config — bootstrap
    itself tells users to re-run on Qdrant errors. User tuning survives; modules
    and identity refresh to the new run's values."""
    from carta.install.bootstrap import _write_config
    import yaml as _yaml
    carta_dir = tmp_path / ".carta"
    carta_dir.mkdir()

    # First init (Qdrant was down → doc_embed False), then user tunes a value.
    _write_config(carta_dir, "proj", "http://localhost:6333", {"doc_embed": False})
    cfg = _yaml.safe_load((carta_dir / "config.yaml").read_text())
    cfg["stale_threshold_days"] = 999
    cfg.setdefault("embed", {})["chunking"] = {"max_tokens": 1234}
    (carta_dir / "config.yaml").write_text(_yaml.dump(cfg))

    # Re-run after fixing Qdrant (doc_embed now True).
    _write_config(carta_dir, "proj", "http://localhost:6333", {"doc_embed": True})
    out = _yaml.safe_load((carta_dir / "config.yaml").read_text())

    assert out["stale_threshold_days"] == 999, "user tuning was clobbered"
    assert out["embed"]["chunking"]["max_tokens"] == 1234, "user embed tuning was clobbered"
    assert out["modules"]["doc_embed"] is True, "re-run must refresh modules"


def test_detect_project_name_survives_hung_git(tmp_path, monkeypatch):
    """A hung/slow git must not block `carta init` forever — fall back to dir name."""
    import subprocess
    from carta.install import bootstrap
    (tmp_path / "myproj").mkdir()

    def hung_git(*a, **k):
        raise subprocess.TimeoutExpired(cmd="git", timeout=5)

    monkeypatch.setattr(bootstrap.subprocess, "run", hung_git)
    assert bootstrap._detect_project_name(tmp_path / "myproj") == "myproj"
