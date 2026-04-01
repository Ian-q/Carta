"""Tests for collection scoping module."""
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

from carta.search.scoped import (
    get_search_collections,
    discover_collections,
    filter_by_permission,
    _is_carta_collection,
)


# ---------------------------------------------------------------------------
# get_search_collections tests
# ---------------------------------------------------------------------------

class TestGetSearchCollectionsRepoScope:
    """scope='repo' should return only current project collections."""

    def test_repo_scope_returns_project_collections(self, minimal_cfg):
        """Should return {project_name}_doc, {project_name}_notes, {project_name}_session."""
        # Arrange
        minimal_cfg["project_name"] = "myproject"
        
        # Act
        result = get_search_collections(minimal_cfg, scope="repo")
        
        # Assert
        assert sorted(result) == sorted([
            "myproject_doc",
            "myproject_notes", 
            "myproject_session"
        ])

    def test_repo_scope_ignores_global_setting(self, minimal_cfg):
        """cross_project_recall.enabled should not affect repo scope."""
        minimal_cfg["project_name"] = "myproject"
        minimal_cfg["cross_project_recall"]["enabled"] = True
        
        result = get_search_collections(minimal_cfg, scope="repo")
        
        # Still only returns project collections
        assert all(c.startswith("myproject_") for c in result)


class TestGetSearchCollectionsGlobalScope:
    """scope='global' should return only carta_global_* collections."""

    def test_global_scope_returns_global_collections(self, minimal_cfg):
        """Should return carta_global_doc, carta_global_notes, carta_global_session."""
        result = get_search_collections(minimal_cfg, scope="global")
        
        assert sorted(result) == sorted([
            "carta_global_doc",
            "carta_global_notes",
            "carta_global_session"
        ])

    def test_global_scope_does_not_discover(self, minimal_cfg):
        """Global scope returns fixed list, does not query Qdrant."""
        with patch("carta.search.scoped.discover_collections") as mock_discover:
            result = get_search_collections(minimal_cfg, scope="global")
            mock_discover.assert_not_called()


class TestGetSearchCollectionsSharedScope:
    """scope='shared' should return current project + permitted other projects."""

    def test_shared_scope_when_disabled_returns_only_current(self, minimal_cfg):
        """cross_project_recall.enabled=false should behave like repo scope."""
        minimal_cfg["project_name"] = "myproject"
        minimal_cfg["cross_project_recall"]["enabled"] = False
        
        with patch("carta.search.scoped.discover_collections") as mock_discover:
            mock_discover.return_value = [
                "myproject_doc", "otherproject_doc", "carta_global_doc"
            ]
            result = get_search_collections(minimal_cfg, scope="shared")
        
        # Only current project despite discovery finding others
        assert all(c.startswith("myproject_") for c in result)

    def test_shared_scope_when_enabled_returns_filtered(self, minimal_cfg):
        """cross_project_recall.enabled=true should include permitted projects."""
        minimal_cfg["project_name"] = "myproject"
        minimal_cfg["cross_project_recall"]["enabled"] = True
        minimal_cfg["cross_project_recall"]["project_filter"]["mode"] = "include"
        minimal_cfg["cross_project_recall"]["project_filter"]["projects"] = ["otherproject"]
        
        with patch("carta.search.scoped.discover_collections") as mock_discover:
            mock_discover.return_value = [
                "myproject_doc", "otherproject_doc", "thirdproject_doc"
            ]
            result = get_search_collections(minimal_cfg, scope="shared")
        
        assert "myproject_doc" in result
        assert "otherproject_doc" in result
        assert "thirdproject_doc" not in result


# ---------------------------------------------------------------------------
# discover_collections tests
# ---------------------------------------------------------------------------

class TestDiscoverCollections:
    """Discovery from Qdrant /collections endpoint."""

    def test_discovers_carta_collections(self):
        """Should return only collections matching carta naming pattern."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "result": {
                "collections": [
                    {"name": "myproject_doc"},
                    {"name": "myproject_notes"},
                    {"name": "system_config"},  # Should be filtered
                    {"name": "carta_global_doc"},
                ]
            }
        }
        
        with patch("carta.search.scoped.requests.get", return_value=mock_response):
            result = discover_collections("http://localhost:6333")
        
        assert "myproject_doc" in result
        assert "myproject_notes" in result
        assert "carta_global_doc" in result
        assert "system_config" not in result

    def test_handles_qdrant_error(self):
        """Should return empty list on Qdrant error."""
        mock_response = MagicMock()
        mock_response.status_code = 500
        
        with patch("carta.search.scoped.requests.get", return_value=mock_response):
            result = discover_collections("http://localhost:6333")
        
        assert result == []

    def test_handles_connection_error(self):
        """Should return empty list on connection failure."""
        with patch("carta.search.scoped.requests.get", side_effect=Exception("Connection refused")):
            result = discover_collections("http://localhost:6333")
        
        assert result == []


# ---------------------------------------------------------------------------
# filter_by_permission tests
# ---------------------------------------------------------------------------

class TestFilterByPermission:
    """Project filtering logic."""

    def test_include_mode_only_includes_specified(self):
        """mode='include' with projects=['a', 'b'] only keeps those."""
        collections = ["a_doc", "b_doc", "c_doc", "d_doc"]
        filter_config = {"mode": "include", "projects": ["a", "b"]}
        
        result = filter_by_permission(collections, "current", filter_config)
        
        assert "a_doc" in result
        assert "b_doc" in result
        assert "c_doc" not in result
        assert "d_doc" not in result

    def test_exclude_mode_excludes_specified(self):
        """mode='exclude' with projects=['c'] keeps all except c."""
        collections = ["a_doc", "b_doc", "c_doc", "d_doc"]
        filter_config = {"mode": "exclude", "projects": ["c"]}
        
        result = filter_by_permission(collections, "current", filter_config)
        
        assert "a_doc" in result
        assert "b_doc" in result
        assert "c_doc" not in result
        assert "d_doc" in result

    def test_all_mode_includes_all(self):
        """mode='all' includes all discovered collections."""
        collections = ["a_doc", "b_doc", "c_doc"]
        filter_config = {"mode": "all", "projects": []}
        
        result = filter_by_permission(collections, "current", filter_config)
        
        assert len(result) == 3

    def test_current_project_always_included(self):
        """Current project should always be in results even if not in filter list."""
        collections = ["current_doc", "other_doc"]
        filter_config = {"mode": "include", "projects": ["other"]}  # Note: missing 'current'
        
        result = filter_by_permission(collections, "current", filter_config)
        
        assert "current_doc" in result  # Always included
        assert "other_doc" in result


# ---------------------------------------------------------------------------
# _is_carta_collection helper tests
# ---------------------------------------------------------------------------

class TestIsCartaCollection:
    """Collection name pattern matching."""

    def test_recognizes_project_collections(self):
        """Names like {project}_{type} are carta collections."""
        assert _is_carta_collection("myproject_doc") is True
        assert _is_carta_collection("myproject_notes") is True
        assert _is_carta_collection("myproject_session") is True

    def test_recognizes_global_collections(self):
        """Names like carta_global_{type} are carta collections."""
        assert _is_carta_collection("carta_global_doc") is True
        assert _is_carta_collection("carta_global_notes") is True

    def test_rejects_non_carta_collections(self):
        """System collections are rejected."""
        assert _is_carta_collection("system_config") is False
        assert _is_carta_collection("") is False
        assert _is_carta_collection("_doc") is False
        assert _is_carta_collection("myproject") is False  # Missing suffix
