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
