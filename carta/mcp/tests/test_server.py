"""Tests for MCP server scaffold."""
import ast
import json
from pathlib import Path


def test_server_module_has_no_print_calls():
    """MCP server must never call print() — stdout is JSON-RPC only."""
    server_path = Path(__file__).parent.parent / "server.py"
    tree = ast.parse(server_path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id == "print":
                assert False, f"server.py contains print() call at line {node.lineno}"


def test_server_module_has_no_sys_exit():
    """MCP server must never call sys.exit() — use structured errors."""
    server_path = Path(__file__).parent.parent / "server.py"
    tree = ast.parse(server_path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr == "exit":
                if isinstance(func.value, ast.Name) and func.value.id == "sys":
                    assert False, f"server.py contains sys.exit() call at line {node.lineno}"


def test_server_configures_stderr_logging():
    """All logging must go to stderr, not stdout."""
    server_path = Path(__file__).parent.parent / "server.py"
    source = server_path.read_text()
    assert "stream=sys.stderr" in source, "server.py must configure logging to stderr"


def test_mcp_json_exists_and_valid():
    """.mcp.json at project root registers carta-mcp."""
    mcp_json_path = Path(__file__).parent.parent.parent.parent / ".mcp.json"
    assert mcp_json_path.exists(), f".mcp.json not found at {mcp_json_path}"
    data = json.loads(mcp_json_path.read_text())
    assert "mcpServers" in data
    assert "carta" in data["mcpServers"]
    assert data["mcpServers"]["carta"]["command"] == "carta-mcp"


def test_server_main_is_callable():
    """main() must be importable and callable."""
    from carta.mcp.server import main
    assert callable(main)
