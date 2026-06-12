"""Tests for corpus-integrity scanning (doctor + embed --repair)."""
import yaml
from pathlib import Path
from unittest.mock import MagicMock

from carta.embed.integrity import scan_corpus_integrity
from carta.embed.lifecycle import compute_file_hash


def _point(file_path, slug, chunk_index, text):
    p = MagicMock()
    p.payload = {"file_path": file_path, "slug": slug,
                 "chunk_index": chunk_index, "text": text}
    return p


def _client_with_points(points):
    client = MagicMock()
    client.collection_exists.return_value = True
    client.scroll.return_value = (points, None)  # single page
    return client


CFG = {"project_name": "test", "qdrant_url": "http://localhost:6333",
       "embed": {"ollama_url": "http://x", "ollama_model": "m"}}


def _write_sidecar(tmp_path, rel_path, chunk_count, status, file_hash):
    """Write a minimal sidecar YAML under tmp_path/.carta/sidecars/."""
    sc_path = tmp_path / ".carta" / "sidecars" / (rel_path + ".embed-meta.yaml")
    sc_path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "current_path": rel_path,
        "chunk_count": chunk_count,
        "status": status,
        "file_hash": file_hash,
    }
    with open(sc_path, "w") as f:
        yaml.dump(data, f)
    return sc_path


class TestScanCorpusIntegrity:
    def test_detects_slug_collisions(self, tmp_path):
        pts = [
            _point("docs/ci/README.md", "readme", 8, "a"),
            _point("docs/diagrams/README.md", "readme", 0, "b"),
            _point("docs/unique.md", "unique", 0, "c"),
        ]
        report = scan_corpus_integrity(CFG, tmp_path, client=_client_with_points(pts))
        assert report["slug_collisions"] == {
            "readme": sorted(["docs/ci/README.md", "docs/diagrams/README.md"])}

    def test_detects_empty_text_files(self, tmp_path):
        pts = [
            _point("docs/scan.pdf", "scan", 0, ""),
            _point("docs/scan.pdf", "scan", 1, ""),
            _point("docs/partial.pdf", "partial", 0, ""),
            _point("docs/partial.pdf", "partial", 1, "real"),
            _point("docs/ok.md", "ok", 0, "fine"),
        ]
        report = scan_corpus_integrity(CFG, tmp_path, client=_client_with_points(pts))
        assert report["empty_files"] == ["docs/scan.pdf"]
        assert report["partial_empty_files"] == {"docs/partial.pdf": 1}

    def test_clean_corpus_reports_nothing(self, tmp_path):
        pts = [_point("docs/ok.md", "ok", 0, "fine")]
        report = scan_corpus_integrity(CFG, tmp_path, client=_client_with_points(pts))
        assert report["slug_collisions"] == {}
        assert report["empty_files"] == []
        assert report["partial_empty_files"] == {}
        assert report["affected_files"] == []

    def test_detects_count_mismatch(self, tmp_path):
        # Write a real source file so hash can be computed
        src = tmp_path / "docs" / "a.md"
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_text("hello world content")
        real_hash = compute_file_hash(src)

        # Write sidecar claiming chunk_count=5 with status embedded
        _write_sidecar(tmp_path, "docs/a.md", 5, "embedded", real_hash)

        # Qdrant has only 2 points for that file
        pts = [
            _point("docs/a.md", "a", 0, "hello"),
            _point("docs/a.md", "a", 1, "world"),
        ]
        report = scan_corpus_integrity(CFG, tmp_path, client=_client_with_points(pts))
        assert report["count_mismatches"] == {"docs/a.md": {"sidecar": 5, "qdrant": 2}}

    def test_detects_stuck_stale(self, tmp_path):
        # File on disk whose hash MATCHES the sidecar → stuck stale
        src_match = tmp_path / "docs" / "match.md"
        src_match.parent.mkdir(parents=True, exist_ok=True)
        src_match.write_text("unchanged content")
        matching_hash = compute_file_hash(src_match)
        _write_sidecar(tmp_path, "docs/match.md", 3, "stale", matching_hash)

        # File on disk whose hash DOES NOT match the sidecar → genuinely stale, skip
        src_diff = tmp_path / "docs" / "diff.md"
        src_diff.write_text("new content")
        _write_sidecar(tmp_path, "docs/diff.md", 3, "stale", "0000deadbeef")

        pts = [
            _point("docs/match.md", "match", 0, "a"),
            _point("docs/diff.md", "diff", 0, "b"),
        ]
        report = scan_corpus_integrity(CFG, tmp_path, client=_client_with_points(pts))
        assert "docs/match.md" in report["stuck_stale"]
        assert "docs/diff.md" not in report["stuck_stale"]

    def test_affected_files_unions_everything(self, tmp_path):
        # slug collision: two files share slug "readme"
        # empty: empty.pdf all empty
        # partial: partial.pdf one empty point
        # mismatch: mismatch.md sidecar=5, qdrant=1
        # stuck_stale: stale.md — should NOT be in affected_files
        src_mm = tmp_path / "docs" / "mismatch.md"
        src_mm.parent.mkdir(parents=True, exist_ok=True)
        src_mm.write_text("mismatch content")
        mm_hash = compute_file_hash(src_mm)
        _write_sidecar(tmp_path, "docs/mismatch.md", 5, "embedded", mm_hash)

        src_stale = tmp_path / "docs" / "stale.md"
        src_stale.write_text("stale content")
        stale_hash = compute_file_hash(src_stale)
        _write_sidecar(tmp_path, "docs/stale.md", 2, "stale", stale_hash)

        pts = [
            _point("docs/ci/README.md", "readme", 0, "x"),
            _point("docs/diagrams/README.md", "readme", 0, "y"),
            _point("docs/empty.pdf", "empty", 0, ""),
            _point("docs/partial.pdf", "partial", 0, ""),
            _point("docs/partial.pdf", "partial", 1, "real"),
            _point("docs/mismatch.md", "mismatch", 0, "z"),
            _point("docs/stale.md", "stale", 0, "s"),
        ]
        report = scan_corpus_integrity(CFG, tmp_path, client=_client_with_points(pts))

        expected_affected = sorted([
            "docs/ci/README.md",
            "docs/diagrams/README.md",
            "docs/empty.pdf",
            "docs/partial.pdf",
            "docs/mismatch.md",
        ])
        assert report["affected_files"] == expected_affected
        assert "docs/stale.md" not in report["affected_files"]

    def test_missing_collection(self, tmp_path):
        client = MagicMock()
        client.collection_exists.return_value = False
        report = scan_corpus_integrity(CFG, tmp_path, client=client)
        assert report["slug_collisions"] == {}
        assert report["empty_files"] == []
        assert report["partial_empty_files"] == {}
        assert report["count_mismatches"] == {}
        assert report["stuck_stale"] == []
        assert report["affected_files"] == []

    def test_multi_page_scroll(self, tmp_path):
        """scroll returning two pages — all points must be seen."""
        page1 = [_point("docs/a.md", "a", 0, "hello")]
        page2 = [_point("docs/b.md", "b", 0, "world")]

        client = MagicMock()
        client.collection_exists.return_value = True
        # First call returns page1 with a non-None offset; second returns page2 with None
        client.scroll.side_effect = [
            (page1, "cursor-abc"),
            (page2, None),
        ]
        report = scan_corpus_integrity(CFG, tmp_path, client=client)
        # Both files should appear; no errors
        assert report["empty_files"] == []
        assert report["slug_collisions"] == {}
        # scroll was called twice
        assert client.scroll.call_count == 2
