from unittest.mock import MagicMock
from carta.embed import pipeline
from carta.embed.pipeline import _visual_chunk_index_pass2
from carta.embed.embed import _point_id_versioned
from carta.embed.visual_queue import VISUAL_PENDING_KEY, VISUAL_DONE_KEY


def _mock_router_embedder(monkeypatch):
    """Patch SmartRouter and ColPaliEmbedder so run_visual_embed doesn't need real models.

    run_visual_embed / _visual_embed_one_page import these *locally* from their source
    modules (carta.embed.colpali, carta.vision.router), so the patch must target the
    source modules — patching carta.embed.pipeline.* has no effect and lets the real
    ColPaliEmbedder constructor run, which ImportErrors when the [visual] extra (torch)
    isn't installed (e.g. in CI).
    """
    monkeypatch.setattr("carta.vision.router.SmartRouter", lambda cfg: MagicMock(), raising=False)
    monkeypatch.setattr("carta.embed.colpali.ColPaliEmbedder", lambda **k: MagicMock())


def test_drainer_checkpoints_each_page(monkeypatch, tmp_path):
    sc = {"current_path": "docs/x.pdf", "slug": "x", VISUAL_PENDING_KEY: [1, 2], VISUAL_DONE_KEY: []}
    monkeypatch.setattr(pipeline, "_discover_visual_pending", lambda repo_root: [("sc_path", sc)], raising=False)
    written = []
    monkeypatch.setattr(pipeline, "_update_sidecar", lambda sc_path, updates: written.append(dict(updates)))
    monkeypatch.setattr(pipeline, "_visual_embed_one_page",
                        lambda sidecar, page, cfg, client, repo_root, router, embedder, verbose=False: True,
                        raising=False)
    monkeypatch.setattr(pipeline, "is_colpali_available", lambda: True, raising=False)
    monkeypatch.setattr(pipeline, "QdrantClient", lambda **k: MagicMock())
    _mock_router_embedder(monkeypatch)
    summary = pipeline.run_visual_embed(tmp_path, {"qdrant_url": "x", "embed": {"visual_timeout_s": 0}})
    assert summary["pages_embedded"] == 2
    assert sc[VISUAL_PENDING_KEY] == [] and sc[VISUAL_DONE_KEY] == [1, 2]


def test_drainer_writes_status_file(monkeypatch, tmp_path):
    """run_visual_embed must drive StatusWriter so the status-line widget tracks
    the --visual pass (previously only `carta embed` wrote embed-status.json)."""
    import json
    sc = {"current_path": "docs/x.pdf", "slug": "x", VISUAL_PENDING_KEY: [1, 2], VISUAL_DONE_KEY: []}
    monkeypatch.setattr(pipeline, "_discover_visual_pending", lambda r: [("sc_path", sc)], raising=False)
    monkeypatch.setattr(pipeline, "_update_sidecar", lambda *a, **k: None)
    monkeypatch.setattr(pipeline, "_visual_embed_one_page",
                        lambda *a, **k: True, raising=False)
    monkeypatch.setattr(pipeline, "is_colpali_available", lambda: True, raising=False)
    monkeypatch.setattr(pipeline, "QdrantClient", lambda **k: MagicMock())
    _mock_router_embedder(monkeypatch)
    (tmp_path / ".carta").mkdir()  # StatusWriter writes into an existing .carta/

    pipeline.run_visual_embed(tmp_path, {"qdrant_url": "x", "embed": {}})

    status_path = tmp_path / ".carta" / "embed-status.json"
    assert status_path.exists(), "run_visual_embed must write embed-status.json"
    st = json.loads(status_path.read_text())
    assert st["total"] == 2          # 2 pending pages
    assert st["embedded"] == 2
    assert st["phase"] == "done"


def test_drainer_leaves_failed_page_pending(monkeypatch, tmp_path):
    sc = {"current_path": "docs/x.pdf", "slug": "x", VISUAL_PENDING_KEY: [1], VISUAL_DONE_KEY: []}
    monkeypatch.setattr(pipeline, "_discover_visual_pending", lambda r: [("sc", sc)], raising=False)
    monkeypatch.setattr(pipeline, "_update_sidecar", lambda *a, **k: None)
    def boom(*a, **k):
        raise RuntimeError("model crashed")
    monkeypatch.setattr(pipeline, "_visual_embed_one_page", boom, raising=False)
    monkeypatch.setattr(pipeline, "is_colpali_available", lambda: True, raising=False)
    monkeypatch.setattr(pipeline, "QdrantClient", lambda **k: MagicMock())
    _mock_router_embedder(monkeypatch)
    summary = pipeline.run_visual_embed(tmp_path, {"qdrant_url": "x", "embed": {}})
    assert summary["pages_embedded"] == 0
    assert summary["pages_failed"] == 1
    assert sc[VISUAL_PENDING_KEY] == [1]


def test_drainer_preflight_when_visual_unavailable(monkeypatch, tmp_path):
    monkeypatch.setattr(pipeline, "is_colpali_available", lambda: False, raising=False)
    summary = pipeline.run_visual_embed(tmp_path, {"qdrant_url": "x", "embed": {}})
    assert summary.get("status") == "visual_unavailable"
    assert summary["pages_embedded"] == 0


def test_pass2_chunks_carry_sidecar_generation(monkeypatch, tmp_path):
    """Pass-2 OCR chunks upserted by _visual_embed_one_page must carry doc_generation
    equal to the sidecar's 'generation' field (not default to 1).

    Regression: before fix, the chunk literal in _visual_embed_one_page omitted
    doc_generation, so build_point defaulted those points to generation 1 and the
    next re-embed's cleanup deleted them permanently.
    """
    from carta.embed.pipeline import _visual_embed_one_page
    from carta.embed import pipeline

    # Sidecar says this file is generation 3
    sidecar = {
        "current_path": "docs/x.pdf",
        "slug": "x",
        "generation": 3,
        VISUAL_PENDING_KEY: [2],
        VISUAL_DONE_KEY: [],
    }

    # Patch out heavy I/O — we only care what upsert_chunks receives
    captured_chunks: list[list[dict]] = []

    def fake_upsert(chunks, cfg, client=None):
        captured_chunks.append(list(chunks))
        return len(chunks)

    monkeypatch.setattr(pipeline, "upsert_chunks", fake_upsert)

    # Fake fitz so no real PDF is opened
    fake_page = MagicMock()
    fake_page.__len__ = lambda self: 5
    fake_doc = MagicMock()
    fake_doc.__len__ = MagicMock(return_value=5)
    fake_doc.__getitem__ = MagicMock(return_value=fake_page)
    fake_doc.__enter__ = MagicMock(return_value=fake_doc)
    fake_doc.__exit__ = MagicMock(return_value=False)
    fake_fitz = MagicMock()
    fake_fitz.open.return_value = fake_doc

    monkeypatch.setattr("carta.embed.pipeline.upsert_visual_pages", lambda *a, **k: None)

    # Router that returns one OCR chunk for the page
    fake_router = MagicMock()
    fake_router.analyzer.analyze.return_value = MagicMock()
    fake_router._route.return_value = [{"text": "ocr text here", "image_index": 0, "model_used": "glm-ocr", "content_type": "visual"}]

    # ColPali embedder that returns nothing (keep test focused on text chunks)
    fake_embedder = MagicMock()
    fake_embedder.embed_pdf_pages.return_value = []

    # Patch fitz import inside the function
    import sys
    sys.modules["fitz"] = fake_fitz

    cfg = {"project_name": "test", "qdrant_url": "x", "embed": {"chunking": {"max_tokens": 400}}}
    # Create a real tmp file so file_path exists
    pdf_file = tmp_path / "docs" / "x.pdf"
    pdf_file.parent.mkdir(parents=True)
    pdf_file.write_bytes(b"%PDF-1.4 fake")

    sidecar["current_path"] = str(pdf_file.relative_to(tmp_path))

    _visual_embed_one_page(sidecar, 2, cfg, MagicMock(), tmp_path, fake_router, fake_embedder)

    sys.modules.pop("fitz", None)

    assert captured_chunks, "upsert_chunks was never called — no OCR chunks were produced"
    all_chunks = [c for batch in captured_chunks for c in batch]
    assert all_chunks, "upsert_chunks was called but with empty chunk list"
    bad = [c for c in all_chunks if c.get("doc_generation") != 3]
    assert not bad, (
        f"Expected all pass-2 chunks to have doc_generation=3 (sidecar generation), "
        f"but got: {[c.get('doc_generation') for c in all_chunks]}"
    )


def test_pass2_point_ids_disjoint_from_pass1():
    """Pass-2 visual/OCR chunk IDs must never collide with pass-1 text chunk IDs.

    upsert_chunks derives the Qdrant point ID as _point_id_versioned(file_path,
    chunk_index, generation), i.e. md5("{file_path}:{chunk_index}:g{gen}").

    Pass-1 uses integer chunk_index values  → md5("{key}:0:g1"), md5("{key}:1:g1"), …
    Pass-2 uses _visual_chunk_index_pass2() → md5("{key}:visual:{page}:{i}:g1")

    An integer string can never equal "visual:{page}:{i}" so the two namespaces
    are provably disjoint.  This test confirms the MD5 outputs don't collide in
    practice across a representative sample.
    """
    key = "docs/my-doc.md"

    # Pass-1: simulate up to 1000 text chunks (generous upper bound)
    pass1_ids = {_point_id_versioned(key, ci, 1) for ci in range(1000)}

    # Pass-2: pages 1..5, sub-indices 0..9 each
    pass2_ids = set()
    for page in range(6):  # includes page 0 edge case
        for i in range(10):
            ci = _visual_chunk_index_pass2(page, i)
            pass2_ids.add(_point_id_versioned(key, ci, 1))

    assert pass1_ids.isdisjoint(pass2_ids), (
        "Pass-1 and pass-2 point IDs collide — namespace isolation is broken"
    )


def test_drainer_filters_out_of_scope_pages():
    """The --visual drain must honor colpali_scoped_paths: out-of-scope sources
    (e.g. patents, not in [datasheets, manuals, suppliers]) that were queued as
    visual_pending must be skipped — otherwise the expensive ColPali pass burns on
    docs the user excluded and pollutes the _visual collection with low-value
    figures. The inline path enforced scope; the two-pass drain did not (the bug)."""
    scopes = ["docs/reference/datasheets/", "docs/reference/manuals/"]
    queued = [
        ("a.yaml", {"current_path": "docs/reference/datasheets/murata.pdf", VISUAL_PENDING_KEY: [1]}),
        ("b.yaml", {"current_path": "docs/reference/patents/prior-art/US123.pdf", VISUAL_PENDING_KEY: [1, 2]}),
    ]
    out = pipeline._filter_visual_pending_in_scope(queued, scopes)
    paths = [sc["current_path"] for _, sc in out]
    assert "docs/reference/datasheets/murata.pdf" in paths
    assert "docs/reference/patents/prior-art/US123.pdf" not in paths


def test_drainer_no_scope_keeps_all_pages():
    """Empty colpali_scoped_paths means no restriction (backward compatible)."""
    queued = [("b.yaml", {"current_path": "docs/reference/patents/x.pdf", VISUAL_PENDING_KEY: [1]})]
    assert pipeline._filter_visual_pending_in_scope(queued, []) == queued
