import json
import sys
import pytest
from pathlib import Path
from unittest.mock import patch

from carta.install.bootstrap import (
    run_bootstrap,
    _update_gitignore,
    _register_hooks,
)


# --- BOOT-01 ---

def test_boot01_residue_causes_exit(tmp_path):
    """run_bootstrap() calls sys.exit(1) when _remove_plugin_cache returns False."""
    with (
        patch("carta.install.bootstrap._check_qdrant", return_value=True),
        patch("carta.install.bootstrap._check_ollama", return_value=True),
        patch("carta.install.bootstrap._write_config"),
        patch("carta.install.bootstrap._register_hooks"),
        patch("carta.install.bootstrap._remove_plugin_cache", return_value=False),
        patch("carta.install.bootstrap._create_qdrant_collections", return_value=True),
        patch("carta.install.bootstrap._update_gitignore"),
        patch("carta.install.bootstrap.shutil.copytree"),
        patch("carta.install.bootstrap._append_claude_md"),
        patch("carta.install.bootstrap._install_skills"),
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

def test_boot03_hook_cmd_uses_exec_quoting(tmp_path):
    """Hook command uses exec with double-quoted $(git rev-parse --show-toplevel) path."""
    hooks_src = Path(__file__).parent.parent / "hooks"
    if not hooks_src.exists():
        pytest.skip("hooks/ source directory not present")

    _register_hooks(tmp_path)
    settings = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    hooks = settings["hooks"]
    for hook_name in ("UserPromptSubmit", "Stop"):
        cmd = hooks[hook_name][0]["hooks"][0]["command"]
        assert "exec" in cmd, f"{hook_name}: exec missing from cmd"
        assert '"$(git rev-parse --show-toplevel)' in cmd, f"{hook_name}: unquoted path"
