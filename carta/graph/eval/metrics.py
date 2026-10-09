"""Ranking metrics for the replay eval, and a seeded bootstrap.

Everything is a pure function of its arguments. The bootstrap draws from
``random.Random(seed)``, so a report is reproducible to the last digit.
"""
from __future__ import annotations

import math
import random
from typing import Collection, Dict, List, Sequence, Tuple

RESAMPLES = 2000


def score_ranking(ranked: Sequence[str], gold: Collection[str], ks: Sequence[int]) -> dict:
    """Score one ranked list against one non-empty gold set.

    ``recall@k = |R[:k] & G| / |G|``; ``precision@k`` divides by ``k``, not by
    how many were returned; ``rr`` is ``1 / rank`` of the first gold doc
    within the top ``max(ks)``, else 0. ``returned`` is capped at ``max(ks)``.
    A ranking that repeats a doc is a bug in the method and raises.
    """
    gold_set = set(gold)
    if not gold_set:
        raise ValueError("gold must not be empty")
    if len(set(ranked)) != len(ranked):
        raise ValueError("a ranking must not repeat a doc")
    top = max(ks)
    head = list(ranked[:top])
    hits = [doc in gold_set for doc in head]
    first = next((i + 1 for i, hit in enumerate(hits) if hit), None)
    recall, precision, any_hit = {}, {}, {}
    for k in ks:
        found = sum(hits[:k])
        recall[k] = found / len(gold_set)
        precision[k] = found / k
        any_hit[k] = 1 if found else 0
    return {"recall_at": recall, "precision_at": precision, "any_hit_at": any_hit,
            "rr": 1.0 / first if first else 0.0, "first_rank": first, "returned": len(head)}


def _mean(values: Sequence[float]) -> float:
    return math.fsum(values) / len(values) if values else 0.0


def aggregate(rows: Sequence[dict], ks: Sequence[int]) -> dict:
    """Macro-average ``score_ranking`` rows. An empty population gives zeros and ``n == 0``."""
    out: Dict[str, object] = {"n": len(rows)}
    for name in ("recall_at", "precision_at", "any_hit_at"):
        out[name] = {str(k): _mean([row[name][k] for row in rows]) for k in ks}
    out["mrr"] = _mean([row["rr"] for row in rows])
    out["mean_returned"] = _mean([row["returned"] for row in rows])
    return out


def _percentiles(means: List[float]) -> Tuple[float, float]:
    means.sort()
    n = len(means)
    return means[int(0.025 * n)], means[min(n - 1, int(math.ceil(0.975 * n)) - 1)]


_SHARED_LIMIT = 4_000_000        # index entries kept in memory for reuse (about 32 MB)
_shared: Dict[Tuple[int, int, int], List[List[int]]] = {}


def _resamples(n: int, resamples: int, seed: int):
    """``resamples`` index samples of size ``n`` from ``random.Random(seed)``.

    Every series of one report has the same length and seed, so they all use
    the same samples; drawing them once makes a report several times faster
    and changes no number. Large populations are streamed instead of kept.
    """
    if n * resamples > _SHARED_LIMIT:
        rng = random.Random(seed)
        population = range(n)
        return (rng.choices(population, k=n) for _ in range(resamples))
    key = (n, resamples, seed)
    hit = _shared.get(key)
    if hit is None:
        rng = random.Random(seed)
        population = list(range(n))
        hit = [rng.choices(population, k=n) for _ in range(resamples)]
        _shared.clear()                 # one population size at a time is all a report needs
        _shared[key] = hit
    return hit


def bootstrap_ci(values: Sequence[float], *, resamples: int = RESAMPLES,
                 seed: int = 0) -> Tuple[float, float]:
    """A 95% percentile interval on the mean of ``values``. ``(0, 0)`` when empty."""
    values = list(values)
    n = len(values)
    if not n:
        return (0.0, 0.0)
    pick = values.__getitem__
    means = [math.fsum(map(pick, sample)) / n for sample in _resamples(n, resamples, seed)]
    return _percentiles(means)


def paired_bootstrap(a: Sequence[float], b: Sequence[float], *, resamples: int = RESAMPLES,
                     seed: int = 0) -> dict:
    """Mean of ``a - b`` over the same items, with a 95% interval and win counts.

    The items are resampled as pairs, so the interval reflects how much the
    two methods differ on the same commits, not how much each varies alone.
    """
    if len(a) != len(b):
        raise ValueError("paired samples must have equal lengths")
    diffs = [x - y for x, y in zip(a, b)]
    lo, hi = bootstrap_ci(diffs, resamples=resamples, seed=seed)
    return {
        "delta": _mean(diffs), "ci95": [lo, hi],
        "better": sum(1 for d in diffs if d > 0),
        "worse": sum(1 for d in diffs if d < 0),
        "tied": sum(1 for d in diffs if d == 0),
    }
