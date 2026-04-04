import sys

import pytest


# Module cache backup for MCP test isolation
_mcp_module_backup = {}

@pytest.fixture(scope="function")
def isolate_mcp_server():
    """Clear FastMCP global state before each MCP-related test.
    
    FastMCP maintains global state (registered tools, hooks, etc.) that can
    cause test isolation issues. This fixture removes cached server modules
    to force a fresh import for each test.
    
    Usage: Add this fixture to MCP tests that need fresh state:
        def test_something(isolate_mcp_server):
            from carta.mcp.server import carta_search
            ...
    """
    # Remove ALL cached modules in the carta.mcp package to force reimport
    modules_to_clear = [
        "carta.mcp.server",
        "carta.mcp",
    ]
    # Also clear any carta submodules that might import from mcp
    for mod in list(sys.modules.keys()):
        if mod.startswith("carta.mcp"):
            modules_to_clear.append(mod)
    
    # Clear the mocked mcp modules that test_mcp_server.py creates
    for mod in ['mcp', 'mcp.server', 'mcp.server.fastmcp']:
        if mod in sys.modules:
            modules_to_clear.append(mod)
    
    modules_to_clear = list(set(modules_to_clear))  # dedupe
    
    # Backup and clear
    for mod in modules_to_clear:
        if mod in sys.modules:
            _mcp_module_backup[mod] = sys.modules[mod]
            del sys.modules[mod]
    
    yield
    
    # Restore modules after test (cleanup)
    for mod in modules_to_clear:
        if mod in _mcp_module_backup:
            sys.modules[mod] = _mcp_module_backup[mod]
            del _mcp_module_backup[mod]


@pytest.fixture
def minimal_cfg():
    """Canonical minimal config dict for tests across the carta package."""
    return {
        "project_name": "test-project",
        "qdrant_url": "http://localhost:6333",
        "docs_root": "docs/",
        "stale_threshold_days": 30,
        "needs_input_at_audit_count": 3,
        "anchor_doc": "CLAUDE.md",
        "excluded_paths": ["node_modules/", ".venv/", "*.tmp"],
        "contradiction_types": ["version numbers"],
        "search": {"top_n": 5},
        "embed": {
            "reference_docs_path": "docs/reference/",
            "audio_path": "docs/audio/",
            "ollama_url": "http://localhost:11434",
            "ollama_model": "nomic-embed-text:latest",
            "chunking": {"max_tokens": 800, "overlap_fraction": 0.15},
        },
        "proactive_recall": {
            "similarity_threshold": 0.78,
            "max_results": 3,
            "ollama_judge": False,
            "ollama_model": "phi3.5-mini",
        },
        "cross_project_recall": {
            "enabled": False,
            "scope": ["quirk"],
            "require_ollama_judge": True,
            "project_filter": {"mode": "all", "projects": []},
        },
        "modules": {
            "doc_audit": True,
            "doc_embed": True,
            "doc_search": True,
            "session_memory": True,
            "proactive_recall": True,
        },
    }


@pytest.fixture
def minimal_config_yaml():
    """Canonical minimal config as a YAML string for tests that need file content."""
    return (
        "project_name: test-project\n"
        "qdrant_url: http://localhost:6333\n"
    )
