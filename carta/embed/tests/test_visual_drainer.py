from unittest.mock import MagicMock
from carta.embed import pipeline
from carta.embed.visual_queue import VISUAL_PENDING_KEY, VISUAL_DONE_KEY


def test_drainer_checkpoints_each_page(monkeypatch, tmp_path):
    sc = {"current_path": "docs/x.pdf", "slug": "x", VISUAL_PENDING_KEY: [1, 2], VISUAL_DONE_KEY: []}
    monkeypatch.setattr(pipeline, "_discover_visual_pending", lambda repo_root: [("sc_path", sc)], raising=False)
    written = []
    monkeypatch.setattr(pipeline, "_update_sidecar", lambda sc_path, updates: written.append(dict(updates)))
    monkeypatch.setattr(pipeline, "_visual_embed_one_page",
                        lambda sidecar, page, cfg, client, repo_root, verbose: True, raising=False)
    monkeypatch.setattr(pipeline, "is_colpali_available", lambda: True, raising=False)
    monkeypatch.setattr(pipeline, "QdrantClient", lambda **k: MagicMock())
    summary = pipeline.run_visual_embed(tmp_path, {"qdrant_url": "x", "embed": {"visual_timeout_s": 0}})
    assert summary["pages_embedded"] == 2
    assert sc[VISUAL_PENDING_KEY] == [] and sc[VISUAL_DONE_KEY] == [1, 2]


def test_drainer_leaves_failed_page_pending(monkeypatch, tmp_path):
    sc = {"current_path": "docs/x.pdf", "slug": "x", VISUAL_PENDING_KEY: [1], VISUAL_DONE_KEY: []}
    monkeypatch.setattr(pipeline, "_discover_visual_pending", lambda r: [("sc", sc)], raising=False)
    monkeypatch.setattr(pipeline, "_update_sidecar", lambda *a, **k: None)
    def boom(*a, **k):
        raise RuntimeError("model crashed")
    monkeypatch.setattr(pipeline, "_visual_embed_one_page", boom, raising=False)
    monkeypatch.setattr(pipeline, "is_colpali_available", lambda: True, raising=False)
    monkeypatch.setattr(pipeline, "QdrantClient", lambda **k: MagicMock())
    summary = pipeline.run_visual_embed(tmp_path, {"qdrant_url": "x", "embed": {}})
    assert summary["pages_embedded"] == 0
    assert summary["pages_failed"] == 1
    assert sc[VISUAL_PENDING_KEY] == [1]


def test_drainer_preflight_when_visual_unavailable(monkeypatch, tmp_path):
    monkeypatch.setattr(pipeline, "is_colpali_available", lambda: False, raising=False)
    summary = pipeline.run_visual_embed(tmp_path, {"qdrant_url": "x", "embed": {}})
    assert summary.get("status") == "visual_unavailable"
    assert summary["pages_embedded"] == 0
