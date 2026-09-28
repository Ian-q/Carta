"""Local cross-encoder relevance scoring via fastembed TextCrossEncoder.

Lazy-loaded, cached. Used twice: to rerank fused search candidates (`rerank_hits`),
and by the proactive-recall hook's judge (`score_pairs`), which asks the same
question — does this passage answer this query — on a prompt-blocking path.

No API key, CPU/ONNX. Models are cached under ~/.carta/models/fastembed, NOT in
fastembed's default location ($TMPDIR/fastembed_cache): the OS reaps temp files, and
a reaped cache means re-downloading ~80MB inside a hook that blocks prompt submission.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

DEFAULT_RERANK_MODEL = "BAAI/bge-reranker-base"

# Indirection so tests can substitute the class without touching fastembed's import.
_TextCrossEncoder = None


def model_cache_dir() -> Path:
    """Durable cache for fastembed models (machine-level, like ~/.carta/traces)."""
    return Path.home() / ".carta" / "models" / "fastembed"


@lru_cache(maxsize=2)
def _model(model_name: str):
    global _TextCrossEncoder
    if _TextCrossEncoder is None:
        from fastembed.rerank.cross_encoder import TextCrossEncoder
        _TextCrossEncoder = TextCrossEncoder
    cache = model_cache_dir()
    cache.mkdir(parents=True, exist_ok=True)
    return _TextCrossEncoder(model_name=model_name, cache_dir=str(cache))


def _scores(query: str, texts: list[str], model_name: str) -> list[float]:
    return [float(s) for s in _model(model_name).rerank(query, texts)]


def score_pairs(query: str, texts: list[str], model_name: str) -> list[float]:
    """Cross-encoder relevance score for each text against *query* (higher is better).

    Raw model logits, unbounded and model-specific: compare them to each other or to a
    threshold calibrated for that model, never across models.
    """
    return _scores(query, texts, model_name)


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
