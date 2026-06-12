"""Tests for carta embed <files> targeted path."""
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch, call
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def _make_args(files):
    args = MagicMock()
    args.files = files
    return args


@patch("carta.embed.pipeline.run_embed_file")
@patch("carta.config.load_config")
@patch("carta.config.find_config")
def test_targeted_calls_run_embed_file(mock_find_config, mock_load_config, mock_run_embed_file, tmp_path):
    """When files are passed, run_embed_file is called for each, lock is skipped."""
    from carta.cli import cmd_embed

    cfg_path = tmp_path / ".carta" / "config.yaml"
    cfg_path.parent.mkdir(parents=True)
    cfg_path.touch()
    mock_find_config.return_value = cfg_path
    mock_load_config.return_value = {
        "modules": {"doc_embed": True},
        "qdrant_url": "http://localhost:6333",
        "embed": {"ollama_url": "http://localhost:11434", "ollama_model": "nomic-embed-text"},
    }
    mock_run_embed_file.return_value = {"status": "ok", "chunks": 42}

    pdf = tmp_path / "test.pdf"
    pdf.touch()

    with patch("carta.cli._acquire_embed_lock") as mock_lock, \
         patch("carta.ui.Progress") as MockProgress:
        mock_progress = MagicMock()
        mock_progress.__enter__ = MagicMock(return_value=mock_progress)
        mock_progress.__exit__ = MagicMock(return_value=False)
        MockProgress.return_value = mock_progress

        with pytest.raises(SystemExit) as exc_info:
            cmd_embed(_make_args([str(pdf)]))

        assert exc_info.value.code == 0
        # Lock must NOT be acquired for targeted embed
        mock_lock.assert_not_called()
        # run_embed_file called with force=True
        mock_run_embed_file.assert_called_once_with(
            Path(str(pdf)), mock_load_config.return_value, force=True, progress=mock_progress
        )


@patch("carta.embed.pipeline.run_embed_file")
@patch("carta.config.load_config")
@patch("carta.config.find_config")
def test_targeted_missing_file_exits_1(mock_find_config, mock_load_config, mock_run_embed_file, tmp_path):
    """FileNotFoundError from run_embed_file causes exit(1)."""
    from carta.cli import cmd_embed

    cfg_path = tmp_path / ".carta" / "config.yaml"
    cfg_path.parent.mkdir(parents=True)
    cfg_path.touch()
    mock_find_config.return_value = cfg_path
    mock_load_config.return_value = {
        "modules": {"doc_embed": True},
        "qdrant_url": "http://localhost:6333",
        "embed": {},
    }
    mock_run_embed_file.side_effect = FileNotFoundError("no such file: ghost.pdf")

    with patch("carta.ui.Progress") as MockProgress:
        mock_progress = MagicMock()
        mock_progress.__enter__ = MagicMock(return_value=mock_progress)
        mock_progress.__exit__ = MagicMock(return_value=False)
        MockProgress.return_value = mock_progress

        with pytest.raises(SystemExit) as exc_info:
            cmd_embed(_make_args(["ghost.pdf"]))

        assert exc_info.value.code == 1


@patch("carta.embed.pipeline.run_embed_file")
@patch("carta.config.load_config")
@patch("carta.config.find_config")
def test_targeted_multiple_files_all_processed(mock_find_config, mock_load_config, mock_run_embed_file, tmp_path):
    """All files are processed even if one errors; exit 1 if any errors."""
    from carta.cli import cmd_embed

    cfg_path = tmp_path / ".carta" / "config.yaml"
    cfg_path.parent.mkdir(parents=True)
    cfg_path.touch()
    mock_find_config.return_value = cfg_path
    mock_load_config.return_value = {
        "modules": {"doc_embed": True},
        "qdrant_url": "http://localhost:6333",
        "embed": {},
    }
    mock_run_embed_file.side_effect = [
        {"status": "ok", "chunks": 10},
        FileNotFoundError("missing.pdf not found"),
        {"status": "ok", "chunks": 5},
    ]

    with patch("carta.ui.Progress") as MockProgress:
        mock_progress = MagicMock()
        mock_progress.__enter__ = MagicMock(return_value=mock_progress)
        mock_progress.__exit__ = MagicMock(return_value=False)
        MockProgress.return_value = mock_progress

        with pytest.raises(SystemExit) as exc_info:
            cmd_embed(_make_args(["a.pdf", "missing.pdf", "b.pdf"]))

        assert exc_info.value.code == 1
        assert mock_run_embed_file.call_count == 3


@patch("carta.embed.pipeline.upsert_chunks", return_value=1)
@patch("carta.embed.pipeline.delete_other_generations")
def test_embed_one_file_cleans_other_generations(mock_del, mock_upsert, tmp_path):
    """_embed_one_file calls delete_other_generations with correct file_path and generation."""
    from carta.embed.pipeline import _embed_one_file

    repo = tmp_path
    doc = repo / "docs" / "a.md"
    doc.parent.mkdir(parents=True)
    doc.write_text("# Title\n\nsome content here\n")
    cfg = {
        "project_name": "test",
        "qdrant_url": "http://localhost:6333",
        "embed": {"ollama_url": "http://x", "ollama_model": "m"},
    }
    file_info = {"slug": "a", "doc_type": "unknown", "generation": 2}
    count, updates = _embed_one_file(doc, file_info, cfg, MagicMock(), repo, 800, 0.15)

    mock_del.assert_called_once()
    args = mock_del.call_args.args
    assert args[2] == "docs/a.md"
    assert args[3] == 2

    # Verify chunks passed to upsert carry doc_generation == 2
    upsert_call_chunks = mock_upsert.call_args.args[0]
    assert all(c.get("doc_generation") == 2 for c in upsert_call_chunks)


# ---------------------------------------------------------------------------
# Test 2: run_embed_file resets visual_done on hash-changed re-embed
# ---------------------------------------------------------------------------

@patch("carta.embed.pipeline.delete_other_generations")
@patch("carta.embed.pipeline.upsert_chunks", return_value=3)
@patch("carta.embed.pipeline.mark_sidecar_stale")
@patch("carta.embed.pipeline.ensure_collection")
@patch("carta.embed.pipeline.QdrantClient")
@patch("carta.embed.pipeline._update_sidecar")
@patch("carta.embed.pipeline.read_sidecar")
@patch("carta.embed.pipeline.write_sidecar")
@patch("carta.embed.pipeline.sidecar_path")
@patch("carta.embed.pipeline.compute_file_hash", return_value="newhash")
@patch("carta.embed.pipeline.needs_rehash", return_value=True)
@patch("carta.embed.pipeline.find_config")
def test_run_embed_file_resets_visual_done_on_hash_change(
    mock_find_config,
    mock_needs_rehash,
    mock_compute_hash,
    mock_sidecar_path,
    mock_write_sidecar,
    mock_read_sidecar,
    mock_update_sidecar,
    MockQdrantClient,
    mock_ensure_collection,
    mock_mark_stale,
    mock_upsert,
    mock_del,
    tmp_path,
):
    """run_embed_file on a hash-changed file must write visual_done: [] into the sidecar.

    Regression: before fix, visual_done was never cleared on re-embed, so
    add_pending_pages excluded already-done pages and OCR chunks were permanently lost.
    """
    from carta.embed.pipeline import run_embed_file
    from carta.embed.visual_queue import VISUAL_DONE_KEY

    # Set up a real markdown file so _embed_one_file can read it
    doc = tmp_path / "docs" / "a.md"
    doc.parent.mkdir(parents=True)
    doc.write_text("# Title\n\nsome content here to chunk\n")

    cfg_path = tmp_path / ".carta" / "config.yaml"
    cfg_path.parent.mkdir(parents=True)
    cfg_path.touch()
    mock_find_config.return_value = cfg_path

    sc_path = tmp_path / ".carta" / "sidecars" / "a.embed-meta.yaml"
    mock_sidecar_path.return_value = sc_path
    sc_path.parent.mkdir(parents=True)
    sc_path.touch()

    # Sidecar already has some visual_done pages from a previous visual pass
    mock_read_sidecar.return_value = {
        "slug": "a",
        "doc_type": "unknown",
        "generation": 1,
        "file_hash": "oldhash",
        "sidecar_id": None,
        VISUAL_DONE_KEY: [1, 2, 3],
    }

    cfg = {
        "project_name": "test",
        "qdrant_url": "http://localhost:6333",
        "embed": {"ollama_url": "http://x", "ollama_model": "m"},
    }

    run_embed_file(doc, cfg)

    # Collect all _update_sidecar calls and merge their updates (last write wins per key)
    assert mock_update_sidecar.called, "_update_sidecar was never called"
    merged = {}
    for c in mock_update_sidecar.call_args_list:
        merged.update(c.args[1])

    assert VISUAL_DONE_KEY in merged, (
        f"visual_done key not written to sidecar on hash-change re-embed; "
        f"sidecar updates keys: {list(merged.keys())}"
    )
    assert merged[VISUAL_DONE_KEY] == [], (
        f"Expected visual_done=[] after hash-change re-embed, got: {merged[VISUAL_DONE_KEY]}"
    )


# ---------------------------------------------------------------------------
# Test 3: partial upsert skips delete_other_generations and prints warning
# ---------------------------------------------------------------------------

@patch("carta.embed.pipeline.delete_other_generations")
@patch("carta.embed.pipeline.upsert_chunks", return_value=1)   # partial: returns 1, expected >= 2
def test_partial_upsert_skips_cleanup_and_warns(mock_upsert, mock_del, tmp_path, capsys):
    """When upsert_chunks returns fewer chunks than attempted, cleanup must be skipped
    and a warning mentioning 'partial upsert' must be printed.

    Regression: before fix, cleanup after partial upsert deleted gen-1 points while
    the failed gen-2 batch left those chunk indexes in neither generation.
    """
    from carta.embed.pipeline import _embed_one_file

    repo = tmp_path
    doc = repo / "docs" / "b.md"
    doc.parent.mkdir(parents=True)
    # Write enough content to produce at least 2 chunks
    doc.write_text("# Title\n\n" + ("word " * 200) + "\n\n" + ("more content " * 200) + "\n")

    cfg = {
        "project_name": "test",
        "qdrant_url": "http://localhost:6333",
        "embed": {"ollama_url": "http://x", "ollama_model": "m"},
    }
    file_info = {"slug": "b", "doc_type": "unknown", "generation": 2}

    _embed_one_file(doc, file_info, cfg, MagicMock(), repo, 800, 0.15)

    # Cleanup must NOT have been called
    mock_del.assert_not_called()

    # Warning must be printed mentioning "partial upsert"
    captured = capsys.readouterr()
    assert "partial upsert" in captured.out or "partial upsert" in captured.err, (
        f"Expected 'partial upsert' warning in stdout/stderr; got:\n"
        f"  stdout: {captured.out!r}\n"
        f"  stderr: {captured.err!r}"
    )


# ---------------------------------------------------------------------------
# Test 4: zero-usable-chunks files are flagged extraction_failed
# ---------------------------------------------------------------------------

@patch("carta.embed.pipeline.upsert_chunks", return_value=0)
def test_zero_usable_chunks_marks_extraction_failed(mock_upsert, tmp_path, capsys):
    """A file that yields zero usable text chunks gets status=extraction_failed.

    An empty markdown file produces zero chunks (not empty-text chunks), so the
    check must handle the case where len(enriched) == 0 directly.
    """
    from carta.embed.pipeline import _embed_one_file

    repo = tmp_path
    doc = repo / "docs" / "scan.md"
    doc.parent.mkdir(parents=True)
    doc.write_text("")  # extraction yields nothing

    cfg = {
        "project_name": "test", "qdrant_url": "http://localhost:6333",
        "embed": {"ollama_url": "http://x", "ollama_model": "m"},
    }
    count, updates = _embed_one_file(
        doc, {"slug": "scan", "doc_type": "unknown"},
        cfg, MagicMock(), repo, 800, 0.15,
    )
    assert count == 0
    assert updates["status"] == "extraction_failed"
    captured = capsys.readouterr()
    assert "0 extractable characters" in captured.out or "0 extractable characters" in captured.err


# ---------------------------------------------------------------------------
# Test 5: partial-empty enriched list — cleanup gate uses non-empty count
# ---------------------------------------------------------------------------

@patch("carta.embed.pipeline.delete_other_generations")
@patch("carta.embed.pipeline.upsert_chunks", return_value=2)
def test_partial_empty_chunks_cleanup_gate_uses_nonempty_count(mock_upsert, mock_del, tmp_path):
    """When some chunks are empty and upsert_chunks returns 2 (non-empty count),
    cleanup SHOULD be called — expected_text must count only non-empty chunks.

    This verifies that the expected_text fix doesn't accidentally break the gate
    for files where some (but not all) chunks are empty.
    """
    from carta.embed.pipeline import _embed_one_file

    repo = tmp_path
    doc = repo / "docs" / "c.md"
    doc.parent.mkdir(parents=True)
    # Write content that produces chunks; upsert_chunks is patched to return 2
    # (simulating 3 raw chunks of which 1 was empty and dropped by the guard).
    doc.write_text("# Title\n\nsome content\n\nmore content\n\neven more content\n")

    cfg = {
        "project_name": "test", "qdrant_url": "http://localhost:6333",
        "embed": {"ollama_url": "http://x", "ollama_model": "m"},
    }
    file_info = {"slug": "c", "doc_type": "unknown", "generation": 1}

    # Patch chunk_text to return 3 chunks where 1 is empty-text, 2 are real.
    # This simulates the scenario where upsert_chunks drops 1 empty, returns 2.
    with patch("carta.embed.pipeline.chunk_text") as mock_chunk:
        mock_chunk.return_value = [
            {"chunk_index": 0, "text": "real content one"},
            {"chunk_index": 1, "text": ""},  # empty — will be dropped by guard
            {"chunk_index": 2, "text": "real content two"},
        ]
        _embed_one_file(doc, file_info, cfg, MagicMock(), repo, 800, 0.15)

    # With expected_text = 2 (non-empty) and upsert returning 2, gate must pass
    mock_del.assert_called_once()


@patch("carta.embed.pipeline.upsert_chunks", return_value=0)
@patch("carta.embed.pipeline.extract_pdf_text_and_classify")
@patch("carta.embed.pipeline.PageAnalyzer")
def test_pdf_zero_usable_no_queue_marks_extraction_failed(
        mock_analyzer, mock_extract, mock_upsert, tmp_path, capsys):
    """A PDF with no extractable text and no pages queued for pass-2 is flagged."""
    from carta.embed.pipeline import _embed_one_file
    repo = tmp_path
    doc = repo / "docs" / "scan.pdf"
    doc.parent.mkdir(parents=True)
    doc.write_bytes(b"%PDF-1.4 fake")
    mock_extract.return_value = ([{"page": 1, "text": ""}], [])
    cfg = {
        "project_name": "test", "qdrant_url": "http://localhost:6333",
        "embed": {"ollama_url": "http://x", "ollama_model": "m"},
    }
    count, updates = _embed_one_file(doc, {"slug": "scan", "doc_type": "unknown"},
                                     cfg, MagicMock(), repo, 800, 0.15)
    assert count == 0
    assert updates["status"] == "extraction_failed"
    assert "0 extractable characters" in capsys.readouterr().out


@patch("carta.embed.pipeline._mark_or_collect_visual_pages")
@patch("carta.embed.pipeline.upsert_chunks", return_value=0)
@patch("carta.embed.pipeline.extract_pdf_text_and_classify")
@patch("carta.embed.pipeline.PageAnalyzer")
def test_pdf_zero_text_with_queued_visual_pages_not_flagged(
        mock_analyzer, mock_extract, mock_upsert, mock_mark, tmp_path):
    """A PDF awaiting pass-2 visual embedding is NOT extraction_failed."""
    from carta.embed.pipeline import _embed_one_file
    from carta.embed.visual_queue import VISUAL_PENDING_KEY
    repo = tmp_path
    doc = repo / "docs" / "scan.pdf"
    doc.parent.mkdir(parents=True)
    doc.write_bytes(b"%PDF-1.4 fake")
    mock_extract.return_value = ([{"page": 1, "text": ""}], [object()])
    mock_mark.return_value = {VISUAL_PENDING_KEY: [1]}
    cfg = {
        "project_name": "test", "qdrant_url": "http://localhost:6333",
        "embed": {"ollama_url": "http://x", "ollama_model": "m"},
    }
    count, updates = _embed_one_file(doc, {"slug": "scan", "doc_type": "unknown"},
                                     cfg, MagicMock(), repo, 800, 0.15)
    assert count == 0
    assert updates["status"] != "extraction_failed"


@patch("carta.embed.pipeline.delete_other_generations")
@patch("carta.embed.pipeline._build_vision_metadata", return_value=None)
@patch("carta.embed.pipeline.extract_pdf_text_and_classify")
@patch("carta.embed.pipeline.PageAnalyzer")
def test_empty_image_chunks_do_not_trip_partial_upsert_gate(
        mock_analyzer, mock_extract, mock_vmeta, mock_del, tmp_path, capsys):
    """A dropped (empty-text) image chunk is a clean drop, not a partial failure:
    the stale-generation cleanup must still run and no partial-upsert warning prints."""
    from carta.embed import pipeline as pl
    repo = tmp_path
    doc = repo / "docs" / "mixed.pdf"
    doc.parent.mkdir(parents=True)
    doc.write_bytes(b"%PDF-1.4 fake")
    # Text extraction yields one real chunk; classification fails (None) so the
    # legacy inline-vision path is skipped — we instead exercise the gate math
    # directly through upsert_chunks' real drop behaviour via the text path.
    mock_extract.side_effect = Exception("classify boom")

    real_counts = {}
    def fake_upsert(chunks, cfg, client=None):
        # emulate upsert_chunks' drop filter faithfully
        kept = [c for c in chunks if (c.get("text") or "").strip()]
        real_counts["attempted"] = len(chunks)
        real_counts["kept"] = len(kept)
        return len(kept)

    with patch("carta.embed.pipeline.upsert_chunks", side_effect=fake_upsert), \
         patch("carta.embed.pipeline.extract_pdf_text",
               return_value=[{"page": 1, "text": "real text"}, {"page": 2, "text": ""}]):
        cfg = {"project_name": "test", "qdrant_url": "http://localhost:6333",
               "embed": {"ollama_url": "http://x", "ollama_model": "m",
                         "two_pass_visual": True}}
        count, updates = pl._embed_one_file(doc, {"slug": "mixed", "doc_type": "unknown"},
                                            cfg, MagicMock(), repo, 800, 0.15)

    out = capsys.readouterr()
    assert "partial upsert" not in out.out + out.err
    mock_del.assert_called_once()


@patch("carta.embed.pipeline.delete_other_generations")
@patch("carta.embed.pipeline._build_vision_metadata", return_value=None)
def test_empty_image_description_does_not_trip_partial_upsert_gate(
        mock_vmeta, mock_del, tmp_path, capsys):
    """An empty VLM/OCR description yields an empty image chunk that upsert_chunks
    drops — a clean drop, not a partial failure. Cleanup must still run."""
    from carta.embed import pipeline as pl

    repo = tmp_path
    doc = repo / "docs" / "imgs.pdf"
    doc.parent.mkdir(parents=True)
    doc.write_bytes(b"%PDF-1.4 fake")

    def fake_upsert(chunks, cfg, client=None):
        return len([c for c in chunks if (c.get("text") or "").strip()])

    descs = [
        {"text": "a real diagram description", "page_num": 1, "image_index": 0},
        {"text": "", "page_num": 2, "image_index": 0},  # blank page → empty desc
    ]
    with patch("carta.embed.pipeline.upsert_chunks", side_effect=fake_upsert), \
         patch("carta.embed.pipeline.extract_pdf_text",
               return_value=[{"page": 1, "text": "real body text"}]), \
         patch("carta.vision.router.extract_image_descriptions_intelligent",
               return_value=descs):
        cfg = {"project_name": "test", "qdrant_url": "http://localhost:6333",
               "embed": {"ollama_url": "http://x", "ollama_model": "m",
                         "two_pass_visual": False}}
        count, updates = pl._embed_one_file(doc, {"slug": "imgs", "doc_type": "unknown"},
                                            cfg, MagicMock(), repo, 800, 0.15)

    out = capsys.readouterr()
    assert "partial upsert" not in out.out + out.err
    mock_del.assert_called_once()
