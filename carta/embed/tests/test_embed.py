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
)
from carta.embed.embed import _point_id, ensure_collection, get_embedding, upsert_chunks
from carta.embed.parse import extract_pdf_text
from carta.embed.pipeline import (
    discover_pending_files,
    is_lfs_pointer,
    run_embed,
    run_search,
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
# induct.py — write_sidecar / read_sidecar round-trip
# ---------------------------------------------------------------------------

def test_write_sidecar_creates_yaml(tmp_path):
    f = tmp_path / "chip.pdf"
    f.touch()
    stub = {"slug": "chip", "status": "pending", "doc_type": "datasheet"}
    path = write_sidecar(f, stub)
    assert path.exists()
    assert path.name == "chip.embed-meta.yaml"


def test_sidecar_round_trip(tmp_path):
    f = tmp_path / "chip.pdf"
    f.touch()
    stub = {"slug": "chip", "status": "pending", "doc_type": "datasheet", "notes": ""}
    sidecar_path = write_sidecar(f, stub)
    loaded = read_sidecar(sidecar_path)
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
# embed.py — _point_id determinism
# ---------------------------------------------------------------------------

def test_point_id_deterministic():
    a = _point_id("my-slug", 0)
    b = _point_id("my-slug", 0)
    assert a == b


def test_point_id_unique_per_chunk():
    a = _point_id("my-slug", 0)
    b = _point_id("my-slug", 1)
    assert a != b


def test_point_id_unique_per_slug():
    a = _point_id("slug-a", 0)
    b = _point_id("slug-b", 0)
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
    assert mock_client.upsert.call_count == 3
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
    assert mock_client.upsert.call_count == 2


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
    sidecar = doc_dir / "manual.embed-meta.yaml"
    sidecar.write_text(_yaml.dump({
        "slug": "manual",
        "doc_type": "reference",
        "status": "pending",
    }))

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
    sidecar = doc_dir / "doc.embed-meta.yaml"
    sidecar.write_text(_yaml.dump({"slug": "doc", "status": "embedded"}))

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


# ---------------------------------------------------------------------------
# pipeline.py — run_search (mocked Qdrant + Ollama)
# ---------------------------------------------------------------------------

@patch("carta.embed.pipeline.get_embedding")
@patch("carta.embed.pipeline.QdrantClient")
def test_run_search_returns_hits(mock_qdrant_cls, mock_embed):
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
    assert len(results) == 1
    assert results[0]["score"] == 0.92
    assert results[0]["source"] == "docs/spec.pdf"
    assert results[0]["excerpt"] == "relevant excerpt"


@patch("carta.embed.pipeline.get_embedding")
@patch("carta.embed.pipeline.QdrantClient")
def test_run_search_uses_cfg_collection(mock_qdrant_cls, mock_embed):
    mock_embed.return_value = [0.0] * 768
    mock_client = MagicMock()
    mock_qdrant_cls.return_value = mock_client
    mock_response = MagicMock()
    mock_response.points = []
    mock_client.query_points.return_value = mock_response

    cfg2 = {**MINIMAL_CFG, "project_name": "proj-x"}
    run_search("query", cfg2)

    call_kwargs = mock_client.query_points.call_args.kwargs
    assert call_kwargs["collection_name"] == "proj-x_doc"


@patch("carta.embed.pipeline.get_embedding")
@patch("carta.embed.pipeline.QdrantClient")
def test_run_search_query_failure_raises(mock_qdrant_cls, mock_embed):
    mock_embed.return_value = [0.0] * 768
    mock_client = MagicMock()
    mock_qdrant_cls.return_value = mock_client
    mock_client.query_points.side_effect = Exception("collection not found")

    with pytest.raises(RuntimeError, match="Qdrant search failed"):
        run_search("query", MINIMAL_CFG)
