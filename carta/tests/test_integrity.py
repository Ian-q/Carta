"""Tests for corpus-integrity scanning (doctor + embed --repair)."""
import yaml
from pathlib import Path
from unittest.mock import MagicMock, patch

from carta.config import collection_name
from carta.embed.integrity import scan_corpus_integrity
from carta.embed.lifecycle import compute_file_hash
from carta.embed.repair import run_repair


def _point(file_path, slug, chunk_index, text):
    p = MagicMock()
    p.payload = {"file_path": file_path, "slug": slug,
                 "chunk_index": chunk_index, "text": text}
    return p


def _vpoint(file_path, page_num):
    """A _visual collection point (payload carries file_path + page_num)."""
    p = MagicMock()
    p.payload = {"file_path": file_path, "page_num": page_num,
                 "doc_type": "visual_page"}
    return p


def _client_with_collections(by_coll: dict):
    """MagicMock Qdrant client whose collection_exists/scroll dispatch on the
    collection name. by_coll maps collection name -> list of points."""
    client = MagicMock()
    client.collection_exists.side_effect = lambda c: c in by_coll
    client.scroll.side_effect = lambda coll, **kw: (by_coll.get(coll, []), None)
    return client


def _client_with_points(points):
    """Single-collection (_doc) client — back-compat for existing tests."""
    return _client_with_collections({collection_name(CFG, "doc"): points})


CFG = {"project_name": "test", "qdrant_url": "http://localhost:6333",
       "embed": {"ollama_url": "http://x", "ollama_model": "m"}}


def _write_sidecar(tmp_path, rel_path, chunk_count, status, file_hash):
    """Write a minimal sidecar YAML at its canonical .carta/sidecars/ location
    (the path sidecar_path() produces — with_suffix semantics, matching prod)."""
    from carta.embed.induct import sidecar_path
    sc_path = sidecar_path(tmp_path / rel_path, tmp_path)
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


def _write_sidecar_at_production_path(tmp_path, rel_path, chunk_count, status, file_hash):
    """Write a sidecar where sidecar_path() will look for it (with_suffix semantics)."""
    from carta.embed.induct import sidecar_path
    sc = sidecar_path(tmp_path / rel_path, tmp_path)
    sc.parent.mkdir(parents=True, exist_ok=True)
    with open(sc, "w") as f:
        yaml.dump({"current_path": rel_path, "chunk_count": chunk_count,
                   "status": status, "file_hash": file_hash}, f)
    return sc


def _write_visual_sidecar(tmp_path, rel_path, visual_done, visual_pending=None):
    """Write a canonical sidecar tracking visual_done/visual_pending pages."""
    from carta.embed.induct import sidecar_path
    sc = sidecar_path(tmp_path / rel_path, tmp_path)
    sc.parent.mkdir(parents=True, exist_ok=True)
    sc.write_text(yaml.dump({
        "current_path": rel_path, "status": "embedded",
        "visual_done": list(visual_done), "visual_pending": list(visual_pending or []),
    }))
    return sc


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

    def test_slug_collisions_are_not_affected_files(self, tmp_path):
        """Same-slug files are healthy under path-based IDs — reported as
        informational but never queued for repair (#40)."""
        pts = [
            _point("docs/a/README.md", "readme", 0, "x"),
            _point("docs/b/README.md", "readme", 0, "y"),
        ]
        report = scan_corpus_integrity(CFG, tmp_path, client=_client_with_points(pts))
        assert "readme" in report["slug_collisions"]
        assert report["affected_files"] == []

    def test_nested_junk_sidecar_is_ignored(self, tmp_path):
        """A misplaced/nested sidecar copy whose current_path resolves to a real
        file must not produce phantom stuck-stale / count-mismatch entries (#40)."""
        src = tmp_path / "docs" / "real.md"
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_text("real content")
        real_hash = compute_file_hash(src)
        # Canonical sidecar: embedded, counts agree with Qdrant — perfectly clean.
        _write_sidecar(tmp_path, "docs/real.md", 1, "embedded", real_hash)
        # Junk nested copy claiming the same current_path but stuck-stale-looking.
        junk = (tmp_path / ".carta" / "sidecars" / ".worktrees" / "x"
                / ".carta" / "sidecars" / "docs" / "real.embed-meta.yaml")
        junk.parent.mkdir(parents=True, exist_ok=True)
        junk.write_text(yaml.dump({"current_path": "docs/real.md", "chunk_count": 99,
                                   "status": "stale", "file_hash": real_hash}))

        pts = [_point("docs/real.md", "real", 0, "real content")]
        report = scan_corpus_integrity(CFG, tmp_path, client=_client_with_points(pts))
        assert report["stuck_stale"] == []
        assert report["count_mismatches"] == {}
        assert report["affected_files"] == []

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

        # Slug collisions are informational only (healthy with path-based IDs),
        # so the README pair must NOT be in affected_files (#40).
        expected_affected = sorted([
            "docs/empty.pdf",
            "docs/partial.pdf",
            "docs/mismatch.md",
        ])
        assert report["affected_files"] == expected_affected
        assert "docs/stale.md" not in report["affected_files"]
        assert report["slug_collisions"] == {
            "readme": sorted(["docs/ci/README.md", "docs/diagrams/README.md"])}
        assert "docs/ci/README.md" not in report["affected_files"]
        assert "docs/diagrams/README.md" not in report["affected_files"]

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
        # Only the _doc collection exists here, so the _visual scan is skipped
        # and scroll is called exactly twice (the two _doc pages).
        client.collection_exists.side_effect = lambda c: c == collection_name(CFG, "doc")
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


class TestVisualIntegrityScan:
    """scan_corpus_integrity also audits the _visual collection (#38 part 2)."""

    def test_visual_count_mismatch_detected(self, tmp_path):
        """Sidecar visual_done count != _visual point count → mismatch."""
        src = tmp_path / "docs" / "scan.pdf"
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_text("pdf bytes")
        _write_visual_sidecar(tmp_path, "docs/scan.pdf", visual_done=[1, 2, 3])
        client = _client_with_collections({
            collection_name(CFG, "doc"): [],
            collection_name(CFG, "visual"): [_vpoint("docs/scan.pdf", 1)],
        })
        report = scan_corpus_integrity(CFG, tmp_path, client=client)
        assert report["visual_count_mismatches"] == {
            "docs/scan.pdf": {"sidecar": 3, "qdrant": 1}}

    def test_visual_counts_match_is_clean(self, tmp_path):
        src = tmp_path / "docs" / "ok.pdf"
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_text("pdf bytes")
        _write_visual_sidecar(tmp_path, "docs/ok.pdf", visual_done=[1, 2])
        client = _client_with_collections({
            collection_name(CFG, "doc"): [],
            collection_name(CFG, "visual"): [
                _vpoint("docs/ok.pdf", 1), _vpoint("docs/ok.pdf", 2)],
        })
        report = scan_corpus_integrity(CFG, tmp_path, client=client)
        assert report["visual_count_mismatches"] == {}

    def test_orphaned_visual_points_when_source_gone(self, tmp_path):
        """_visual points for a file whose source no longer exists → orphan."""
        client = _client_with_collections({
            collection_name(CFG, "doc"): [],
            collection_name(CFG, "visual"): [
                _vpoint("docs/gone.pdf", 1), _vpoint("docs/gone.pdf", 2)],
        })
        report = scan_corpus_integrity(CFG, tmp_path, client=client)
        assert report["orphaned_visual_files"] == ["docs/gone.pdf"]
        # source-gone files are orphans, not count mismatches
        assert report["visual_count_mismatches"] == {}

    def test_no_visual_collection_is_safe(self, tmp_path):
        report = scan_corpus_integrity(CFG, tmp_path, client=_client_with_points([]))
        assert report["visual_count_mismatches"] == {}
        assert report["orphaned_visual_files"] == []


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
        # The re-embed wrote an extraction_failed sidecar (zero usable text)
        _write_sidecar_at_production_path(tmp_path, "docs/scan.pdf",
                                          0, "extraction_failed", "abc")
        mock_reembed.return_value = {"status": "ok", "chunks": 0}
        summary = run_repair(tmp_path, CFG)
        assert summary["flagged"] == 1
        assert summary.get("queued_visual", 0) == 0

    @patch("carta.embed.repair.run_embed_file")
    @patch("carta.embed.repair.scan_corpus_integrity")
    @patch("carta.embed.repair.QdrantClient")
    def test_zero_chunks_but_embedded_counts_queued_visual_not_flagged(
            self, mock_qc, mock_scan, mock_reembed, tmp_path, capsys):
        """A two-pass-visual PDF re-embeds with 0 chunks but a HEALTHY sidecar
        (pages queued for pass-2). It must not be reported as extraction_failed."""
        mock_scan.return_value = {
            "slug_collisions": {}, "empty_files": ["docs/imgheavy.pdf"],
            "partial_empty_files": {}, "count_mismatches": {}, "stuck_stale": [],
            "affected_files": ["docs/imgheavy.pdf"],
        }
        f = tmp_path / "docs" / "imgheavy.pdf"
        f.parent.mkdir(parents=True)
        f.write_bytes(b"%PDF-1.4 fake")
        _write_sidecar_at_production_path(tmp_path, "docs/imgheavy.pdf",
                                          0, "embedded", "abc")
        mock_reembed.return_value = {"status": "ok", "chunks": 0}
        summary = run_repair(tmp_path, CFG)
        assert summary["flagged"] == 0
        assert summary["queued_visual"] == 1
        out = capsys.readouterr().out
        assert "extraction_failed" not in out.split("Repair complete")[0]

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


class TestVisualRepair:
    """run_repair handles _visual integrity issues (#38 part 2)."""

    @patch("carta.embed.repair.run_embed_file")
    @patch("carta.embed.repair.scan_corpus_integrity")
    @patch("carta.embed.repair.QdrantClient")
    def test_orphaned_visual_points_are_purged(
            self, mock_qc, mock_scan, mock_reembed, tmp_path):
        """A file gone from disk with _visual points → purge those points."""
        mock_scan.return_value = {
            "slug_collisions": {}, "empty_files": [], "partial_empty_files": {},
            "count_mismatches": {}, "stuck_stale": [], "affected_files": [],
            "visual_count_mismatches": {}, "orphaned_visual_files": ["docs/gone.pdf"],
        }
        summary = run_repair(tmp_path, CFG)

        assert mock_qc.return_value.delete.call_count == 1   # purged from _visual
        mock_reembed.assert_not_called()
        assert summary["visual_purged"] == 1
        assert summary["visual_requeued"] == 0

    @patch("carta.embed.repair.run_embed_file")
    @patch("carta.embed.repair.scan_corpus_integrity")
    @patch("carta.embed.repair.QdrantClient")
    def test_visual_mismatch_requeues_without_deleting(
            self, mock_qc, mock_scan, mock_reembed, tmp_path):
        """A count mismatch (source present) re-queues for re-drain and never
        deletes _visual points (ColPali embeddings can't be re-created)."""
        src = tmp_path / "docs" / "scan.pdf"
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_bytes(b"%PDF-1.4 fake")
        _write_visual_sidecar(tmp_path, "docs/scan.pdf",
                              visual_done=[1, 2, 3], visual_pending=[])
        mock_scan.return_value = {
            "slug_collisions": {}, "empty_files": [], "partial_empty_files": {},
            "count_mismatches": {}, "stuck_stale": [], "affected_files": [],
            "visual_count_mismatches": {"docs/scan.pdf": {"sidecar": 3, "qdrant": 1}},
            "orphaned_visual_files": [],
        }
        summary = run_repair(tmp_path, CFG)

        mock_qc.return_value.delete.assert_not_called()  # never destroy visual points
        mock_reembed.assert_not_called()
        assert summary["visual_requeued"] == 1
        # Sidecar reset: every page pending again, none done — ready to re-drain.
        from carta.embed.induct import sidecar_path, read_sidecar
        sc = read_sidecar(sidecar_path(tmp_path / "docs/scan.pdf", tmp_path))
        assert sc["visual_pending"] == [1, 2, 3]
        assert sc["visual_done"] == []
