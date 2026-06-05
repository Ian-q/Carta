"""Local second-stage cross-encoder reranker via fastembed TextCrossEncoder.

Lazy-loaded, cached. Reorders fused candidates by query-chunk relevance and
truncates to top_n. No API key, CPU/ONNX.
"""
from __future__ import annotations

from functools import lru_cache

DEFAULT_RERANK_MODEL = "BAAI/bge-reranker-base"


@lru_cache(maxsize=2)
def _model(model_name: str):
    from fastembed.rerank.cross_encoder import TextCrossEncoder
    return TextCrossEncoder(model_name=model_name)


def _scores(query: str, texts: list[str], model_name: str) -> list[float]:
    return [float(s) for s in _model(model_name).rerank(query, texts)]


def rerank_hits(query: str, hits: list[dict], model_name: str, top_n: int) -> list[dict]:
    if not hits:
        return hits
    texts = [h.get("text", "") for h in hits]
    scores = _scores(query, texts, model_name)
    for h, s in zip(hits, scores):
        h["rerank_score"] = s
    hits.sort(key=lambda h: h["rerank_score"], reverse=True)
    return hits[:top_n]
