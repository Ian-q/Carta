"""Tests for MCP server scaffold and tool handlers."""
import ast
import concurrent.futures
import json
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock, Mock

import pytest

# Store the real module for reference
_REAL_SERVER_MODULE = None

def _get_server_module():
    """Import and return the carta.mcp.server module, clearing cache first."""
    global _REAL_SERVER_MODULE
    
    # Clear any cached carta.mcp modules
    modules_to_clear = [k for k in sys.modules.keys() if k.startswith('carta.mcp')]
    # Also clear the mocked mcp modules from test_mcp_server.py
    for mod in ['mcp', 'mcp.server', 'mcp.server.fastmcp']:
        if mod in sys.modules:
            modules_to_clear.append(mod)
    
    for mod in modules_to_clear:
        del sys.modules[mod]
    
    # Now import fresh
    from carta.mcp import server
    _REAL_SERVER_MODULE = server
    return server


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------

_TEST_CFG = {
    "project_name": "test-project",
    "qdrant_url": "http://localhost:6333",
    "embed": {
        "ollama_url": "http://localhost:11434",
        "ollama_model": "nomic-embed-text:latest",
        "chunking": {"max_tokens": 800, "overlap_fraction": 0.15},
    },
    "search": {"top_n": 5},
    "cross_project_recall": {
        "enabled": False,
        "project_filter": {"mode": "all", "projects": []},
    },
}

_MOCK_REPO_ROOT = Path("/tmp/test-project")

# Helper to check if running in CI
def _running_in_ci():
    import os
    return os.environ.get("CI", "") == "true"


# ---------------------------------------------------------------------------
# Existing scaffold tests (must remain passing)
# ---------------------------------------------------------------------------

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
    """.mcp.json at project root registers carta-mcp.
    
    Note: This test runs from source checkout where .mcp.json should exist.
    Skip if running from installed package without a .mcp.json.
    """
    mcp_json_path = Path(__file__).parent.parent.parent.parent / ".mcp.json"
    if not mcp_json_path.exists():
        pytest.skip(f".mcp.json not found at {mcp_json_path} - run from project root")
    data = json.loads(mcp_json_path.read_text())
    assert "mcpServers" in data
    assert "carta" in data["mcpServers"]
    assert data["mcpServers"]["carta"]["command"] == "carta-mcp"


def test_server_main_is_callable():
    """main() must be importable and callable."""
    server = _get_server_module()
    assert callable(server.main)


# ---------------------------------------------------------------------------
# carta_search tests
# ---------------------------------------------------------------------------

def _patch_server_functions(server_module, **kwargs):
    """Helper to patch functions in the server module.
    
    This patches the module's __dict__ directly, which is what decorated
    functions use for global lookups.
    """
    patches = {}
    originals = {}
    
    for name, mock_or_value in kwargs.items():
        if isinstance(mock_or_value, Mock):
            # It's already a Mock/MagicMock, use it directly
            mock = mock_or_value
        elif callable(mock_or_value) and not isinstance(mock_or_value, (Mock, type)):
            # It's a function/side_effect callable (not a Mock and not a class)
            mock = MagicMock(side_effect=mock_or_value)
        else:
            # It's a return value
            mock = MagicMock(return_value=mock_or_value)
        
        originals[name] = server_module.__dict__.get(name)
        server_module.__dict__[name] = mock
        patches[name] = mock
    
    return patches, originals


def _restore_server_functions(server_module, originals):
    """Restore original functions after test."""
    for name, original in originals.items():
        if original is not None:
            server_module.__dict__[name] = original
        else:
            # Wasn't there before, remove it
            server_module.__dict__.pop(name, None)


def test_carta_search_returns_scored_results():
    """Happy path: returns list of dicts with score, source, excerpt keys."""
    server = _get_server_module()
    carta_search = server.carta_search
    
    mock_results = [
        {"score": 0.95, "source": "docs/spec.pdf", "excerpt": "some text here"},
        {"score": 0.88, "source": "docs/ref.pdf", "excerpt": "other text"},
        {"score": 0.72, "source": "docs/guide.pdf", "excerpt": "guide text"},
    ]
    
    patches, originals = _patch_server_functions(
        server,
        _load_cfg=_TEST_CFG,
        _repo_root_from_cfg=_MOCK_REPO_ROOT,
        get_search_collections=["test-project_doc"],
        _run_search_collection=mock_results
    )
    
    try:
        result = carta_search("test query")
        assert isinstance(result, list)
        assert len(result) == 3
        for item in result:
            assert "score" in item
            assert "source" in item
            assert "excerpt" in item
    finally:
        _restore_server_functions(server, originals)


def test_carta_search_truncates_excerpt():
    """Excerpts longer than 300 chars are truncated to 300."""
    server = _get_server_module()
    carta_search = server.carta_search
    
    long_excerpt = "x" * 500
    mock_results = [{"score": 0.9, "source": "a.pdf", "excerpt": long_excerpt}]
    
    patches, originals = _patch_server_functions(
        server,
        _load_cfg=_TEST_CFG,
        _repo_root_from_cfg=_MOCK_REPO_ROOT,
        get_search_collections=["test-project_doc"],
        _run_search_collection=mock_results
    )
    
    try:
        result = carta_search("query")
        assert isinstance(result, list)
        assert len(result[0]["excerpt"]) <= 300
    finally:
        _restore_server_functions(server, originals)


def test_carta_search_respects_top_k():
    """top_k parameter limits result count."""
    server = _get_server_module()
    carta_search = server.carta_search
    
    mock_results = [
        {"score": 0.9 - i * 0.1, "source": f"doc{i}.pdf", "excerpt": "text"}
        for i in range(5)
    ]
    
    patches, originals = _patch_server_functions(
        server,
        _load_cfg=_TEST_CFG,
        _repo_root_from_cfg=_MOCK_REPO_ROOT,
        get_search_collections=["test-project_doc"],
        _run_search_collection=mock_results
    )
    
    try:
        result = carta_search("query", top_k=2)
        assert len(result) == 2
    finally:
        _restore_server_functions(server, originals)


def test_carta_search_rounds_score():
    """Scores are rounded to 4 decimal places."""
    server = _get_server_module()
    carta_search = server.carta_search
    
    mock_results = [{"score": 0.123456789, "source": "a.pdf", "excerpt": "text"}]
    
    patches, originals = _patch_server_functions(
        server,
        _load_cfg=_TEST_CFG,
        _repo_root_from_cfg=_MOCK_REPO_ROOT,
        get_search_collections=["test-project_doc"],
        _run_search_collection=mock_results
    )
    
    try:
        result = carta_search("query")
        assert result[0]["score"] == round(0.123456789, 4)
    finally:
        _restore_server_functions(server, originals)


def test_carta_search_service_unavailable():
    """RuntimeError from _run_search_collection is skipped, not propagated."""
    server = _get_server_module()
    carta_search = server.carta_search
    
    def raise_runtime_error(*args, **kwargs):
        raise RuntimeError("Qdrant down")
    
    patches, originals = _patch_server_functions(
        server,
        _load_cfg=_TEST_CFG,
        _repo_root_from_cfg=_MOCK_REPO_ROOT,
        get_search_collections=["test-project_doc"],
        _run_search_collection=raise_runtime_error
    )
    
    try:
        result = carta_search("query")
        # When all collections fail, we return empty results (not an error)
        assert isinstance(result, list)
        assert len(result) == 0
    finally:
        _restore_server_functions(server, originals)


def test_carta_search_config_not_found():
    """FileNotFoundError from _load_cfg returns service_unavailable error dict."""
    server = _get_server_module()
    carta_search = server.carta_search
    
    def raise_not_found(*args, **kwargs):
        raise FileNotFoundError("no config")
    
    patches, originals = _patch_server_functions(
        server,
        _load_cfg=raise_not_found,
        _repo_root_from_cfg=_MOCK_REPO_ROOT,
    )
    
    try:
        result = carta_search("query")
        assert isinstance(result, dict)
        assert result["error"] == "service_unavailable"
        assert "detail" in result
    finally:
        _restore_server_functions(server, originals)


def test_carta_search_with_repo_scope():
    """scope='repo' uses get_search_collections with 'repo' scope."""
    server = _get_server_module()
    carta_search = server.carta_search
    
    mock_results = [{"score": 0.9, "source": "a.pdf", "excerpt": "text"}]
    
    # Track calls
    collections_calls = []
    def mock_get_collections(cfg, scope):
        collections_calls.append((cfg, scope))
        return ["test-project_doc"]
    
    patches, originals = _patch_server_functions(
        server,
        _load_cfg=_TEST_CFG,
        _repo_root_from_cfg=_MOCK_REPO_ROOT,
        get_search_collections=mock_get_collections,
        _run_search_collection=mock_results
    )
    
    try:
        result = carta_search("query", scope="repo")
        assert len(collections_calls) == 1
        assert collections_calls[0] == (_TEST_CFG, "repo")
        assert isinstance(result, list)
    finally:
        _restore_server_functions(server, originals)


def test_carta_search_with_global_scope():
    """scope='global' uses get_search_collections with 'global' scope."""
    server = _get_server_module()
    carta_search = server.carta_search
    
    mock_results = [{"score": 0.9, "source": "a.pdf", "excerpt": "text"}]
    
    collections_calls = []
    def mock_get_collections(cfg, scope):
        collections_calls.append((cfg, scope))
        return ["carta_global_doc"]
    
    patches, originals = _patch_server_functions(
        server,
        _load_cfg=_TEST_CFG,
        _repo_root_from_cfg=_MOCK_REPO_ROOT,
        get_search_collections=mock_get_collections,
        _run_search_collection=mock_results
    )
    
    try:
        result = carta_search("query", scope="global")
        assert len(collections_calls) == 1
        assert collections_calls[0] == (_TEST_CFG, "global")
        assert isinstance(result, list)
    finally:
        _restore_server_functions(server, originals)


def test_carta_search_invalid_scope():
    """Invalid scope returns invalid_request error."""
    server = _get_server_module()
    carta_search = server.carta_search
    
    def raise_value_error(*args, **kwargs):
        raise ValueError("Invalid scope: invalid")
    
    patches, originals = _patch_server_functions(
        server,
        _load_cfg=_TEST_CFG,
        _repo_root_from_cfg=_MOCK_REPO_ROOT,
        get_search_collections=raise_value_error,
    )
    
    try:
        result = carta_search("query", scope="invalid")
        assert isinstance(result, dict)
        assert result["error"] == "invalid_request"
    finally:
        _restore_server_functions(server, originals)


def test_carta_search_merges_results_from_multiple_collections():
    """Results from multiple collections are merged and sorted by score."""
    server = _get_server_module()
    carta_search = server.carta_search
    
    def mock_search_side_effect(query, cfg, coll_name, top_n):
        if coll_name == "test-project_doc":
            return [{"score": 0.7, "source": "project.pdf", "excerpt": "project text"}]
        elif coll_name == "other-project_doc":
            return [{"score": 0.9, "source": "other.pdf", "excerpt": "other text"}]
        return []
    
    patches, originals = _patch_server_functions(
        server,
        _load_cfg=_TEST_CFG,
        _repo_root_from_cfg=_MOCK_REPO_ROOT,
        get_search_collections=["test-project_doc", "other-project_doc"],
        _run_search_collection=mock_search_side_effect
    )
    
    try:
        result = carta_search("query", scope="shared")
        
        assert isinstance(result, list)
        assert len(result) == 2
        # Results should be sorted by score descending
        assert result[0]["score"] == 0.9
        assert result[1]["score"] == 0.7
    finally:
        _restore_server_functions(server, originals)


# ---------------------------------------------------------------------------
# carta_embed tests
# ---------------------------------------------------------------------------

def test_carta_embed_success():
    """Happy path: returns {"status": "ok", "chunks": N, "scope": "file"}."""
    server = _get_server_module()
    carta_embed = server.carta_embed
    
    patches, originals = _patch_server_functions(
        server,
        _load_cfg=_TEST_CFG,
        _repo_root_from_cfg=_MOCK_REPO_ROOT,
        run_embed_file={"status": "ok", "chunks": 5}
    )
    
    try:
        result = carta_embed("/tmp/test.pdf")
        assert result == {"status": "ok", "chunks": 5, "scope": "file"}
    finally:
        _restore_server_functions(server, originals)


def test_carta_embed_skipped():
    """Already-current file returns skipped dict."""
    server = _get_server_module()
    carta_embed = server.carta_embed
    
    skip_result = {"status": "skipped", "reason": "already embedded, file unchanged"}
    
    patches, originals = _patch_server_functions(
        server,
        _load_cfg=_TEST_CFG,
        _repo_root_from_cfg=_MOCK_REPO_ROOT,
        run_embed_file=skip_result
    )
    
    try:
        result = carta_embed("/tmp/test.pdf")
        assert result == skip_result
    finally:
        _restore_server_functions(server, originals)


def test_carta_embed_file_not_found():
    """FileNotFoundError returns file_not_found error dict."""
    server = _get_server_module()
    carta_embed = server.carta_embed
    
    def raise_not_found(*args, **kwargs):
        raise FileNotFoundError("no such file")
    
    patches, originals = _patch_server_functions(
        server,
        _load_cfg=_TEST_CFG,
        _repo_root_from_cfg=_MOCK_REPO_ROOT,
        run_embed_file=raise_not_found
    )
    
    try:
        result = carta_embed("/tmp/missing.pdf")
        assert isinstance(result, dict)
        assert result["error"] == "file_not_found"
        assert "detail" in result
    finally:
        _restore_server_functions(server, originals)


def test_carta_embed_timeout():
    """TimeoutError returns timeout error dict."""
    server = _get_server_module()
    carta_embed = server.carta_embed
    
    def raise_timeout(*args, **kwargs):
        raise concurrent.futures.TimeoutError()
    
    patches, originals = _patch_server_functions(
        server,
        _load_cfg=_TEST_CFG,
        _repo_root_from_cfg=_MOCK_REPO_ROOT,
        run_embed_file=raise_timeout
    )
    
    try:
        result = carta_embed("/tmp/test.pdf")
        assert isinstance(result, dict)
        assert result["error"] == "timeout"
        assert "detail" in result
    finally:
        _restore_server_functions(server, originals)


def test_carta_embed_service_unavailable():
    """RuntimeError returns service_unavailable error dict."""
    server = _get_server_module()
    carta_embed = server.carta_embed
    
    def raise_runtime(*args, **kwargs):
        raise RuntimeError("Cannot connect")
    
    patches, originals = _patch_server_functions(
        server,
        _load_cfg=_TEST_CFG,
        _repo_root_from_cfg=_MOCK_REPO_ROOT,
        run_embed_file=raise_runtime
    )
    
    try:
        result = carta_embed("/tmp/test.pdf")
        assert isinstance(result, dict)
        assert result["error"] == "service_unavailable"
        assert "detail" in result
    finally:
        _restore_server_functions(server, originals)


def test_carta_embed_force_passed():
    """force=True is forwarded to run_embed_file."""
    server = _get_server_module()
    carta_embed = server.carta_embed
    
    mock_embed = MagicMock(return_value={"status": "ok", "chunks": 3})
    
    patches, originals = _patch_server_functions(
        server,
        _load_cfg=_TEST_CFG,
        _repo_root_from_cfg=_MOCK_REPO_ROOT,
        run_embed_file=mock_embed
    )
    
    try:
        carta_embed("/tmp/test.pdf", force=True)
        call_kwargs = mock_embed.call_args
        assert call_kwargs.kwargs.get("force") is True or (
            len(call_kwargs.args) >= 3 and call_kwargs.args[2] is True
        )
    finally:
        _restore_server_functions(server, originals)


# ---------------------------------------------------------------------------
# carta_scan tests
# ---------------------------------------------------------------------------

def test_carta_scan_returns_pending_and_drift():
    """Happy path: returns dict with pending and drift path lists."""
    server = _get_server_module()
    carta_scan = server.carta_scan
    
    pending_issues = [{"type": "embed_induction_needed", "doc": "a.pdf"}]
    drift_issues = [{"type": "embed_drift", "doc": "b.pdf"}]
    
    patches, originals = _patch_server_functions(
        server,
        _load_cfg=_TEST_CFG,
        _repo_root_from_cfg=_MOCK_REPO_ROOT,
        check_embed_induction_needed=pending_issues,
        check_embed_drift=drift_issues
    )
    
    try:
        result = carta_scan()
        assert result == {"pending": ["a.pdf"], "drift": ["b.pdf"]}
    finally:
        _restore_server_functions(server, originals)


def test_carta_scan_empty():
    """No pending or drift files returns empty arrays."""
    server = _get_server_module()
    carta_scan = server.carta_scan
    
    patches, originals = _patch_server_functions(
        server,
        _load_cfg=_TEST_CFG,
        _repo_root_from_cfg=_MOCK_REPO_ROOT,
        check_embed_induction_needed=[],
        check_embed_drift=[]
    )
    
    try:
        result = carta_scan()
        assert result == {"pending": [], "drift": []}
    finally:
        _restore_server_functions(server, originals)


def test_carta_scan_config_not_found():
    """FileNotFoundError from _load_cfg returns service_unavailable error dict."""
    server = _get_server_module()
    carta_scan = server.carta_scan
    
    def raise_not_found(*args, **kwargs):
        raise FileNotFoundError("no config")
    
    patches, originals = _patch_server_functions(
        server,
        _load_cfg=raise_not_found,
    )
    
    try:
        result = carta_scan()
        assert isinstance(result, dict)
        assert result["error"] == "service_unavailable"
        assert "detail" in result
    finally:
        _restore_server_functions(server, originals)


# ---------------------------------------------------------------------------
# Visual search tests (Issue #1)
# ---------------------------------------------------------------------------

def test_carta_search_includes_visual_results():
    """Visual collections are searched and results include type='visual'."""
    server = _get_server_module()
    carta_search = server.carta_search
    
    text_results = [
        {"score": 0.85, "source": "docs/spec.pdf", "excerpt": "text content"},
    ]
    visual_results = [
        {
            "score": 0.92,
            "source": "docs/datasheet.pdf (page 5)",
            "excerpt": "Visual match from page 5",
            "type": "visual",
            "image_b64": "base64data",
            "page_num": 5,
        },
    ]
    
    def mock_search_collection(*args, **kwargs):
        return text_results
    
    def mock_search_visual(*args, **kwargs):
        return visual_results
    
    patches, originals = _patch_server_functions(
        server,
        _load_cfg=_TEST_CFG,
        _repo_root_from_cfg=_MOCK_REPO_ROOT,
        get_search_collections=["test-project_doc", "test-project_visual"],
        _run_search_collection=mock_search_collection,
        _run_search_visual_collection=mock_search_visual
    )
    
    try:
        result = carta_search("test query")
        
        assert isinstance(result, list)
        assert len(result) == 2
        # Results should be sorted by score (visual 0.92 first, then text 0.85)
        assert result[0]["score"] == 0.92
        assert result[0]["type"] == "visual"
        assert result[0]["image_b64"] == "base64data"
    finally:
        _restore_server_functions(server, originals)


def test_run_search_visual_collection_skips_when_colpali_unavailable():
    """Visual search returns empty list when ColPali is not installed."""
    from carta.mcp.server import _run_search_visual_collection
    from carta.embed import colpali as colpali_module
    with patch.object(colpali_module, "is_colpali_available", return_value=False):
        result = _run_search_visual_collection("query", _TEST_CFG, "test_visual", 5, _MOCK_REPO_ROOT)
    assert result == []


def test_load_image_as_base64_returns_empty_on_missing_file():
    """_load_image_as_base64 returns empty string when PNG doesn't exist."""
    from carta.mcp.server import _load_image_as_base64
    result = _load_image_as_base64(Path("/nonexistent/path.png"))
    assert result == ""
