"""Tests for carta.embed.lifecycle — hash and mtime primitives."""

import os
import tempfile
from pathlib import Path

import pytest

from carta.embed.lifecycle import compute_file_hash, needs_rehash


class TestComputeFileHash:
    """Test compute_file_hash with different file types."""

    def test_markdown_crlf_lf_same_hash(self):
        """Markdown files with CRLF and LF should produce the same hash (LF-normalized)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create two identical markdown files: one with CRLF, one with LF
            crlf_file = Path(tmpdir) / "crlf.md"
            lf_file = Path(tmpdir) / "lf.md"

            content = "# Title\r\nParagraph\r\nMore text"
            crlf_file.write_bytes(content.encode("utf-8"))

            # Same content but with LF only
            lf_content = "# Title\nParagraph\nMore text"
            lf_file.write_bytes(lf_content.encode("utf-8"))

            # Both should produce the same hash
            hash_crlf = compute_file_hash(crlf_file)
            hash_lf = compute_file_hash(lf_file)

            assert hash_crlf == hash_lf
            assert isinstance(hash_crlf, str)
            assert len(hash_crlf) == 64  # SHA256 hex is 64 chars

    def test_pdf_raw_bytes_not_normalized(self):
        """PDF files should be hashed raw without line-ending normalization."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create two "PDF" files (actually binary-like content)
            pdf1 = Path(tmpdir) / "file1.pdf"
            pdf2 = Path(tmpdir) / "file2.pdf"

            # First PDF: raw bytes with CRLF
            pdf1.write_bytes(b"%PDF-1.4\r\nContent")

            # Second PDF: same content but with LF only
            pdf2.write_bytes(b"%PDF-1.4\nContent")

            # Hashes should be DIFFERENT because PDF is not normalized
            hash1 = compute_file_hash(pdf1)
            hash2 = compute_file_hash(pdf2)

            assert hash1 != hash2, "PDF files should NOT have line-endings normalized"
            assert isinstance(hash1, str)
            assert len(hash1) == 64

    def test_unknown_extension_treated_as_markdown(self):
        """Files with unknown extensions should be treated as markdown (LF-normalized)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            txt_file = Path(tmpdir) / "notes.txt"
            md_file = Path(tmpdir) / "notes.md"

            # Same content with CRLF in both
            content = "# Title\r\nContent\r\nMore"
            txt_file.write_bytes(content.encode("utf-8"))
            md_file.write_bytes(content.encode("utf-8"))

            # Both should hash the same (both normalized)
            hash_txt = compute_file_hash(txt_file)
            hash_md = compute_file_hash(md_file)

            assert hash_txt == hash_md, "Unknown extensions should be treated like .md"

    def test_hash_is_lowercase_hex(self):
        """Hash should be returned as lowercase hexadecimal string."""
        with tempfile.TemporaryDirectory() as tmpdir:
            file = Path(tmpdir) / "test.md"
            file.write_text("test content")

            hash_val = compute_file_hash(file)

            assert isinstance(hash_val, str)
            assert len(hash_val) == 64
            assert all(c in "0123456789abcdef" for c in hash_val)


class TestNeedsRehash:
    """Test needs_rehash mtime fast-path logic."""

    def test_needs_rehash_missing_file_mtime_key(self):
        """If sidecar has no 'file_mtime' key, should return True."""
        sidecar = {"doc_slug": "test"}  # Missing file_mtime
        with tempfile.TemporaryDirectory() as tmpdir:
            file_path = Path(tmpdir) / "test.md"
            file_path.write_text("content")

            result = needs_rehash(file_path, sidecar)

            assert result is True

    def test_needs_rehash_mtime_matches(self):
        """If sidecar file_mtime matches actual mtime, should return False."""
        with tempfile.TemporaryDirectory() as tmpdir:
            file_path = Path(tmpdir) / "test.md"
            file_path.write_text("content")

            # Get the actual mtime
            actual_mtime = os.path.getmtime(file_path)

            sidecar = {"file_mtime": actual_mtime}

            result = needs_rehash(file_path, sidecar)

            assert result is False

    def test_needs_rehash_mtime_differs(self):
        """If sidecar file_mtime differs from actual mtime, should return True."""
        with tempfile.TemporaryDirectory() as tmpdir:
            file_path = Path(tmpdir) / "test.md"
            file_path.write_text("content")

            # Use a fake/old mtime
            old_mtime = 1000000.0

            sidecar = {"file_mtime": old_mtime}

            result = needs_rehash(file_path, sidecar)

            assert result is True
