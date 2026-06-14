"""Tests for contextual chunk headers (issue #19)."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

from carta.config import DEFAULTS


def test_default_enables_contextual_header():
    assert DEFAULTS["embed"]["chunking"]["contextual_header"] is True


# ---------------------------------------------------------------------------
# resolve_doc_title
# ---------------------------------------------------------------------------

from carta.embed.parse import resolve_doc_title


def _pages(text):
    return [{"page": 1, "text": text, "headings": []}]


def test_title_prefers_frontmatter():
    title = resolve_doc_title({"title": "My Doc"}, _pages("# Other H1\nbody"), Path("x.md"))
    assert title == "My Doc"


def test_title_falls_back_to_h1():
    title = resolve_doc_title({}, _pages("# Real Title\n\nbody text"), Path("x.md"))
    assert title == "Real Title"


def test_title_h1_ignores_h2():
    # Only '# ' (H1) counts as a title, not '## '
    title = resolve_doc_title({}, _pages("## Section Only\n\nbody"), Path("FSM_GAIN_SCHEDULER.md"))
    assert title == "FSM GAIN SCHEDULER"


def test_title_falls_back_to_humanized_filename():
    title = resolve_doc_title({}, _pages("no heading here"), Path("docs/hardware/vcu/connector-map.md"))
    assert title == "connector map"


def test_title_blank_frontmatter_title_skipped():
    title = resolve_doc_title({"title": "   "}, _pages("# H1 Wins\nbody"), Path("x.md"))
    assert title == "H1 Wins"


# ---------------------------------------------------------------------------
# build_chunk_header
# ---------------------------------------------------------------------------

from carta.embed.parse import build_chunk_header


def test_header_title_and_heading():
    assert build_chunk_header("VCU Power Architecture", "## 12V Rail") == "VCU Power Architecture > 12V Rail"


def test_header_strips_hashes_from_heading():
    assert build_chunk_header("Doc", "### Task 1") == "Doc > Task 1"


def test_header_intro_heading_is_title_only():
    assert build_chunk_header("Doc", "(intro)") == "Doc"


def test_header_empty_heading_is_title_only():
    assert build_chunk_header("Doc", "") == "Doc"


def test_header_dedupes_title_equal_heading():
    assert build_chunk_header("Timing architecture", "# Timing architecture") == "Timing architecture"


def test_header_heading_only_when_no_title():
    assert build_chunk_header("", "## Pinout") == "Pinout"


def test_header_empty_when_nothing():
    assert build_chunk_header("", "") == ""


# ---------------------------------------------------------------------------
# apply_contextual_headers
# ---------------------------------------------------------------------------

from carta.embed.parse import apply_contextual_headers


def test_apply_sets_embed_text_keeps_text():
    chunks = [{"text": "body one", "section_heading": "## Pinout", "chunk_index": 0}]
    apply_contextual_headers(chunks, "CTS Control Harness")
    assert chunks[0]["text"] == "body one"  # unchanged
    assert chunks[0]["embed_text"] == "CTS Control Harness > Pinout\n\nbody one"


def test_apply_title_only_when_no_heading():
    chunks = [{"text": "cont chunk", "section_heading": "", "chunk_index": 1}]
    apply_contextual_headers(chunks, "CTS Control Harness")
    assert chunks[0]["embed_text"] == "CTS Control Harness\n\ncont chunk"


def test_apply_no_embed_text_when_header_empty():
    chunks = [{"text": "x", "section_heading": "", "chunk_index": 0}]
    apply_contextual_headers(chunks, "")  # no title, no heading -> empty header
    assert "embed_text" not in chunks[0]
