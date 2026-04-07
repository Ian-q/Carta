"""Tests for audit module."""

import tempfile
from pathlib import Path
from unittest.mock import Mock

import pytest
import yaml

from carta.audit.audit import (
    _build_sidecar_registry,
    _build_qdrant_chunk_index,
    detect_orphaned_chunks,
    detect_missing_sidecars,
)


class TestBuildSidecarRegistry:
    """Test sidecar registry building."""

    def test_empty_docs_root(self):
        """Registry is empty when no sidecars exist."""
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            docs_root = repo_root / "docs"
            docs_root.mkdir()

            cfg = {"docs_root": "docs", "excluded_paths": []}
            registry = _build_sidecar_registry(repo_root, cfg)

            assert registry == {}

    def test_single_sidecar_loaded(self):
        """Single sidecar is loaded with correct data."""
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            docs_root = repo_root / "docs"
            docs_root.mkdir()

            # Create a source file
            test_file = docs_root / "test.md"
            test_file.write_text("# Test")

            # Create sidecar
            sidecar_file = docs_root / "test.md.embed-meta.yaml"
            sidecar_data = {
                "sidecar_id": "test_sidecar_123",
                "file_hash": "abc123",
                "file_mtime": 1234567890.0,
                "chunk_count": 5,
                "doc_type": "doc"
            }
            sidecar_file.write_text(yaml.dump(sidecar_data))

            cfg = {"docs_root": "docs", "excluded_paths": []}
            registry = _build_sidecar_registry(repo_root, cfg)

            assert "test_sidecar_123" in registry
            assert registry["test_sidecar_123"]["path"] == sidecar_file
            assert registry["test_sidecar_123"]["data"]["chunk_count"] == 5

    def test_excluded_paths_skipped(self):
        """Sidecars in excluded paths are not registered."""
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            docs_root = repo_root / "docs"
            docs_root.mkdir()

            # Create sidecar in excluded path
            excluded_dir = docs_root / "node_modules"
            excluded_dir.mkdir()
            sidecar_file = excluded_dir / "pkg.md.embed-meta.yaml"
            sidecar_data = {"sidecar_id": "excluded_123"}
            sidecar_file.write_text(yaml.dump(sidecar_data))

            # Create sidecar in included path
            included_sidecar = docs_root / "included.md.embed-meta.yaml"
            included_sidecar.write_text(yaml.dump({"sidecar_id": "included_123"}))

            cfg = {"docs_root": "docs", "excluded_paths": ["node_modules/"]}
            registry = _build_sidecar_registry(repo_root, cfg)

            assert "excluded_123" not in registry
            assert "included_123" in registry


class TestBuildQdrantChunkIndex:
    """Test Qdrant chunk indexing."""

    def test_empty_collection(self):
        """Index is empty when collection has no chunks."""
        mock_client = Mock()
        mock_client.scroll.return_value = ([], None)

        index = _build_qdrant_chunk_index(mock_client, "test_doc")

        assert index == {}

    def test_index_chunks_by_sidecar_id(self):
        """Chunks are indexed by sidecar_id."""
        mock_client = Mock()

        # Mock chunk records
        chunk1 = Mock(id=1, payload={"sidecar_id": "sidecar_1", "chunk_index": 0})
        chunk2 = Mock(id=2, payload={"sidecar_id": "sidecar_1", "chunk_index": 1})
        chunk3 = Mock(id=3, payload={"sidecar_id": "sidecar_2", "chunk_index": 0})

        mock_client.scroll.return_value = ([chunk1, chunk2, chunk3], None)

        index = _build_qdrant_chunk_index(mock_client, "test_doc")

        assert len(index["sidecar_1"]) == 2
        assert len(index["sidecar_2"]) == 1
        assert index["sidecar_1"][0]["id"] == 1

    def test_skip_chunks_without_sidecar_id(self):
        """Chunks without sidecar_id (pre-999.1) are skipped."""
        mock_client = Mock()

        chunk1 = Mock(id=1, payload={"sidecar_id": "sidecar_1"})
        chunk2 = Mock(id=2, payload={})  # No sidecar_id

        mock_client.scroll.return_value = ([chunk1, chunk2], None)

        index = _build_qdrant_chunk_index(mock_client, "test_doc")

        assert len(index) == 1
        assert "sidecar_1" in index


class TestDetectOrphanedChunks:
    """Test detection of orphaned chunks."""

    def test_no_orphans_when_sidecars_match(self):
        """No issues when all chunks have matching sidecars."""
        mock_client = Mock()
        cfg = {"docs_root": "docs", "excluded_paths": []}
        sidecar_registry = {
            "sidecar_1": {"data": {"chunk_count": 2}, "path": Path("docs/test.md.embed-meta.yaml")}
        }

        issues = detect_orphaned_chunks(mock_client, cfg, sidecar_registry, {})

        assert issues == []

    def test_detects_orphaned_chunks(self):
        """Orphaned chunks (no matching sidecar) are detected."""
        mock_client = Mock()
        cfg = {"docs_root": "docs"}
        sidecar_registry = {
            "sidecar_1": {"data": {}}
        }

        # Build qdrant index with orphaned sidecar_id
        qdrant_index = {
            "sidecar_1": [{"id": 1, "payload": {"text": "chunk content"}}, {"id": 2, "payload": {}}],
            "orphaned_sidecar": [{"id": 3, "payload": {"text": "orphaned content"}}]
        }

        issues = detect_orphaned_chunks(mock_client, cfg, sidecar_registry, qdrant_index)

        assert len(issues) == 1
        assert issues[0]["category"] == "orphaned_chunks"
        assert issues[0]["sidecar_id"] == "orphaned_sidecar"
        assert len(issues[0]["chunk_ids"]) == 1


class TestDetectMissingSidecars:
    """Test detection of missing sidecars."""

    def test_no_missing_when_all_have_sidecars(self):
        """No issues when all chunked files have sidecars."""
        sidecar_registry = {
            "sidecar_1": {"file_path": Path("docs/test.md"), "data": {"file_hash": "abc123"}}
        }
        qdrant_index = {"sidecar_1": [{"id": 1}]}

        issues = detect_missing_sidecars(Path("/repo"), {}, sidecar_registry, qdrant_index)

        assert issues == []

    def test_detects_missing_sidecars(self):
        """Files with chunks but no sidecar are detected."""
        sidecar_registry = {}
        qdrant_index = {
            "phantom_sidecar": [{"id": 1, "payload": {"file_path": "docs/orphaned.md"}}]
        }

        issues = detect_missing_sidecars(Path("/repo"), {}, sidecar_registry, qdrant_index)

        assert len(issues) == 1
        assert issues[0]["category"] == "missing_sidecars"
