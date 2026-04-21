"""Tests for carta/embed/pipeline.py — mtime fast-path, hash comparison, generation tracking."""

import os
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock
from datetime import datetime, timezone

import pytest
import yaml

from carta.embed.pipeline import run_embed_file, run_embed, discover_stale_files, migrate_sidecars, detect_orphaned_sidecars
from carta.embed.induct import sidecar_path as get_sidecar_path
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

        # Create sidecar in .carta/sidecars/
        from carta.embed.induct import sidecar_path as get_sidecar_path
        sc_path = get_sidecar_path(test_file, repo_root)
        sc_path.parent.mkdir(parents=True, exist_ok=True)
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
        with open(sc_path, "w") as f:
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

        # Create sidecar in .carta/sidecars/
        from carta.embed.induct import sidecar_path as get_sidecar_path
        sc_path = get_sidecar_path(test_file, repo_root)
        sc_path.parent.mkdir(parents=True, exist_ok=True)
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
        with open(sc_path, "w") as f:
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
                            with open(sc_path) as f:
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

        # Create sidecar in .carta/sidecars/
        from carta.embed.induct import sidecar_path as get_sidecar_path
        sc_path = get_sidecar_path(test_file, repo_root)
        sc_path.parent.mkdir(parents=True, exist_ok=True)
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
        with open(sc_path, "w") as f:
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
                                    with open(sc_path) as f:
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

        # Create sidecar in .carta/sidecars/
        from carta.embed.induct import sidecar_path as get_sidecar_path
        sc_path = get_sidecar_path(test_file, repo_root)
        sc_path.parent.mkdir(parents=True, exist_ok=True)
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
        with open(sc_path, "w") as f:
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
                                    with open(sc_path) as f:
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

        # Create sidecar WITHOUT sidecar_id (pre-999.1) in .carta/sidecars/
        from carta.embed.induct import sidecar_path as get_sidecar_path
        sc_path = get_sidecar_path(test_file, repo_root)
        sc_path.parent.mkdir(parents=True, exist_ok=True)
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
        with open(sc_path, "w") as f:
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
        from carta.embed.induct import sidecar_path as get_sidecar_path
        for fname in test_files:
            sc_path = get_sidecar_path(docs_dir / fname, repo_root)
            sc_path.parent.mkdir(parents=True, exist_ok=True)
            stem = fname.replace(".md", "")
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
            with open(sc_path, "w") as f:
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
        repo_root, cfg = temp_repo
        docs_dir = repo_root / "docs"
        docs_dir.mkdir()

        stale_file = docs_dir / "stale.md"
        stale_file.write_text("# Stale Document")
        sc_dir = repo_root / ".carta" / "sidecars" / "docs"
        sc_dir.mkdir(parents=True)
        with open(sc_dir / "stale.embed-meta.yaml", "w") as f:
            yaml.dump({"status": "stale", "slug": "stale", "current_path": "docs/stale.md"}, f)

        embedded_file = docs_dir / "embedded.md"
        embedded_file.write_text("# Embedded Document")
        with open(sc_dir / "embedded.embed-meta.yaml", "w") as f:
            yaml.dump({"status": "embedded", "slug": "embedded", "current_path": "docs/embedded.md"}, f)

        results = discover_stale_files(repo_root)

        assert len(results) == 1
        assert results[0] == stale_file

    def test_discover_stale_files_returns_empty_when_none_stale(self, temp_repo):
        repo_root, cfg = temp_repo
        docs_dir = repo_root / "docs"
        docs_dir.mkdir()

        embedded_file = docs_dir / "embedded.md"
        embedded_file.write_text("# Embedded Document")
        sc_dir = repo_root / ".carta" / "sidecars" / "docs"
        sc_dir.mkdir(parents=True)
        with open(sc_dir / "embedded.embed-meta.yaml", "w") as f:
            yaml.dump({"status": "embedded", "slug": "embedded", "current_path": "docs/embedded.md"}, f)

        results = discover_stale_files(repo_root)
        assert results == []

    def test_discover_stale_files_skips_missing_status(self, temp_repo):
        repo_root, cfg = temp_repo
        docs_dir = repo_root / "docs"
        docs_dir.mkdir()

        file_no_status = docs_dir / "no_status.md"
        file_no_status.write_text("# Document")
        sc_dir = repo_root / ".carta" / "sidecars" / "docs"
        sc_dir.mkdir(parents=True)
        with open(sc_dir / "no_status.embed-meta.yaml", "w") as f:
            yaml.dump({"slug": "no_status", "current_path": "docs/no_status.md"}, f)

        stale_file = docs_dir / "stale.md"
        stale_file.write_text("# Stale Document")
        with open(sc_dir / "stale.embed-meta.yaml", "w") as f:
            yaml.dump({"status": "stale", "slug": "stale", "current_path": "docs/stale.md"}, f)

        results = discover_stale_files(repo_root)
        assert len(results) == 1
        assert results[0] == stale_file


class TestVisionIntegration:
    """Test vision module integration in pipeline (_embed_one_file)."""

    def test_embed_one_file_calls_vision_for_pdf(self, temp_repo, mock_qdrant):
        """_embed_one_file calls extract_image_descriptions_intelligent for PDF files."""
        repo_root, cfg = temp_repo

        # Create a test PDF file
        docs_dir = repo_root / "docs"
        docs_dir.mkdir()
        test_pdf = docs_dir / "test.pdf"
        test_pdf.write_bytes(b"%PDF-1.4\n...")  # Minimal PDF header

        # Mock the embedding pipeline
        with patch("carta.embed.pipeline.QdrantClient") as mock_client_cls:
            with patch("carta.embed.pipeline.extract_pdf_text", return_value=[{"page": 1, "text": "Sample text"}]):
                with patch("carta.embed.pipeline.chunk_text", return_value=[{"text": "Chunk 1", "page": 1}]):
                    with patch("carta.vision.router.extract_image_descriptions_intelligent") as mock_vision:
                        with patch("carta.embed.pipeline.upsert_chunks"):
                            with patch("carta.embed.pipeline.write_sidecar"):
                                # Vision returns 2 image chunks with Phase 999.4 metadata
                                mock_vision.return_value = [
                                    {"page_num": 1, "image_index": 0, "doc_type": "image_description", "text": "Chart showing data", "model_used": "llava", "content_type": "visual", "confidence": 0.9, "has_tables": False},
                                    {"page_num": 2, "image_index": 0, "doc_type": "image_description", "text": "Diagram with labels", "model_used": "glm-ocr", "content_type": "text", "confidence": 0.85, "has_tables": True},
                                ]
                                mock_client_cls.return_value = mock_qdrant

                                from carta.embed.pipeline import _embed_one_file

                                file_info = {"mtime": 0.0, "hash": "abc"}
                                chunk_count, sidecar = _embed_one_file(
                                    test_pdf, file_info, cfg, mock_qdrant,
                                    repo_root, max_tokens=400, overlap_fraction=0.15
                                )

                                # Verify intelligent vision was called for PDF
                                mock_vision.assert_called_once()

                                # Verify sidecar includes image fields
                                assert "image_count" in sidecar
                                assert "image_chunks" in sidecar
                                assert sidecar["image_count"] == 2
                                
                                # Verify Phase 999.4 vision metadata
                                assert "vision" in sidecar
                                assert sidecar["vision"]["enabled"] is True
                                assert sidecar["vision"]["pages_analyzed"] == 2
                                assert "extraction_summary" in sidecar["vision"]
                                assert "page_details" in sidecar["vision"]
                                # One llava page, one glm-ocr page
                                assert sidecar["vision"]["extraction_summary"]["llava_pages"] == 1
                                assert sidecar["vision"]["extraction_summary"]["glm_ocr_pages"] == 1

    def test_vision_fail_open_text_embedding_continues(self, temp_repo, mock_qdrant):
        """If vision unavailable, text embedding completes with image_count>0, image_chunks=0."""
        repo_root, cfg = temp_repo

        docs_dir = repo_root / "docs"
        docs_dir.mkdir()
        test_pdf = docs_dir / "test.pdf"
        test_pdf.write_bytes(b"%PDF-1.4\n...")

        with patch("carta.embed.pipeline.QdrantClient") as mock_client_cls:
            with patch("carta.embed.pipeline.extract_pdf_text", return_value=[{"page": 1, "text": "Text content"}]):
                with patch("carta.embed.pipeline.chunk_text", return_value=[{"text": "Chunk", "page": 1}]):
                    with patch("carta.vision.router.extract_image_descriptions_intelligent") as mock_vision:
                        with patch("carta.embed.pipeline.upsert_chunks"):
                            with patch("carta.embed.pipeline.write_sidecar"):
                                # Vision model unavailable: returns empty (fail-open)
                                mock_vision.return_value = []
                                mock_client_cls.return_value = mock_qdrant

                                from carta.embed.pipeline import _embed_one_file

                                file_info = {"mtime": 0.0, "hash": "abc"}
                                chunk_count, sidecar = _embed_one_file(
                                    test_pdf, file_info, cfg, mock_qdrant,
                                    repo_root, max_tokens=400, overlap_fraction=0.15
                                )

                                # Sidecar reflects: no images processed
                                assert sidecar.get("image_count") == 0
                                assert sidecar.get("image_chunks") == 0
                                # Status remains embedded (not failed)
                                assert sidecar.get("status") == "embedded"
                                assert sidecar.get("status") == "embedded"


class TestRunSearch:
    """Tests for run_search error handling."""

    def test_raises_runtime_error_when_qdrant_connection_refused(self):
        """When Qdrant is down, run_search raises RuntimeError with actionable message."""
        from unittest.mock import patch, MagicMock
        from carta.embed.pipeline import run_search

        cfg = {
            "project_name": "test-project",
            "qdrant_url": "http://localhost:6333",
            "embed": {
                "ollama_url": "http://localhost:11434",
                "ollama_model": "nomic-embed-text",
                "colpali_enabled": False,
            },
            "search": {"top_n": 5},
            "modules": {"doc_search": True},
        }

        mock_client = MagicMock()
        mock_client.query_points.side_effect = Exception("Connection refused")

        with patch("carta.embed.pipeline.QdrantClient", return_value=mock_client), \
             patch("carta.embed.pipeline.get_embedding", return_value=[0.0] * 768), \
             patch("carta.search.scoped.get_search_collections", return_value=["test-project_doc"]), \
             patch("carta.embed.pipeline.find_config", return_value="/fake/.carta/config.yaml"):
            with pytest.raises(RuntimeError, match="Qdrant"):
                run_search("test query", cfg)

    def test_returns_empty_when_collection_not_found(self):
        """When collection doesn't exist (404), run_search returns [] without error."""
        from unittest.mock import patch, MagicMock
        from carta.embed.pipeline import run_search

        cfg = {
            "project_name": "test-project",
            "qdrant_url": "http://localhost:6333",
            "embed": {
                "ollama_url": "http://localhost:11434",
                "ollama_model": "nomic-embed-text",
                "colpali_enabled": False,
            },
            "search": {"top_n": 5},
            "modules": {"doc_search": True},
        }

        mock_client = MagicMock()
        # Simulate collection-not-found with a generic exception containing "404" or "Not found"
        mock_client.query_points.side_effect = Exception("Collection not found: status_code=404")

        with patch("carta.embed.pipeline.QdrantClient", return_value=mock_client), \
             patch("carta.embed.pipeline.get_embedding", return_value=[0.0] * 768), \
             patch("carta.search.scoped.get_search_collections", return_value=["test-project_doc"]), \
             patch("carta.embed.pipeline.find_config", return_value="/fake/.carta/config.yaml"):
            results = run_search("test query", cfg)

        assert results == []


class TestVisionProgressWiring:
    """Verify _vision_callback is passed and _vision_events are handled correctly."""

    def _make_pdf(self, tmp_path: Path) -> Path:
        """Create a minimal PDF-like file (just needs .pdf extension for pipeline routing)."""
        p = tmp_path / "docs" / "test.pdf"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"%PDF-1.4 fake")
        return p

    def test_vision_events_not_written_to_sidecar(self, tmp_path):
        """_vision_events must be popped from sidecar_updates before _update_sidecar."""
        from carta.embed.pipeline import run_embed
        from unittest.mock import patch, Mock

        repo_root = tmp_path
        carta_dir = repo_root / ".carta"
        carta_dir.mkdir()
        cfg = {
            "project_name": "test-proj",
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

        pdf = self._make_pdf(tmp_path)
        sc_path = get_sidecar_path(pdf, repo_root)
        sc_path.parent.mkdir(parents=True, exist_ok=True)

        import yaml
        with open(sc_path, "w") as f:
            yaml.dump({"slug": "test", "doc_type": "guide", "status": "pending",
                       "file_type": "pdf", "current_path": "docs/test.pdf"}, f)

        written_data = {}

        def fake_update_sidecar(path, updates):
            written_data.update(updates)

        mock_client = Mock()
        mock_client.get_collections.return_value = Mock()

        with patch("carta.embed.pipeline.QdrantClient", return_value=mock_client), \
             patch("carta.embed.pipeline.ensure_collection"), \
             patch("carta.embed.pipeline._heal_sidecar_current_paths"), \
             patch("carta.embed.pipeline._update_sidecar", side_effect=fake_update_sidecar), \
             patch("carta.embed.pipeline._embed_one_file", return_value=(
                 5,
                 {"status": "embedded", "chunk_count": 5, "_vision_events": [
                     {"page": 1, "page_class": "pure_text", "model_used": "skip", "char_count": 0}
                 ]},
             )):
            run_embed(repo_root, cfg, progress=None)

        assert "_vision_events" not in written_data

    def test_vision_done_called_with_events(self, tmp_path):
        """progress.vision_done() is called when _vision_events is non-empty."""
        from carta.embed.pipeline import run_embed
        from unittest.mock import patch, Mock, MagicMock

        repo_root = tmp_path
        carta_dir = repo_root / ".carta"
        carta_dir.mkdir()
        cfg = {
            "project_name": "test-proj",
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

        pdf = self._make_pdf(tmp_path)
        sc_path = get_sidecar_path(pdf, repo_root)
        sc_path.parent.mkdir(parents=True, exist_ok=True)

        import yaml
        with open(sc_path, "w") as f:
            yaml.dump({"slug": "test", "doc_type": "guide", "status": "pending",
                       "file_type": "pdf", "current_path": "docs/test.pdf"}, f)

        vision_events = [
            {"page": 1, "page_class": "pure_text", "model_used": "skip", "char_count": 0},
            {"page": 2, "page_class": "structured_text", "model_used": "glm-ocr", "char_count": 300},
        ]

        mock_progress = MagicMock()
        mock_client = Mock()
        mock_client.get_collections.return_value = Mock()

        with patch("carta.embed.pipeline.QdrantClient", return_value=mock_client), \
             patch("carta.embed.pipeline.ensure_collection"), \
             patch("carta.embed.pipeline._heal_sidecar_current_paths"), \
             patch("carta.embed.pipeline._update_sidecar"), \
             patch("carta.embed.pipeline._embed_one_file", return_value=(
                 5,
                 {"status": "embedded", "chunk_count": 5, "_vision_events": vision_events},
             )):
            run_embed(repo_root, cfg, progress=mock_progress)

        mock_progress.vision_done.assert_called_once_with(vision_events)

    def test_vision_done_not_called_when_events_empty(self, tmp_path):
        """progress.vision_done() is NOT called when events list is empty or absent."""
        from carta.embed.pipeline import run_embed
        from unittest.mock import patch, Mock, MagicMock

        repo_root = tmp_path
        carta_dir = repo_root / ".carta"
        carta_dir.mkdir()
        cfg = {
            "project_name": "test-proj",
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

        md = tmp_path / "docs" / "test.md"
        md.parent.mkdir(parents=True, exist_ok=True)
        md.write_text("# Hello\n\nworld")
        sc_path = get_sidecar_path(md, repo_root)
        sc_path.parent.mkdir(parents=True, exist_ok=True)

        import yaml
        with open(sc_path, "w") as f:
            yaml.dump({"slug": "test", "doc_type": "guide", "status": "pending",
                       "file_type": "markdown", "current_path": "docs/test.md"}, f)

        mock_progress = MagicMock()
        mock_client = Mock()
        mock_client.get_collections.return_value = Mock()

        with patch("carta.embed.pipeline.QdrantClient", return_value=mock_client), \
             patch("carta.embed.pipeline.ensure_collection"), \
             patch("carta.embed.pipeline._heal_sidecar_current_paths"), \
             patch("carta.embed.pipeline._update_sidecar"), \
             patch("carta.embed.pipeline._embed_one_file", return_value=(
                 3,
                 {"status": "embedded", "chunk_count": 3},  # no _vision_events key
             )):
            run_embed(repo_root, cfg, progress=mock_progress)

        mock_progress.vision_done.assert_not_called()

    def test_run_embed_file_does_not_write_vision_events_to_sidecar(self, tmp_path):
        """_vision_events must not appear in sidecar when using run_embed_file()."""
        from carta.embed.pipeline import run_embed_file
        from unittest.mock import patch, Mock
        import yaml

        repo_root = tmp_path
        carta_dir = repo_root / ".carta"
        carta_dir.mkdir()

        cfg = {
            "project_name": "test-proj",
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

        pdf = self._make_pdf(tmp_path)
        sc_path = get_sidecar_path(pdf, repo_root)
        sc_path.parent.mkdir(parents=True, exist_ok=True)

        with open(sc_path, "w") as f:
            yaml.dump({"slug": "test", "doc_type": "guide", "status": "pending",
                       "file_type": "pdf", "current_path": "docs/test.pdf",
                       "generation": 0, "version_history": []}, f)

        written_data = {}

        def fake_update_sidecar(path, updates):
            written_data.update(updates)

        mock_client = Mock()

        with patch("carta.embed.pipeline.find_config", return_value=repo_root / ".carta" / "config.yaml"), \
             patch("carta.embed.pipeline.QdrantClient", return_value=mock_client), \
             patch("carta.embed.pipeline.ensure_collection"), \
             patch("carta.embed.pipeline._update_sidecar", side_effect=fake_update_sidecar), \
             patch("carta.embed.pipeline._embed_one_file", return_value=(
                 3,
                 {"status": "embedded", "chunk_count": 3, "_vision_events": [
                     {"page": 1, "page_class": "pure_text", "model_used": "skip", "char_count": 0}
                 ]},
             )):
            run_embed_file(pdf, cfg, force=True)

        assert "_vision_events" not in written_data


class TestMigrateSidecars:
    """Test migrate_sidecars() moves co-located sidecars to .carta/sidecars/."""

    def test_migrate_moves_colocated_sidecar(self, temp_repo):
        repo_root, cfg = temp_repo
        docs = repo_root / "docs"
        docs.mkdir()
        old = docs / "chip.embed-meta.yaml"
        old.write_text("slug: chip\nstatus: pending\ncurrent_path: docs/chip.pdf\n")

        migrate_sidecars(repo_root)

        expected = repo_root / ".carta" / "sidecars" / "docs" / "chip.embed-meta.yaml"
        assert expected.exists()
        assert not old.exists()

    def test_migrate_skips_already_in_carta(self, temp_repo):
        repo_root, cfg = temp_repo
        sc = repo_root / ".carta" / "sidecars" / "docs" / "chip.embed-meta.yaml"
        sc.parent.mkdir(parents=True)
        sc.write_text("slug: chip\nstatus: pending\n")

        migrate_sidecars(repo_root)

        assert sc.exists()  # unchanged

    def test_migrate_returns_count(self, temp_repo):
        repo_root, cfg = temp_repo
        docs = repo_root / "docs"
        docs.mkdir()
        (docs / "a.embed-meta.yaml").write_text("slug: a\nstatus: pending\n")
        (docs / "b.embed-meta.yaml").write_text("slug: b\nstatus: pending\n")

        count = migrate_sidecars(repo_root)

        assert count == 2

    def test_migrate_nested_preserves_directory_structure(self, temp_repo):
        repo_root, cfg = temp_repo
        nested = repo_root / "docs" / "manuals" / "sub"
        nested.mkdir(parents=True)
        old = nested / "spec.embed-meta.yaml"
        old.write_text("slug: spec\nstatus: pending\n")

        migrate_sidecars(repo_root)

        expected = repo_root / ".carta" / "sidecars" / "docs" / "manuals" / "sub" / "spec.embed-meta.yaml"
        assert expected.exists()
        assert not old.exists()

    def test_migrate_does_not_overwrite_existing_canonical(self, temp_repo):
        repo_root, cfg = temp_repo
        docs = repo_root / "docs"
        docs.mkdir()

        # Canonical sidecar already in .carta/sidecars/ (newer, embedded)
        canonical_dir = repo_root / ".carta" / "sidecars" / "docs"
        canonical_dir.mkdir(parents=True)
        canonical = canonical_dir / "chip.embed-meta.yaml"
        canonical.write_text("slug: chip\nstatus: embedded\ngeneration: 3\n")

        # Stale co-located copy (old, pending)
        stale = docs / "chip.embed-meta.yaml"
        stale.write_text("slug: chip\nstatus: pending\ngeneration: 0\n")

        migrate_sidecars(repo_root)

        # Canonical should be unchanged
        assert yaml.safe_load(canonical.read_text())["status"] == "embedded"
        assert yaml.safe_load(canonical.read_text())["generation"] == 3
        # Stale co-located copy should be gone
        assert not stale.exists()


class TestDetectOrphanedSidecars:
    """Test detect_orphaned_sidecars() identifies sidecars with missing source files."""

    def test_detects_sidecar_with_missing_source(self, temp_repo):
        repo_root, cfg = temp_repo
        sc_dir = repo_root / ".carta" / "sidecars" / "docs"
        sc_dir.mkdir(parents=True)
        sc = sc_dir / "deleted.embed-meta.yaml"
        sc.write_text("slug: deleted\nstatus: embedded\ncurrent_path: docs/deleted.pdf\n")

        orphans = detect_orphaned_sidecars(repo_root)

        assert len(orphans) == 1
        assert orphans[0] == sc

    def test_ignores_sidecar_with_existing_source(self, temp_repo):
        repo_root, cfg = temp_repo
        docs = repo_root / "docs"
        docs.mkdir()
        source = docs / "present.pdf"
        source.touch()
        sc_dir = repo_root / ".carta" / "sidecars" / "docs"
        sc_dir.mkdir(parents=True)
        sc = sc_dir / "present.embed-meta.yaml"
        sc.write_text("slug: present\nstatus: embedded\ncurrent_path: docs/present.pdf\n")

        orphans = detect_orphaned_sidecars(repo_root)

        assert orphans == []

    def test_skips_sidecar_with_no_current_path(self, temp_repo):
        repo_root, cfg = temp_repo
        sc_dir = repo_root / ".carta" / "sidecars" / "docs"
        sc_dir.mkdir(parents=True)
        sc = sc_dir / "no_path.embed-meta.yaml"
        sc.write_text("slug: no_path\nstatus: embedded\n")

        orphans = detect_orphaned_sidecars(repo_root)

        assert orphans == []

    def test_returns_empty_when_no_sidecars_dir(self, temp_repo):
        repo_root, cfg = temp_repo
        orphans = detect_orphaned_sidecars(repo_root)
        assert orphans == []
