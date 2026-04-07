"""Tests for audit module."""

import os
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
import yaml

from carta.audit.audit import (
    _build_sidecar_registry,
    _build_qdrant_chunk_index,
    detect_orphaned_chunks,
    detect_missing_sidecars,
    detect_stale_sidecars,
    detect_hash_mismatches,
    detect_disconnected_files,
    detect_qdrant_sidecar_mismatches,
)
from carta.embed.lifecycle import compute_file_hash


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


class TestDetectStaleSidecars:
    """Test detection of stale sidecars."""

    def test_no_stale_when_mtime_matches(self):
        """No issues when file mtime matches sidecar."""
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            docs_root = repo_root / "docs"
            docs_root.mkdir()

            test_file = docs_root / "test.md"
            test_file.write_text("content")
            mtime = os.path.getmtime(test_file)

            sidecar_registry = {
                "sidecar_1": {
                    "file_path": test_file,
                    "data": {"file_mtime": mtime, "last_embedded": "2026-04-07T10:00:00"}
                }
            }

            issues = detect_stale_sidecars(repo_root, {}, sidecar_registry)

            assert issues == []

    def test_detects_stale_sidecars(self):
        """File newer than sidecar is detected as stale."""
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            docs_root = repo_root / "docs"
            docs_root.mkdir()

            test_file = docs_root / "test.md"
            test_file.write_text("content")

            sidecar_registry = {
                "sidecar_1": {
                    "path": docs_root / "test.md.embed-meta.yaml",
                    "file_path": test_file,
                    "data": {"file_mtime": 1000000000.0, "last_embedded": "2026-01-01T00:00:00"}
                }
            }

            issues = detect_stale_sidecars(repo_root, {}, sidecar_registry)

            assert len(issues) == 1
            assert issues[0]["category"] == "stale_sidecars"


class TestDetectHashMismatches:
    """Test detection of hash mismatches."""

    def test_no_mismatch_when_hashes_match(self):
        """No issues when file hash matches sidecar."""
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            docs_root = repo_root / "docs"
            docs_root.mkdir()

            test_file = docs_root / "test.md"
            test_file.write_text("# Header\n")

            # Compute actual hash
            actual_hash = compute_file_hash(test_file)

            sidecar_registry = {
                "sidecar_1": {
                    "file_path": test_file,
                    "data": {"file_hash": actual_hash}
                }
            }

            issues = detect_hash_mismatches(repo_root, {}, sidecar_registry)

            assert issues == []

    def test_detects_hash_mismatches(self):
        """File with different hash than sidecar is detected."""
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            docs_root = repo_root / "docs"
            docs_root.mkdir()

            test_file = docs_root / "test.md"
            test_file.write_text("# Header\n")

            sidecar_registry = {
                "sidecar_1": {
                    "path": docs_root / "test.md.embed-meta.yaml",
                    "file_path": test_file,
                    "data": {"file_hash": "wrong_hash_value"}
                }
            }

            issues = detect_hash_mismatches(repo_root, {}, sidecar_registry)

            assert len(issues) == 1
            assert issues[0]["category"] == "hash_mismatches"


class TestDetectDisconnectedFiles:
    """Test detection of disconnected files."""

    def test_no_disconnected_when_all_embedded(self):
        """No issues when all discoverable files have sidecars or chunks."""
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            docs_root = repo_root / "docs"
            docs_root.mkdir()

            test_file = docs_root / "test.md"
            test_file.write_text("# Test")

            sidecar_registry = {
                "sidecar_1": {"file_path": test_file, "data": {}}
            }
            qdrant_index = {}

            issues = detect_disconnected_files(repo_root, {"docs_root": "docs"}, sidecar_registry, qdrant_index)

            assert issues == []

    def test_detects_disconnected_files(self):
        """Files with no sidecar and no chunks are detected."""
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            docs_root = repo_root / "docs"
            docs_root.mkdir()

            # Create discoverable file with no sidecar
            orphaned_file = docs_root / "orphaned.md"
            orphaned_file.write_text("# Orphaned")

            sidecar_registry = {}
            qdrant_index = {}

            issues = detect_disconnected_files(repo_root, {"docs_root": "docs", "excluded_paths": []}, sidecar_registry, qdrant_index)

            assert len(issues) == 1
            assert issues[0]["category"] == "disconnected_files"
            assert "orphaned.md" in issues[0]["file_path"]


class TestDetectQdrantSidecarMismatches:
    """Test detection of Qdrant/sidecar metadata mismatches."""

    def test_no_mismatch_when_aligned(self):
        """No issues when chunk count and indices align."""
        sidecar_registry = {
            "sidecar_1": {"data": {"chunk_count": 2, "chunk_indices": [0, 1]}}
        }
        qdrant_index = {
            "sidecar_1": [
                {"id": 1, "payload": {"chunk_index": 0}},
                {"id": 2, "payload": {"chunk_index": 1}}
            ]
        }

        issues = detect_qdrant_sidecar_mismatches(None, {}, sidecar_registry, qdrant_index)

        assert issues == []

    def test_detects_count_mismatch(self):
        """Chunk count mismatch is detected."""
        sidecar_registry = {
            "sidecar_1": {"data": {"chunk_count": 5}}
        }
        qdrant_index = {
            "sidecar_1": [
                {"id": 1, "payload": {"chunk_index": 0}},
                {"id": 2, "payload": {"chunk_index": 1}}
            ]
        }

        issues = detect_qdrant_sidecar_mismatches(None, {}, sidecar_registry, qdrant_index)

        assert len(issues) == 1
        assert issues[0]["category"] == "qdrant_sidecar_mismatches"
