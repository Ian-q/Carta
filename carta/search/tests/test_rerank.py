from carta.search.rerank import rerank_hits


def test_rerank_reorders_by_cross_encoder_score(monkeypatch):
    import carta.search.rerank as r
    # Fake cross-encoder: score = +1 if "baud" in the chunk text, else 0.
    monkeypatch.setattr(r, "_scores",
                        lambda query, texts, model_name: [1.0 if "baud" in t else 0.0 for t in texts])

    hits = [
        {"text": "unrelated content", "score": 0.99, "file_path": "a.md"},
        {"text": "the bridge runs at 921600 baud", "score": 0.10, "file_path": "b.md"},
    ]
    out = rerank_hits("serial bridge baud", hits, model_name="x", top_n=2)
    assert out[0]["file_path"] == "b.md"            # promoted despite lower original score
    assert out[0]["rerank_score"] == 1.0


def test_rerank_truncates_to_top_n(monkeypatch):
    import carta.search.rerank as r
    monkeypatch.setattr(r, "_scores", lambda q, texts, m: list(range(len(texts))))
    hits = [{"text": str(i), "file_path": f"{i}.md"} for i in range(5)]
    out = rerank_hits("q", hits, model_name="x", top_n=2)
    assert len(out) == 2
