"""Offline retrieval-quality eval for Carta.

An eval set is a YAML file:

    queries:
      - q: "what baud rate is the serial bridge"
        expect: ["serial-bridge", "CLAUDE.md"]   # case-insensitive substrings of result file_path

A query "hits" if ANY expected substring is found in the file_path of a returned
result within the top-k. Metrics: recall@k (share of queries with >=1 hit) and
MRR (mean reciprocal rank of the first hit).
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import yaml


@dataclass(frozen=True)
class EvalQuery:
    q: str
    expect: list[str]


def load_eval_set(path: Path) -> list[EvalQuery]:
    data = yaml.safe_load(Path(path).read_text()) or {}
    out: list[EvalQuery] = []
    for row in data.get("queries", []):
        out.append(EvalQuery(q=str(row["q"]), expect=[str(e) for e in row.get("expect", [])]))
    return out


def _first_hit_rank(expect: list[str], file_paths: list[str], k: int) -> Optional[int]:
    needles = [e.lower() for e in expect]
    for rank, fp in enumerate(file_paths[:k], start=1):
        hay = (fp or "").lower()
        if any(n in hay for n in needles):
            return rank
    return None


def compute_metrics(eval_queries: list[EvalQuery],
                    results_per_query: list[list[str]],
                    k: int) -> dict:
    per_query = []
    recall_hits = 0
    rr_sum = 0.0
    for eq, results in zip(eval_queries, results_per_query):
        rank = _first_hit_rank(eq.expect, results, k)
        if rank is not None:
            recall_hits += 1
            rr_sum += 1.0 / rank
        per_query.append({"q": eq.q, "first_hit_rank": rank})
    n = len(eval_queries)
    return {
        "n_queries": n,
        "k": k,
        "recall_at_k": (recall_hits / n) if n else 0.0,
        "mrr": (rr_sum / n) if n else 0.0,
        "per_query": per_query,
    }


def run_eval(eval_path: Path,
             search_fn: Callable[[str, int], list[dict]],
             k: int = 5) -> dict:
    """Run an eval set through `search_fn(query, k) -> [{file_path, score, ...}]`."""
    eval_queries = load_eval_set(eval_path)
    results_per_query: list[list[str]] = []
    for eq in eval_queries:
        hits = search_fn(eq.q, k) or []
        results_per_query.append([h.get("file_path", "") for h in hits])
    return compute_metrics(eval_queries, results_per_query, k)
