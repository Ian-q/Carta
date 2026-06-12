"""Tests for corpus-integrity scanning (doctor + embed --repair)."""
import yaml
from pathlib import Path
from unittest.mock import MagicMock, patch

from carta.embed.integrity import scan_corpus_integrity
from carta.embed.lifecycle import compute_file_hash
from carta.embed.repair import run_repair


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

        # chunk_count matches Qdrant (1 point) — purely stuck-stale, no mismatch,
        # so it must NOT reach affected_files (repair fixes its status in place).
        src_stale = tmp_path / "docs" / "stale.md"
        src_stale.write_text("stale content")
        stale_hash = compute_file_hash(src_stale)
        _write_sidecar(tmp_path, "docs/stale.md", 1, "stale", stale_hash)

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


class TestStuckStaleCountMismatch:
    """A stuck-stale sidecar (status stale, hash matches disk) with a count
    mismatch must appear in BOTH stuck_stale and count_mismatches, so repair
    re-embeds it in one pass instead of converging over two runs."""

    def test_stuck_stale_with_mismatch_is_in_both(self, tmp_path):
        src = tmp_path / "docs" / "stuck.md"
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_text("unchanged content with lost chunks")
        real_hash = compute_file_hash(src)
        _write_sidecar(tmp_path, "docs/stuck.md", 5, "stale", real_hash)

        pts = [
            _point("docs/stuck.md", "stuck", 8, "x"),
            _point("docs/stuck.md", "stuck", 9, "y"),
        ]
        report = scan_corpus_integrity(CFG, tmp_path, client=_client_with_points(pts))
        assert "docs/stuck.md" in report["stuck_stale"]
        assert report["count_mismatches"] == {"docs/stuck.md": {"sidecar": 5, "qdrant": 2}}
        assert "docs/stuck.md" in report["affected_files"]

    def test_genuinely_stale_mismatch_is_exempt(self, tmp_path):
        src = tmp_path / "docs" / "pending.md"
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_text("edited content awaiting re-embed")
        _write_sidecar(tmp_path, "docs/pending.md", 5, "stale", "0000deadbeef")

        pts = [_point("docs/pending.md", "pending", 0, "old text")]
        report = scan_corpus_integrity(CFG, tmp_path, client=_client_with_points(pts))
        assert report["count_mismatches"] == {}
        assert report["stuck_stale"] == []


class TestScannerRobustness:
    def test_non_dict_sidecar_yaml_does_not_abort_scan(self, tmp_path):
        """A corrupt sidecar that parses to a list/str must be skipped, not crash."""
        bad = tmp_path / ".carta" / "sidecars" / "docs" / "bad.embed-meta.yaml"
        bad.parent.mkdir(parents=True, exist_ok=True)
        bad.write_text("- a\n- b\n")
        pts = [_point("docs/ok.md", "ok", 0, "fine")]
        report = scan_corpus_integrity(CFG, tmp_path, client=_client_with_points(pts))
        assert report["affected_files"] == []

    def test_fully_lost_file_is_a_count_mismatch(self, tmp_path):
        """A sidecar claiming chunks for a file with ZERO surviving points must
        reach count_mismatches/affected_files (fully shadowed by legacy collisions)."""
        src = tmp_path / "docs" / "lost.md"
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_text("content whose points were all overwritten")
        _write_sidecar(tmp_path, "docs/lost.md", 5, "embedded",
                       compute_file_hash(src))
        pts = [_point("docs/other.md", "other", 0, "fine")]
        report = scan_corpus_integrity(CFG, tmp_path, client=_client_with_points(pts))
        assert report["count_mismatches"] == {"docs/lost.md": {"sidecar": 5, "qdrant": 0}}
        assert "docs/lost.md" in report["affected_files"]


class TestRunRepair:
    @patch("carta.embed.repair.run_embed_file")
    @patch("carta.embed.repair.scan_corpus_integrity")
    @patch("carta.embed.repair.QdrantClient")
    def test_repair_deletes_points_then_reembeds_each_affected_file(
            self, mock_qc, mock_scan, mock_reembed, tmp_path):
        mock_scan.return_value = {
            "slug_collisions": {"readme": ["docs/a/README.md", "docs/b/README.md"]},
            "empty_files": [], "partial_empty_files": {}, "count_mismatches": {},
            "stuck_stale": [],
            "affected_files": ["docs/a/README.md", "docs/b/README.md"],
        }
        mock_reembed.return_value = {"status": "ok", "chunks": 5}
        for rel in ("docs/a/README.md", "docs/b/README.md"):
            f = tmp_path / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text("# x\ncontent\n")

        summary = run_repair(tmp_path, CFG)

        client = mock_qc.return_value
        assert client.delete.call_count == 2          # one purge per file
        assert mock_reembed.call_count == 2
        assert summary["repaired"] == 2
        assert summary["purged_only"] == 0

    @patch("carta.embed.repair.run_embed_file")
    @patch("carta.embed.repair.scan_corpus_integrity")
    @patch("carta.embed.repair.QdrantClient")
    def test_missing_file_is_purged_not_reembedded(
            self, mock_qc, mock_scan, mock_reembed, tmp_path):
        mock_scan.return_value = {
            "slug_collisions": {}, "empty_files": ["docs/gone.pdf"],
            "partial_empty_files": {}, "count_mismatches": {}, "stuck_stale": [],
            "affected_files": ["docs/gone.pdf"],
        }
        summary = run_repair(tmp_path, CFG)
        assert mock_qc.return_value.delete.call_count == 1
        mock_reembed.assert_not_called()
        assert summary["purged_only"] == 1

    @patch("carta.embed.repair.run_embed_file")
    @patch("carta.embed.repair.scan_corpus_integrity")
    @patch("carta.embed.repair.QdrantClient")
    def test_extraction_failed_counts_as_flagged(
            self, mock_qc, mock_scan, mock_reembed, tmp_path):
        mock_scan.return_value = {
            "slug_collisions": {}, "empty_files": ["docs/scan.pdf"],
            "partial_empty_files": {}, "count_mismatches": {}, "stuck_stale": [],
            "affected_files": ["docs/scan.pdf"],
        }
        f = tmp_path / "docs" / "scan.pdf"
        f.parent.mkdir(parents=True)
        f.write_bytes(b"%PDF-1.4 fake")
        mock_reembed.return_value = {"status": "ok", "chunks": 0}
        summary = run_repair(tmp_path, CFG)
        assert summary["flagged"] == 1

    @patch("carta.embed.repair.run_embed_file")
    @patch("carta.embed.repair.scan_corpus_integrity")
    @patch("carta.embed.repair.QdrantClient")
    def test_stuck_stale_sidecars_fixed_in_place(
            self, mock_qc, mock_scan, mock_reembed, tmp_path):
        """Stuck-stale file not in affected_files: sidecar gets status fixed, no delete/re-embed."""
        from carta.embed.induct import sidecar_path as _sidecar_path
        # Create the source file
        src = tmp_path / "docs" / "s.md"
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_text("content")
        # Write sidecar at the path sidecar_path() would compute (production path)
        sc_path = _sidecar_path(src, tmp_path)
        sc_path.parent.mkdir(parents=True, exist_ok=True)
        with open(sc_path, "w") as f:
            yaml.dump({"current_path": "docs/s.md", "chunk_count": 3,
                       "status": "stale", "file_hash": "abc123"}, f)

        mock_scan.return_value = {
            "slug_collisions": {}, "empty_files": [], "partial_empty_files": {},
            "count_mismatches": {}, "stuck_stale": ["docs/s.md"],
            "affected_files": [],
        }

        summary = run_repair(tmp_path, CFG)

        # No delete or re-embed for stuck-stale-only file
        mock_qc.return_value.delete.assert_not_called()
        mock_reembed.assert_not_called()
        assert summary["stale_fixed"] == 1

        # Sidecar YAML should now have status "embedded" and stale_as_of None
        with open(sc_path) as f:
            updated = yaml.safe_load(f)
        assert updated["status"] == "embedded"
        assert updated.get("stale_as_of") is None

    @patch("carta.embed.repair.run_embed_file")
    @patch("carta.embed.repair.scan_corpus_integrity")
    @patch("carta.embed.repair.QdrantClient")
    def test_stuck_stale_also_affected_not_double_handled(
            self, mock_qc, mock_scan, mock_reembed, tmp_path):
        """File in BOTH stuck_stale and affected_files → re-embedded once, stale_fixed == 0."""
        src = tmp_path / "docs" / "both.md"
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_text("content")

        mock_scan.return_value = {
            "slug_collisions": {}, "empty_files": [], "partial_empty_files": {},
            "count_mismatches": {"docs/both.md": {"sidecar": 5, "qdrant": 2}},
            "stuck_stale": ["docs/both.md"],
            "affected_files": ["docs/both.md"],
        }
        mock_reembed.return_value = {"status": "ok", "chunks": 5}

        summary = run_repair(tmp_path, CFG)

        # Re-embedded exactly once (via affected_files loop)
        assert mock_reembed.call_count == 1
        # stale_fixed == 0: the sidecar was already rewritten by re-embed
        assert summary["stale_fixed"] == 0

    @patch("carta.embed.repair.run_embed_file")
    @patch("carta.embed.repair.scan_corpus_integrity")
    @patch("carta.embed.repair.QdrantClient")
    def test_reembed_exception_counts_failed(
            self, mock_qc, mock_scan, mock_reembed, tmp_path):
        """run_embed_file raising RuntimeError → failed count increments, loop continues."""
        for rel in ("docs/a.md", "docs/b.md"):
            f = tmp_path / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text("content")

        mock_scan.return_value = {
            "slug_collisions": {}, "empty_files": [], "partial_empty_files": {},
            "count_mismatches": {}, "stuck_stale": [],
            "affected_files": ["docs/a.md", "docs/b.md"],
        }
        # First call raises, second succeeds
        mock_reembed.side_effect = [RuntimeError("boom"), {"status": "ok", "chunks": 3}]

        summary = run_repair(tmp_path, CFG)

        assert summary["failed"] == 1
        assert summary["repaired"] == 1   # second file still processed
        assert mock_reembed.call_count == 2

    @patch("carta.embed.repair.run_embed_file")
    @patch("carta.embed.repair.scan_corpus_integrity")
    @patch("carta.embed.repair.QdrantClient")
    def test_clean_corpus_noop(
            self, mock_qc, mock_scan, mock_reembed, tmp_path, capsys):
        """Empty report → all counters 0, no client.delete, friendly message printed."""
        mock_scan.return_value = {
            "slug_collisions": {}, "empty_files": [], "partial_empty_files": {},
            "count_mismatches": {}, "stuck_stale": [],
            "affected_files": [],
        }

        summary = run_repair(tmp_path, CFG)

        mock_qc.return_value.delete.assert_not_called()
        mock_reembed.assert_not_called()
        assert summary["affected"] == 0
        assert summary["repaired"] == 0
        assert summary["purged_only"] == 0
        assert summary["flagged"] == 0
        assert summary["failed"] == 0
        assert summary["stale_fixed"] == 0
        out = capsys.readouterr().out
        assert "nothing to repair" in out.lower()
