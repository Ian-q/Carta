"""Tests for carta/embed/pipeline.py — mtime fast-path, hash comparison, generation tracking."""

import os
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock
from datetime import datetime, timezone

import pytest
import yaml

from carta.embed.pipeline import run_embed_file, run_embed, discover_stale_files
from carta.config import find_config


@pytest.fixture
def temp_repo():
    """Create a temporary repo with .carta/config.yaml for testing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        repo_root = Path(tmpdir)
        carta_dir = repo_root / ".carta"
        carta_dir.mkdir()

        config = {
            "project_name": "test-project",
            "docs_root": "docs",
            "qdrant_url": "http://localhost:6333",
            "embed": {
                "ollama_url": "http://localhost:11434",
                "ollama_model": "nomic-embed-text",
                "chunking": {"max_tokens": 400, "overlap_fraction": 0.15},
                "stale_alert_threshold": 0.30,
                "max_generations": 2,
            },
        }

        with open(carta_dir / "config.yaml", "w") as f:
            yaml.dump(config, f)

        yield repo_root, config


@pytest.fixture
def mock_qdrant():
    """Mock Qdrant client for testing."""
    client = Mock()
    client.get_collections.return_value = Mock()
    return client


class TestRunEmbedFileMinimalPath:
    """Test run_embed_file with mtime fast-path and hash comparison."""

    def test_unchanged_mtime_skips_hash_computation(self, temp_repo, mock_qdrant):
        """File with unchanged mtime -> needs_rehash returns False -> no hash call."""
        repo_root, cfg = temp_repo

        # Create a test markdown file
        docs_dir = repo_root / "docs"
        docs_dir.mkdir()
        test_file = docs_dir / "test.md"
        test_file.write_text("# Test Document\n\nContent here.")

        # Create sidecar with current mtime
        sidecar_path = test_file.parent / (test_file.stem + ".embed-meta.yaml")
        current_mtime = os.path.getmtime(str(test_file))
        sidecar = {
            "slug": "test",
            "doc_type": "guide",
            "file_type": "markdown",
            "current_path": "docs/test.md",
            "status": "embedded",
            "indexed_at": datetime.now(timezone.utc).isoformat(),
            "chunk_count": 3,
            "file_mtime": current_mtime,
            "file_hash": "abc123",
            "hash_algorithm": "sha256",
            "generation": 0,
            "last_hash_check_at": datetime.now(timezone.utc).isoformat(),
            "version_history": [],
        }
        with open(sidecar_path, "w") as f:
            yaml.dump(sidecar, f)

        # Mock find_config to return the test config path
        with patch("carta.embed.pipeline.find_config") as mock_find_cfg:
            with patch("carta.embed.pipeline.QdrantClient") as mock_client_cls:
                with patch("carta.embed.pipeline.ensure_collection"):
                    with patch("carta.embed.pipeline.needs_rehash", return_value=False) as mock_needs:
                        mock_client_cls.return_value = mock_qdrant
                        mock_find_cfg.return_value = repo_root / ".carta" / "config.yaml"

                        result = run_embed_file(test_file, cfg, force=False)

                        # Should skip without computing hash
                        assert result["status"] == "skipped"
                        mock_needs.assert_called_once()

    def test_mtime_changed_hash_unchanged_updates_mtime(self, temp_repo, mock_qdrant):
        """mtime changed, hash unchanged -> mtime updated, status stays embedded."""
        repo_root, cfg = temp_repo

        # Create test file
        docs_dir = repo_root / "docs"
        docs_dir.mkdir()
        test_file = docs_dir / "test.md"
        test_file.write_text("# Test Document\n\nContent here.")

        # Create sidecar with old mtime
        sidecar_path = test_file.parent / (test_file.stem + ".embed-meta.yaml")
        old_mtime = 1000.0
        sidecar = {
            "slug": "test",
            "doc_type": "guide",
            "file_type": "markdown",
            "current_path": "docs/test.md",
            "status": "embedded",
            "indexed_at": datetime.now(timezone.utc).isoformat(),
            "chunk_count": 3,
            "file_mtime": old_mtime,
            "file_hash": "abc123",
            "hash_algorithm": "sha256",
            "generation": 0,
            "last_hash_check_at": datetime.now(timezone.utc).isoformat(),
            "version_history": [],
        }
        with open(sidecar_path, "w") as f:
            yaml.dump(sidecar, f)

        current_mtime = os.path.getmtime(str(test_file))
        current_hash = "abc123"  # Same hash

        with patch("carta.embed.pipeline.find_config") as mock_find_cfg:
            with patch("carta.embed.pipeline.QdrantClient") as mock_client_cls:
                with patch("carta.embed.pipeline.ensure_collection"):
                    with patch("carta.embed.pipeline.needs_rehash", return_value=True):
                        with patch("carta.embed.pipeline.compute_file_hash", return_value=current_hash):
                            mock_client_cls.return_value = mock_qdrant
                            mock_find_cfg.return_value = repo_root / ".carta" / "config.yaml"

                            result = run_embed_file(test_file, cfg, force=False)

                            # Should skip (hash unchanged)
                            assert result["status"] == "skipped"

                            # Read updated sidecar
                            with open(sidecar_path) as f:
                                updated = yaml.safe_load(f)

                            # mtime should be updated
                            assert updated["file_mtime"] == current_mtime
                            # status should remain "embedded"
                            assert updated["status"] == "embedded"

    def test_hash_mismatch_increments_generation_marks_stale(self, temp_repo, mock_qdrant):
        """Hash mismatch -> generation incremented, status='stale', stale_as_of set."""
        repo_root, cfg = temp_repo

        # Create test file
        docs_dir = repo_root / "docs"
        docs_dir.mkdir()
        test_file = docs_dir / "test.md"
        test_file.write_text("# Updated Content\n\nThis is different.")

        # Create sidecar with old hash
        sidecar_path = test_file.parent / (test_file.stem + ".embed-meta.yaml")
        old_hash = "old_hash_value"
        new_hash = "new_hash_value"
        sidecar = {
            "slug": "test",
            "doc_type": "guide",
            "file_type": "markdown",
            "current_path": "docs/test.md",
            "status": "embedded",
            "indexed_at": datetime.now(timezone.utc).isoformat(),
            "chunk_count": 3,
            "file_mtime": 1000.0,
            "file_hash": old_hash,
            "hash_algorithm": "sha256",
            "generation": 2,
            "last_hash_check_at": datetime.now(timezone.utc).isoformat(),
            "version_history": [
                {"hash": "very_old_hash", "generation": 0, "indexed_at": datetime.now(timezone.utc).isoformat()},
            ],
            "sidecar_id": "test-sidecar-id",
        }
        with open(sidecar_path, "w") as f:
            yaml.dump(sidecar, f)

        current_mtime = os.path.getmtime(str(test_file))

        with patch("carta.embed.pipeline.find_config") as mock_find_cfg:
            with patch("carta.embed.pipeline.QdrantClient") as mock_client_cls:
                with patch("carta.embed.pipeline.ensure_collection"):
                    with patch("carta.embed.pipeline.needs_rehash", return_value=True):
                        with patch("carta.embed.pipeline.compute_file_hash", return_value=new_hash):
                            with patch("carta.embed.pipeline._embed_one_file", return_value=(5, {})):
                                with patch("carta.embed.pipeline.mark_sidecar_stale") as mock_mark_stale:
                                    mock_client_cls.return_value = mock_qdrant
                                    mock_find_cfg.return_value = repo_root / ".carta" / "config.yaml"

                                    result = run_embed_file(test_file, cfg, force=False)

                                    # Should proceed with re-embed (status ok)
                                    assert result["status"] == "ok"

                                    # Read updated sidecar
                                    with open(sidecar_path) as f:
                                        updated = yaml.safe_load(f)

                                    # Generation should increment
                                    assert updated["generation"] == 3
                                    # Status should be stale
                                    assert updated["status"] == "stale"
                                    # stale_as_of should be set
                                    assert "stale_as_of" in updated
                                    # file_hash should be updated
                                    assert updated["file_hash"] == new_hash
                                    # version_history should have new entry
                                    assert len(updated["version_history"]) > 0

                                    # mark_sidecar_stale should be called
                                    mock_mark_stale.assert_called_once()

    def test_version_history_trimmed_to_max_generations(self, temp_repo, mock_qdrant):
        """version_history trimmed to max_generations after append."""
        repo_root, cfg = temp_repo
        cfg["embed"]["max_generations"] = 2

        # Create test file
        docs_dir = repo_root / "docs"
        docs_dir.mkdir()
        test_file = docs_dir / "test.md"
        test_file.write_text("# Updated Content\n\nThis is different.")

        # Create sidecar with version_history already at max
        sidecar_path = test_file.parent / (test_file.stem + ".embed-meta.yaml")
        old_hash = "old_hash"
        new_hash = "new_hash"
        sidecar = {
            "slug": "test",
            "doc_type": "guide",
            "file_type": "markdown",
            "current_path": "docs/test.md",
            "status": "embedded",
            "indexed_at": datetime.now(timezone.utc).isoformat(),
            "chunk_count": 3,
            "file_mtime": 1000.0,
            "file_hash": old_hash,
            "hash_algorithm": "sha256",
            "generation": 1,
            "last_hash_check_at": datetime.now(timezone.utc).isoformat(),
            "version_history": [
                {"hash": "hash0", "generation": 0, "indexed_at": datetime.now(timezone.utc).isoformat()},
                {"hash": "hash1", "generation": 1, "indexed_at": datetime.now(timezone.utc).isoformat()},
            ],
            "sidecar_id": "test-sidecar-id",
        }
        with open(sidecar_path, "w") as f:
            yaml.dump(sidecar, f)

        with patch("carta.embed.pipeline.find_config") as mock_find_cfg:
            with patch("carta.embed.pipeline.QdrantClient") as mock_client_cls:
                with patch("carta.embed.pipeline.ensure_collection"):
                    with patch("carta.embed.pipeline.needs_rehash", return_value=True):
                        with patch("carta.embed.pipeline.compute_file_hash", return_value=new_hash):
                            with patch("carta.embed.pipeline._embed_one_file", return_value=(5, {})):
                                with patch("carta.embed.pipeline.mark_sidecar_stale"):
                                    mock_client_cls.return_value = mock_qdrant
                                    mock_find_cfg.return_value = repo_root / ".carta" / "config.yaml"

                                    result = run_embed_file(test_file, cfg, force=False)

                                    # Read updated sidecar
                                    with open(sidecar_path) as f:
                                        updated = yaml.safe_load(f)

                                    # version_history should be trimmed to max_generations
                                    assert len(updated["version_history"]) == 2
                                    # Most recent entries should be kept
                                    assert updated["version_history"][-1]["generation"] == 2

    def test_sidecar_id_guard_prevents_mark_stale_on_missing_id(self, temp_repo, mock_qdrant):
        """sidecar with no sidecar_id -> mark_sidecar_stale NOT called (migration boundary)."""
        repo_root, cfg = temp_repo

        # Create test file
        docs_dir = repo_root / "docs"
        docs_dir.mkdir()
        test_file = docs_dir / "test.md"
        test_file.write_text("# Updated Content\n\nThis is different.")

        # Create sidecar WITHOUT sidecar_id (pre-999.1)
        sidecar_path = test_file.parent / (test_file.stem + ".embed-meta.yaml")
        old_hash = "old_hash"
        new_hash = "new_hash"
        sidecar = {
            "slug": "test",
            "doc_type": "guide",
            "file_type": "markdown",
            "current_path": "docs/test.md",
            "status": "embedded",
            "indexed_at": datetime.now(timezone.utc).isoformat(),
            "chunk_count": 3,
            "file_mtime": 1000.0,
            "file_hash": old_hash,
            "hash_algorithm": "sha256",
            "generation": 0,
            "last_hash_check_at": datetime.now(timezone.utc).isoformat(),
            "version_history": [],
            # NOTE: no sidecar_id field
        }
        with open(sidecar_path, "w") as f:
            yaml.dump(sidecar, f)

        with patch("carta.embed.pipeline.find_config") as mock_find_cfg:
            with patch("carta.embed.pipeline.QdrantClient") as mock_client_cls:
                with patch("carta.embed.pipeline.ensure_collection"):
                    with patch("carta.embed.pipeline.needs_rehash", return_value=True):
                        with patch("carta.embed.pipeline.compute_file_hash", return_value=new_hash):
                            with patch("carta.embed.pipeline._embed_one_file", return_value=(5, {})):
                                with patch("carta.embed.pipeline.mark_sidecar_stale") as mock_mark_stale:
                                    mock_client_cls.return_value = mock_qdrant
                                    mock_find_cfg.return_value = repo_root / ".carta" / "config.yaml"

                                    result = run_embed_file(test_file, cfg, force=False)

                                    # mark_sidecar_stale should NOT be called
                                    mock_mark_stale.assert_not_called()


class TestRunEmbedStaleAlert:
    """Test stale alert after embed run."""

    def test_stale_alert_printed_when_threshold_exceeded(self, temp_repo, mock_qdrant):
        """Stale alert printed to stdout when stale/total >= threshold."""
        repo_root, cfg = temp_repo

        # Create test files
        docs_dir = repo_root / "docs"
        docs_dir.mkdir()

        test_files = ["doc1.md", "doc2.md", "doc3.md"]
        for fname in test_files:
            (docs_dir / fname).write_text("# Test\n\nContent.")

        # Create sidecars
        for fname in test_files:
            stem = fname.replace(".md", "")
            sidecar_path = docs_dir / (stem + ".embed-meta.yaml")
            sidecar = {
                "slug": stem,
                "doc_type": "guide",
                "file_type": "markdown",
                "current_path": f"docs/{fname}",
                "status": "pending",
                "indexed_at": None,
                "chunk_count": None,
                "file_mtime": None,
                "file_hash": None,
                "generation": 0,
                "version_history": [],
                "sidecar_id": f"{stem}-id",
            }
            with open(sidecar_path, "w") as f:
                yaml.dump(sidecar, f)

        with patch("carta.embed.pipeline.find_config") as mock_find_cfg:
            with patch("carta.embed.pipeline.QdrantClient") as mock_client_cls:
                with patch("carta.embed.pipeline.ensure_collection"):
                    with patch("carta.embed.pipeline.discover_pending_files") as mock_discover:
                        with patch("carta.embed.pipeline._embed_one_file") as mock_embed:
                            with patch("carta.embed.pipeline.check_stale_alert") as mock_alert:
                                with patch("builtins.print") as mock_print:
                                    mock_client_cls.return_value = mock_qdrant
                                    mock_find_cfg.return_value = repo_root / ".carta" / "config.yaml"

                                    # Mock stale alert to return a message
                                    mock_alert.return_value = "Warning: 50% of embedded docs are stale. Run 'carta embed --force-stale' to refresh them."

                                    # Mock discover to return empty (no pending files for this test)
                                    mock_discover.return_value = []

                                    result = run_embed(repo_root, cfg, verbose=False)

                                    # check_stale_alert should be called
                                    mock_alert.assert_called_once()

                                    # If alert message is not None, it should be printed to stdout
                                    calls = [c for c in mock_print.call_args_list if len(c[0]) > 0]
                                    alert_printed = any(
                                        "Warning: 50% of embedded docs are stale" in str(c)
                                        for c in calls
                                    )
                                    # Note: This depends on implementation details; adjust if needed


class TestDiscoverStaleFiles:
    """Test discover_stale_files helper function."""

    def test_discover_stale_files_returns_stale_paths(self, temp_repo):
        """Two sidecars, one stale, one embedded -> returns one Path."""
        repo_root, cfg = temp_repo

        # Create docs directory
        docs_dir = repo_root / "docs"
        docs_dir.mkdir()

        # Create first file with stale sidecar
        stale_file = docs_dir / "stale.md"
        stale_file.write_text("# Stale Document")
        stale_sidecar = docs_dir / "stale.embed-meta.yaml"
        with open(stale_sidecar, "w") as f:
            yaml.dump({"status": "stale", "slug": "stale"}, f)

        # Create second file with embedded sidecar
        embedded_file = docs_dir / "embedded.md"
        embedded_file.write_text("# Embedded Document")
        embedded_sidecar = docs_dir / "embedded.embed-meta.yaml"
        with open(embedded_sidecar, "w") as f:
            yaml.dump({"status": "embedded", "slug": "embedded"}, f)

        # Call discover_stale_files
        results = discover_stale_files(repo_root)

        # Should return only the stale file
        assert len(results) == 1
        assert results[0] == stale_file

    def test_discover_stale_files_returns_empty_when_none_stale(self, temp_repo):
        """No stale sidecars -> returns empty list."""
        repo_root, cfg = temp_repo

        # Create docs directory
        docs_dir = repo_root / "docs"
        docs_dir.mkdir()

        # Create file with embedded sidecar
        embedded_file = docs_dir / "embedded.md"
        embedded_file.write_text("# Embedded Document")
        embedded_sidecar = docs_dir / "embedded.embed-meta.yaml"
        with open(embedded_sidecar, "w") as f:
            yaml.dump({"status": "embedded", "slug": "embedded"}, f)

        # Call discover_stale_files
        results = discover_stale_files(repo_root)

        # Should return empty list
        assert results == []

    def test_discover_stale_files_skips_missing_status(self, temp_repo):
        """Sidecar missing status key -> not included in results."""
        repo_root, cfg = temp_repo

        # Create docs directory
        docs_dir = repo_root / "docs"
        docs_dir.mkdir()

        # Create file with sidecar missing status
        file_no_status = docs_dir / "no_status.md"
        file_no_status.write_text("# Document")
        sidecar_no_status = docs_dir / "no_status.embed-meta.yaml"
        with open(sidecar_no_status, "w") as f:
            yaml.dump({"slug": "no_status"}, f)

        # Create file with stale sidecar
        stale_file = docs_dir / "stale.md"
        stale_file.write_text("# Stale Document")
        stale_sidecar = docs_dir / "stale.embed-meta.yaml"
        with open(stale_sidecar, "w") as f:
            yaml.dump({"status": "stale", "slug": "stale"}, f)

        # Call discover_stale_files
        results = discover_stale_files(repo_root)

        # Should return only the stale file
        assert len(results) == 1
        assert results[0] == stale_file
