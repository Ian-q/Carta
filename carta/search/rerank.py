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
    """Score, sort, and truncate *hits* by cross-encoder relevance to *query*.

    Mutates each dict in *hits* by stamping a ``rerank_score`` key, then sorts
    the list in-place (highest score first) and returns the leading *top_n*
    entries.  The caller is responsible for stripping ``rerank_score`` if a
    stable output shape is required.  ``_model`` is lazy-loaded and cached; it
    may raise (e.g. ``ImportError`` or a download error) on the first call if
    the fastembed model cannot be loaded.
    """
    if not hits:
        return hits
    texts = [h.get("text", "") for h in hits]
    scores = _scores(query, texts, model_name)
    for h, s in zip(hits, scores):
        h["rerank_score"] = s
    hits.sort(key=lambda h: h["rerank_score"], reverse=True)
    return hits[:top_n]


def rerank_dispatch(query: str, hits: list[dict], *, rr_cfg: dict, ollama_url: str,
                    top_n: int) -> list[dict]:
    """Route reranking to the configured backend. Defaults to cross-encoder.

    rr_cfg is cfg["search"]["rerank"]. backend="llm" uses the listwise Ollama
    reranker; anything else (incl. unset) uses the fastembed cross-encoder.
    """
    if rr_cfg.get("backend", "cross-encoder") == "llm":
        from carta.search.llm_rerank import llm_rerank_hits
        return llm_rerank_hits(
            query, hits,
            model=rr_cfg.get("llm_model", "qwen3.5:0.8b"),
            ollama_url=ollama_url,
            top_n=top_n,
            timeout_s=rr_cfg.get("llm_timeout_s", 20),
        )
    return rerank_hits(query, hits, model_name=rr_cfg.get("model", DEFAULT_RERANK_MODEL), top_n=top_n)
