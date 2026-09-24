"""Offline retrieval-quality eval for Carta.

An eval set is a YAML file:

    queries:
      - q: "what baud rate is the serial bridge"
        expect: ["serial-bridge", "CLAUDE.md"]   # case-insensitive substrings of result file_path

A query "hits" if ANY expected substring is found in the file_path of a returned
result within the top-k. Metrics: recall@k (share of queries with >=1 hit) and
MRR (mean reciprocal rank of the first hit), plus recall at the shallower
cutoffs 1 and 3 — recall@k saturates long before ranking stops improving.

Two optional per-query fields:

      - q: "what is the maximum sense voltage of the newer current monitor"
        expect: ["current-monitor-rev-b"]
        reject: ["current-monitor-rev-a"]   # plausible-but-wrong docs (a sibling part, a stale plan)
        tags: [pdf-deep, hard-negative] # free-form labels; metrics are broken down per tag

A reject VIOLATION is a reject doc ranked above the first expect hit within the
top-k (or present in the top-k when no expect doc is). A path that matches an
expect substring is never counted as a reject.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import yaml


@dataclass
class EvalQuery:
    q: str
    expect: list[str]
    reject: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)


def _clean(values) -> list[str]:
    """Stringify and drop blanks — a blank substring matches every path.

    YAML's scalar form (`reject: foo`) is one entry: iterating a bare str would split it into
    characters, and a one-letter substring matches nearly every path.
    """
    if isinstance(values, (str, int, float)):
        values = [values]
    return [s for v in (values or []) if (s := str(v).strip())]


def load_eval_set(path: Path) -> list[EvalQuery]:
    data = yaml.safe_load(Path(path).read_text()) or {}
    out: list[EvalQuery] = []
    for i, row in enumerate(data.get("queries", [])):
        # Drop blank expectations: an empty string is a substring of every file_path
        # (guaranteed HIT) and an empty list is a guaranteed MISS — both silently
        # corrupt recall/MRR (audit CA-26). A query with no usable expectation can
        # never be scored meaningfully, so fail loudly rather than fake a number.
        expect = _clean(row.get("expect"))
        if not expect:
            raise ValueError(
                f"eval query #{i + 1} ({row.get('q', '?')!r}) has no usable 'expect' "
                f"entries after dropping blanks — it can never be scored meaningfully."
            )
        q = str(row.get("q") or "").strip()
        if not q:
            raise ValueError(f"eval query #{i + 1} has no 'q' text")
        out.append(EvalQuery(q=q, expect=expect,
                             reject=_clean(row.get("reject")), tags=_clean(row.get("tags"))))
    return out


def _first_hit_rank(expect: list[str], file_paths: list[str], k: int) -> Optional[int]:
    needles = [e.lower() for e in expect]
    for rank, fp in enumerate(file_paths[:k], start=1):
        hay = (fp or "").lower()
        if any(n in hay for n in needles):
            return rank
    return None


def _first_reject_rank(eq: EvalQuery, file_paths: list[str], k: int) -> Optional[int]:
    """Rank of the first reject doc in the top-k, skipping any path that matches expect."""
    if not eq.reject:
        return None
    rejects = [r.lower() for r in eq.reject]
    for rank, fp in enumerate(file_paths[:k], start=1):
        if _first_hit_rank(eq.expect, [fp], 1) is not None:
            continue
        if any(r in (fp or "").lower() for r in rejects):
            return rank
    return None


def _summarise(ranks: list[Optional[int]]) -> tuple[float, float]:
    """(recall, MRR) over a list of first-hit ranks (None = miss)."""
    n = len(ranks)
    if not n:
        return 0.0, 0.0
    return (sum(r is not None for r in ranks) / n,
            sum(1.0 / r for r in ranks if r is not None) / n)


def compute_metrics(eval_queries: list[EvalQuery],
                    results_per_query: list[list[str]],
                    k: int) -> dict:
    if len(eval_queries) != len(results_per_query):
        raise ValueError(
            f"eval_queries ({len(eval_queries)}) and results_per_query "
            f"({len(results_per_query)}) must have the same length"
        )
    per_query = []
    for eq, results in zip(eval_queries, results_per_query):
        rank = _first_hit_rank(eq.expect, results, k)
        reject_rank = _first_reject_rank(eq, results, k)
        violation = reject_rank is not None and (rank is None or reject_rank < rank)
        per_query.append({"q": eq.q, "first_hit_rank": rank,
                          "first_reject_rank": reject_rank, "reject_violation": violation,
                          "tags": list(eq.tags)})
    ranks = [row["first_hit_rank"] for row in per_query]
    recall, mrr = _summarise(ranks)
    by_tag: dict[str, dict] = {}
    for tag in sorted({t for eq in eval_queries for t in eq.tags}):
        tag_ranks = [row["first_hit_rank"] for row in per_query if tag in row["tags"]]
        t_recall, t_mrr = _summarise(tag_ranks)
        by_tag[tag] = {"n": len(tag_ranks), "recall_at_k": t_recall, "mrr": t_mrr}
    n = len(eval_queries)
    return {
        "n_queries": n,
        "k": k,
        "recall_at_k": recall,
        "recall_at": {c: _summarise([r if r is not None and r <= c else None for r in ranks])[0]
                      for c in sorted({1, 3, k}) if c <= k},
        "mrr": mrr,
        "n_with_reject": sum(bool(eq.reject) for eq in eval_queries),
        "reject_violations": sum(row["reject_violation"] for row in per_query),
        "by_tag": by_tag,
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
