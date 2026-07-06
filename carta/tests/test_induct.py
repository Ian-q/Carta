"""Tests for carta embed induction (sidecar generation)."""

import pytest
import yaml
from pathlib import Path
from carta.embed.induct import generate_sidecar_stub, iter_canonical_sidecars


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


class TestIterCanonicalSidecars:
    """iter_canonical_sidecars yields only well-placed, well-formed sidecars."""

    def _write(self, repo_root, rel, data):
        sc = repo_root / ".carta" / "sidecars" / rel
        sc.parent.mkdir(parents=True, exist_ok=True)
        sc.write_text(yaml.dump(data))
        return sc

    def test_yields_canonical_sidecar(self, tmp_path):
        sc = self._write(tmp_path, "docs/a.embed-meta.yaml",
                         {"current_path": "docs/a.md", "status": "embedded"})
        out = list(iter_canonical_sidecars(tmp_path))
        assert [p for p, _ in out] == [sc]
        assert out[0][1]["current_path"] == "docs/a.md"

    def test_skips_nested_junk_copy(self, tmp_path):
        # A misplaced copy whose current_path points at a real-looking repo file
        # but whose on-disk location is NOT the canonical path for that file.
        self._write(tmp_path, ".worktrees/x/.carta/sidecars/docs/a.embed-meta.yaml",
                    {"current_path": "docs/a.md", "status": "embedded"})
        assert list(iter_canonical_sidecars(tmp_path)) == []

    def test_skips_non_dict_sidecar(self, tmp_path):
        sc = tmp_path / ".carta" / "sidecars" / "bad.embed-meta.yaml"
        sc.parent.mkdir(parents=True, exist_ok=True)
        sc.write_text("- just\n- a\n- list\n")
        assert list(iter_canonical_sidecars(tmp_path)) == []

    def test_skips_sidecar_without_current_path(self, tmp_path):
        self._write(tmp_path, "docs/a.embed-meta.yaml", {"status": "embedded"})
        assert list(iter_canonical_sidecars(tmp_path)) == []

    def test_empty_when_no_sidecars_dir(self, tmp_path):
        assert list(iter_canonical_sidecars(tmp_path)) == []


class TestSidecarNaming:
    """Extension-preserving sidecar names for spreadsheet types (spec: sidecar collision)."""

    def test_md_mapping_unchanged(self, tmp_path):
        f = tmp_path / "docs" / "data.md"
        from carta.embed.induct import sidecar_path
        assert sidecar_path(f, tmp_path) == (
            tmp_path / ".carta" / "sidecars" / "docs" / "data.embed-meta.yaml")

    def test_pdf_mapping_unchanged(self, tmp_path):
        f = tmp_path / "docs" / "data.pdf"
        from carta.embed.induct import sidecar_path
        assert sidecar_path(f, tmp_path) == (
            tmp_path / ".carta" / "sidecars" / "docs" / "data.embed-meta.yaml")

    def test_csv_preserves_extension(self, tmp_path):
        f = tmp_path / "docs" / "data.csv"
        from carta.embed.induct import sidecar_path
        assert sidecar_path(f, tmp_path) == (
            tmp_path / ".carta" / "sidecars" / "docs" / "data.csv.embed-meta.yaml")

    def test_xlsx_preserves_extension_case_kept(self, tmp_path):
        f = tmp_path / "docs" / "Data.XLSX"
        from carta.embed.induct import sidecar_path
        # suffix *check* is case-insensitive; the filename itself is preserved as-is
        assert sidecar_path(f, tmp_path) == (
            tmp_path / ".carta" / "sidecars" / "docs" / "Data.XLSX.embed-meta.yaml")

    def test_same_stem_different_type_no_collision(self, tmp_path):
        from carta.embed.induct import sidecar_path
        md = sidecar_path(tmp_path / "docs" / "data.md", tmp_path)
        cs = sidecar_path(tmp_path / "docs" / "data.csv", tmp_path)
        assert md != cs

    def test_iter_canonical_accepts_extension_preserving_sidecar(self, tmp_path):
        from carta.embed.induct import sidecar_path, write_sidecar
        src = tmp_path / "docs" / "data.csv"
        src.parent.mkdir(parents=True)
        src.write_text("a,b\n1,2\n")
        cfg = {"project_name": "p", "qdrant_url": "http://localhost:6333"}
        stub = generate_sidecar_stub(src, tmp_path, cfg)
        write_sidecar(src, stub, tmp_path)
        found = [data["current_path"] for _, data in iter_canonical_sidecars(tmp_path)]
        assert "docs/data.csv" in found


class TestSpreadsheetFileType:
    def _stub(self, tmp_path, name):
        f = tmp_path / "docs" / name
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("x")
        cfg = {"project_name": "p", "qdrant_url": "http://localhost:6333"}
        return generate_sidecar_stub(f, tmp_path, cfg)

    def test_csv_and_xlsx_are_spreadsheet(self, tmp_path):
        assert self._stub(tmp_path, "a.csv")["file_type"] == "spreadsheet"
        assert self._stub(tmp_path, "b.xlsx")["file_type"] == "spreadsheet"
        assert self._stub(tmp_path, "c.XLSX")["file_type"] == "spreadsheet"

    def test_md_uppercase_is_markdown(self, tmp_path):
        # latent case bug fixed while touching this line
        assert self._stub(tmp_path, "d.MD")["file_type"] == "markdown"
        assert self._stub(tmp_path, "e.md")["file_type"] == "markdown"

    def test_pdf_unchanged(self, tmp_path):
        assert self._stub(tmp_path, "f.pdf")["file_type"] == "pdf"
