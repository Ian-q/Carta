"""Local BM25 sparse encoder via fastembed (ONNX, CPU, no API key).

Model is loaded lazily and cached. Document and query share the model; Qdrant
applies the IDF modifier server-side (set on the collection), so we ship raw
term frequencies here.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

DEFAULT_BM25_MODEL = "Qdrant/bm25"


@dataclass(frozen=True)
class SparseVec:
    indices: list[int]
    values: list[float]


@lru_cache(maxsize=4)
def _model(model_name: str):
    from fastembed import SparseTextEmbedding
    return SparseTextEmbedding(model_name=model_name)


def _to_sparsevec(emb) -> SparseVec:
    return SparseVec(indices=[int(i) for i in emb.indices],
                     values=[float(v) for v in emb.values])


def embed_sparse_document(text: str, model_name: str = DEFAULT_BM25_MODEL) -> SparseVec:
    return _to_sparsevec(next(iter(_model(model_name).embed([text]))))


def embed_sparse_query(text: str, model_name: str = DEFAULT_BM25_MODEL) -> SparseVec:
    return _to_sparsevec(next(iter(_model(model_name).query_embed([text]))))
