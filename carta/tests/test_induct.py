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


class TestDocTypeResolution:
    """Frontmatter doc_type wins over parent-dir inference; quirks/notes dirs map;
    the stub's collection field routes via collection_for_doc_type."""

    def _stub(self, tmp_path, rel, content="# T"):
        fp = tmp_path / rel
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content)
        cfg = {"project_name": "p", "qdrant_url": "http://localhost:6333"}
        return generate_sidecar_stub(fp, tmp_path, cfg)

    def test_frontmatter_doc_type_wins_over_path(self, tmp_path):
        stub = self._stub(tmp_path, "docs/reference/x.md",
                          "---\ndoc_type: quirk\n---\n\nBody text")
        assert stub["doc_type"] == "quirk"
        assert stub["collection"] == "p_notes"

    def test_quirks_dir_maps_to_quirk(self, tmp_path):
        stub = self._stub(tmp_path, "docs/quirks/x.md")
        assert stub["doc_type"] == "quirk"
        assert stub["collection"] == "p_notes"

    def test_notes_dir_maps_to_helpful_note(self, tmp_path):
        stub = self._stub(tmp_path, "docs/notes/x.md")
        assert stub["doc_type"] == "helpful-note"
        assert stub["collection"] == "p_notes"

    def test_unmapped_dir_no_frontmatter_unchanged(self, tmp_path):
        stub = self._stub(tmp_path, "docs/misc/x.md")
        assert stub["doc_type"] == "unknown"
        assert stub["collection"] == "p_doc"

    def test_mapped_dir_without_frontmatter_still_routes_doc(self, tmp_path):
        stub = self._stub(tmp_path, "docs/reference/datasheets/x.md")
        assert stub["doc_type"] == "datasheet"
        assert stub["collection"] == "p_doc"

    def test_malformed_frontmatter_falls_back_to_path(self, tmp_path):
        stub = self._stub(tmp_path, "docs/quirks/x.md", "---\n: : bad yaml [\n---\nBody")
        assert stub["doc_type"] == "quirk"
