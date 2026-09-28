"""Calibrate the proactive-recall judge against a labelled corpus (issue #118).

The hook's gray-zone judge answers "is any of this relevant to the prompt?", and its
threshold is a number that has to come from data. This builds the labelled set, scores it
with the configured judge model, and reports where to put the threshold.

Labels come from a retrieval eval set (`carta eval` format) over the SAME corpus, using
the indexed chunk text as ground truth:

  positive     the chunk holding the query's verified `evidence.quote`
  easy negative a random chunk from an unrelated area of the corpus
  hard negative the best lexical match inside a verified `reject:` doc — a plausible
                but wrong answer, which is what the judge most needs to turn down

Usage (from any Carta project; needs the corpus embedded):

    python -m carta.hook.eval.calibrate chunks   <qdrant_url> <collection> chunks.jsonl
    python -m carta.hook.eval.calibrate judge    <eval.yaml> chunks.jsonl [--model M]

`judge` prints, per excerpt length, AUC against each negative kind and two candidate
thresholds: Youden's J (best overall separation) and the lowest threshold whose random-
chunk false-positive rate stays <= 5% (the operating point Carta ships, because injecting
unrelated documentation is the specific cost the gate exists to avoid).

Scores are raw cross-encoder logits — model-specific and not probabilities. Re-run this
whenever `proactive_recall.judge_model` changes.
"""
from __future__ import annotations

import json
import random
import re
import statistics
import sys
import time
from pathlib import Path

EXCERPT_VIEWS = {"head200": 200, "head400": 400}   # what the hook can actually pass


# ---------------------------------------------------------------------------
# Corpus dump
# ---------------------------------------------------------------------------

def dump_chunks(qdrant_url: str, collection: str, out_path: Path) -> int:
    """Write every live chunk of *collection* to JSONL: {file_path, text, live}."""
    import requests

    n, offset = 0, None
    with open(out_path, "w", encoding="utf-8") as f:
        while True:
            body: dict = {"limit": 1000, "with_payload": True, "with_vector": False}
            if offset is not None:
                body["offset"] = offset
            r = requests.post(f"{qdrant_url}/collections/{collection}/points/scroll",
                              json=body, timeout=60)
            r.raise_for_status()
            result = r.json()["result"]
            for p in result["points"]:
                pl = p.get("payload") or {}
                f.write(json.dumps({
                    "file_path": pl.get("file_path"),
                    "text": pl.get("text", ""),
                    "live": not (pl.get("stale_as_of") or pl.get("superseded_at")
                                 or pl.get("orphaned_at")),
                }) + "\n")
                n += 1
            offset = result.get("next_page_offset")
            if offset is None:
                return n


# ---------------------------------------------------------------------------
# Labelled pairs
# ---------------------------------------------------------------------------

def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip().lower()


def build_pairs(queries: list[dict], chunks: list[dict], seed: int = 7) -> list[dict]:
    """Labelled (prompt, excerpt) pairs from eval queries + indexed chunks.

    A query contributes a pair only when its `evidence.quote` can be located in the
    indexed text of its `evidence.file` — an unlocatable quote means the eval set and the
    index disagree, and a pair built on that would measure the disagreement.
    """
    live = [c for c in chunks if c.get("live", True) and c.get("text")]
    by_file: dict[str, list[dict]] = {}
    for c in live:
        by_file.setdefault(c["file_path"], []).append(c)
    rng = random.Random(seed)
    pairs: list[dict] = []
    for row in queries:
        q = row.get("q", "")
        ev = row.get("evidence") or {}
        probe = _norm(ev.get("quote", ""))[:60]
        gold_file = ev.get("file", "")
        pos = next((c for c in by_file.get(gold_file, []) if probe and probe in _norm(c["text"])), None)
        if not pos:
            continue
        pairs.append({"q": q, "kind": "pos", "text": pos["text"]})
        area = gold_file.split("/")[1] if "/" in gold_file else gold_file
        others = [c for c in live
                  if c["file_path"].split("/")[1:2] != [area] and len(c["text"]) > 200]
        if others:
            pairs.append({"q": q, "kind": "easy", "text": rng.choice(others)["text"]})
        for rj in row.get("reject") or []:
            cand = [c for f, cs in by_file.items() if rj.lower() in f.lower()
                    for c in cs if len(c["text"]) > 150]
            if not cand:
                continue
            qw = set(re.findall(r"[a-z0-9]{4,}", q.lower()))
            best = max(cand, key=lambda c: len(qw & set(re.findall(r"[a-z0-9]{4,}", c["text"].lower()))))
            pairs.append({"q": q, "kind": "hard", "text": best["text"]})
            break
    return pairs


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

def auroc(pos: list[float], neg: list[float]) -> float:
    """Probability a random positive outscores a random negative (ties count a half)."""
    if not pos or not neg:
        return float("nan")
    return sum(1.0 if a > b else 0.5 if a == b else 0.0 for a in pos for b in neg) / (len(pos) * len(neg))


def sweep(scores: list[float], kinds: list[str], max_easy_fpr: float = 0.05) -> dict:
    """AUCs plus two candidate thresholds over the observed score range."""
    pick = lambda k: [s for s, kk in zip(scores, kinds) if kk == k]
    pos, easy, hard = pick("pos"), pick("easy"), pick("hard")
    neg = easy + hard
    rate = lambda xs, t: (sum(s >= t for s in xs) / len(xs)) if xs else float("nan")
    best = (-2.0, None)
    low_noise = None
    for t in sorted({round(s, 2) for s in scores}):
        j = rate(pos, t) - rate(neg, t)
        if j > best[0]:
            best = (j, t)
        if low_noise is None and easy and rate(easy, t) <= max_easy_fpr:
            low_noise = t
    at = lambda t: None if t is None else {
        "threshold": t, "tpr": round(rate(pos, t), 3),
        "fpr_easy": round(rate(easy, t), 3), "fpr_hard": round(rate(hard, t), 3)}
    return {"n": {"pos": len(pos), "easy": len(easy), "hard": len(hard)},
            "auc_vs_easy": round(auroc(pos, easy), 3),
            "auc_vs_hard": round(auroc(pos, hard), 3),
            "youden": at(best[1]), f"max_easy_fpr_{max_easy_fpr}": at(low_noise)}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _load(eval_path: str, chunks_path: str) -> tuple[list[dict], list[dict]]:
    import yaml
    queries = (yaml.safe_load(Path(eval_path).read_text(encoding="utf-8")) or {}).get("queries", [])
    chunks = [json.loads(l) for l in open(chunks_path, encoding="utf-8")]
    return queries, chunks


def run_judge(eval_path: str, chunks_path: str, model: str | None = None) -> dict:
    from carta.config import DEFAULTS
    from carta.search.rerank import score_pairs

    model = model or DEFAULTS["proactive_recall"]["judge_model"]
    queries, chunks = _load(eval_path, chunks_path)
    pairs = build_pairs(queries, chunks)
    kinds = [p["kind"] for p in pairs]
    print(f"model={model}  pairs={len(pairs)} "
          f"(pos={kinds.count('pos')} easy={kinds.count('easy')} hard={kinds.count('hard')})",
          flush=True)
    out = {}
    for view, chars in EXCERPT_VIEWS.items():
        lat, scores = [], []
        for p in pairs:
            t = time.perf_counter()
            scores.append(score_pairs(p["q"][:300], [p["text"][:chars]], model)[0])
            lat.append(time.perf_counter() - t)
        res = sweep(scores, kinds)
        res["p50_ms"] = round(1000 * statistics.median(lat), 1)
        out[view] = res
        print(f"{view}: {json.dumps(res)}", flush=True)
    return out


def main(argv: list[str] | None = None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print(__doc__)
        sys.exit(2)
    cmd, rest = argv[0], argv[1:]
    if cmd == "chunks":
        url, coll, out = rest[0], rest[1], rest[2]
        print(f"{dump_chunks(url, coll, Path(out))} points -> {out}")
    elif cmd == "judge":
        model = None
        if "--model" in rest:
            i = rest.index("--model")
            model = rest[i + 1]
            rest = rest[:i] + rest[i + 2:]
        run_judge(rest[0], rest[1], model)
    else:
        print(f"unknown command {cmd!r}")
        sys.exit(2)


if __name__ == "__main__":
    main()
