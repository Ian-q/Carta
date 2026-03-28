"""Tests for carta.embed.lifecycle — hash, mtime, and Qdrant lifecycle primitives."""

import os
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from carta.embed.lifecycle import (
    compute_file_hash,
    needs_rehash,
    mark_sidecar_stale,
    cleanup_expired_orphans,
    is_protected_doc_type,
    check_stale_alert,
)


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


class TestMarkSidecarStale:
    """Test mark_sidecar_stale Qdrant lifecycle function."""

    def test_mark_sidecar_stale_valid_id(self):
        """Valid sidecar_id should call client.set_payload with correct filter."""
        mock_client = MagicMock()
        collection_name = "test_doc"
        sidecar_id = "abc123"
        now = datetime.utcnow()

        mark_sidecar_stale(mock_client, collection_name, sidecar_id, now)

        mock_client.set_payload.assert_called_once()
        call_args = mock_client.set_payload.call_args
        assert call_args[1]["collection_name"] == collection_name
        assert call_args[1]["payload"]["status"] == "stale"
        assert call_args[1]["payload"]["stale_as_of"] == now.isoformat()

    def test_mark_sidecar_stale_empty_id(self):
        """Empty sidecar_id should NOT call set_payload (pre-999.1 guard)."""
        mock_client = MagicMock()
        collection_name = "test_doc"
        sidecar_id = ""
        now = datetime.utcnow()

        mark_sidecar_stale(mock_client, collection_name, sidecar_id, now)

        mock_client.set_payload.assert_not_called()

    def test_mark_sidecar_stale_none_id(self):
        """None sidecar_id should NOT call set_payload (pre-999.1 guard)."""
        mock_client = MagicMock()
        collection_name = "test_doc"
        sidecar_id = None
        now = datetime.utcnow()

        mark_sidecar_stale(mock_client, collection_name, sidecar_id, now)

        mock_client.set_payload.assert_not_called()


class TestCleanupExpiredOrphans:
    """Test cleanup_expired_orphans Qdrant lifecycle function."""

    def test_cleanup_expired_orphans_mixed_points(self):
        """Should skip points without sidecar_id and delete those with valid IDs."""
        mock_client = MagicMock()
        collection_name = "test_doc"
        ttl_days = 7
        now = datetime.utcnow()

        # Mock scroll result with mixed points
        point_with_id = MagicMock()
        point_with_id.id = 1
        point_with_id.payload = {"sidecar_id": "valid-id"}

        point_without_id = MagicMock()
        point_without_id.id = 2
        point_without_id.payload = {}

        mock_client.scroll.return_value = ([point_with_id, point_without_id], None)

        deleted_count = cleanup_expired_orphans(mock_client, collection_name, ttl_days, now)

        assert deleted_count == 1
        mock_client.delete.assert_called_once()

    def test_cleanup_expired_orphans_empty_scroll(self):
        """Should return 0 and not call delete when scroll is empty."""
        mock_client = MagicMock()
        collection_name = "test_doc"
        ttl_days = 7
        now = datetime.utcnow()

        mock_client.scroll.return_value = ([], None)

        deleted_count = cleanup_expired_orphans(mock_client, collection_name, ttl_days, now)

        assert deleted_count == 0
        mock_client.delete.assert_not_called()

    def test_cleanup_expired_orphans_all_missing_sidecar_id(self):
        """Should skip all points if all lack sidecar_id."""
        mock_client = MagicMock()
        collection_name = "test_doc"
        ttl_days = 7
        now = datetime.utcnow()

        point1 = MagicMock()
        point1.id = 1
        point1.payload = {}

        point2 = MagicMock()
        point2.id = 2
        point2.payload = {"other_field": "value"}

        mock_client.scroll.return_value = ([point1, point2], None)

        deleted_count = cleanup_expired_orphans(mock_client, collection_name, ttl_days, now)

        assert deleted_count == 0
        mock_client.delete.assert_not_called()


class TestIsProtectedDocType:
    """Test is_protected_doc_type function."""

    def test_protected_types_return_true(self):
        """Protected doc types should return True."""
        assert is_protected_doc_type("quirk") is True
        assert is_protected_doc_type("bug-note") is True
        assert is_protected_doc_type("helpful-note") is True

    def test_non_protected_types_return_false(self):
        """Non-protected doc types should return False."""
        assert is_protected_doc_type("doc") is False
        assert is_protected_doc_type("datasheet") is False
        assert is_protected_doc_type("manual") is False
        assert is_protected_doc_type("session") is False
        assert is_protected_doc_type("unknown") is False


class TestCheckStaleAlert:
    """Test check_stale_alert function."""

    def test_zero_total_returns_none(self):
        """total_count == 0 should return None."""
        result = check_stale_alert(0, 0, 0.30)
        assert result is None

    def test_below_threshold_returns_none(self):
        """stale_count / total_count below threshold should return None."""
        result = check_stale_alert(0, 10, 0.30)
        assert result is None

        result = check_stale_alert(2, 10, 0.30)  # 20% < 30%
        assert result is None

    def test_at_or_above_threshold_returns_warning(self):
        """stale_count / total_count at or above threshold should return warning string."""
        result = check_stale_alert(3, 10, 0.30)  # 30% >= 30%
        assert result is not None
        assert isinstance(result, str)
        assert "30%" in result or "3 out of 10" in result.lower()
        assert "carta embed --force-stale" in result

    def test_above_threshold_returns_warning(self):
        """Higher stale percentage should return warning."""
        result = check_stale_alert(5, 10, 0.30)  # 50% >= 30%
        assert result is not None
        assert isinstance(result, str)
        assert "carta embed --force-stale" in result
