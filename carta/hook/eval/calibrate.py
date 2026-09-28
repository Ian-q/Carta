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
    python -m carta.hook.eval.calibrate gate     <eval.yaml> [--json out.json]

`judge` prints, per excerpt length, AUC against each negative kind and two candidate
thresholds: Youden's J (best overall separation) and the lowest threshold whose random-
chunk false-positive rate stays <= 5% (the operating point Carta ships, because injecting
unrelated documentation is the specific cost the gate exists to avoid).

Scores are raw cross-encoder logits — model-specific and not probabilities. Re-run this
whenever `proactive_recall.judge_model` changes.

`gate` calibrates the zone gate in front of the judge. It runs each eval query through the
hook's own search path and its own `_gate_zone`, labels the top hit from the eval set
(`expect` = relevant, `reject` = plausible-but-wrong), and sweeps `low_threshold` x
`agree_rank`. What matters per combination: how many relevant top hits are silenced (recall
thrown away before the judge can see it) versus how many irrelevant ones are injected
without any judge at all.
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
# Gate calibration (low_threshold x agree_rank)
# ---------------------------------------------------------------------------

# Prompts with no answer in ANY project corpus. An eval set cannot supply these — every
# eval query has an answer by construction — yet they are exactly the case `low_threshold`
# exists for: retrieval still returns a top hit, and the gate must go silent on it.
OFFTOPIC_PROMPTS = [
    "how do I centre a div with flexbox",
    "write a bash loop that renames files by extension",
    "what is the difference between git rebase and git merge",
    "explain python's global interpreter lock",
    "convert this SQL join to a window function",
    "how do I mock a REST call in jest",
    "what does kubernetes CrashLoopBackOff mean",
    "regex to validate an email address",
    "set up a postgres connection pool in node",
    "what is the time complexity of quicksort",
    "how do I add a dark mode toggle in react",
    "best way to paginate a graphql api",
    "explain the CAP theorem",
    "how do I profile memory usage in a rust binary",
    "write a dockerfile for a static site",
]


def label_top_hit(source: str, expect: list[str], reject: list[str]) -> str:
    """"relevant" | "wrong" | "other" for a top hit, from the eval set's own labels.

    `expect` wins over `reject`, exactly as the eval harness scores them. "other" is
    neither claimed nor verified by the eval set, so it is reported apart rather than
    silently counted as noise.
    """
    low = (source or "").lower()
    if any(e.lower() in low for e in expect):
        return "relevant"
    if any(r.lower() in low for r in reject):
        return "wrong"
    return "other"


def collect_gate_rows(queries: list[dict], cfg: dict, max_results: int = 5,
                      offtopic: list[str] | None = None) -> list[dict]:
    """Run each query through the hook's search path; record the top hit's gate signals.

    `offtopic` prompts are labelled "offtopic": whatever they retrieve is by definition
    noise, so they measure the one thing the eval set cannot — what the gate sees when
    nothing in the corpus answers the prompt.
    """
    import copy

    from carta.embed.pipeline import run_search

    rows = []
    for row in list(queries) + [{"q": q, "offtopic": True} for q in (offtopic or [])]:
        search_cfg = {
            **copy.deepcopy(cfg),
            "embed": {**cfg.get("embed", {}), "colpali_enabled": False},
            "search": {**cfg.get("search", {}), "top_n": max_results,
                       "rerank": {**cfg.get("search", {}).get("rerank", {}), "enabled": False}},
        }
        try:
            hits = (run_search(row["q"], search_cfg) or [])[:max_results]
        except Exception as e:                                    # a dead backend is not data
            print(f"  search failed for {row['q'][:50]!r}: {e}", file=sys.stderr)
            continue
        top = hits[0] if hits else None
        rows.append({
            "q": row["q"],
            "top_source": top.get("source") if top else None,
            "dense_score": top.get("dense_score") if top else None,
            "lane_ranks": top.get("lane_ranks") if top else None,
            "label": "offtopic" if row.get("offtopic") else
                     label_top_hit(top.get("source", "") if top else "",
                                   row.get("expect") or [], row.get("reject") or []),
        })
    return rows


def sweep_gate(rows: list[dict], lows: list[float], agree_ranks: list[int]) -> list[dict]:
    """Zone outcome per (low_threshold, agree_rank), using the hook's real `_gate_zone`."""
    from carta.hook.hook import _gate_zone

    out = []
    for low in lows:
        for ar in agree_ranks:
            tally: dict[str, int] = {}
            for r in rows:
                hit = {"score": r["dense_score"], "lane_ranks": r["lane_ranks"],
                       "dense_score": r["dense_score"]}
                zone = _gate_zone([hit] if r["top_source"] else [], agree_rank=ar, low=low)
                tally[f"{r['label']}/{zone}"] = tally.get(f"{r['label']}/{zone}", 0) + 1
            n_rel = sum(1 for r in rows if r["label"] == "relevant")
            n_off = sum(1 for r in rows if r["label"] == "offtopic")
            out.append({
                "low_threshold": low, "agree_rank": ar,
                # The two costs: recall thrown away, and noise admitted without a judge.
                "relevant_silenced": tally.get("relevant/silent", 0),
                "relevant_injected": tally.get("relevant/inject", 0),
                "relevant_judged": tally.get("relevant/judge", 0),
                "wrong_injected": tally.get("wrong/inject", 0),
                "other_injected": tally.get("other/inject", 0),
                # What low_threshold is actually for: silencing prompts nothing answers.
                "offtopic_silenced": tally.get("offtopic/silent", 0),
                "offtopic_injected": tally.get("offtopic/inject", 0),
                "offtopic_judged": tally.get("offtopic/judge", 0),
                "relevant_silenced_pct": round(100 * tally.get("relevant/silent", 0) / n_rel, 1) if n_rel else None,
                "offtopic_silenced_pct": round(100 * tally.get("offtopic/silent", 0) / n_off, 1) if n_off else None,
            })
    return out


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _load_queries(eval_path: str) -> list[dict]:
    import yaml
    return (yaml.safe_load(Path(eval_path).read_text(encoding="utf-8")) or {}).get("queries", [])


def _load(eval_path: str, chunks_path: str) -> tuple[list[dict], list[dict]]:
    chunks = [json.loads(l) for l in open(chunks_path, encoding="utf-8")]
    return _load_queries(eval_path), chunks


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


def run_gate(eval_path: str, json_out: str | None = None) -> dict:
    from carta.config import find_config, load_config

    cfg = load_config(find_config())
    queries = _load_queries(eval_path)
    pr = cfg.get("proactive_recall", {})
    rows = collect_gate_rows(queries, cfg, max_results=pr.get("max_results", 5),
                             offtopic=OFFTOPIC_PROMPTS)
    labels = {k: sum(1 for r in rows if r["label"] == k)
              for k in ("relevant", "wrong", "other", "offtopic")}
    dense = [r["dense_score"] for r in rows if r["dense_score"] is not None]
    print(f"queries={len(rows)}  top-hit labels={labels}  "
          f"with dense cosine={len(dense)}", flush=True)
    for lab in ("relevant", "wrong", "other", "offtopic"):
        ds = sorted(r["dense_score"] for r in rows
                    if r["label"] == lab and r["dense_score"] is not None)
        if ds:
            print(f"dense cosine, {lab:9} n={len(ds):3}  min={ds[0]:.3f} "
                  f"p25={ds[len(ds) // 4]:.3f} median={statistics.median(ds):.3f} "
                  f"max={ds[-1]:.3f}", flush=True)
    table = sweep_gate(rows, [0.0, 0.50, 0.55, 0.60, 0.62, 0.65, 0.68, 0.70],
                       [1, 2, 3, 4, 5])
    for t in table:
        print(json.dumps(t), flush=True)
    res = {"rows": rows, "labels": labels, "sweep": table}
    if json_out:
        Path(json_out).write_text(json.dumps(res, indent=1))
    return res


def main(argv: list[str] | None = None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print(__doc__)
        sys.exit(2)
    cmd, rest = argv[0], argv[1:]
    if cmd == "chunks":
        url, coll, out = rest[0], rest[1], rest[2]
        print(f"{dump_chunks(url, coll, Path(out))} points -> {out}")
    elif cmd == "gate":
        json_out = None
        if "--json" in rest:
            i = rest.index("--json")
            json_out = rest[i + 1]
            rest = rest[:i] + rest[i + 2:]
        run_gate(rest[0], json_out)
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
