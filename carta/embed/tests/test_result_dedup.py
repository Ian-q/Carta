"""Result de-duplication: distinct docs in the shown top_n (spec 2026-06-17)."""
from carta.embed.pipeline import _dedupe_by_source


def test_dedupe_keeps_first_occurrence_and_order():
    results = [
        {"source": "a.md", "excerpt": "1"},
        {"source": "b.md", "excerpt": "2"},
        {"source": "a.md", "excerpt": "3"},   # dup of a.md
        {"source": "c.md", "excerpt": "4"},
        {"source": "b.md", "excerpt": "5"},   # dup of b.md
    ]
    out = _dedupe_by_source(results)
    assert [h["source"] for h in out] == ["a.md", "b.md", "c.md"]
    assert out[0]["excerpt"] == "1"  # best-ranked occurrence kept


def test_dedupe_keeps_distinct_visual_pages():
    results = [
        {"source": "ds.pdf (page 20)"},
        {"source": "ds.pdf (page 20)"},   # exact dup
        {"source": "ds.pdf (page 31)"},   # different page = distinct result
    ]
    out = _dedupe_by_source(results)
    assert [h["source"] for h in out] == ["ds.pdf (page 20)", "ds.pdf (page 31)"]


def test_dedupe_passes_through_missing_source():
    results = [{"excerpt": "x"}, {"excerpt": "y"}]  # no source key
    out = _dedupe_by_source(results)
    assert len(out) == 2
