"""Tests for carta.embed.parse string-level helpers."""
from carta.embed.parse import sections_from_markdown


def test_sections_from_markdown_splits_on_headings_and_strips_frontmatter():
    text = (
        "---\ntitle: Demo\n---\n"
        "intro paragraph\n\n"
        "## First\nbody one\n\n"
        "## Second\nbody two\n"
    )
    sections, fm = sections_from_markdown(text)
    assert fm == {"title": "Demo"}
    headings = [s["headings"][0] for s in sections]
    assert "## First" in headings
    assert "## Second" in headings
    # the intro (no heading) is captured as "(intro)"
    assert any(h == "(intro)" for h in headings)


def test_sections_from_markdown_empty_string():
    sections, fm = sections_from_markdown("")
    assert sections == []
    assert fm == {}
