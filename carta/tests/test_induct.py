"""Tests for carta embed induction (sidecar generation)."""

import pytest
from pathlib import Path
from carta.embed.induct import generate_sidecar_stub


class TestGenerateSidecarStub:
    """Test SIDECAR-01: lifecycle fields in stub schema."""

    def test_sidecar_stub_contains_lifecycle_fields(self, tmp_path):
        """Generated stub includes file_hash, hash_algorithm, generation, last_hash_check_at, version_history."""
        file_path = tmp_path / "docs" / "test.md"
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text("# Test")

        cfg = {
            "project_name": "test-project",
            "qdrant_url": "http://localhost:6333",
        }

        stub = generate_sidecar_stub(file_path, tmp_path, cfg)

        # Check new lifecycle fields
        assert "file_hash" in stub
        assert stub["file_hash"] is None
        assert "hash_algorithm" in stub
        assert stub["hash_algorithm"] == "sha256"
        assert "generation" in stub
        assert stub["generation"] == 0
        assert "last_hash_check_at" in stub
        assert stub["last_hash_check_at"] is None
        assert "version_history" in stub
        assert stub["version_history"] == []

    def test_sidecar_stub_preserves_existing_fields(self, tmp_path):
        """Existing fields (slug, doc_type, file_type, current_path, status) are unchanged."""
        file_path = tmp_path / "docs" / "my-file.md"
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text("# Content")

        cfg = {
            "project_name": "test-project",
            "qdrant_url": "http://localhost:6333",
        }

        stub = generate_sidecar_stub(file_path, tmp_path, cfg, notes="test notes")

        # Check existing fields still present
        assert "slug" in stub
        assert stub["slug"] == "my-file"
        assert "doc_type" in stub
        assert "file_type" in stub
        assert "current_path" in stub
        assert "status" in stub
        assert stub["status"] == "pending"
        assert "notes" in stub
        assert stub["notes"] == "test notes"

    def test_version_history_is_list(self, tmp_path):
        """version_history is initialized as an empty list, not None or missing."""
        file_path = tmp_path / "test.pdf"
        file_path.write_bytes(b"pdf content")

        cfg = {"project_name": "p", "qdrant_url": "http://localhost:6333"}
        stub = generate_sidecar_stub(file_path, tmp_path, cfg)

        assert isinstance(stub["version_history"], list)
        assert len(stub["version_history"]) == 0
