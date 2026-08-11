"""Retrieval tracing: what each stage did with each result.

A retrieval miss can happen at five stages (never retrieved, one lane only,
demoted by fusion, collapsed by dedup, dropped by the visual cap) and they are
indistinguishable from the outside. This records which one.

Two consumers: the recall hook appends JSONL for gate calibration; `carta
search --trace` prints per-stage ranks for error analysis.
"""
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _trace_path(repo_root: Path, when: Optional[str] = None) -> Path:
    stamp = (when or _utc_now_iso())[:7]          # YYYY-MM
    return repo_root / ".carta" / "traces" / f"hook-{stamp}.jsonl"


def build_trace_record(*, query: str, collections: list, hits: list, zone: str,
                       judge, latency_ms: int, score_kind: str,
                       rrf_k: Optional[int]) -> dict:
    """Build one trace record from a completed search.

    `query` is the DERIVED query (post `_extract_query`), never the raw prompt.
    `zone` is one of "silent" | "judge" | "inject".
    """
    top = hits[0] if hits else None
    return {
        "ts": _utc_now_iso(),
        "query": query,
        "collections": list(collections),
        "n_hits": len(hits),
        "top_source": top.get("source") if top else None,
        "lanes": top.get("lane_ranks") if top else None,
        "score": top.get("score") if top else None,
        "fused_score": top.get("fused_score") if top else None,
        "score_kind": score_kind,
        "rrf_k": rrf_k,
        "zone": zone,
        "judge": judge,
        "latency_ms": latency_ms,
    }


def append_trace(repo_root: Path, record: dict) -> None:
    """Append one JSONL record. Never raises — tracing must not break search."""
    try:
        path = _trace_path(repo_root)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass
