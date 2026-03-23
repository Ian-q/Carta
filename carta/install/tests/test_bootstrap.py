import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock
import yaml

def test_bootstrap_creates_carta_dir(tmp_path):
    from carta.install.bootstrap import run_bootstrap
    with patch("install.bootstrap._check_qdrant", return_value=True), \
         patch("install.bootstrap._check_ollama", return_value=True), \
         patch("install.bootstrap._register_hooks"), \
         patch("install.bootstrap._create_qdrant_collections"):
        run_bootstrap(tmp_path)
    assert (tmp_path / ".carta").exists()
    assert (tmp_path / ".carta" / "config.yaml").exists()

def test_bootstrap_config_has_all_fields(tmp_path):
    from carta.install.bootstrap import run_bootstrap
    with patch("install.bootstrap._check_qdrant", return_value=True), \
         patch("install.bootstrap._check_ollama", return_value=True), \
         patch("install.bootstrap._register_hooks"), \
         patch("install.bootstrap._create_qdrant_collections"):
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
    with patch("install.bootstrap._check_qdrant", return_value=True), \
         patch("install.bootstrap._check_ollama", return_value=True), \
         patch("install.bootstrap._register_hooks"), \
         patch("install.bootstrap._create_qdrant_collections"):
        run_bootstrap(tmp_path)
    content = (tmp_path / ".gitignore").read_text()
    assert ".carta/scan-results.json" in content

def test_bootstrap_appends_claude_md(tmp_path):
    (tmp_path / "CLAUDE.md").write_text("# My Project\n")
    from carta.install.bootstrap import run_bootstrap
    with patch("install.bootstrap._check_qdrant", return_value=True), \
         patch("install.bootstrap._check_ollama", return_value=True), \
         patch("install.bootstrap._register_hooks"), \
         patch("install.bootstrap._create_qdrant_collections"):
        run_bootstrap(tmp_path)
    content = (tmp_path / "CLAUDE.md").read_text()
    assert "Carta is active" in content

def test_bootstrap_creates_namespaced_collections(tmp_path):
    from carta.install.bootstrap import run_bootstrap
    mock_create = MagicMock()
    with patch("install.bootstrap._check_qdrant", return_value=True), \
         patch("install.bootstrap._check_ollama", return_value=True), \
         patch("install.bootstrap._register_hooks"), \
         patch("install.bootstrap._create_qdrant_collections", mock_create):
        run_bootstrap(tmp_path)
    project_name = mock_create.call_args[0][0]
    assert isinstance(project_name, str) and len(project_name) > 0

def test_create_qdrant_collections_uses_namespaced_names():
    from carta.install.bootstrap import _create_qdrant_collections
    with patch("install.bootstrap.requests") as mock_req:
        mock_req.put.return_value.status_code = 200
        _create_qdrant_collections("my-project", "http://localhost:6333")
    called_urls = [call.args[0] for call in mock_req.put.call_args_list]
    assert any("my-project:doc" in url for url in called_urls)
    assert any("my-project:session" in url for url in called_urls)
    assert any("my-project:quirk" in url for url in called_urls)

def test_bootstrap_exits_if_qdrant_unavailable(tmp_path):
    from carta.install.bootstrap import run_bootstrap
    with patch("install.bootstrap._check_qdrant", return_value=False):
        with pytest.raises(SystemExit) as exc_info:
            run_bootstrap(tmp_path)
    assert exc_info.value.code != 0
