"""Tests for MCP config creation in bootstrap."""
import json
from pathlib import Path
from unittest.mock import patch


def test_create_mcp_configs_creates_both_files(tmp_path):
    """_create_mcp_configs() should create both .mcp.json and .opencode.json."""
    from carta.install.bootstrap import _create_mcp_configs
    
    _create_mcp_configs(tmp_path)
    
    mcp_path = tmp_path / ".mcp.json"
    opencode_path = tmp_path / ".opencode.json"
    
    assert mcp_path.exists(), ".mcp.json should be created"
    assert opencode_path.exists(), ".opencode.json should be created"


def test_mcp_json_has_correct_structure(tmp_path):
    """.mcp.json should have correct structure for Claude Code."""
    from carta.install.bootstrap import _create_mcp_configs
    
    _create_mcp_configs(tmp_path)
    
    mcp_data = json.loads((tmp_path / ".mcp.json").read_text())
    
    assert "mcpServers" in mcp_data
    assert "carta" in mcp_data["mcpServers"]
    assert mcp_data["mcpServers"]["carta"]["command"] == "carta-mcp"


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
