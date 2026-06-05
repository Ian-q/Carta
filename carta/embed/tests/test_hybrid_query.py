from unittest.mock import MagicMock
from carta.embed import pipeline


def test_hybrid_query_uses_prefetch_and_rrf(monkeypatch):
    captured = {}

    class FakeClient:
        def query_points(self, **kwargs):
            captured.update(kwargs)
            resp = MagicMock()
            resp.points = []
            return resp

    monkeypatch.setattr(pipeline, "embed_sparse_query",
                        lambda *a, **k: pipeline.__dict__.get("_TestSparse",
                            type("S", (), {"indices": [1, 2], "values": [0.5, 0.5]})()))

    pipeline._hybrid_query_collection(
        FakeClient(), "ET-embed_doc", "serial bridge baud",
        dense_vec=[0.0] * 768, top_n=5, prefetch_limit=40, bm25_model="Qdrant/bm25",
    )

    assert "prefetch" in captured
    assert len(captured["prefetch"]) == 2
    assert captured["limit"] == 5
