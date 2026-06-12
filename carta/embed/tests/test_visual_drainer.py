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
