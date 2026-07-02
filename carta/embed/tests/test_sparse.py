import pytest

# These tests exercise the real fastembed BM25 model, which ships in the optional
# `[hybrid]` extra. Skip cleanly when it is not installed (e.g. base CI) rather
# than failing — mirrors the optional-dependency handling in test_colpali.py.
pytest.importorskip("fastembed", reason="hybrid extra (fastembed) not installed")

from carta.embed.sparse import embed_sparse_document, embed_sparse_query, SparseVec


def test_sparse_document_returns_aligned_indices_and_values():
    sv = embed_sparse_document("the serial bridge runs at 921600 baud")
    assert isinstance(sv, SparseVec)
    assert len(sv.indices) == len(sv.values)
    assert len(sv.indices) > 0
    assert all(v >= 0 for v in sv.values)


def test_sparse_query_shape_matches():
    sv = embed_sparse_query("serial bridge baud rate")
    assert isinstance(sv, SparseVec)
    assert len(sv.indices) == len(sv.values)


def test_underscore_identifier_query_shares_tokens_with_document():
    """Spreadsheet retrieval leans on BM25 matching identifiers like
    TMS_CoolantTemp verbatim. Pin that the fastembed Qdrant/bm25 tokenizer
    gives a whole-identifier query and the document overlapping token
    indices. (Fragment queries like "TMS" do NOT match — the tokenizer
    hashes identifiers whole rather than splitting on underscores; see
    docs/field-notes.md.)"""
    pytest.importorskip("fastembed")
    from carta.embed.sparse import embed_sparse_document, embed_sparse_query
    doc = embed_sparse_document(
        "Signal: BMS_PackVoltage, BMS_PackCurrent, TMS_CoolantTemp, TMS_FlowRate")
    qv = embed_sparse_query("TMS_CoolantTemp")
    overlap = set(qv.indices) & set(doc.indices)
    assert overlap, "query 'TMS_CoolantTemp' shares no BM25 tokens with the document"
