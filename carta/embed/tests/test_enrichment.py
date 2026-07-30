"""Tests for carta.embed.enrichment — enrichment doc paths, staleness, and
the pipeline ingestion hook that stamps the SOURCE sidecar.
"""
from pathlib import Path
from unittest.mock import MagicMock, patch

import yaml

from carta.embed.enrichment import (
    enrichment_is_stale,
    enrichment_rel_path,
    record_enrichment,
    source_rel_for_enrichment,
)
from carta.embed.induct import read_sidecar, sidecar_path, write_sidecar

REPO_VISIBLE = {"embed": {"enrichment": {"repo_visible": True, "suffix": ".extraction.md"}}}
INTERNAL = {"embed": {"enrichment": {"repo_visible": False, "suffix": ".extraction.md"}}}
SRC = Path("docs/reference/suppliers/CTS/schematic.pdf")


def test_repo_visible_path_is_sibling():
    assert enrichment_rel_path(SRC, REPO_VISIBLE) == Path(
        "docs/reference/suppliers/CTS/schematic.pdf.extraction.md")


def test_internal_path_mirrors_companions():
    assert enrichment_rel_path(SRC, INTERNAL) == Path(
        ".carta/companions/docs/reference/suppliers/CTS/schematic.pdf.extraction.md")


def test_inverse_mapping_both_branches():
    for cfg in (REPO_VISIBLE, INTERNAL):
        assert source_rel_for_enrichment(enrichment_rel_path(SRC, cfg), cfg) == SRC
    assert source_rel_for_enrichment(Path("docs/notes.md"), REPO_VISIBLE) is None


def test_staleness_by_source_hash():
    assert enrichment_is_stale({"enrichment_source_hash": "aa", "file_hash": "bb"})
    assert not enrichment_is_stale({"enrichment_source_hash": "aa", "file_hash": "aa"})
    assert not enrichment_is_stale({"file_hash": "aa"})


class TestRecordEnrichmentRoundTrip:
    def test_stamps_source_sidecar_and_promotes_deep_scan(self, tmp_path):
        repo_root = tmp_path
        src_rel = Path("docs/reference/suppliers/CTS/schematic.pdf")
        src = repo_root / src_rel
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_bytes(b"%PDF-1.4 fake")

        write_sidecar(src, {
            "slug": "schematic",
            "doc_type": "reference",
            "current_path": str(src_rel),
            "status": "embedded",
            "file_hash": "abc",
            "deep_scan": "requested",
        }, repo_root)

        enr_rel = enrichment_rel_path(src_rel, INTERNAL)
        record_enrichment(repo_root, src_rel, enr_rel)

        sc = read_sidecar(sidecar_path(src, repo_root))
        assert sc["enrichment_path"] == str(enr_rel)
        assert sc["enrichment_source_hash"] == "abc"
        assert sc["deep_scan"] == "done"

    def test_creates_minimal_sidecar_when_source_has_none(self, tmp_path):
        """No pre-existing sidecar: record_enrichment creates a minimal one via
        the read_sidecar-or-{} -> write_sidecar round trip (no full stub needed).

        Critically the minimal sidecar must carry current_path — otherwise
        iter_canonical_sidecars (and everything built on it: the integrity
        scan, status counters) silently skips it forever (induct.py)."""
        repo_root = tmp_path
        src_rel = Path("docs/notes/loose.pdf")
        src = repo_root / src_rel
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_bytes(b"%PDF-1.4 fake")

        enr_rel = enrichment_rel_path(src_rel, INTERNAL)
        record_enrichment(repo_root, src_rel, enr_rel)

        sc_path = sidecar_path(src, repo_root)
        sc = read_sidecar(sc_path)
        assert sc["current_path"] == str(src_rel)
        assert sc["enrichment_path"] == str(enr_rel)
        # No file_hash existed yet — record_enrichment must hash the source
        # from disk rather than stamp the falsy "" blindly (a blind stamp
        # would make enrichment_is_stale return False forever).
        from carta.embed.lifecycle import compute_file_hash
        real_hash = compute_file_hash(src)
        assert sc["enrichment_source_hash"] == real_hash
        assert sc["file_hash"] == real_hash

        # Discoverable: iter_canonical_sidecars requires current_path to yield
        # the sidecar at all (it skips entries without one) — without the fix
        # this dict would be empty and the entry invisible forever, along with
        # every consumer built on iter_canonical_sidecars (scan_corpus_integrity,
        # status counters).
        from carta.embed.induct import iter_canonical_sidecars
        discovered = dict(iter_canonical_sidecars(repo_root))
        assert sc_path in discovered
        assert discovered[sc_path]["current_path"] == str(src_rel)

        # Reachable by the integrity scan too (same iter_canonical_sidecars
        # generator under the hood): a later embed stamping a real file_hash
        # differing from the recorded enrichment_source_hash makes it
        # a genuine count/stale candidate the scan can now actually see.
        from carta.embed.integrity import scan_corpus_integrity
        sc["enrichment_source_hash"] = "old-hash-from-this-record"
        sc["file_hash"] = "a-real-hash-from-a-later-embed"
        write_sidecar(src, sc, repo_root)
        cfg = {"project_name": "test", "qdrant_url": "http://localhost:6333"}
        client = MagicMock()
        client.collection_exists.return_value = False
        report = scan_corpus_integrity(cfg, repo_root, client=client)
        assert str(src_rel) in report["stale_enrichments"]

    def test_flag_created_stub_gets_real_hash_and_flips_stale_on_change(self, tmp_path):
        """A `carta flag`-created stub sidecar has file_hash=None (generate_sidecar_stub's
        default). record_enrichment must not stamp that blind falsy value — it must hash
        the source from disk so a later content change is actually detectable as stale."""
        repo_root = tmp_path
        src_rel = Path("docs/flagged/needs-deep-scan.pdf")
        src = repo_root / src_rel
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_bytes(b"%PDF-1.4 original content")

        write_sidecar(src, {
            "current_path": str(src_rel),
            "status": "pending",
            "file_hash": None,  # exactly what generate_sidecar_stub sets pre-embed
            "deep_scan": "requested",
        }, repo_root)

        enr_rel = enrichment_rel_path(src_rel, INTERNAL)
        record_enrichment(repo_root, src_rel, enr_rel)

        sc = read_sidecar(sidecar_path(src, repo_root))
        from carta.embed.lifecycle import compute_file_hash
        real_hash = compute_file_hash(src)
        assert sc["enrichment_source_hash"] == real_hash
        assert len(sc["enrichment_source_hash"]) == 64
        assert all(c in "0123456789abcdef" for c in sc["enrichment_source_hash"])
        assert sc["file_hash"] == real_hash  # sidecar made coherent too
        assert not enrichment_is_stale(sc)
        assert sc["deep_scan"] == "done"

        # Source content changes; simulate a later re-embed of the SOURCE that
        # recomputes file_hash (enrichment_source_hash is untouched until the
        # extraction is re-verified) — staleness must now flip true.
        src.write_bytes(b"%PDF-1.4 changed content")
        sc["file_hash"] = compute_file_hash(src)
        assert enrichment_is_stale(sc)

    def test_unreadable_source_warns_and_does_not_stamp(self, tmp_path, capsys):
        """A missing/unreadable source (open() raises OSError) must leave the
        sidecar's enrichment fields unstamped and warn to stderr, rather than
        silently persisting a bogus/blind hash."""
        repo_root = tmp_path
        src_rel = Path("docs/flagged/gone.pdf")
        src = repo_root / src_rel
        # Deliberately do NOT create the source file — compute_file_hash's
        # read_bytes() will raise FileNotFoundError (an OSError).

        write_sidecar(src, {
            "current_path": str(src_rel),
            "status": "pending",
            "file_hash": None,
            "deep_scan": "requested",
        }, repo_root)

        enr_rel = enrichment_rel_path(src_rel, INTERNAL)
        record_enrichment(repo_root, src_rel, enr_rel)

        sc = read_sidecar(sidecar_path(src, repo_root))
        assert sc.get("enrichment_source_hash") is None
        assert "enrichment_path" not in sc
        assert sc["deep_scan"] == "requested"  # unchanged — not promoted to "done"

        err = capsys.readouterr().err
        assert "warning" in err.lower()
        assert str(src_rel) in err


class TestPipelineAttributionHook:
    """`_embed_one_file` tags enrichment-doc chunks with `enriches` and, on a
    successful embed, stamps the SOURCE sidecar via record_enrichment."""

    def _cfg(self, tmp_path):
        return {
            "project_name": "test-project",
            "docs_root": "docs",
            "qdrant_url": "http://localhost:6333",
            "embed": {
                "ollama_url": "http://localhost:11434",
                "ollama_model": "nomic-embed-text",
                "chunking": {"max_tokens": 400, "overlap_fraction": 0.15},
                "enrichment": {"repo_visible": False, "suffix": ".extraction.md"},
            },
        }

    def test_embed_one_file_sets_enriches_metadata_and_stamps_source(self, tmp_path):
        repo_root = tmp_path
        cfg = self._cfg(tmp_path)

        src_rel = Path("docs/reference/suppliers/CTS/schematic.pdf")
        src = repo_root / src_rel
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_bytes(b"%PDF-1.4 fake")
        write_sidecar(src, {
            "slug": "schematic", "doc_type": "reference",
            "current_path": str(src_rel), "status": "embedded",
            "file_hash": "abc123", "deep_scan": "requested",
        }, repo_root)

        from carta.embed.enrichment import enrichment_rel_path
        enr_rel = enrichment_rel_path(src_rel, cfg)
        enr_path = repo_root / enr_rel
        enr_path.parent.mkdir(parents=True, exist_ok=True)
        enr_path.write_text("# Schematic extraction\n\nStructured content describing the schematic.")

        mock_client = MagicMock()
        with patch("carta.embed.pipeline.upsert_chunks", return_value=1):
            from carta.embed.pipeline import _embed_one_file
            file_info = {"slug": "schematic-extraction", "doc_type": "reference", "generation": 1}
            count, sidecar_updates = _embed_one_file(
                enr_path, file_info, cfg, mock_client, repo_root,
                max_tokens=400, overlap_fraction=0.15,
            )

        assert sidecar_updates["status"] == "embedded"

        src_sc = read_sidecar(sidecar_path(src, repo_root))
        assert src_sc["enrichment_path"] == str(enr_rel)
        assert src_sc["enrichment_source_hash"] == "abc123"
        assert src_sc["deep_scan"] == "done"

    def test_zero_chunks_persisted_does_not_stamp_source(self, tmp_path):
        """A total upsert failure (count==0 — e.g. Qdrant/Ollama down) must NOT
        stamp the source sidecar. record_enrichment must gate on the embed's
        FINAL status (after the honest-success-accounting downgrade to
        embed_failed), not the optimistic 'embedded' set at dict-construction
        time — otherwise the source records "enrichment ingested" against its
        own CURRENT file_hash, which enrichment_is_stale can then never flag,
        permanently hiding the failure."""
        repo_root = tmp_path
        cfg = self._cfg(tmp_path)

        src_rel = Path("docs/reference/suppliers/CTS/schematic.pdf")
        src = repo_root / src_rel
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_bytes(b"%PDF-1.4 fake")
        write_sidecar(src, {
            "slug": "schematic", "doc_type": "reference",
            "current_path": str(src_rel), "status": "embedded",
            "file_hash": "abc123", "deep_scan": "requested",
        }, repo_root)

        from carta.embed.enrichment import enrichment_rel_path
        enr_rel = enrichment_rel_path(src_rel, cfg)
        enr_path = repo_root / enr_rel
        enr_path.parent.mkdir(parents=True, exist_ok=True)
        enr_path.write_text("# Schematic extraction\n\nStructured content describing the schematic.")

        mock_client = MagicMock()
        # upsert_chunks persists NOTHING (simulates Qdrant/Ollama failure) —
        # the write attempted real (non-empty) chunks but zero landed.
        with patch("carta.embed.pipeline.upsert_chunks", return_value=0):
            from carta.embed.pipeline import _embed_one_file
            file_info = {"slug": "schematic-extraction", "doc_type": "reference", "generation": 1}
            count, sidecar_updates = _embed_one_file(
                enr_path, file_info, cfg, mock_client, repo_root,
                max_tokens=400, overlap_fraction=0.15,
            )

        assert sidecar_updates["status"] == "embed_failed"
        assert count == 0

        # Source sidecar must be untouched — no enrichment fields at all.
        src_sc = read_sidecar(sidecar_path(src, repo_root))
        assert "enrichment_path" not in src_sc
        assert "enrichment_source_hash" not in src_sc
        assert src_sc["deep_scan"] == "requested"  # unchanged

    def test_non_enrichment_file_is_unaffected(self, tmp_path):
        """A plain markdown file (not ending in the enrichment suffix) must not
        trigger record_enrichment or touch any source sidecar."""
        repo_root = tmp_path
        cfg = self._cfg(tmp_path)

        doc = repo_root / "docs" / "plain.md"
        doc.parent.mkdir(parents=True, exist_ok=True)
        doc.write_text("# Plain doc\n\nNothing special.")

        mock_client = MagicMock()
        with patch("carta.embed.pipeline.upsert_chunks", return_value=1):
            from carta.embed.pipeline import _embed_one_file
            file_info = {"slug": "plain", "doc_type": "guide", "generation": 1}
            count, sidecar_updates = _embed_one_file(
                doc, file_info, cfg, mock_client, repo_root,
                max_tokens=400, overlap_fraction=0.15,
            )

        assert sidecar_updates["status"] == "embedded"
        # No sidecar directory should have been created for any "source" —
        # only the (not-yet-written) sidecar for `doc` itself would ever exist,
        # and _embed_one_file never writes sidecars directly.
        assert not (repo_root / ".carta" / "sidecars").exists()


class TestCompanionInternalIngestion:
    """Enrichment docs under .carta/companions/ are not auto-inducted
    (_iter_inductable_files skips .carta); `carta embed <path>` -> run_embed_file
    must still accept them, tag their chunks, and stamp the source sidecar."""

    def test_run_embed_file_accepts_companion_internal_enrichment(self, tmp_path):
        repo_root = tmp_path
        carta_dir = repo_root / ".carta"
        carta_dir.mkdir()
        cfg = {
            "project_name": "test-project",
            "docs_root": "docs",
            "qdrant_url": "http://localhost:6333",
            "embed": {
                "ollama_url": "http://localhost:11434",
                "ollama_model": "nomic-embed-text",
                "chunking": {"max_tokens": 400, "overlap_fraction": 0.15},
                "enrichment": {"repo_visible": False, "suffix": ".extraction.md"},
            },
        }
        with open(carta_dir / "config.yaml", "w") as f:
            yaml.dump(cfg, f)

        src_rel = Path("docs/reference/suppliers/CTS/schematic.pdf")
        src = repo_root / src_rel
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_bytes(b"%PDF-1.4 fake")
        write_sidecar(src, {
            "slug": "schematic", "doc_type": "reference",
            "current_path": str(src_rel), "status": "embedded",
            "file_hash": "abc123", "generation": 1, "deep_scan": "requested",
        }, repo_root)

        from carta.embed.enrichment import enrichment_rel_path
        enr_rel = enrichment_rel_path(src_rel, cfg)
        enr_path = repo_root / enr_rel
        enr_path.parent.mkdir(parents=True, exist_ok=True)
        enr_path.write_text("# Schematic extraction\n\nStructured content describing the schematic.")

        with patch("carta.embed.pipeline.find_config") as mock_find_cfg, \
             patch("carta.embed.pipeline.QdrantClient") as mock_client_cls, \
             patch("carta.embed.pipeline.ensure_collection"), \
             patch("carta.embed.pipeline.upsert_chunks", return_value=1):
            mock_find_cfg.return_value = carta_dir / "config.yaml"
            mock_client_cls.return_value = MagicMock()

            from carta.embed.pipeline import run_embed_file
            result = run_embed_file(enr_path, cfg, force=True)

        assert result["status"] == "ok"

        src_sc = read_sidecar(sidecar_path(src, repo_root))
        assert src_sc["enrichment_path"] == str(enr_rel)
        assert src_sc["enrichment_source_hash"] == "abc123"
        assert src_sc["deep_scan"] == "done"
