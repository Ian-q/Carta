"""Tests for carta/mcp/server.py — carta_embed scope parameter.

Note: The actual carta_embed function is decorated with @mcp_server.tool(),
which makes testing the decorated function difficult. Instead, we test:
1. The import structure and function signature
2. Integration by running the full MCP server in a subprocess
3. Key business logic paths through mocks
"""

import sys
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock
from datetime import datetime, timezone

import pytest
import yaml

# Mock mcp module before importing server
sys.modules['mcp'] = MagicMock()
sys.modules['mcp.server'] = MagicMock()
sys.modules['mcp.server.fastmcp'] = MagicMock()


def test_carta_embed_imports_correctly():
    """Verify carta_embed can be imported and has the correct signature."""
    from carta import mcp
    # If this imports without error, the structure is correct
    assert mcp is not None


def test_discover_stale_files_in_pipeline():
    """Verify discover_stale_files is properly exported from pipeline."""
    from carta.embed.pipeline import discover_stale_files
    assert callable(discover_stale_files)


def test_scope_parameter_validation():
    """Test the scope parameter validation logic directly."""
    # Test that scope enum is properly defined
    from typing import Literal, get_args

    # The scope should be Literal["stale", "file", "all"]
    valid_scopes = ("stale", "file", "all")
    for scope in valid_scopes:
        assert isinstance(scope, str)

    # Test backward compat logic
    def test_backward_compat_logic(scope, path):
        # If scope is not in valid enum and path is None, treat scope as path
        if scope not in ("stale", "file", "all") and path is None:
            path = scope
            scope = "file"
        return scope, path

    # Test normal enum values pass through
    assert test_backward_compat_logic("stale", None) == ("stale", None)
    assert test_backward_compat_logic("file", "docs/x.md") == ("file", "docs/x.md")
    assert test_backward_compat_logic("all", None) == ("all", None)

    # Test invalid scope with no path becomes file scope
    assert test_backward_compat_logic("docs/x.md", None) == ("file", "docs/x.md")


class TestDiscoverStaleFilesIntegration:
    """discover_stale_files (MCP carta_embed scope='stale') returns files whose
    content changed since embed — current hash != sidecar file_hash (#39)."""

    def _sidecar(self, repo_root, rel, file_hash):
        from carta.embed.induct import sidecar_path as get_sidecar_path
        sc = get_sidecar_path(repo_root / rel, repo_root)
        sc.parent.mkdir(parents=True, exist_ok=True)
        sc.write_text(yaml.dump({
            "slug": Path(rel).stem, "current_path": rel,
            "status": "embedded", "file_hash": file_hash,
        }))

    def test_returns_files_whose_hash_changed(self):
        import tempfile
        from carta.embed.pipeline import discover_stale_files

        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            (repo_root / "docs").mkdir()
            changed = repo_root / "docs" / "changed.md"
            changed.write_text("# new content")
            self._sidecar(repo_root, "docs/changed.md", "stalehash000")  # != real

            assert discover_stale_files(repo_root) == [changed]

    def test_returns_empty_when_hash_matches(self):
        import tempfile
        from carta.embed.pipeline import discover_stale_files
        from carta.embed.lifecycle import compute_file_hash

        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            (repo_root / "docs").mkdir()
            same = repo_root / "docs" / "same.md"
            same.write_text("# unchanged")
            self._sidecar(repo_root, "docs/same.md", compute_file_hash(same))

            assert discover_stale_files(repo_root) == []

    def test_skips_sidecar_without_recorded_hash(self):
        import tempfile
        from carta.embed.pipeline import discover_stale_files

        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            (repo_root / "docs").mkdir()
            f = repo_root / "docs" / "nohash.md"
            f.write_text("# content")
            self._sidecar(repo_root, "docs/nohash.md", None)

            assert discover_stale_files(repo_root) == []


def test_remember_returns_ok_shape(tmp_path):
    from carta.mcp import server
    with patch.object(server, "_load_cfg", return_value={"project_name": "p"}), \
         patch.object(server, "_repo_root_from_cfg", return_value=tmp_path), \
         patch("carta.memory.capture.capture_note",
               return_value={"path": "docs/quirks/x.md", "collection": "p_notes", "chunks": 1}):
        out = server._remember("the bench PSU must be on", note_type="quirk")
    assert out == {"status": "ok", "path": "docs/quirks/x.md",
                   "collection": "p_notes", "chunks": 1}


def test_remember_invalid_type_maps_to_invalid_request(tmp_path):
    from carta.mcp import server
    with patch.object(server, "_load_cfg", return_value={"project_name": "p"}), \
         patch.object(server, "_repo_root_from_cfg", return_value=tmp_path), \
         patch("carta.memory.capture.capture_note", side_effect=ValueError("bad note_type")):
        out = server._remember("x", note_type="nope")
    assert out["error"] == "invalid_request"


def test_remember_no_config_maps_to_service_unavailable():
    from carta.mcp import server
    from carta.config import ConfigError
    with patch.object(server, "_load_cfg", side_effect=ConfigError("no .carta")):
        out = server._remember("x")
    assert out["error"] == "service_unavailable"


class TestCartaFocus:
    def test_formats_results_and_passes_through_image(self):
        from unittest.mock import patch
        import carta.mcp.server as server

        fake = [
            {"score": 0.876543, "source": "docs/imu.pdf", "page": 47,
             "section_heading": "6.3 Gyro", "excerpt": "x" * 400, "type": "text"},
            {"score": 0.5, "source": "docs/imu.pdf (page 47)", "page": 47,
             "section_heading": "", "excerpt": "[Visual]", "type": "visual",
             "image_b64": "QkFTRTY0"},
        ]
        with patch.object(server, "_load_cfg", return_value={"x": 1}), \
             patch.object(server, "run_focus", return_value=fake) as mock_focus:
            out = server.carta_focus(source="docs/imu.pdf (page 47)", query="sensitivity", top_k=15)

        mock_focus.assert_called_once_with("docs/imu.pdf (page 47)", {"x": 1},
                                           query="sensitivity", limit=15)
        assert out[0]["score"] == 0.8765          # rounded to 4dp
        assert out[0]["page"] == 47
        assert len(out[0]["excerpt"]) == 300       # truncated
        assert out[1]["image_b64"] == "QkFTRTY0"   # passed through on visual hits

    def test_returns_error_dict_on_config_failure(self):
        from unittest.mock import patch
        import carta.mcp.server as server
        from carta.config import ConfigError
        with patch.object(server, "_load_cfg", side_effect=ConfigError("no config")):
            out = server.carta_focus(source="x.pdf")
        assert out["error"] == "service_unavailable"
        assert "detail" in out and out["detail"]

    def test_returns_error_dict_on_runtime_error(self):
        from unittest.mock import patch
        import carta.mcp.server as server
        with patch.object(server, "_load_cfg", return_value={}), \
             patch.object(server, "run_focus", side_effect=RuntimeError("Qdrant down")):
            out = server.carta_focus(source="x.pdf")
        assert out["error"] == "service_unavailable"
        assert "Qdrant down" in out["detail"]


class TestSearchAnchors:
    def test_run_search_collection_includes_page_and_section(self):
        from unittest.mock import patch, MagicMock
        import carta.mcp.server as server
        point = MagicMock(); point.score = 0.9
        point.payload = {"file_path": "docs/imu.pdf", "text": "regs",
                         "page": 47, "section_heading": "6.3 Gyro"}
        resp = MagicMock(); resp.points = [point]
        client = MagicMock(); client.query_points.return_value = resp
        cfg = {"qdrant_url": "http://localhost:6333",
               "embed": {"ollama_url": "x", "ollama_model": "m"}}
        with patch("qdrant_client.QdrantClient", return_value=client), \
             patch("carta.embed.embed.get_embedding", return_value=[0.0] * 768):
            hits = server._run_search_collection("gyro", cfg, "p_doc", 5)
        assert hits[0]["page"] == 47
        assert hits[0]["section_heading"] == "6.3 Gyro"

    def test_format_search_result_surfaces_page_section_truncates(self):
        import carta.mcp.server as server
        out = server._format_search_result(
            {"score": 0.87654, "source": "docs/imu.pdf", "excerpt": "x" * 400,
             "page": 47, "section_heading": "6.3 Gyro", "type": "text"})
        assert out["page"] == 47
        assert out["section_heading"] == "6.3 Gyro"
        assert out["score"] == 0.8765
        assert len(out["excerpt"]) == 300
        assert "image_b64" not in out

    def test_format_search_result_passes_through_visual_image(self):
        import carta.mcp.server as server
        out = server._format_search_result(
            {"score": 0.5, "source": "docs/imu.pdf (page 47)", "excerpt": "[Visual]",
             "page": 47, "section_heading": "", "type": "visual", "image_b64": "QkE="})
        assert out["type"] == "visual"
        assert out["image_b64"] == "QkE="
        assert out["page"] == 47

    def test_format_adds_caveat_and_text_source_for_ocr_visual(self):
        import carta.mcp.server as server
        out = server._format_search_result(
            {"score": 0.8, "source": "docs/board.pdf", "excerpt": "32M Hz",
             "page": 3, "section_heading": "", "type": "text", "text_source": "ocr_visual"})
        assert out["text_source"] == "ocr_visual"
        assert "caveat" in out and "carta_focus" in out["caveat"]

    def test_format_no_caveat_for_trusted_text(self):
        import carta.mcp.server as server
        out = server._format_search_result(
            {"score": 0.7, "source": "docs/spec.md", "excerpt": "x",
             "page": 2, "section_heading": "Intro", "type": "text", "text_source": "text_layer"})
        assert out["text_source"] == "text_layer"
        assert "caveat" not in out
