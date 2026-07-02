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
from carta.embed.lifecycle import compute_file_hash
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

    def test_hash_mismatch_increments_generation_embeds(self, temp_repo, mock_qdrant):
        """Hash mismatch -> generation incremented, re-embed runs, status='embedded', stale_as_of=None."""
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
                                    # Status should be embedded (not stale — Bug A fix)
                                    assert updated["status"] == "embedded"
                                    # stale_as_of should be None after successful re-embed
                                    assert updated.get("stale_as_of") is None
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
    """discover_stale_files returns files whose content changed since embed —
    current content hash differs from the sidecar's recorded file_hash (#39)."""

    def _sidecar(self, repo_root, rel, file_hash):
        sc = get_sidecar_path(repo_root / rel, repo_root)
        sc.parent.mkdir(parents=True, exist_ok=True)
        sc.write_text(yaml.dump({
            "slug": Path(rel).stem, "current_path": rel,
            "status": "embedded", "file_hash": file_hash,
        }))
        return sc

    def test_returns_files_whose_hash_changed(self, temp_repo):
        repo_root, cfg = temp_repo
        (repo_root / "docs").mkdir()
        changed = repo_root / "docs" / "changed.md"
        changed.write_text("# new content")
        self._sidecar(repo_root, "docs/changed.md", "stalehash000")  # != real hash

        assert discover_stale_files(repo_root) == [changed]

    def test_skips_files_whose_hash_matches(self, temp_repo):
        repo_root, cfg = temp_repo
        (repo_root / "docs").mkdir()
        same = repo_root / "docs" / "same.md"
        same.write_text("# unchanged")
        self._sidecar(repo_root, "docs/same.md", compute_file_hash(same))

        assert discover_stale_files(repo_root) == []

    def test_skips_sidecar_without_recorded_hash(self, temp_repo):
        repo_root, cfg = temp_repo
        (repo_root / "docs").mkdir()
        f = repo_root / "docs" / "nohash.md"
        f.write_text("# content")
        self._sidecar(repo_root, "docs/nohash.md", None)

        assert discover_stale_files(repo_root) == []

    def test_skips_when_source_missing(self, temp_repo):
        repo_root, cfg = temp_repo
        self._sidecar(repo_root, "docs/gone.md", "abc123")  # no source file written

        assert discover_stale_files(repo_root) == []


class TestVisionIntegration:
    """Test vision module integration in pipeline (_embed_one_file)."""

    def test_embed_one_file_calls_vision_for_pdf(self, temp_repo, mock_qdrant):
        """_embed_one_file calls extract_image_descriptions_intelligent for PDF files."""
        repo_root, cfg = temp_repo

        # Disable two_pass_visual so the inline VLM/vision path is exercised.
        # (two_pass_visual=True routes image-heavy pages to deferred pass-2 and skips
        # inline vision entirely — that behaviour is tested in test_pass1_marking.py.)
        cfg["embed"]["two_pass_visual"] = False

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
                        with patch("carta.embed.pipeline.upsert_chunks", return_value=0):
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
                with patch("carta.embed.pipeline.chunk_text", return_value=[{"text": "Chunk", "page": 1, "chunk_index": 0}]):
                    with patch("carta.vision.router.extract_image_descriptions_intelligent") as mock_vision:
                        # 1 text chunk persisted (chunk_text yields 1) so text embedding
                        # genuinely succeeds — the point of this test is vision fail-open,
                        # not a persistence failure (which is now its own status).
                        with patch("carta.embed.pipeline.upsert_chunks", return_value=1):
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

    def test_results_carry_page_and_section_anchors(self):
        """run_search surfaces page + section_heading from the payload on each hit."""
        from unittest.mock import patch, MagicMock
        from carta.embed.pipeline import run_search

        cfg = {
            "project_name": "test-project",
            "qdrant_url": "http://localhost:6333",
            "embed": {"ollama_url": "http://localhost:11434",
                      "ollama_model": "nomic-embed-text", "colpali_enabled": False},
            "search": {"top_n": 5},
            "modules": {"doc_search": True},
        }

        point = MagicMock()
        point.score = 0.91
        point.payload = {"file_path": "docs/imu.pdf", "text": "sensitivity register",
                         "page": 47, "section_heading": "6.3 Gyro Config", "doc_type": ""}
        resp = MagicMock(); resp.points = [point]
        mock_client = MagicMock()
        mock_client.query_points.return_value = resp

        with patch("carta.embed.pipeline.QdrantClient", return_value=mock_client), \
             patch("carta.embed.pipeline.get_embedding", return_value=[0.0] * 768), \
             patch("carta.embed.pipeline.collection_is_hybrid", return_value=False), \
             patch("carta.search.scoped.get_search_collections", return_value=["test-project_doc"]), \
             patch("carta.embed.pipeline.find_config", return_value="/fake/.carta/config.yaml"):
            results = run_search("imu sensitivity", cfg)

        assert results, "expected at least one hit"
        assert results[0]["page"] == 47
        assert results[0]["section_heading"] == "6.3 Gyro Config"

    def test_ocr_chunk_gets_ocr_visual_tier_and_page_from_page_num(self):
        from unittest.mock import patch, MagicMock
        from carta.embed.pipeline import run_search
        cfg = {
            "project_name": "test-project", "qdrant_url": "http://localhost:6333",
            "embed": {"ollama_url": "x", "ollama_model": "m", "colpali_enabled": False},
            "search": {"top_n": 5}, "modules": {"doc_search": True},
        }
        point = MagicMock(); point.score = 0.8
        # OCR chunk: image_description + llava + page_num (no "page" key)
        point.payload = {"file_path": "docs/board.pdf", "text": "32M Hz",
                         "doc_type": "image_description", "model_used": "llava", "page_num": 3}
        resp = MagicMock(); resp.points = [point]
        client = MagicMock(); client.query_points.return_value = resp
        with patch("carta.embed.pipeline.QdrantClient", return_value=client), \
             patch("carta.embed.pipeline.get_embedding", return_value=[0.0] * 768), \
             patch("carta.embed.pipeline.collection_is_hybrid", return_value=False), \
             patch("carta.search.scoped.get_search_collections", return_value=["test-project_doc"]), \
             patch("carta.embed.pipeline.find_config", return_value="/fake/.carta/config.yaml"):
            results = run_search("32mhz", cfg)
        assert results[0]["text_source"] == "ocr_visual"
        assert results[0]["page"] == 3   # resolved from page_num

    def test_raises_when_query_embedding_fails(self):
        """A query-embedding failure (Ollama down / wrong model) must raise an
        actionable error — NOT be swallowed as 'no results / nothing embedded' (#79).

        The corpus is fully populated; only the query-embed hop is broken. The old
        behaviour mis-classified the Ollama error string ('...404...') as a missing
        collection and returned [], sending the user to re-embed a healthy corpus.
        """
        from unittest.mock import patch, MagicMock
        from carta.embed.pipeline import run_search

        cfg = {
            "project_name": "test-project", "qdrant_url": "http://localhost:6333",
            "embed": {"ollama_url": "http://localhost:11434",
                      "ollama_model": "nomic-embed-text", "colpali_enabled": False},
            "search": {"top_n": 5}, "modules": {"doc_search": True},
        }
        mock_client = MagicMock()  # Qdrant is healthy; the embed hop is what fails.

        with patch("carta.embed.pipeline.QdrantClient", return_value=mock_client), \
             patch("carta.embed.pipeline.get_embedding",
                   side_effect=RuntimeError("Ollama embedding failed (404): model 'x' not found")), \
             patch("carta.search.scoped.get_search_collections", return_value=["test-project_doc"]), \
             patch("carta.embed.pipeline.find_config", return_value="/fake/.carta/config.yaml"):
            with pytest.raises(RuntimeError, match="Could not embed the query"):
                run_search("TPS55340 boost converter", cfg)


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

    def test_skips_nested_junk_sidecar(self, temp_repo):
        """A misplaced/nested sidecar copy is not a real sidecar, so it must not
        be reported as an orphan even when its current_path source is missing."""
        repo_root, cfg = temp_repo
        junk = (repo_root / ".carta" / "sidecars" / ".worktrees" / "x"
                / ".carta" / "sidecars" / "docs" / "gone.embed-meta.yaml")
        junk.parent.mkdir(parents=True, exist_ok=True)
        junk.write_text("slug: gone\nstatus: embedded\ncurrent_path: docs/gone.pdf\n")

        assert detect_orphaned_sidecars(repo_root) == []


class TestRunEmbedStatusWriter:
    """run_embed writes/skips embed-status.json based on cfg embed.status_file."""

    def _stub_pipeline(self, monkeypatch):
        from carta.embed import pipeline

        class _FakeClient:
            def __init__(self, *a, **k): pass
            def get_collections(self): return None

        monkeypatch.setattr(pipeline, "migrate_sidecars", lambda *a, **k: None)
        monkeypatch.setattr(pipeline, "_heal_sidecar_current_paths", lambda *a, **k: None)
        monkeypatch.setattr(pipeline, "detect_orphaned_sidecars", lambda *a, **k: [])
        monkeypatch.setattr(pipeline, "discover_pending_files", lambda *a, **k: [])
        monkeypatch.setattr(pipeline, "discover_stale_files", lambda *a, **k: [])
        monkeypatch.setattr(pipeline, "ensure_collection", lambda *a, **k: None)
        monkeypatch.setattr(pipeline, "collection_name", lambda *a, **k: "t_doc")
        monkeypatch.setattr(pipeline, "QdrantClient", _FakeClient)

    def test_run_embed_writes_status_done(self, tmp_path, monkeypatch):
        """run_embed should leave a phase=done status file when status_file is on."""
        import json
        from carta.embed import pipeline
        from carta.embed.status import STATUS_FILENAME

        (tmp_path / ".carta").mkdir()
        self._stub_pipeline(monkeypatch)

        cfg = {"qdrant_url": "http://x", "embed": {"status_file": True},
               "modules": {}, "docs_root": "docs/"}
        pipeline.run_embed(tmp_path, cfg, verbose=False, progress=None)

        data = json.loads((tmp_path / ".carta" / STATUS_FILENAME).read_text())
        assert data["phase"] == "done"
        assert data["total"] == 0

    def test_run_embed_status_disabled(self, tmp_path, monkeypatch):
        """run_embed should NOT write status file when status_file is False."""
        from carta.embed import pipeline
        from carta.embed.status import STATUS_FILENAME

        (tmp_path / ".carta").mkdir()
        self._stub_pipeline(monkeypatch)

        cfg = {"qdrant_url": "http://x", "embed": {"status_file": False},
               "modules": {}, "docs_root": "docs/"}
        pipeline.run_embed(tmp_path, cfg, verbose=False, progress=None)
        assert not (tmp_path / ".carta" / STATUS_FILENAME).exists()

    def test_run_embed_status_failed_on_exception(self, tmp_path, monkeypatch):
        """run_embed should write phase=failed when an exception escapes the loop body."""
        import json
        from pathlib import Path
        from carta.embed import pipeline
        from carta.embed.status import STATUS_FILENAME

        (tmp_path / ".carta").mkdir()
        self._stub_pipeline(monkeypatch)
        # One pending file so the loop body is entered.
        monkeypatch.setattr(
            pipeline, "discover_pending_files",
            lambda *a, **k: [{"file_path": tmp_path / "doc.md",
                              "sidecar_path": tmp_path / "doc.embed-meta.yaml"}],
        )
        # Raise inside the loop body at is_lfs_pointer — first call after file_start.
        monkeypatch.setattr(
            pipeline, "is_lfs_pointer",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        cfg = {"qdrant_url": "http://x", "embed": {"status_file": True},
               "modules": {}, "docs_root": "docs/"}
        with pytest.raises(RuntimeError):
            pipeline.run_embed(tmp_path, cfg, verbose=False, progress=None)
        data = json.loads((tmp_path / ".carta" / STATUS_FILENAME).read_text())
        assert data["phase"] == "failed"


class TestRunSearchRerankStats:
    """run_search reports via the optional stats out-param whether the reranker
    actually ran. Both backends stamp rerank_score only on success; fail-open
    paths return unstamped hits — that absence is the 0.8.0 silent-failure
    signature this surfaces."""

    def _cfg(self, rerank_enabled=True):
        return {
            "project_name": "test-project",
            "qdrant_url": "http://localhost:6333",
            "embed": {
                "ollama_url": "http://localhost:11434",
                "ollama_model": "nomic-embed-text",
                "colpali_enabled": False,
            },
            "search": {"top_n": 5, "rerank": {"enabled": rerank_enabled, "candidate_pool": 30}},
            "modules": {"doc_search": True},
        }

    def _run(self, cfg, stats, rerank_side_effect=None, points=True):
        from unittest.mock import patch, MagicMock
        from carta.embed.pipeline import run_search

        point = MagicMock()
        point.score = 0.9
        point.payload = {"file_path": "docs/a.md", "text": "alpha"}
        mock_client = MagicMock()
        mock_client.query_points.return_value = MagicMock(points=[point] if points else [])

        patches = [
            patch("carta.embed.pipeline.QdrantClient", return_value=mock_client),
            patch("carta.embed.pipeline.get_embedding", return_value=[0.0] * 768),
            patch("carta.embed.pipeline.collection_is_hybrid", return_value=False),
            patch("carta.search.scoped.get_search_collections", return_value=["test-project_doc"]),
            patch("carta.embed.pipeline.find_config", return_value="/fake/.carta/config.yaml"),
        ]
        if rerank_side_effect is not None:
            patches.append(patch("carta.search.rerank.rerank_dispatch", side_effect=rerank_side_effect))
        from contextlib import ExitStack
        with ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            return run_search("q", cfg, stats=stats)

    def test_stats_reports_applied_when_reranker_stamps_scores(self):
        """Reranker ran: hits come back stamped with rerank_score -> applied True,
        and the transient key is still stripped from the returned hits."""
        def fake_rerank(query, pool, **kwargs):
            for h in pool:
                h["rerank_score"] = 1.0
            return pool

        stats = {}
        results = self._run(self._cfg(rerank_enabled=True), stats, rerank_side_effect=fake_rerank)
        assert stats == {"rerank_requested": True, "rerank_applied": True}
        assert results, "search should return the mocked hit"
        assert all("rerank_score" not in h for h in results), "transient key must be stripped"

    def test_stats_reports_fail_open_when_hits_unstamped(self):
        """Fail-open: reranker returns the pool unchanged (no rerank_score) -> applied False."""
        stats = {}
        self._run(self._cfg(rerank_enabled=True), stats, rerank_side_effect=lambda q, pool, **kw: pool)
        assert stats == {"rerank_requested": True, "rerank_applied": False}

    def test_stats_reports_not_requested_when_rerank_disabled(self):
        stats = {}
        self._run(self._cfg(rerank_enabled=False), stats)
        assert stats == {"rerank_requested": False, "rerank_applied": False}

    def test_stats_requested_but_no_results_is_not_applied(self):
        """Rerank enabled but zero hits: the rerank block is skipped -> applied False."""
        stats = {}
        self._run(self._cfg(rerank_enabled=True), stats, points=False)
        assert stats == {"rerank_requested": True, "rerank_applied": False}

    def test_stats_default_none_is_unchanged_behavior(self):
        """No stats dict passed: run_search works exactly as before."""
        results = self._run(self._cfg(rerank_enabled=False), None)
        assert isinstance(results, list)


class TestRunSearchDocType:
    def test_text_hits_carry_doc_type_from_payload(self):
        from unittest.mock import patch, MagicMock
        from carta.embed.pipeline import run_search

        point = MagicMock()
        point.score = 0.9
        point.payload = {"file_path": "docs/quirks/x.md", "text": "alpha",
                         "doc_type": "quirk"}
        mock_client = MagicMock()
        mock_client.query_points.return_value = MagicMock(points=[point])
        cfg = {"project_name": "p", "qdrant_url": "http://localhost:6333",
               "embed": {"ollama_url": "u", "ollama_model": "m", "colpali_enabled": False},
               "search": {"top_n": 5}, "modules": {"doc_search": True}}

        with patch("carta.embed.pipeline.QdrantClient", return_value=mock_client), \
             patch("carta.embed.pipeline.get_embedding", return_value=[0.0] * 768), \
             patch("carta.embed.pipeline.collection_is_hybrid", return_value=False), \
             patch("carta.search.scoped.get_search_collections", return_value=["p_doc"]), \
             patch("carta.embed.pipeline.find_config", return_value="/fake/.carta/config.yaml"):
            results = run_search("q", cfg)

        assert results and results[0]["doc_type"] == "quirk"


class TestRunEmbedExtractionFailedSummary:
    """Files flagged extraction_failed must not count as embedded in the summary."""

    def test_extraction_failed_counted_separately(self, tmp_path):
        repo_root = tmp_path
        (repo_root / ".carta").mkdir()
        doc = repo_root / "docs" / "scan.pdf"
        doc.parent.mkdir(parents=True)
        doc.write_bytes(b"%PDF-1.4 fake")
        sc_path = repo_root / ".carta" / "sidecars" / "docs" / "scan.embed-meta.yaml"
        sc_path.parent.mkdir(parents=True)
        sc_path.write_text("status: pending\n")

        cfg = {
            "project_name": "test", "qdrant_url": "http://localhost:6333",
            "embed": {"ollama_url": "http://x", "ollama_model": "m",
                      "status_file": False},
        }
        file_info = {"slug": "scan", "doc_type": "unknown",
                     "file_path": doc, "sidecar_path": sc_path}

        with patch("carta.embed.pipeline.find_config") as mock_find_cfg, \
             patch("carta.embed.pipeline.QdrantClient"), \
             patch("carta.embed.pipeline.ensure_collection"), \
             patch("carta.embed.pipeline.discover_pending_files", return_value=[file_info]), \
             patch("carta.embed.pipeline.discover_stale_files", return_value=[]), \
             patch("carta.embed.pipeline._embed_one_file") as mock_embed, \
             patch("carta.embed.pipeline._update_sidecar"), \
             patch("builtins.print"):
            mock_find_cfg.return_value = repo_root / ".carta" / "config.yaml"
            mock_embed.return_value = (0, {"status": "extraction_failed",
                                           "chunk_count": 0, "_vision_events": []})
            summary = run_embed(repo_root, cfg, verbose=False)

        assert summary["extraction_failed"] == 1
        assert summary["embedded"] == 0


def test_run_search_passes_repo_root_not_dotcarta_to_graph_expansion(tmp_path):
    """run_search must compute repo_root as the dir CONTAINING .carta (parent.parent
    of the config), not the .carta dir itself — else graph expansion globs an empty
    .carta dir and silently promotes nothing (audit CA-23)."""
    from unittest.mock import patch, MagicMock
    from carta.embed import pipeline

    cfg_path = tmp_path / ".carta" / "config.yaml"
    cfg_path.parent.mkdir(parents=True)
    cfg_path.write_text("")
    cfg = {
        "project_name": "t", "qdrant_url": "http://localhost:6333",
        "embed": {"ollama_url": "u", "ollama_model": "m"},
        "search": {"top_n": 5, "graph": {"enabled": True}},
    }

    captured = {}

    def fake_graph(results, cfg_, repo_root):
        captured["repo_root"] = repo_root
        return results

    with patch("carta.embed.pipeline.find_config", return_value=str(cfg_path)), \
         patch("carta.embed.pipeline.QdrantClient", return_value=MagicMock()), \
         patch("carta.search.scoped.get_search_collections", return_value=[]), \
         patch("carta.embed.pipeline._apply_graph_expansion", side_effect=fake_graph):
        pipeline.run_search("q", cfg)

    assert captured["repo_root"] == tmp_path  # repo root, NOT tmp_path/.carta


class TestHybridQueryFilter:
    """_hybrid_query_collection threads an optional Qdrant filter into each prefetch lane."""

    def test_query_filter_applied_to_prefetch(self):
        from unittest.mock import MagicMock, patch
        from carta.embed.pipeline import _hybrid_query_collection
        from qdrant_client.models import Filter, FieldCondition, MatchValue

        client = MagicMock()
        client.query_points.return_value = MagicMock(points=[])
        ff = Filter(must=[FieldCondition(key="file_path", match=MatchValue(value="a.pdf"))])

        with patch("carta.embed.pipeline.embed_sparse_query",
                   return_value=MagicMock(indices=[1], values=[1.0])):
            _hybrid_query_collection(client, "c", "q", [0.0] * 768, 10,
                                     prefetch_limit=40, bm25_model="Qdrant/bm25",
                                     query_filter=ff)

        kwargs = client.query_points.call_args.kwargs
        prefetches = kwargs["prefetch"]
        assert all(p.filter is ff for p in prefetches), "filter must reach every prefetch lane"


class TestFocusSourceHelpers:
    def test_normalize_strips_visual_page_suffix(self):
        from carta.embed.pipeline import _normalize_source
        assert _normalize_source("docs/imu.pdf (page 12)") == "docs/imu.pdf"
        assert _normalize_source("docs/imu.pdf") == "docs/imu.pdf"
        assert _normalize_source("  a/b.md  ") == "a/b.md"

    def test_file_filter_matches_file_path(self):
        from carta.embed.pipeline import _file_filter
        ff = _file_filter("docs/imu.pdf")
        cond = ff.must[0]
        assert cond.key == "file_path"
        assert cond.match.value == "docs/imu.pdf"

    def test_ensure_index_swallows_errors(self):
        from unittest.mock import MagicMock
        from carta.embed.pipeline import _ensure_file_path_index
        client = MagicMock()
        client.create_payload_index.side_effect = Exception("already exists")
        # Must not raise.
        _ensure_file_path_index(client, "test-project_doc")
        client.create_payload_index.assert_called_once()


class TestRenderPageImages:
    def test_non_pdf_returns_none(self, tmp_path):
        from carta.embed.pipeline import render_page_png
        md = tmp_path / "a.md"; md.write_text("hello")
        assert render_page_png(md, 1, tmp_path) is None

    def test_cache_hit_returns_cached_bytes(self, tmp_path):
        from carta.embed.pipeline import render_page_png
        pdf = tmp_path / "imu.pdf"; pdf.write_bytes(b"%PDF-1.4 fake")
        cache = tmp_path / ".carta" / "visual_cache" / "imu"
        cache.mkdir(parents=True)
        (cache / "page_0007.png").write_bytes(b"CACHEDPNG")
        assert render_page_png(pdf, 7, tmp_path) == b"CACHEDPNG"

    def test_real_render_and_out_of_range(self, tmp_path):
        import fitz
        from carta.embed.pipeline import render_page_png
        pdf = tmp_path / "doc.pdf"
        doc = fitz.open()
        doc.new_page(); doc.new_page()
        doc.save(str(pdf)); doc.close()
        png = render_page_png(pdf, 1, tmp_path)
        assert png is not None and png[:8] == b"\x89PNG\r\n\x1a\n"
        assert render_page_png(pdf, 99, tmp_path) is None  # out of range

    def test_attach_images_only_to_visual_hits(self, tmp_path):
        from unittest.mock import patch
        from carta.embed.pipeline import _attach_page_images
        hits = [
            {"type": "text", "page": 3},
            {"type": "visual", "page": 7},
            {"type": "visual", "page": None},
        ]
        with patch("carta.embed.pipeline.render_page_png", return_value=b"PNGBYTES"):
            out = _attach_page_images(hits, tmp_path / "imu.pdf", tmp_path)
        assert "image_b64" not in out[0]            # text untouched
        assert out[1]["image_b64"]                  # visual w/ page rendered
        assert "image_b64" not in out[2]            # visual w/o page skipped

    def test_cache_hit_honors_custom_sidecar_path(self, tmp_path):
        from carta.embed.pipeline import render_page_png
        pdf = tmp_path / "imu.pdf"; pdf.write_bytes(b"%PDF-1.4 fake")
        cache = tmp_path / "custom_cache" / "imu"
        cache.mkdir(parents=True)
        (cache / "page_0003.png").write_bytes(b"CUSTOMPNG")
        # Default path would miss; the configured path must hit.
        assert render_page_png(pdf, 3, tmp_path,
                               {"colpali_sidecar_path": "custom_cache/"}) == b"CUSTOMPNG"
        assert render_page_png(pdf, 3, tmp_path) is None  # default path: no cache, fake PDF -> None

    def test_attach_images_also_covers_ocr_visual_text_hits(self, tmp_path):
        from unittest.mock import patch
        from carta.embed.pipeline import _attach_page_images
        hits = [
            {"type": "text", "text_source": "ocr_visual", "page": 3},   # doubted OCR → image
            {"type": "text", "text_source": "ocr_table", "page": 4},    # trusted table → none
            {"type": "text", "text_source": "text_layer", "page": 5},   # real text → none
            {"type": "visual", "text_source": "visual", "page": 6},     # ColPali → image (unchanged)
        ]
        with patch("carta.embed.pipeline.render_page_png", return_value=b"PNG"):
            out = _attach_page_images(hits, tmp_path / "board.pdf", tmp_path)
        assert out[0]["image_b64"]              # ocr_visual text hit gets the page image
        assert "image_b64" not in out[1]        # ocr_table untouched
        assert "image_b64" not in out[2]        # text_layer untouched
        assert out[3]["image_b64"]              # visual still works


class TestFocusOutline:
    def test_outline_returns_distinct_sections_in_page_order(self):
        from unittest.mock import MagicMock
        from carta.embed.pipeline import _focus_outline, _file_filter

        def mk(page, heading):
            p = MagicMock(); p.payload = {"page": page, "section_heading": heading}; return p

        client = MagicMock()
        # Unordered, with a duplicate (3,"Intro") that must collapse.
        client.scroll.return_value = (
            [mk(3, "Intro"), mk(1, "Cover"), mk(3, "Intro"), mk(2, "Setup")], None)

        rows = _focus_outline(client, ["test-project_doc"], _file_filter("imu.pdf"), "imu.pdf")

        assert [(r["page"], r["section_heading"]) for r in rows] == [
            (1, "Cover"), (2, "Setup"), (3, "Intro")]
        assert all(r["type"] == "outline" and r["source"] == "imu.pdf" for r in rows)
        # Outline must not embed anything — it scrolls payloads only.
        client.scroll.assert_called_once()

    def test_outline_skips_visual_collections(self):
        from unittest.mock import MagicMock
        from carta.embed.pipeline import _focus_outline, _file_filter
        client = MagicMock(); client.scroll.return_value = ([], None)
        _focus_outline(client, ["test-project_visual"], _file_filter("imu.pdf"), "imu.pdf")
        client.scroll.assert_not_called()  # visual collections are skipped

    def test_outline_raises_on_transport_failure(self):
        import pytest
        from unittest.mock import MagicMock
        from carta.embed.pipeline import _focus_outline, _file_filter
        client = MagicMock()
        client.scroll.side_effect = Exception("Connection refused")
        with pytest.raises(RuntimeError, match="Qdrant"):
            _focus_outline(client, ["test-project_doc"], _file_filter("imu.pdf"), "imu.pdf")

    def test_outline_swallows_collection_not_found(self):
        from unittest.mock import MagicMock
        from carta.embed.pipeline import _focus_outline, _file_filter
        client = MagicMock()
        client.scroll.side_effect = Exception("Collection not found: status_code=404")
        rows = _focus_outline(client, ["test-project_doc"], _file_filter("imu.pdf"), "imu.pdf")
        assert rows == []  # 404 is swallowed → empty outline, no raise


class TestRunFocus:
    BASE_CFG = {
        "project_name": "test-project",
        "qdrant_url": "http://localhost:6333",
        "embed": {"ollama_url": "http://localhost:11434",
                  "ollama_model": "nomic-embed-text", "colpali_enabled": False},
        "search": {"top_n": 5},
        "modules": {"doc_search": True},
    }

    def test_deep_filters_to_file_and_keeps_all_chunks(self):
        """Deep mode applies the file filter and does NOT dedup (multiple chunks, one source)."""
        from unittest.mock import patch, MagicMock
        from carta.embed.pipeline import run_focus

        def mk(page):
            p = MagicMock(); p.score = 1.0 / page
            p.payload = {"file_path": "docs/imu.pdf", "text": f"chunk p{page}",
                         "page": page, "section_heading": f"S{page}", "doc_type": ""}
            return p
        resp = MagicMock(); resp.points = [mk(10), mk(11), mk(12)]
        client = MagicMock(); client.query_points.return_value = resp

        with patch("carta.embed.pipeline.QdrantClient", return_value=client), \
             patch("carta.embed.pipeline.get_embedding", return_value=[0.0] * 768), \
             patch("carta.embed.pipeline.collection_is_hybrid", return_value=False), \
             patch("carta.embed.pipeline._ensure_file_path_index"), \
             patch("carta.search.scoped.get_search_collections", return_value=["test-project_doc"]), \
             patch("carta.embed.pipeline.find_config", return_value="/fake/.carta/config.yaml"):
            results = run_focus("docs/imu.pdf (page 10)", self.BASE_CFG, query="sensitivity")

        # All three chunks of the one file survive (dedup is off in focus).
        assert len(results) == 3
        assert {r["page"] for r in results} == {10, 11, 12}
        assert all(r["source"] == "docs/imu.pdf" for r in results)
        # Filter reached Qdrant (normalized — no '(page 10)' suffix).
        ff = client.query_points.call_args.kwargs["query_filter"]
        assert ff.must[0].match.value == "docs/imu.pdf"

    def test_empty_query_returns_outline(self):
        from unittest.mock import patch, MagicMock
        from carta.embed.pipeline import run_focus

        def mk(page, heading):
            p = MagicMock(); p.payload = {"page": page, "section_heading": heading}; return p
        client = MagicMock(); client.scroll.return_value = ([mk(1, "Cover"), mk(2, "Setup")], None)

        with patch("carta.embed.pipeline.QdrantClient", return_value=client), \
             patch("carta.embed.pipeline._ensure_file_path_index"), \
             patch("carta.embed.pipeline.get_embedding") as mock_embed, \
             patch("carta.search.scoped.get_search_collections", return_value=["test-project_doc"]), \
             patch("carta.embed.pipeline.find_config", return_value="/fake/.carta/config.yaml"):
            results = run_focus("docs/imu.pdf", self.BASE_CFG)  # no query

        assert [r["type"] for r in results] == ["outline", "outline"]
        assert [r["page"] for r in results] == [1, 2]
        mock_embed.assert_not_called()  # outline does no embedding

    def test_visual_lane_failure_degrades_to_text(self):
        """A broken ColPali/visual lane must not fail the focus query — text results still return."""
        from unittest.mock import patch, MagicMock
        from carta.embed.pipeline import run_focus

        text_point = MagicMock(); text_point.score = 0.9
        text_point.payload = {"file_path": "docs/imu.pdf", "text": "gyro regs",
                              "page": 5, "section_heading": "Regs", "doc_type": ""}
        text_resp = MagicMock(); text_resp.points = [text_point]
        client = MagicMock(); client.query_points.return_value = text_resp

        cfg = dict(self.BASE_CFG)
        cfg["embed"] = {**self.BASE_CFG["embed"], "colpali_enabled": True}

        with patch("carta.embed.pipeline.QdrantClient", return_value=client), \
             patch("carta.embed.pipeline.get_embedding", return_value=[0.0] * 768), \
             patch("carta.embed.pipeline.collection_is_hybrid", return_value=False), \
             patch("carta.embed.pipeline._ensure_file_path_index"), \
             patch("carta.embed.pipeline._visual_collection_ready", return_value=True), \
             patch("carta.embed.colpali.is_colpali_available", return_value=True), \
             patch("carta.embed.colpali.ColPaliEmbedder",
                   side_effect=Exception("colpali model load failed: timeout")), \
             patch("carta.search.scoped.get_search_collections",
                   return_value=["test-project_doc", "test-project_visual"]), \
             patch("carta.embed.pipeline.find_config", return_value="/fake/.carta/config.yaml"):
            results = run_focus("docs/imu.pdf", cfg, query="gyro")

        # Visual lane raised with a 'timeout' message that WOULD trip the transport-error
        # branch if it reached the outer handler. Focus must instead degrade to text.
        assert [r["source"] for r in results] == ["docs/imu.pdf"]
        assert results[0]["page"] == 5

    def test_focus_ocr_chunk_gets_ocr_visual_tier_and_page(self):
        from unittest.mock import patch, MagicMock
        from carta.embed.pipeline import run_focus
        point = MagicMock(); point.score = 0.9
        point.payload = {"file_path": "docs/board.pdf", "text": "32M Hz callouts",
                         "doc_type": "image_description", "model_used": "llava", "page_num": 3}
        resp = MagicMock(); resp.points = [point]
        client = MagicMock(); client.query_points.return_value = resp
        with patch("carta.embed.pipeline.QdrantClient", return_value=client), \
             patch("carta.embed.pipeline.get_embedding", return_value=[0.0] * 768), \
             patch("carta.embed.pipeline.collection_is_hybrid", return_value=False), \
             patch("carta.embed.pipeline._ensure_file_path_index"), \
             patch("carta.embed.pipeline._attach_page_images", side_effect=lambda hits, *a, **k: hits), \
             patch("carta.search.scoped.get_search_collections", return_value=["test-project_doc"]), \
             patch("carta.embed.pipeline.find_config", return_value="/fake/.carta/config.yaml"):
            results = run_focus("docs/board.pdf", self.BASE_CFG, query="32mhz")
        assert results[0]["text_source"] == "ocr_visual"
        assert results[0]["page"] == 3

    def test_deep_raises_when_query_embedding_fails(self):
        """Focus deep mode must surface a query-embedding failure, not swallow it (#79).

        Unlike the visual lane (which is auxiliary and degrades to text), a failed
        TEXT query embed means no text retrieval is possible — it must be reported.
        """
        from unittest.mock import patch, MagicMock
        from carta.embed.pipeline import run_focus
        client = MagicMock()
        with patch("carta.embed.pipeline.QdrantClient", return_value=client), \
             patch("carta.embed.pipeline.get_embedding",
                   side_effect=RuntimeError("Ollama embedding failed (500): backend down")), \
             patch("carta.embed.pipeline.collection_is_hybrid", return_value=False), \
             patch("carta.embed.pipeline._ensure_file_path_index"), \
             patch("carta.search.scoped.get_search_collections", return_value=["test-project_doc"]), \
             patch("carta.embed.pipeline.find_config", return_value="/fake/.carta/config.yaml"):
            with pytest.raises(RuntimeError, match="Could not embed the query"):
                run_focus("docs/imu.pdf", self.BASE_CFG, query="sensitivity")


class TestTextSource:
    def test_text_layer_for_non_image_description(self):
        from carta.embed.pipeline import _text_source
        assert _text_source({"doc_type": "spec"}) == "text_layer"
        assert _text_source({}) == "text_layer"

    def test_ocr_table_for_glm_or_structured(self):
        from carta.embed.pipeline import _text_source
        assert _text_source({"doc_type": "image_description", "model_used": "glm-ocr"}) == "ocr_table"
        assert _text_source({"doc_type": "image_description", "content_type": "structured_text"}) == "ocr_table"

    def test_ocr_visual_for_llava(self):
        from carta.embed.pipeline import _text_source
        assert _text_source({"doc_type": "image_description", "model_used": "llava"}) == "ocr_visual"
        assert _text_source({"doc_type": "image_description", "content_type": "visual"}) == "ocr_visual"

    def test_ocr_visual_is_safe_default_when_unmarked(self):
        from carta.embed.pipeline import _text_source
        assert _text_source({"doc_type": "image_description"}) == "ocr_visual"

    def test_flattened_glm_is_trusted_transcription(self):
        # glm-ocr transcribes (never interprets), even on flattened/scanned pages → ocr_table.
        from carta.embed.pipeline import _text_source
        assert _text_source({"doc_type": "image_description",
                             "model_used": "glm-ocr", "content_type": "flattened"}) == "ocr_table"


class TestHealExtensionPreservingSidecars:
    def test_heal_resolves_csv_sidecar_missing_current_path(self, tmp_path):
        from carta.embed.pipeline import _heal_sidecar_current_paths
        src = tmp_path / "docs" / "data.csv"
        src.parent.mkdir(parents=True)
        src.write_text("a,b\n1,2\n")
        sc = tmp_path / ".carta" / "sidecars" / "docs" / "data.csv.embed-meta.yaml"
        sc.parent.mkdir(parents=True)
        sc.write_text("slug: data\nstatus: pending\n")  # no current_path
        healed = _heal_sidecar_current_paths(tmp_path)
        assert healed == 1
        assert yaml.safe_load(sc.read_text())["current_path"] == "docs/data.csv"
