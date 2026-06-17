"""Tests for carta/embed — parse, embed, induct, pipeline."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from carta.config import collection_name
from carta.embed.parse import (
    _estimate_tokens,
    chunk_text,
    chunk_transcript,
)
from carta.embed.induct import (
    generate_sidecar_stub,
    infer_doc_type,
    read_sidecar,
    slug_from_filename,
    write_sidecar,
    sidecar_path,
)
from carta.embed.embed import _point_id_versioned, ensure_collection, get_embedding, upsert_chunks
from carta.embed.parse import extract_pdf_text
from carta.embed.pipeline import (
    discover_pending_files,
    is_lfs_pointer,
    run_embed,
    run_search,
    _heal_sidecar_current_paths,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

MINIMAL_CFG = {
    "project_name": "test-proj",
    "qdrant_url": "http://localhost:6333",
    "search": {"top_n": 5},
    "embed": {
        "reference_docs_path": "docs/reference/",
        "ollama_url": "http://localhost:11434",
        "ollama_model": "nomic-embed-text:latest",
        "chunking": {"max_tokens": 800, "overlap_fraction": 0.15},
    },
}


# ---------------------------------------------------------------------------
# collection_name helper
# ---------------------------------------------------------------------------

def test_collection_name_uses_project_name():
    assert collection_name(MINIMAL_CFG, "doc") == "test-proj_doc"


# ---------------------------------------------------------------------------
# parse.py — _estimate_tokens
# ---------------------------------------------------------------------------

def test_estimate_tokens_short():
    # 2 words → word_estimate = int(2 * 1.3) = 2; 11 chars → char_estimate = int(11/3) = 3
    # char estimate wins (more conservative for technical content)
    assert _estimate_tokens("hello world") == 3


def test_estimate_tokens_empty():
    # max(1, ...) so empty string returns 1
    assert _estimate_tokens("") == 1


# ---------------------------------------------------------------------------
# parse.py — chunk_text
# ---------------------------------------------------------------------------

def _make_pages(text: str, page: int = 1) -> list[dict]:
    return [{"page": page, "text": text, "headings": []}]


def test_chunk_text_short_page_single_chunk():
    pages = _make_pages("short text here")
    chunks = chunk_text(pages, max_tokens=800)
    assert len(chunks) == 1
    assert chunks[0]["chunk_index"] == 0
    assert chunks[0]["page"] == 1
    assert "short text here" in chunks[0]["text"]


def test_chunk_text_long_page_splits():
    # 200 words — well above max_tokens=10
    long_text = " ".join(["word"] * 200)
    pages = _make_pages(long_text)
    chunks = chunk_text(pages, max_tokens=10, overlap_fraction=0.0)
    assert len(chunks) > 1
    for i, c in enumerate(chunks):
        assert c["chunk_index"] == i


def test_chunk_text_pathological_long_token_with_overlap_terminates():
    # A single "word" that is very long can force the fallback splitter path.
    # With overlap enabled, chunk_text must still make monotonic progress and terminate.
    long_token = "X" * 10_000
    words = [long_token] + ["word"] * 50
    pages = _make_pages(" ".join(words))

    chunks = chunk_text(pages, max_tokens=10, overlap_fraction=0.5)

    assert chunks  # should produce something
    assert len(chunks) < 500  # sanity bound: no runaway chunk explosion
    for i, c in enumerate(chunks):
        assert c["chunk_index"] == i
        assert c["text"].strip()


def test_chunk_text_tiny_max_tokens_with_overlap_terminates():
    # Stress the overlap logic with a very small max_tokens.
    # This previously risked re-prepending the entire chunk as overlap.
    pages = _make_pages(" ".join(["word"] * 80))
    chunks = chunk_text(pages, max_tokens=5, overlap_fraction=0.6)
    assert chunks
    assert len(chunks) < 1000


def test_chunk_text_preserves_section_heading():
    pages = [{"page": 1, "text": "Introduction\n\nSome text here.", "headings": ["Introduction"]}]
    chunks = chunk_text(pages, max_tokens=800)
    assert chunks[0]["section_heading"] == "Introduction"


def test_chunk_text_multiple_pages():
    pages = [
        {"page": 1, "text": "page one text", "headings": []},
        {"page": 2, "text": "page two text", "headings": []},
    ]
    chunks = chunk_text(pages, max_tokens=800)
    assert len(chunks) == 2
    assert chunks[0]["page"] == 1
    assert chunks[1]["page"] == 2


# ---------------------------------------------------------------------------
# parse.py — chunk_transcript
# ---------------------------------------------------------------------------

def test_chunk_transcript_short():
    text = "Alice: Hello there.\nBob: Hi!"
    chunks = chunk_transcript(text, max_tokens=800)
    assert len(chunks) >= 1
    assert all("chunk_index" in c and "text" in c for c in chunks)


def test_chunk_transcript_empty():
    chunks = chunk_transcript("", max_tokens=800)
    assert chunks == []


def test_chunk_transcript_splits_long():
    # Build a segment longer than max_tokens=10
    long_seg = "Alice: " + " ".join(["word"] * 100)
    chunks = chunk_transcript(long_seg, max_tokens=10)
    assert len(chunks) > 1


# ---------------------------------------------------------------------------
# parse.py — slug_from_filename (also tested via induct)
# ---------------------------------------------------------------------------

def test_slug_from_filename_basic():
    assert slug_from_filename("My Document.pdf") == "my-document"


def test_slug_from_filename_underscores():
    assert slug_from_filename("some_file_name.txt") == "some-file-name"


def test_slug_from_filename_special_chars():
    result = slug_from_filename("data (v2).pdf")
    assert result == "data-v2"


# ---------------------------------------------------------------------------
# induct.py — infer_doc_type
# ---------------------------------------------------------------------------

def test_infer_doc_type_datasheets():
    p = Path("docs/reference/datasheets/chip.pdf")
    assert infer_doc_type(p) == "datasheet"


def test_infer_doc_type_unknown():
    p = Path("docs/misc/file.pdf")
    assert infer_doc_type(p) == "unknown"


def test_infer_doc_type_manuals():
    p = Path("project/manuals/guide.pdf")
    assert infer_doc_type(p) == "manual"


# ---------------------------------------------------------------------------
# induct.py — generate_sidecar_stub
# ---------------------------------------------------------------------------

def test_generate_sidecar_stub_fields(tmp_path):
    f = tmp_path / "docs" / "reference" / "chip.pdf"
    f.parent.mkdir(parents=True)
    f.touch()
    stub = generate_sidecar_stub(f, tmp_path, MINIMAL_CFG)
    assert stub["slug"] == "chip"
    assert stub["status"] == "pending"
    assert stub["collection"] == "test-proj_doc"
    assert stub["indexed_at"] is None
    assert stub["chunk_count"] is None
    assert "spec_summary" in stub


def test_generate_sidecar_stub_collection_from_cfg(tmp_path):
    cfg2 = {**MINIMAL_CFG, "project_name": "my-project"}
    f = tmp_path / "file.pdf"
    f.touch()
    stub = generate_sidecar_stub(f, tmp_path, cfg2)
    assert stub["collection"] == "my-project_doc"


# ---------------------------------------------------------------------------
# induct.py — sidecar_path()
# ---------------------------------------------------------------------------

def test_sidecar_path_mirrors_repo_structure(tmp_path):
    file_path = tmp_path / "docs" / "manuals" / "chip.pdf"
    result = sidecar_path(file_path, tmp_path)
    expected = tmp_path / ".carta" / "sidecars" / "docs" / "manuals" / "chip.embed-meta.yaml"
    assert result == expected


def test_sidecar_path_markdown(tmp_path):
    file_path = tmp_path / "docs" / "guide.md"
    result = sidecar_path(file_path, tmp_path)
    expected = tmp_path / ".carta" / "sidecars" / "docs" / "guide.embed-meta.yaml"
    assert result == expected


def test_sidecar_path_at_repo_root(tmp_path):
    file_path = tmp_path / "README.md"
    result = sidecar_path(file_path, tmp_path)
    expected = tmp_path / ".carta" / "sidecars" / "README.embed-meta.yaml"
    assert result == expected


# ---------------------------------------------------------------------------
# induct.py — write_sidecar / read_sidecar round-trip
# ---------------------------------------------------------------------------

def test_write_sidecar_creates_yaml(tmp_path):
    repo_root = tmp_path
    f = tmp_path / "docs" / "chip.pdf"
    f.parent.mkdir()
    f.touch()
    stub = {"slug": "chip", "status": "pending", "doc_type": "datasheet"}
    path = write_sidecar(f, stub, repo_root)
    assert path.exists()
    assert path == tmp_path / ".carta" / "sidecars" / "docs" / "chip.embed-meta.yaml"


def test_sidecar_round_trip(tmp_path):
    repo_root = tmp_path
    f = tmp_path / "docs" / "chip.pdf"
    f.parent.mkdir()
    f.touch()
    stub = {"slug": "chip", "status": "pending", "doc_type": "datasheet", "notes": ""}
    sc_path = write_sidecar(f, stub, repo_root)
    loaded = read_sidecar(sc_path)
    assert loaded == stub


def test_read_sidecar_returns_none_on_missing_file(tmp_path):
    assert read_sidecar(tmp_path / "nonexistent.embed-meta.yaml") is None


def test_read_sidecar_returns_none_on_invalid_yaml(tmp_path):
    bad = tmp_path / "bad.embed-meta.yaml"
    bad.write_text(": : : not valid yaml [[[")
    assert read_sidecar(bad) is None


# ---------------------------------------------------------------------------
# embed.py — get_embedding error path
# ---------------------------------------------------------------------------

@patch("carta.embed.embed.requests")
def test_get_embedding_raises_on_non_200(mock_requests):
    mock_resp = MagicMock()
    mock_resp.status_code = 500
    mock_resp.text = "Internal Server Error"
    mock_requests.post.return_value = mock_resp
    with pytest.raises(RuntimeError, match=r"Ollama embedding failed \(500\)"):
        get_embedding("test text")


@patch("carta.embed.embed.requests")
def test_get_embedding_sends_keep_alive(mock_requests):
    """Every Ollama embedding request carries keep_alive so the model stays resident
    across idle gaps instead of reloading."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"embedding": [0.0] * 768}
    mock_requests.post.return_value = mock_resp
    get_embedding("hello world")
    payload = mock_requests.post.call_args.kwargs["json"]
    assert "keep_alive" in payload


# ---------------------------------------------------------------------------
# embed.py — ensure_collection create vs skip
# ---------------------------------------------------------------------------

def test_ensure_collection_creates_when_missing():
    client = MagicMock()
    client.collection_exists.return_value = False
    ensure_collection(client, "proj_doc")
    client.create_collection.assert_called_once()
    assert client.create_collection.call_args.kwargs["collection_name"] == "proj_doc"


def test_ensure_collection_skips_when_exists():
    client = MagicMock()
    client.collection_exists.return_value = True
    ensure_collection(client, "proj_doc")
    client.create_collection.assert_not_called()


# ---------------------------------------------------------------------------
# parse.py — extract_pdf_text (real PDF via pymupdf)
# ---------------------------------------------------------------------------

def test_extract_pdf_text_basic(tmp_path):
    import fitz

    pdf_path = tmp_path / "test.pdf"
    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    page.insert_text((72, 72), "Normal body text here.", fontsize=12)
    page.insert_text((72, 120), "Section Heading", fontsize=16)
    doc.save(str(pdf_path))
    doc.close()

    pages = extract_pdf_text(pdf_path)
    assert len(pages) == 1
    assert pages[0]["page"] == 1
    assert "Normal body text" in pages[0]["text"]
    assert "Section Heading" in pages[0]["text"]
    assert "Section Heading" in pages[0]["headings"]
    assert "Normal body text here." not in pages[0]["headings"]


def test_extract_pdf_text_multi_page(tmp_path):
    import fitz

    pdf_path = tmp_path / "multi.pdf"
    doc = fitz.open()
    for i in range(3):
        page = doc.new_page(width=612, height=792)
        page.insert_text((72, 72), f"Page {i + 1} content", fontsize=12)
    doc.save(str(pdf_path))
    doc.close()

    pages = extract_pdf_text(pdf_path)
    assert len(pages) == 3
    for i, p in enumerate(pages):
        assert p["page"] == i + 1
        assert f"Page {i + 1} content" in p["text"]


# ---------------------------------------------------------------------------
# embed.py — _point_id_versioned determinism
# ---------------------------------------------------------------------------

def test_point_id_deterministic():
    a = _point_id_versioned("docs/my-slug.md", 0, 1)
    b = _point_id_versioned("docs/my-slug.md", 0, 1)
    assert a == b


def test_point_id_unique_per_chunk():
    a = _point_id_versioned("docs/my-slug.md", 0, 1)
    b = _point_id_versioned("docs/my-slug.md", 1, 1)
    assert a != b


def test_point_id_unique_per_key():
    a = _point_id_versioned("docs/a/slug.md", 0, 1)
    b = _point_id_versioned("docs/b/slug.md", 0, 1)
    assert a != b


# ---------------------------------------------------------------------------
# embed.py — upsert_chunks (mocked Qdrant + Ollama)
# ---------------------------------------------------------------------------

def _make_chunks(n: int = 2) -> list[dict]:
    return [
        {"slug": "test-doc", "chunk_index": i, "text": f"chunk text {i}", "doc_type": "reference"}
        for i in range(n)
    ]


@patch("carta.embed.embed.QdrantClient")
@patch("carta.embed.embed.get_embedding")
def test_upsert_chunks_calls_qdrant(mock_embed, mock_qdrant_cls):
    mock_embed.return_value = [0.1] * 768
    mock_client = MagicMock()
    mock_client.collection_exists.return_value = True
    mock_qdrant_cls.return_value = mock_client

    chunks = _make_chunks(3)
    count = upsert_chunks(chunks, MINIMAL_CFG)

    assert count == 3
    # With batching, 3 chunks < BATCH_SIZE=32 so 1 upsert call (remainder flush)
    assert mock_client.upsert.call_count == 1
    call_kwargs = mock_client.upsert.call_args
    assert call_kwargs.kwargs["collection_name"] == "test-proj_doc"


@patch("carta.embed.embed.QdrantClient")
@patch("carta.embed.embed.get_embedding")
def test_upsert_chunks_uses_cfg_collection(mock_embed, mock_qdrant_cls):
    mock_embed.return_value = [0.0] * 768
    mock_client = MagicMock()
    mock_client.collection_exists.return_value = True
    mock_qdrant_cls.return_value = mock_client

    cfg2 = {**MINIMAL_CFG, "project_name": "proj-b"}
    upsert_chunks(_make_chunks(1), cfg2)

    call_kwargs = mock_client.upsert.call_args
    assert call_kwargs.kwargs["collection_name"] == "proj-b_doc"


@patch("carta.embed.embed.QdrantClient")
@patch("carta.embed.embed.get_embedding")
def test_upsert_chunks_bad_chunk_does_not_kill_good_chunks(mock_embed, mock_qdrant_cls):
    """A chunk that fails embedding should not prevent other chunks from being upserted."""
    call_count = 0

    def embed_side_effect(text, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            raise RuntimeError("context length exceeded")
        return [0.1] * 768

    mock_embed.side_effect = embed_side_effect
    mock_client = MagicMock()
    mock_client.collection_exists.return_value = True
    mock_qdrant_cls.return_value = mock_client

    chunks = _make_chunks(3)
    count = upsert_chunks(chunks, MINIMAL_CFG)

    assert count == 2  # chunk 2 failed, chunks 1 and 3 succeeded
    # With batching: 2 successful chunks < BATCH_SIZE=32 -> 1 flush call
    assert mock_client.upsert.call_count == 1


@patch("carta.embed.embed.QdrantClient")
@patch("carta.embed.embed.get_embedding")
def test_upsert_chunks_empty_noop(mock_embed, mock_qdrant_cls):
    mock_client = MagicMock()
    mock_client.collection_exists.return_value = True
    mock_qdrant_cls.return_value = mock_client

    count = upsert_chunks([], MINIMAL_CFG)
    assert count == 0
    mock_client.upsert.assert_not_called()


@patch("carta.embed.embed.QdrantClient")
@patch("carta.embed.embed.get_embedding")
def test_upsert_chunks_uses_cfg_ollama_url(mock_embed, mock_qdrant_cls):
    mock_embed.return_value = [0.0] * 768
    mock_client = MagicMock()
    mock_client.collection_exists.return_value = True
    mock_qdrant_cls.return_value = mock_client

    cfg3 = {
        **MINIMAL_CFG,
        "embed": {**MINIMAL_CFG["embed"], "ollama_url": "http://custom-ollama:11434"},
    }
    upsert_chunks(_make_chunks(1), cfg3)

    _, kwargs = mock_embed.call_args
    assert kwargs.get("ollama_url") == "http://custom-ollama:11434"


@patch("carta.embed.embed.QdrantClient")
@patch("carta.embed.embed.get_embedding")
def test_upsert_chunks_serial_when_workers_is_1(mock_embed, mock_qdrant_cls):
    """embedding_workers=1 must use serial path and embed every chunk in order."""
    seen_order: list[int] = []

    def embed_side_effect(text, **kwargs):
        seen_order.append(int(text.split()[-1]))
        return [0.1] * 768

    mock_embed.side_effect = embed_side_effect
    mock_client = MagicMock()
    mock_client.collection_exists.return_value = True
    mock_qdrant_cls.return_value = mock_client

    cfg = {**MINIMAL_CFG, "embed": {**MINIMAL_CFG["embed"], "embedding_workers": 1}}
    count = upsert_chunks(_make_chunks(5), cfg)

    assert count == 5
    assert seen_order == [0, 1, 2, 3, 4]  # serial path preserves submit order


@patch("carta.embed.embed.QdrantClient")
@patch("carta.embed.embed.get_embedding")
def test_upsert_chunks_parallel_embeds_all_chunks(mock_embed, mock_qdrant_cls):
    """embedding_workers>1 must call get_embedding for every chunk (any order)."""
    mock_embed.return_value = [0.1] * 768
    mock_client = MagicMock()
    mock_client.collection_exists.return_value = True
    mock_qdrant_cls.return_value = mock_client

    cfg = {**MINIMAL_CFG, "embed": {**MINIMAL_CFG["embed"], "embedding_workers": 4}}
    count = upsert_chunks(_make_chunks(10), cfg)

    assert count == 10
    assert mock_embed.call_count == 10


# ---------------------------------------------------------------------------
# pipeline.py — is_lfs_pointer
# ---------------------------------------------------------------------------

def test_is_lfs_pointer_true(tmp_path):
    f = tmp_path / "file.pdf"
    f.write_bytes(b"version https://git-lfs.github.com/spec/v1\noid sha256:abc\nsize 1234\n")
    assert is_lfs_pointer(f) is True


def test_is_lfs_pointer_false(tmp_path):
    f = tmp_path / "file.pdf"
    f.write_bytes(b"%PDF-1.4 regular content")
    assert is_lfs_pointer(f) is False


def test_is_lfs_pointer_missing_file(tmp_path):
    assert is_lfs_pointer(tmp_path / "nonexistent.pdf") is False


# ---------------------------------------------------------------------------
# pipeline.py — discover_pending_files
# ---------------------------------------------------------------------------

def test_discover_pending_files(tmp_path):
    import yaml as _yaml

    doc_dir = tmp_path / "docs" / "reference"
    doc_dir.mkdir(parents=True)
    pdf = doc_dir / "manual.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    sc_dir = tmp_path / ".carta" / "sidecars" / "docs" / "reference"
    sc_dir.mkdir(parents=True)
    (sc_dir / "manual.embed-meta.yaml").write_text(
        _yaml.dump({
            "slug": "manual",
            "doc_type": "reference",
            "status": "pending",
            "current_path": "docs/reference/manual.pdf",
        })
    )

    pending = discover_pending_files(tmp_path)
    assert len(pending) == 1
    assert pending[0]["slug"] == "manual"
    assert pending[0]["file_path"] == pdf


def test_discover_pending_files_skips_embedded(tmp_path):
    import yaml as _yaml

    doc_dir = tmp_path / "docs"
    doc_dir.mkdir()
    pdf = doc_dir / "doc.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    sc_dir = tmp_path / ".carta" / "sidecars" / "docs"
    sc_dir.mkdir(parents=True)
    (sc_dir / "doc.embed-meta.yaml").write_text(
        _yaml.dump({
            "slug": "doc",
            "status": "embedded",
            "current_path": "docs/doc.pdf",
        })
    )

    pending = discover_pending_files(tmp_path)
    assert pending == []


# ---------------------------------------------------------------------------
# pipeline.py — run_embed (mocked Qdrant + Ollama)
# ---------------------------------------------------------------------------

@patch("carta.embed.pipeline.extract_pdf_text")
@patch("carta.embed.pipeline.upsert_chunks")
@patch("carta.embed.pipeline.QdrantClient")
def test_run_embed_returns_summary(mock_qdrant_cls, mock_upsert, mock_extract, tmp_path):
    import yaml as _yaml

    mock_client = MagicMock()
    mock_qdrant_cls.return_value = mock_client
    mock_client.collection_exists.return_value = True

    mock_extract.return_value = [{"page": 1, "text": "hello world", "headings": []}]
    mock_upsert.return_value = 1

    doc_dir = tmp_path / "docs"
    doc_dir.mkdir()
    pdf = doc_dir / "spec.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    sidecar = doc_dir / "spec.embed-meta.yaml"
    sidecar.write_text(_yaml.dump({"slug": "spec", "doc_type": "spec", "status": "pending"}))

    result = run_embed(tmp_path, MINIMAL_CFG)
    assert result["embedded"] == 1
    assert result["skipped"] == 0
    assert result["errors"] == []


@patch("carta.embed.pipeline.QdrantClient")
def test_run_embed_qdrant_unreachable(mock_qdrant_cls, tmp_path):
    mock_qdrant_cls.side_effect = Exception("connection refused")
    result = run_embed(tmp_path, MINIMAL_CFG)
    assert result["embedded"] == 0
    assert len(result["errors"]) == 1
    assert "Qdrant" in result["errors"][0]


# Auto-induct discovery must match supported extensions case-insensitively, so an
# uppercase `.PDF` is inducted like `.pdf` (the scanner already flags it via
# `f.suffix.lower()`, so case-sensitive discovery left those files perpetually
# "needs induction" but never embeddable without an explicit path).

def test_iter_inductable_files_matches_extensions_case_insensitively(tmp_path):
    from carta.embed.pipeline import _iter_inductable_files

    docs = tmp_path / "docs"
    (docs / "sub").mkdir(parents=True)
    (docs / "a.pdf").write_bytes(b"%PDF")
    (docs / "b.PDF").write_bytes(b"%PDF")
    (docs / "sub" / "c.md").write_text("# c")
    (docs / "sub" / "d.MD").write_text("# d")
    (docs / "e.txt").write_text("nope")  # unsupported extension

    found = {p.name for p in _iter_inductable_files(docs)}
    assert found == {"a.pdf", "b.PDF", "c.md", "d.MD"}


# ---------------------------------------------------------------------------
# parse.py — encoding robustness (audit Group C: CA-13, BOM)
# ---------------------------------------------------------------------------

def test_extract_markdown_text_handles_utf8_bom(tmp_path):
    """A UTF-8 BOM must not defeat frontmatter detection or leak YAML into the body."""
    from carta.embed.parse import extract_markdown_text
    md = tmp_path / "doc.md"
    md.write_bytes("﻿---\ntitle: Hello\n---\n\n# Body\n\nsome text".encode("utf-8"))
    sections, fm = extract_markdown_text(md)
    assert fm.get("title") == "Hello"
    body = " ".join(s["text"] for s in sections)
    assert "title: Hello" not in body  # frontmatter stripped, not embedded as body


def test_extract_markdown_text_tolerates_non_utf8_bytes(tmp_path):
    """A stray non-UTF8 byte must not crash extraction (else the file is silently
    dropped from the index with no bookkeeping)."""
    from carta.embed.parse import extract_markdown_text
    md = tmp_path / "doc.md"
    md.write_bytes(b"# Title\n\ncaf\xe9 not utf8 here\n")  # 0xe9 = latin-1 'e-acute'
    sections, _fm = extract_markdown_text(md)  # must not raise
    assert sections, "should still produce content rather than dropping the file"


# ---------------------------------------------------------------------------
# pipeline.py — honest embed success accounting (audit Group A: CA-1/4/6/7)
# ---------------------------------------------------------------------------

def _write_md_with_pending_sidecar(tmp_path, rel="docs/spec.md", slug="spec"):
    """Create a markdown source + a pending sidecar under .carta/sidecars/."""
    import yaml as _yaml
    src = tmp_path / rel
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text("hello")
    sc = tmp_path / ".carta" / "sidecars" / Path(rel).with_suffix(".embed-meta.yaml")
    sc.parent.mkdir(parents=True, exist_ok=True)
    sc.write_text(_yaml.dump({
        "slug": slug, "doc_type": "spec", "status": "pending", "current_path": rel,
    }))
    return src, sc


_NO_HEADER_CFG = {
    **MINIMAL_CFG,
    "embed": {
        **MINIMAL_CFG["embed"],
        "two_pass_visual": False,
        "chunking": {"max_tokens": 800, "overlap_fraction": 0.15, "contextual_header": False},
    },
}


@patch("carta.embed.pipeline.QdrantClient")
@patch("carta.embed.pipeline.upsert_chunks")
@patch("carta.embed.pipeline.chunk_text")
@patch("carta.embed.pipeline.extract_markdown_text")
def test_run_embed_total_persist_failure_not_marked_embedded(
    mock_extract, mock_chunk, mock_upsert, mock_qdrant_cls, tmp_path
):
    """A file whose chunks all fail to persist (upsert returns 0 while text was
    expected) must NOT be counted as embedded nor stamped status='embedded'."""
    import yaml as _yaml
    mock_qdrant_cls.return_value = MagicMock()
    mock_extract.return_value = ([{"page": 1, "text": "hello", "headings": []}], {})
    mock_chunk.return_value = [
        {"text": "chunk a", "chunk_index": 0},
        {"text": "chunk b", "chunk_index": 1},
    ]
    mock_upsert.return_value = 0  # nothing persisted despite 2 expected text chunks

    _src, sc = _write_md_with_pending_sidecar(tmp_path)
    result = run_embed(tmp_path, _NO_HEADER_CFG)

    assert result["embedded"] == 0, "0 persisted vectors must not count as embedded"
    assert "spec.md" in result["failed"]
    updated = _yaml.safe_load(sc.read_text())
    assert updated["status"] == "embed_failed"


@patch("carta.embed.pipeline.QdrantClient")
@patch("carta.embed.pipeline.upsert_chunks")
@patch("carta.embed.pipeline.chunk_text")
@patch("carta.embed.pipeline.extract_markdown_text")
def test_run_embed_partial_persist_not_marked_embedded(
    mock_extract, mock_chunk, mock_upsert, mock_qdrant_cls, tmp_path
):
    """A partial upsert (some batches lost) must be re-pickable, not 'embedded'."""
    import yaml as _yaml
    mock_qdrant_cls.return_value = MagicMock()
    mock_extract.return_value = ([{"page": 1, "text": "hello", "headings": []}], {})
    mock_chunk.return_value = [
        {"text": "chunk a", "chunk_index": 0},
        {"text": "chunk b", "chunk_index": 1},
    ]
    mock_upsert.return_value = 1  # only 1 of 2 expected text chunks persisted

    _src, sc = _write_md_with_pending_sidecar(tmp_path)
    result = run_embed(tmp_path, _NO_HEADER_CFG)

    assert result["embedded"] == 0
    assert "spec.md" in result["partial"]
    updated = _yaml.safe_load(sc.read_text())
    assert updated["status"] == "partial"


def test_discover_pending_files_repicks_failed_and_partial(tmp_path):
    """Re-pickable statuses (embed_failed, partial) must be re-discovered so a
    transient failure is retried on the next run."""
    import yaml as _yaml
    doc_dir = tmp_path / "docs"
    doc_dir.mkdir()
    (doc_dir / "a.md").write_text("a")
    (doc_dir / "b.md").write_text("b")
    sc_dir = tmp_path / ".carta" / "sidecars" / "docs"
    sc_dir.mkdir(parents=True)
    (sc_dir / "a.embed-meta.yaml").write_text(_yaml.dump(
        {"slug": "a", "status": "embed_failed", "current_path": "docs/a.md"}))
    (sc_dir / "b.embed-meta.yaml").write_text(_yaml.dump(
        {"slug": "b", "status": "partial", "current_path": "docs/b.md"}))

    pending = discover_pending_files(tmp_path)
    assert {p["slug"] for p in pending} == {"a", "b"}


def test_upsert_chunks_raises_on_embedding_dim_mismatch():
    """A model whose vectors aren't VECTOR_DIM must fail loudly, not be silently
    rejected batch-by-batch and reported as success."""
    from carta.embed.embed import EmbedDimError
    chunks = [{
        "slug": "d", "text": "hello", "chunk_index": 0,
        "file_path": "docs/d.md", "doc_type": "spec",
    }]
    client = MagicMock()
    client.collection_exists.return_value = True
    with patch("carta.embed.embed.get_embedding", return_value=[0.0] * 100):
        with pytest.raises(EmbedDimError):
            upsert_chunks(chunks, MINIMAL_CFG, client=client)


def test_upsert_chunks_retries_transient_batch_upsert_failure():
    """A transient batch-upsert error must be retried, not dropped on first failure
    (which would silently lose up to BATCH_SIZE already-embedded chunks)."""
    client = MagicMock()
    client.collection_exists.return_value = True
    calls = {"n": 0}

    def flaky_upsert(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient 503")
        return None

    client.upsert.side_effect = flaky_upsert
    chunks = [{
        "slug": "d", "text": "hello", "chunk_index": 0,
        "file_path": "docs/d.md", "doc_type": "spec",
    }]
    with patch("carta.embed.embed.get_embedding", return_value=[0.1] * 768):
        n = upsert_chunks(chunks, MINIMAL_CFG, client=client)
    assert n == 1, "retry should persist the batch after a transient failure"
    assert calls["n"] >= 2


# ---------------------------------------------------------------------------
# pipeline.py — run_search (mocked Qdrant + Ollama)
# ---------------------------------------------------------------------------

@patch("carta.embed.pipeline.find_config")
@patch("carta.embed.pipeline.get_embedding")
@patch("carta.embed.pipeline.QdrantClient")
def test_run_search_returns_hits(mock_qdrant_cls, mock_embed, mock_find_config):
    mock_find_config.return_value = Path("/mock/.carta/config.yaml")
    mock_embed.return_value = [0.1] * 768
    mock_client = MagicMock()
    mock_qdrant_cls.return_value = mock_client

    hit = MagicMock()
    hit.score = 0.92
    hit.payload = {"file_path": "docs/spec.pdf", "text": "relevant excerpt"}
    mock_response = MagicMock()
    mock_response.points = [hit]
    mock_client.query_points.return_value = mock_response

    results = run_search("what is the voltage rating", MINIMAL_CFG)
    # Search now returns results from all collections (doc, notes, session)
    assert len(results) == 3
    assert results[0]["score"] == pytest.approx(0.92)
    assert results[0]["source"] == "docs/spec.pdf"
    assert results[0]["excerpt"] == "relevant excerpt"


@patch("carta.embed.pipeline.find_config")
@patch("carta.embed.pipeline.get_embedding")
@patch("carta.embed.pipeline.QdrantClient")
def test_run_search_uses_cfg_collection(mock_qdrant_cls, mock_embed, mock_find_config):
    mock_find_config.return_value = Path("/mock/.carta/config.yaml")
    mock_embed.return_value = [0.0] * 768
    mock_client = MagicMock()
    mock_qdrant_cls.return_value = mock_client
    mock_response = MagicMock()
    mock_response.points = []
    mock_client.query_points.return_value = mock_response

    cfg2 = {**MINIMAL_CFG, "project_name": "proj-x"}
    run_search("query", cfg2)

    # Check that query_points was called for all 3 collections
    call_count = mock_client.query_points.call_count
    assert call_count == 3
    # Check that proj-x_doc is one of the queried collections
    queried_collections = [call.kwargs["collection_name"] for call in mock_client.query_points.call_args_list]
    assert "proj-x_doc" in queried_collections


@patch("carta.embed.pipeline.find_config")
@patch("carta.embed.pipeline.get_embedding")
@patch("carta.embed.pipeline.QdrantClient")
def test_run_search_query_failure_raises(mock_qdrant_cls, mock_embed, mock_find_config):
    """Individual collection failures are swallowed; connection failures raise."""
    mock_find_config.return_value = Path("/mock/.carta/config.yaml")
    mock_embed.return_value = [0.0] * 768
    mock_client = MagicMock()
    mock_qdrant_cls.return_value = mock_client
    # Individual collection query failures are caught and skipped
    mock_client.query_points.side_effect = Exception("collection not found")

    # All collections fail but function returns empty results (fail open)
    results = run_search("query", MINIMAL_CFG)
    assert results == []
    # Verify query_points was called (and failed) for all collections
    assert mock_client.query_points.call_count == 3


def test_run_embed_zero_timeout_means_unbounded_not_instant_timeout(tmp_path, monkeypatch):
    """file_timeout_s=0 must mean UNBOUNDED (matching visual_timeout_s '0 = unbounded'),
    not a literal join(0) that instantly flags every file TIMEOUT and embeds nothing
    while still exiting 0 (audit CA-3)."""
    import time as _time
    from carta.embed import pipeline as pipeline_mod

    _src, _sc = _write_md_with_pending_sidecar(tmp_path)

    def _slow_embed(*a, **kw):
        _time.sleep(0.2)  # outlives a join(0) so the bug would flag it timed-out
        return 1, {"status": "embedded", "chunk_count": 1}

    monkeypatch.setattr(pipeline_mod, "_embed_one_file", _slow_embed)
    cfg = {**_NO_HEADER_CFG, "embed": {**_NO_HEADER_CFG["embed"], "file_timeout_s": 0}}
    with patch("carta.embed.pipeline.QdrantClient", return_value=MagicMock()):
        result = run_embed(tmp_path, cfg)

    assert result["timed_out"] == [], "0 must not instantly time out every file"
    assert result["embedded"] == 1


# ---------------------------------------------------------------------------
# embed.py — upsert_chunks batching (PIPE-01)
# ---------------------------------------------------------------------------

@patch("carta.embed.embed.QdrantClient")
@patch("carta.embed.embed.get_embedding")
def test_upsert_chunks_batches_at_32(mock_embed, mock_qdrant_cls):
    """64 chunks should produce exactly 2 client.upsert() calls (32 + 32)."""
    mock_embed.return_value = [0.0] * 768
    mock_client = MagicMock()
    mock_client.collection_exists.return_value = True
    mock_qdrant_cls.return_value = mock_client

    chunks = _make_chunks(64)
    count = upsert_chunks(chunks, MINIMAL_CFG)

    assert count == 64
    assert mock_client.upsert.call_count == 2
    for call in mock_client.upsert.call_args_list:
        assert len(call.kwargs["points"]) <= 32


@patch("carta.embed.embed.QdrantClient")
@patch("carta.embed.embed.get_embedding")
def test_upsert_chunks_remainder_flushed(mock_embed, mock_qdrant_cls):
    """10 chunks < BATCH_SIZE -> exactly 1 upsert call with 10 points."""
    mock_embed.return_value = [0.0] * 768
    mock_client = MagicMock()
    mock_client.collection_exists.return_value = True
    mock_qdrant_cls.return_value = mock_client

    chunks = _make_chunks(10)
    count = upsert_chunks(chunks, MINIMAL_CFG)

    assert count == 10
    assert mock_client.upsert.call_count == 1
    assert len(mock_client.upsert.call_args.kwargs["points"]) == 10


@patch("carta.embed.embed.QdrantClient")
@patch("carta.embed.embed.get_embedding")
def test_upsert_chunks_skips_bad_embedding(mock_embed, mock_qdrant_cls):
    """Embedding failure on one chunk skips it; others still upserted."""
    call_count_tracker = {"n": 0}

    def embed_side_effect(text, **kwargs):
        call_count_tracker["n"] += 1
        if call_count_tracker["n"] == 5:  # fail on chunk index 4 (5th call)
            raise RuntimeError("embedding failed")
        return [0.0] * 768

    mock_embed.side_effect = embed_side_effect
    mock_client = MagicMock()
    mock_client.collection_exists.return_value = True
    mock_qdrant_cls.return_value = mock_client

    chunks = _make_chunks(10)
    count = upsert_chunks(chunks, MINIMAL_CFG)

    assert count == 9  # 1 chunk skipped


# ---------------------------------------------------------------------------
# parse.py — overlap cap (PIPE-03)
# ---------------------------------------------------------------------------

def test_chunk_text_overlap_cap_25_percent():
    """5000-word single paragraph with overlap_fraction=0.5 must terminate and return chunks."""
    big_text = " ".join(["word"] * 5000)
    pages = _make_pages(big_text)
    chunks = chunk_text(pages, max_tokens=800, overlap_fraction=0.5)
    assert chunks
    for c in chunks:
        assert c["text"].strip()


def test_chunk_text_safety_counter_lowered():
    """chunk_text completes on pathological input with lowered safety counter."""
    # Pathological: single paragraph 200 words, tiny max_tokens, high overlap
    pages = _make_pages(" ".join(["word"] * 200))
    # Should terminate quickly (not hit 10_000 iterations)
    chunks = chunk_text(pages, max_tokens=5, overlap_fraction=0.8)
    assert chunks
    assert len(chunks) < 500


# ---------------------------------------------------------------------------
# induct.py — sidecar current_path (PIPE-05)
# ---------------------------------------------------------------------------

def test_generate_sidecar_stub_includes_current_path(tmp_path):
    """generate_sidecar_stub must include 'current_path' with the relative path string."""
    f = tmp_path / "docs" / "ref" / "chip.pdf"
    f.parent.mkdir(parents=True)
    f.touch()
    stub = generate_sidecar_stub(f, tmp_path, MINIMAL_CFG)
    assert "current_path" in stub
    assert stub["current_path"] == "docs/ref/chip.pdf"


# ---------------------------------------------------------------------------
# pipeline.py — verbose suppression (PIPE-04)
# ---------------------------------------------------------------------------

@patch("carta.embed.pipeline.extract_pdf_text")
@patch("carta.embed.pipeline.upsert_chunks")
@patch("carta.embed.pipeline.QdrantClient")
def test_run_embed_verbose_false_no_stdout(mock_qdrant_cls, mock_upsert, mock_extract, tmp_path, capsys):
    """run_embed(verbose=False) must produce zero stdout output."""
    mock_client = MagicMock()
    mock_qdrant_cls.return_value = mock_client
    mock_client.collection_exists.return_value = True

    result = run_embed(tmp_path, MINIMAL_CFG, verbose=False)
    captured = capsys.readouterr()
    assert captured.out == ""


@patch("carta.embed.pipeline.extract_pdf_text")
@patch("carta.embed.pipeline.upsert_chunks")
@patch("carta.embed.pipeline.QdrantClient")
def test_run_embed_verbose_true_has_stdout(mock_qdrant_cls, mock_upsert, mock_extract, tmp_path, capsys):
    """run_embed(verbose=True) must produce stdout output."""
    import yaml as _yaml

    mock_client = MagicMock()
    mock_qdrant_cls.return_value = mock_client
    mock_client.collection_exists.return_value = True
    mock_extract.return_value = [{"page": 1, "text": "hello", "headings": []}]
    mock_upsert.return_value = 1

    doc_dir = tmp_path / "docs"
    doc_dir.mkdir()
    pdf = doc_dir / "spec.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    sidecar = doc_dir / "spec.embed-meta.yaml"
    sidecar.write_text(_yaml.dump({"slug": "spec", "doc_type": "spec", "status": "pending"}))

    run_embed(tmp_path, MINIMAL_CFG, verbose=True)
    captured = capsys.readouterr()
    assert captured.out != ""


# ---------------------------------------------------------------------------
# pipeline.py — per-file timeout (PIPE-02)
# ---------------------------------------------------------------------------

@patch("carta.embed.pipeline.QdrantClient")
def test_embed_one_file_timeout(mock_qdrant_cls, tmp_path, monkeypatch):
    """A file that exceeds FILE_TIMEOUT_S is skipped; pipeline continues."""
    import yaml as _yaml
    import carta.embed.pipeline as pipeline_mod

    mock_client = MagicMock()
    mock_qdrant_cls.return_value = mock_client
    mock_client.collection_exists.return_value = True

    # Monkeypatch FILE_TIMEOUT_S to 1 second
    monkeypatch.setattr(pipeline_mod, "FILE_TIMEOUT_S", 1)

    # Monkeypatch _embed_one_file to sleep longer than timeout
    import time as _time

    def _slow_embed(*args, **kwargs):
        _time.sleep(5)
        return (0, {})

    monkeypatch.setattr(pipeline_mod, "_embed_one_file", _slow_embed)

    doc_dir = tmp_path / "docs"
    doc_dir.mkdir()
    pdf = doc_dir / "slow.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    sidecar = doc_dir / "slow.embed-meta.yaml"
    sidecar.write_text(_yaml.dump({"slug": "slow", "doc_type": "spec", "status": "pending"}))

    result = run_embed(tmp_path, MINIMAL_CFG, verbose=False)
    assert result["skipped"] == 1
    assert result["timed_out"] == ["slow.pdf"]


@patch("carta.embed.pipeline.QdrantClient")
def test_run_embed_respects_cfg_file_timeout_s(mock_qdrant_cls, tmp_path, monkeypatch):
    """cfg['embed']['file_timeout_s'] overrides FILE_TIMEOUT_S — supports --timeout flag."""
    import yaml as _yaml
    import carta.embed.pipeline as pipeline_mod

    mock_client = MagicMock()
    mock_qdrant_cls.return_value = mock_client
    mock_client.collection_exists.return_value = True

    # Leave FILE_TIMEOUT_S at default (300s) but override via cfg to 1s
    import time as _time

    def _slow_embed(*args, **kwargs):
        _time.sleep(5)
        return (0, {})

    monkeypatch.setattr(pipeline_mod, "_embed_one_file", _slow_embed)

    doc_dir = tmp_path / "docs"
    doc_dir.mkdir()
    pdf = doc_dir / "slow.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    sidecar = doc_dir / "slow.embed-meta.yaml"
    sidecar.write_text(_yaml.dump({"slug": "slow", "doc_type": "spec", "status": "pending"}))

    cfg = {**MINIMAL_CFG, "embed": {**MINIMAL_CFG.get("embed", {}), "file_timeout_s": 1}}
    result = run_embed(tmp_path, cfg, verbose=False)
    assert result["timed_out"] == ["slow.pdf"]


# ---------------------------------------------------------------------------
# pipeline.py — sidecar heal pass (PIPE-05)
# ---------------------------------------------------------------------------

def test_heal_sidecar_current_paths(tmp_path):
    """Sidecars missing current_path are healed when the source PDF exists."""
    import yaml as _yaml

    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    sc_dir = tmp_path / ".carta" / "sidecars"
    sc_dir.mkdir(parents=True)
    sidecar = sc_dir / "doc.embed-meta.yaml"
    sidecar.write_text(_yaml.dump({"slug": "doc", "status": "embedded"}))

    healed = _heal_sidecar_current_paths(tmp_path)
    assert healed == 1

    data = _yaml.safe_load(sidecar.read_text())
    assert data["current_path"] == "doc.pdf"


def test_heal_sidecar_skips_missing_source(tmp_path):
    """Sidecars without a matching source file are skipped during heal."""
    import yaml as _yaml

    sc_dir = tmp_path / ".carta" / "sidecars"
    sc_dir.mkdir(parents=True)
    sidecar = sc_dir / "ghost.embed-meta.yaml"
    sidecar.write_text(_yaml.dump({"slug": "ghost", "status": "embedded"}))

    healed = _heal_sidecar_current_paths(tmp_path)
    assert healed == 0

    data = _yaml.safe_load(sidecar.read_text())
    assert "current_path" not in data


# ---------------------------------------------------------------------------
# parse.py — extract_markdown_text (03-01)
# ---------------------------------------------------------------------------

def test_markdown_extract_sections(tmp_path):
    """Two ## sections produce 2 dicts with correct page/text/headings shape."""
    from carta.embed.parse import extract_markdown_text

    md = tmp_path / "doc.md"
    md.write_text("## Section A\ntext a\n## Section B\ntext b\n", encoding="utf-8")
    sections, meta = extract_markdown_text(md)
    assert len(sections) == 2
    assert sections[0]["headings"] == ["## Section A"]
    assert "text a" in sections[0]["text"]
    assert sections[1]["headings"] == ["## Section B"]
    assert "text b" in sections[1]["text"]
    assert all("page" in s and "text" in s and "headings" in s for s in sections)


def test_markdown_strip_frontmatter(tmp_path):
    """YAML frontmatter is stripped from text; keys returned in meta dict."""
    from carta.embed.parse import extract_markdown_text

    md = tmp_path / "doc.md"
    md.write_text("---\ntitle: Test\n---\n## Body\ncontent\n", encoding="utf-8")
    sections, meta = extract_markdown_text(md)
    assert meta == {"title": "Test"}
    # frontmatter must not appear in text
    for s in sections:
        assert "title:" not in s["text"]
        assert "---" not in s["text"]


def test_markdown_empty_sections_skipped(tmp_path):
    """Sections with only whitespace are skipped."""
    from carta.embed.parse import extract_markdown_text

    md = tmp_path / "doc.md"
    # Middle section has no body text
    md.write_text("## A\ntext\n##\n## B\ntext\n", encoding="utf-8")
    sections, _ = extract_markdown_text(md)
    # Empty section (##\n) should be skipped
    texts = [s["text"].strip() for s in sections]
    assert all(t for t in texts)


# ---------------------------------------------------------------------------
# induct.py — file_type in sidecar (03-01)
# ---------------------------------------------------------------------------

def test_sidecar_file_type_markdown(tmp_path):
    """generate_sidecar_stub sets file_type='markdown' for .md files."""
    f = tmp_path / "note.md"
    f.touch()
    stub = generate_sidecar_stub(f, tmp_path, MINIMAL_CFG)
    assert stub["file_type"] == "markdown"


def test_sidecar_file_type_pdf(tmp_path):
    """generate_sidecar_stub sets file_type='pdf' for .pdf files."""
    f = tmp_path / "note.pdf"
    f.touch()
    stub = generate_sidecar_stub(f, tmp_path, MINIMAL_CFG)
    assert stub["file_type"] == "pdf"


# ---------------------------------------------------------------------------
# pipeline.py — _SUPPORTED_EXTENSIONS includes .md (03-01)
# ---------------------------------------------------------------------------

def test_supported_extensions_includes_md():
    """Pipeline must support .md files."""
    from carta.embed.pipeline import _SUPPORTED_EXTENSIONS
    assert ".md" in _SUPPORTED_EXTENSIONS


# ---------------------------------------------------------------------------
# embed.py — Visual collection support (Issue #1)
# ---------------------------------------------------------------------------

def test_ensure_visual_collection_creates_multi_vector_collection():
    """ensure_visual_collection creates a multi-vector collection for ColPali."""
    from carta.embed.embed import ensure_visual_collection, COLPALI_VECTOR_DIM

    mock_client = MagicMock()
    mock_client.collection_exists.return_value = False

    ensure_visual_collection(mock_client, "test_visual")

    mock_client.create_collection.assert_called_once()
    call_kwargs = mock_client.create_collection.call_args.kwargs
    assert call_kwargs["collection_name"] == "test_visual"
    # Should be a multi-vector config
    vectors_config = call_kwargs["vectors_config"]
    assert "colpali" in vectors_config
    assert vectors_config["colpali"].size == COLPALI_VECTOR_DIM


def test_ensure_visual_collection_skips_when_exists():
    """ensure_visual_collection skips creation if collection already exists."""
    from carta.embed.embed import ensure_visual_collection

    mock_client = MagicMock()
    mock_client.collection_exists.return_value = True

    ensure_visual_collection(mock_client, "test_visual")

    mock_client.create_collection.assert_not_called()


@patch("carta.embed.embed.QdrantClient")
def test_upsert_visual_pages(mock_qdrant_cls):
    """upsert_visual_pages stores multi-vector visual pages."""
    from carta.embed.embed import upsert_visual_pages
    import numpy as np

    mock_client = MagicMock()
    mock_client.collection_exists.return_value = True
    mock_qdrant_cls.return_value = mock_client

    # Mock visual page with 1024 patches x 128 dims
    pages = [
        {
            "slug": "datasheet",
            "file_path": "docs/datasheet.pdf",
            "page_num": 1,
            "vectors": np.random.randn(1024, 128).astype(np.float32),
            "png_path": ".carta/visual_cache/datasheet/page_0001.png",
            "doc_type": "visual_page",
            "extraction_model": "vidore/colqwen2-v1.0",
        }
    ]

    cfg = {
        **MINIMAL_CFG,
        "project_name": "test",
    }

    count = upsert_visual_pages(pages, cfg, client=mock_client)

    assert count == 1
    mock_client.upsert.assert_called_once()
    call_kwargs = mock_client.upsert.call_args.kwargs
    assert call_kwargs["collection_name"] == "test_visual"
    points = call_kwargs["points"]
    assert len(points) == 1
    # Check payload contains metadata
    assert points[0].payload["slug"] == "datasheet"
    assert points[0].payload["doc_type"] == "visual_page"


@patch("carta.embed.embed.QdrantClient")
def test_upsert_visual_pages_skips_bad_vectors(mock_qdrant_cls):
    """upsert_visual_pages skips pages with bad vector data."""
    from carta.embed.embed import upsert_visual_pages

    mock_client = MagicMock()
    mock_client.collection_exists.return_value = True
    mock_qdrant_cls.return_value = mock_client

    pages = [
        {
            "slug": "bad",
            "file_path": "docs/bad.pdf",
            "page_num": 1,
            "vectors": None,  # Invalid vectors
            "png_path": ".carta/visual_cache/bad/page_0001.png",
        }
    ]

    cfg = {
        **MINIMAL_CFG,
        "project_name": "test",
    }

    count = upsert_visual_pages(pages, cfg, client=mock_client)

    assert count == 0


def test_visual_point_id_deterministic():
    """_visual_point_id should produce deterministic UUIDs."""
    from carta.embed.embed import _visual_point_id

    a = _visual_point_id("docs/ref/datasheet.pdf", 42)
    b = _visual_point_id("docs/ref/datasheet.pdf", 42)
    assert a == b


def test_visual_point_id_unique_per_page():
    """_visual_point_id should be unique per page number."""
    from carta.embed.embed import _visual_point_id

    a = _visual_point_id("docs/ref/datasheet.pdf", 1)
    b = _visual_point_id("docs/ref/datasheet.pdf", 2)
    assert a != b


# ---------------------------------------------------------------------------
# pipeline.py — perf log helpers
# ---------------------------------------------------------------------------

import json as _json
import os as _os

from carta.embed.pipeline import (
    _resolve_perf_log_path,
    _build_perf_context,
    _summarize_vision_strategies,
    _write_perf_log_entry,
)


def test_resolve_perf_log_path_unset_returns_none(tmp_path, monkeypatch):
    monkeypatch.delenv("CARTA_PERF_LOG", raising=False)
    assert _resolve_perf_log_path(tmp_path) is None


def test_resolve_perf_log_path_relative_resolves_under_repo_root(tmp_path, monkeypatch):
    monkeypatch.setenv("CARTA_PERF_LOG", ".carta/perf.jsonl")
    resolved = _resolve_perf_log_path(tmp_path)
    assert resolved == tmp_path / ".carta" / "perf.jsonl"
    assert resolved.parent.is_dir()  # parent created


def test_resolve_perf_log_path_absolute_kept(tmp_path, monkeypatch):
    target = tmp_path / "custom" / "perf.jsonl"
    monkeypatch.setenv("CARTA_PERF_LOG", str(target))
    resolved = _resolve_perf_log_path(tmp_path)
    assert resolved == target
    assert resolved.parent.is_dir()


def test_summarize_vision_strategies_counts_by_model_used():
    events = [
        {"model_used": "glm-ocr"},
        {"model_used": "glm-ocr"},
        {"model_used": "llava"},
        {"model_used": "skip"},
        {"model_used": "skip"},
        {"model_used": "skip"},
    ]
    assert _summarize_vision_strategies(events) == {"glm-ocr": 2, "llava": 1, "skip": 3}


def test_summarize_vision_strategies_empty_returns_empty_dict():
    assert _summarize_vision_strategies([]) == {}
    assert _summarize_vision_strategies(None) == {}


def test_build_perf_context_picks_up_models_and_workers():
    cfg = {
        "embed": {
            "ollama_model": "nomic-embed-text:latest",
            "ollama_vision_model": "qwen3-vl:8b",
            "ocr_model": "glm-ocr:latest",
            "vision_workers": 4,
            "embedding_workers": 8,
        }
    }
    ctx = _build_perf_context(cfg)
    assert ctx["models"] == {
        "embedding": "nomic-embed-text:latest",
        "vision":    "qwen3-vl:8b",
        "ocr":       "glm-ocr:latest",
    }
    assert ctx["workers"] == {"vision": 4, "embedding": 8}
    assert "carta_version" in ctx


def test_write_perf_log_entry_appends_jsonl(tmp_path):
    path = tmp_path / "perf.jsonl"
    _write_perf_log_entry(path, {"file": "a.pdf", "status": "ok", "chunks": 12})
    _write_perf_log_entry(path, {"file": "b.pdf", "status": "skip", "chunks": 0})

    rows = [_json.loads(line) for line in path.read_text().splitlines()]
    assert len(rows) == 2
    assert rows[0]["file"] == "a.pdf" and rows[0]["status"] == "ok"
    assert rows[1]["file"] == "b.pdf" and rows[1]["status"] == "skip"
    # ts is auto-injected
    assert all("ts" in r for r in rows)


def test_write_perf_log_entry_none_path_is_noop(tmp_path):
    # Must not raise and must not create files
    _write_perf_log_entry(None, {"file": "x"})
    assert list(tmp_path.iterdir()) == []
