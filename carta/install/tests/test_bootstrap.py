import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock
import json
import stat
import os
import yaml

def test_bootstrap_creates_carta_dir(tmp_path):
    from carta.install.bootstrap import run_bootstrap
    with patch("carta.install.bootstrap._check_qdrant", return_value=True), \
         patch("carta.install.bootstrap._check_ollama", return_value=True), \
         patch("carta.install.bootstrap._register_hooks"), \
         patch("carta.install.bootstrap._create_qdrant_collections"):
        run_bootstrap(tmp_path)
    assert (tmp_path / ".carta").exists()
    assert (tmp_path / ".carta" / "config.yaml").exists()

def test_bootstrap_config_has_all_fields(tmp_path):
    from carta.install.bootstrap import run_bootstrap
    with patch("carta.install.bootstrap._check_qdrant", return_value=True), \
         patch("carta.install.bootstrap._check_ollama", return_value=True), \
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
    with patch("carta.install.bootstrap._check_qdrant", return_value=True), \
         patch("carta.install.bootstrap._check_ollama", return_value=True), \
         patch("carta.install.bootstrap._register_hooks"), \
         patch("carta.install.bootstrap._create_qdrant_collections"):
        run_bootstrap(tmp_path)
    content = (tmp_path / ".gitignore").read_text()
    assert ".carta/scan-results.json" in content

def test_bootstrap_appends_claude_md(tmp_path):
    (tmp_path / "CLAUDE.md").write_text("# My Project\n")
    from carta.install.bootstrap import run_bootstrap
    with patch("carta.install.bootstrap._check_qdrant", return_value=True), \
         patch("carta.install.bootstrap._check_ollama", return_value=True), \
         patch("carta.install.bootstrap._register_hooks"), \
         patch("carta.install.bootstrap._create_qdrant_collections"):
        run_bootstrap(tmp_path)
    content = (tmp_path / "CLAUDE.md").read_text()
    assert "Carta is active" in content

def test_bootstrap_creates_namespaced_collections(tmp_path):
    from carta.install.bootstrap import run_bootstrap
    mock_create = MagicMock()
    with patch("carta.install.bootstrap._check_qdrant", return_value=True), \
         patch("carta.install.bootstrap._check_ollama", return_value=True), \
         patch("carta.install.bootstrap._register_hooks"), \
         patch("carta.install.bootstrap._create_qdrant_collections", mock_create):
        run_bootstrap(tmp_path)
    project_name = mock_create.call_args[0][0]
    assert isinstance(project_name, str) and len(project_name) > 0

def test_create_qdrant_collections_uses_namespaced_names():
    from carta.install.bootstrap import _create_qdrant_collections
    with patch("carta.install.bootstrap.requests") as mock_req:
        mock_req.put.return_value.status_code = 200
        _create_qdrant_collections("my-project", "http://localhost:6333")
    called_urls = [call.args[0] for call in mock_req.put.call_args_list]
    assert any("my-project_doc" in url for url in called_urls)
    assert any("my-project_session" in url for url in called_urls)
    assert any("my-project_quirk" in url for url in called_urls)

def test_bootstrap_exits_if_qdrant_unavailable(tmp_path):
    from carta.install.bootstrap import run_bootstrap
    with patch("carta.install.bootstrap._check_qdrant", return_value=False):
        with pytest.raises(SystemExit) as exc_info:
            run_bootstrap(tmp_path)
    assert exc_info.value.code != 0


def test_bootstrap_uses_qdrant_url_from_env(tmp_path):
    from carta.install.bootstrap import run_bootstrap

    custom_url = "http://qdrant.example:7000"
    with patch.dict(os.environ, {"CARTA_QDRANT_URL": custom_url}, clear=False), \
         patch("carta.install.bootstrap._check_qdrant", return_value=True), \
         patch("carta.install.bootstrap._check_ollama", return_value=True), \
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


def test_bootstrap_copytree_ignores_non_runtime_artifacts(tmp_path):
    from carta.install.bootstrap import run_bootstrap

    with patch("carta.install.bootstrap._check_qdrant", return_value=True), \
         patch("carta.install.bootstrap._check_ollama", return_value=True), \
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


def test_register_hooks_sets_executable_and_settings_paths(tmp_path):
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

    settings_path = tmp_path / ".claude" / "settings.json"
    settings = json.loads(settings_path.read_text())
    prompt_cmd = settings["hooks"]["UserPromptSubmit"]
    stop_cmd = settings["hooks"]["Stop"]
    assert "git rev-parse --show-toplevel" in prompt_cmd
    assert "carta-prompt-hook.sh" in prompt_cmd
    assert "git rev-parse --show-toplevel" in stop_cmd
    assert "carta-stop-hook.sh" in stop_cmd
    assert str(tmp_path) not in prompt_cmd, "hook path must not contain absolute project path"
    assert str(tmp_path) not in stop_cmd, "hook path must not contain absolute project path"


def test_install_skills_copies_skill_markdown(tmp_path):
    from carta.install.bootstrap import _install_skills

    _install_skills(tmp_path)

    skills_dir = tmp_path / ".claude" / "skills"
    expected = ["carta-init", "doc-audit", "doc-embed", "doc-search"]
    for skill_name in expected:
        skill_file = skills_dir / skill_name / "SKILL.md"
        assert skill_file.exists()
        assert skill_file.read_text().startswith("# /")
