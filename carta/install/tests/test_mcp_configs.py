"""Tests for MCP config creation in bootstrap."""
import json
from pathlib import Path
from unittest.mock import patch


def test_create_mcp_configs_creates_only_opencode_file(tmp_path):
    """_create_mcp_configs() should create .opencode.json but NOT .mcp.json.

    Claude Code MCP registration is now plugin-native (plugin-root .mcp.json);
    bootstrap must not create a project-level .mcp.json to avoid conflicts for
    marketplace users.
    """
    from carta.install.bootstrap import _create_mcp_configs

    _create_mcp_configs(tmp_path)

    mcp_path = tmp_path / ".mcp.json"
    opencode_path = tmp_path / ".opencode.json"

    assert not mcp_path.exists(), ".mcp.json must NOT be created by bootstrap (plugin-native handles this)"
    assert opencode_path.exists(), ".opencode.json should be created for OpenCode compatibility"


def test_mcp_json_not_written_by_bootstrap(tmp_path):
    """.mcp.json must not be written — Claude Code registration is plugin-native."""
    from carta.install.bootstrap import _create_mcp_configs

    _create_mcp_configs(tmp_path)

    assert not (tmp_path / ".mcp.json").exists(), \
        ".mcp.json must not be written by _create_mcp_configs (plugin-native handles Claude Code MCP)"


def test_opencode_json_has_correct_structure(tmp_path):
    """.opencode.json should have correct structure for OpenCode."""
    from carta.install.bootstrap import _create_mcp_configs
    
    _create_mcp_configs(tmp_path)
    
    opencode_data = json.loads((tmp_path / ".opencode.json").read_text())
    
    assert "$schema" in opencode_data
    assert "mcp" in opencode_data
    assert "carta" in opencode_data["mcp"]
    assert opencode_data["mcp"]["carta"]["type"] == "local"
    assert opencode_data["mcp"]["carta"]["enabled"] is True
