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


def test_carta_search_skips_missing_collection():
    """CollectionMissing from _run_search_collection is skipped, not propagated — other
    collections may still answer."""
    server = _get_server_module()
    carta_search = server.carta_search

    def raise_collection_missing(*args, **kwargs):
        raise server.CollectionMissing("test-project_doc")

    patches, originals = _patch_server_functions(
        server,
        _load_cfg=_TEST_CFG,
        _repo_root_from_cfg=_MOCK_REPO_ROOT,
        get_search_collections=["test-project_doc"],
        _run_search_collection=raise_collection_missing
    )

    try:
        result = carta_search("query")
        # A missing collection is not an error — when all collections are missing, we
        # return empty results (not an error).
        assert isinstance(result, list)
        assert len(result) == 0
    finally:
        _restore_server_functions(server, originals)


def test_carta_search_reports_query_failure_instead_of_empty_list():
    """QdrantQueryError from _run_search_collection propagates as an error dict rather
    than being swallowed into an empty list — it is identical for every collection (the
    live 400-on-named-vector bug this task fixes), so silently returning [] would mislead
    the agent into thinking nothing is embedded."""
    server = _get_server_module()
    carta_search = server.carta_search

    def raise_query_error(*args, **kwargs):
        raise server.QdrantQueryError("Qdrant search failed for test-project_doc: 400 Wrong input")

    patches, originals = _patch_server_functions(
        server,
        _load_cfg=_TEST_CFG,
        _repo_root_from_cfg=_MOCK_REPO_ROOT,
        get_search_collections=["test-project_doc"],
        _run_search_collection=raise_query_error
    )

    try:
        result = carta_search("query")
        assert isinstance(result, dict)
        assert result.get("error") == "service_unavailable"
        assert "Qdrant search failed" in result.get("detail", "")
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
    """Results from multiple collections are merged by RANK, not by raw score.

    Cross-collection ordering is Reciprocal Rank Fusion, matching `run_search`
    (#120). Both hits here are rank 0 in their own collection, so they tie on
    fused score and the tie breaks toward the collection listed first — the 0.7
    hit leads the 0.9 one. That is deliberate: scores from different collections
    are not a ranking signal, only ranks within a collection are.
    """
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
        # Rank-based: both are rank 0, tie breaks toward the earlier collection.
        assert [r["source"] for r in result] == ["project.pdf", "other.pdf"]
        # Both survive the merge; `score` keeps its intra-collection value and is
        # explicitly NOT the ordering signal.
        assert {r["score"] for r in result} == {0.7, 0.9}
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
    """Visual collections are searched and their hits survive the merge intact.

    The text hit leads: MaxSim 0.92 does not outrank cosine 0.85 by magnitude,
    because ordering is by rank and both are rank 0 in their own lane (#120).
    The visual hit must still be present and still carry its `image_b64` — the
    merge reorders hits, it never strips the image passthrough.
    """
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
        # Text leads on rank; MaxSim magnitude no longer buys slot 0.
        assert result[0]["source"] == "docs/spec.pdf"
        assert result[0].get("type", "text") == "text"
        # The visual hit survives, with its image payload intact.
        visual = [r for r in result if r.get("type") == "visual"]
        assert len(visual) == 1
        assert visual[0]["image_b64"] == "base64data"
        assert visual[0]["score"] == 0.92
    finally:
        _restore_server_functions(server, originals)


def test_carta_search_maxsim_visual_does_not_bury_text():
    """ColPali MaxSim (~10-40) must not outrank dense cosine (~0.5) on the MCP path.

    The two producers stamp `score` from incomparable metrics, so a single
    score-descending sort puts every visual page above every text chunk. Merge by
    rank (RRF), never by raw score — the fix `run_search` already carries (#36).
    """
    server = _get_server_module()

    text_results = [
        {"score": 0.55 - i * 0.01, "source": f"docs/t{i}.md", "excerpt": "t"}
        for i in range(5)
    ]
    visual_results = [
        {
            "score": 34.0 - i,
            "source": f"docs/v{i}.pdf (page {i})",
            "excerpt": "v",
            "type": "visual",
            "image_b64": "b",
            "page_num": i,
        }
        for i in range(5)
    ]

    patches, originals = _patch_server_functions(
        server,
        _load_cfg=_TEST_CFG,
        _repo_root_from_cfg=_MOCK_REPO_ROOT,
        get_search_collections=["test-project_doc", "test-project_visual"],
        _run_search_collection=lambda *a, **k: text_results,
        _run_search_visual_collection=lambda *a, **k: visual_results,
    )

    try:
        result = server.carta_search("query", top_k=5)
        types = [r.get("type", "text") for r in result]
        assert result[0]["source"] == "docs/t0.md", (
            f"top text hit lost slot 0 to a MaxSim score: {result[0]}"
        )
        assert types.count("text") >= 2, f"text crowded out by MaxSim: {types}"
    finally:
        _restore_server_functions(server, originals)


def test_carta_search_does_not_over_fetch_any_lane():
    """No lane is fetched deeper than the caller asked for.

    The fused order is rank-major and `_apply_visual_cap` admits non-visual hits
    unconditionally until the pool is full, so `top_k` per lane already supplies
    every hit that can be admitted — fetching deeper is output-identical and pure
    cost. Each text hit carries its chunk text; each visual hit costs a page render
    plus a base64 encode, paid whether or not the merge keeps it.
    """
    server = _get_server_module()

    seen = {}

    def record_text(query, cfg, coll_name, top_n):
        seen["text"] = top_n
        return [{"score": 0.5, "source": "docs/a.md", "excerpt": "t"}]

    def record_visual(query, cfg, coll_name, top_n, repo_root):
        seen["visual"] = top_n
        return [{"score": 30.0, "source": "docs/b.pdf (page 1)", "excerpt": "v",
                 "type": "visual", "image_b64": "b", "page_num": 1}]

    patches, originals = _patch_server_functions(
        server,
        _load_cfg=_TEST_CFG,
        _repo_root_from_cfg=_MOCK_REPO_ROOT,
        get_search_collections=["test-project_doc", "test-project_visual"],
        _run_search_collection=record_text,
        _run_search_visual_collection=record_visual,
    )

    try:
        server.carta_search("query", top_k=5)
        assert seen == {"text": 5, "visual": 5}, (
            f"a lane was fetched deeper than top_k, which cannot change the "
            f"output and costs payload/renders per query: {seen}"
        )
    finally:
        _restore_server_functions(server, originals)


def test_carta_search_keeps_one_visual_slot_at_small_top_k():
    """A small agent-chosen top_k must not silently suppress the visual lane entirely.

    The cap is round(visual_max_ratio * limit), so at the shipped 0.2 it is 0 for
    top_k 1-2 — `carta_search(q, top_k=2)` would return no page image at all while
    any text hit exists. The ratio was swept against the CLI's 30-deep pool at
    top_n=5 (where it lands on 1); it is a borrowed number here, not a calibrated
    one, so the visual lane keeps a floor of one slot when it has hits to offer.
    """
    server = _get_server_module()

    cfg = dict(_TEST_CFG)
    cfg["search"] = {"top_n": 5, "fusion": {"visual_max_ratio": 0.2}}

    text_results = [
        {"score": 0.55 - i * 0.01, "source": f"docs/t{i}.md", "excerpt": "t"}
        for i in range(5)
    ]
    visual_results = [
        {
            "score": 34.0 - i,
            "source": f"docs/v{i}.pdf (page {i})",
            "excerpt": "v",
            "type": "visual",
            "image_b64": "b",
            "page_num": i,
        }
        for i in range(5)
    ]

    patches, originals = _patch_server_functions(
        server,
        _load_cfg=cfg,
        _repo_root_from_cfg=_MOCK_REPO_ROOT,
        get_search_collections=["test-project_doc", "test-project_visual"],
        _run_search_collection=lambda *a, **k: text_results,
        _run_search_visual_collection=lambda *a, **k: visual_results,
    )

    try:
        result = server.carta_search("query", top_k=2)
        types = [r.get("type", "text") for r in result]
        assert types.count("visual") == 1, (
            f"visual lane suppressed entirely at top_k=2 (cap rounds to 0): {types}"
        )
        assert types.count("text") == 1, f"expected one text hit alongside: {types}"
    finally:
        _restore_server_functions(server, originals)


def test_carta_search_visual_cap_still_binds_at_normal_depth():
    """The floor lifts a zero cap to one; it must not disable the cap itself.

    At top_k=5 the shipped 0.2 gives a cap of 1, and RRF's ~1:1 interleave would
    otherwise fill half the pool with page images.
    """
    server = _get_server_module()

    cfg = dict(_TEST_CFG)
    cfg["search"] = {"top_n": 5, "fusion": {"visual_max_ratio": 0.2}}

    text_results = [
        {"score": 0.55 - i * 0.01, "source": f"docs/t{i}.md", "excerpt": "t"}
        for i in range(10)
    ]
    visual_results = [
        {
            "score": 34.0 - i,
            "source": f"docs/v{i}.pdf (page {i})",
            "excerpt": "v",
            "type": "visual",
            "image_b64": "b",
            "page_num": i,
        }
        for i in range(10)
    ]

    patches, originals = _patch_server_functions(
        server,
        _load_cfg=cfg,
        _repo_root_from_cfg=_MOCK_REPO_ROOT,
        get_search_collections=["test-project_doc", "test-project_visual"],
        _run_search_collection=lambda *a, **k: text_results,
        _run_search_visual_collection=lambda *a, **k: visual_results,
    )

    try:
        result = server.carta_search("query", top_k=5)
        types = [r.get("type", "text") for r in result]
        assert types.count("visual") == 1, f"cap did not bind at top_k=5: {types}"
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


def test_carta_embed_returns_busy_when_lock_held(tmp_path):
    """carta_embed must refuse with a busy error (not race the cleanup-delete) when
    another live writer holds the embed lock — e.g. a CLI ET-embed (audit CA-5/12)."""
    import os
    from unittest.mock import patch
    server = _get_server_module()

    # Lock held by THIS (live) process.
    lock = tmp_path / ".carta" / "embed.lock"
    lock.parent.mkdir(parents=True)
    lock.write_text(str(os.getpid()))

    with patch.object(server, "_load_cfg", return_value=_TEST_CFG), \
         patch.object(server, "_repo_root_from_cfg", return_value=tmp_path), \
         patch.object(server, "run_embed") as mock_run_embed:
        result = server.carta_embed("all")

    assert result.get("error") == "busy"
    mock_run_embed.assert_not_called()  # must NOT write while another holds the lock


# ---------------------------------------------------------------------------
# Contract-faithful Qdrant fake: using= on named-vector collections (retrieval-repair #1)
# ---------------------------------------------------------------------------
from qdrant_client.http.exceptions import UnexpectedResponse

from carta.mcp.tests.fakes import ContractFakeQdrant, NamedVectorContractError


def _cfg():
    return {
        "project_name": "p",
        "qdrant_url": "http://localhost:6333",
        "embed": {"ollama_url": "http://localhost:11434",
                  "ollama_model": "nomic-embed-text:latest"},
    }


def _point(path="docs/a.md", text="hello"):
    p = MagicMock()
    p.score = 0.9
    p.payload = {"file_path": path, "text": text}
    return p


def test_search_named_vector_collection_returns_hits(monkeypatch):
    server = _get_server_module()
    fake = ContractFakeQdrant(named_vectors=True, points=[_point()])
    monkeypatch.setattr(server, "QdrantClient", lambda **kw: fake)
    monkeypatch.setattr(server, "get_embedding", lambda *a, **k: [0.0] * 768)

    hits = server._run_search_collection("q", _cfg(), "ET-embed_doc", 5)

    assert len(hits) == 1
    assert fake.calls[-1]["using"] == "dense"


def test_search_legacy_unnamed_collection_omits_using(monkeypatch):
    server = _get_server_module()
    fake = ContractFakeQdrant(named_vectors=False, points=[_point()])
    monkeypatch.setattr(server, "QdrantClient", lambda **kw: fake)
    monkeypatch.setattr(server, "get_embedding", lambda *a, **k: [0.0] * 768)

    hits = server._run_search_collection("q", _cfg(), "Elementrailer_doc", 5)

    assert len(hits) == 1
    assert "using" not in fake.calls[-1]


def test_query_failure_propagates_and_is_not_swallowed(monkeypatch):
    server = _get_server_module()

    class Exploding(ContractFakeQdrant):
        def query_points(self, **kwargs):
            raise RuntimeError("boom")

    fake = Exploding(named_vectors=True)
    monkeypatch.setattr(server, "QdrantClient", lambda **kw: fake)
    monkeypatch.setattr(server, "get_embedding", lambda *a, **k: [0.0] * 768)

    with pytest.raises(server.QdrantQueryError):
        server._run_search_collection("q", _cfg(), "ET-embed_doc", 5)


def test_run_search_collection_classifies_404_as_collection_missing(monkeypatch):
    """A real Qdrant 404 (UnexpectedResponse with status_code=404) is the only thing
    that should be classified as CollectionMissing — safe to skip."""
    server = _get_server_module()

    class Returns404(ContractFakeQdrant):
        def query_points(self, **kwargs):
            raise UnexpectedResponse(
                status_code=404, reason_phrase="Not Found",
                content=b'{"status":{"error":"Collection `ET-embed_doc` doesn\'t exist"}}',
                headers=None,
            )

    fake = Returns404(named_vectors=True)
    monkeypatch.setattr(server, "QdrantClient", lambda **kw: fake)
    monkeypatch.setattr(server, "get_embedding", lambda *a, **k: [0.0] * 768)

    with pytest.raises(server.CollectionMissing):
        server._run_search_collection("q", _cfg(), "ET-embed_doc", 5)


def test_run_search_collection_does_not_misclassify_500_with_not_found_in_body(monkeypatch):
    """Regression test for the reviewer finding: qdrant_client's UnexpectedResponse.__str__()
    embeds up to 200 bytes of the raw HTTP response body verbatim. A 500 (or any non-404
    failure) whose body happens to contain the substring "not found" must NOT be
    classified as CollectionMissing on that textual coincidence — only an actual 404
    status_code counts. Misclassifying it would silently skip a real query failure,
    reintroducing the exact "swallowed failure reported as empty results" bug this task
    exists to eliminate."""
    server = _get_server_module()

    class Returns500(ContractFakeQdrant):
        def query_points(self, **kwargs):
            raise UnexpectedResponse(
                status_code=500, reason_phrase="Internal Server Error",
                content=b'{"status":{"error":"upstream proxy: resource not found in cache"}}',
                headers=None,
            )

    fake = Returns500(named_vectors=True)
    monkeypatch.setattr(server, "QdrantClient", lambda **kw: fake)
    monkeypatch.setattr(server, "get_embedding", lambda *a, **k: [0.0] * 768)

    with pytest.raises(server.QdrantQueryError):
        server._run_search_collection("q", _cfg(), "ET-embed_doc", 5)
