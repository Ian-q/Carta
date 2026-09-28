"""carta-hook — UserPromptSubmit hook entry point.

Reads stdin JSON from Claude Code, extracts the prompt, queries Qdrant via
run_search, and routes through three zones. The FUSED score is never
thresholded — its scale depends on k and lane count, so an absolute threshold
on it is meaningless. Instead: lane rank for agreement, the dense lane's raw
cosine for relevance (see `_gate_zone`).

  top hit's dense cosine < low_threshold          → noise gate (silent exit)
  else top hit within agree_rank in BOTH lanes    → fast-path inject (no Ollama)
  else                                            → Ollama judge with timeout;
                                                     inject on yes, skip on no
                                                     or timeout (HOOK-05: no
                                                     injection on timeout)

A top hit with no dense_score (sparse-only, or a producer that omits the field)
can never be silenced — it goes to the judge.

Non-hybrid (cosine-score) searches fall back to the legacy high_threshold /
low_threshold gate, which remains valid there.

All paths exit 0 (the prompt always proceeds unblocked). stdout is reserved for
the JSON context block.
All diagnostic output goes to stderr.
"""

import concurrent.futures
import json
import sys
import time
from pathlib import Path

import requests

from carta.config import find_config, load_config, NOTE_DOC_TYPES, ollama_keep_alive
from carta.embed.pipeline import run_search


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def main() -> None:
    """Entry point for carta-hook console script."""
    try:
        _run()
    except SystemExit:
        raise
    except Exception as e:
        print(f"carta-hook: unexpected error (fail-open): {e}", file=sys.stderr)
        sys.exit(0)


def _run() -> None:
    """Inner logic — wrapped by main() for fail-open guarantee."""
    # 1. Read stdin
    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
    except Exception as e:
        print(f"carta-hook: stdin parse error (fail-open): {e}", file=sys.stderr)
        sys.exit(0)

    prompt = data.get("prompt", "")
    if not prompt:
        sys.exit(0)

    # 2. Load config
    try:
        cfg_path = find_config(Path.cwd())
        cfg = load_config(cfg_path)
    except Exception as e:
        print(f"carta-hook: config error (fail-open): {e}", file=sys.stderr)
        sys.exit(0)

    # 3. Gate on module enabled
    if not cfg.get("modules", {}).get("proactive_recall", False):
        sys.exit(0)

    # 4. Read thresholds
    pr = cfg.get("proactive_recall", {})
    high_threshold = pr.get("high_threshold", 0.85)
    low_threshold = pr.get("low_threshold", 0.60)
    max_results = pr.get("max_results", 5)
    judge_timeout_s = pr.get("judge_timeout_s", 3)
    search_timeout_s = pr.get("search_timeout_s", 3)
    agree_rank = pr.get("agree_rank", 3)

    # 5. Extract query
    # started_at captures from here, not just before run_search: for prompts
    # >500 chars, _extract_query makes its own Ollama call (up to 4s) that can
    # dominate the latency the hook adds to prompt submission. Starting the
    # clock after it would make that cost invisible in the trace record used
    # for calibration (issue #118).
    started_at = time.monotonic()
    query = _extract_query(prompt, cfg)

    # 6. Search — text-only, never reranked. Proactive recall fires on every
    # prompt and blocks submission, so it must never trigger the heavy ColPali
    # visual path (model load ~9s/prompt) nor pay reranker latency (an LLM
    # rerank call can take 10s+). The three-zone judge below already filters
    # for relevance. Force both off for this search regardless of the
    # project's setting.
    #
    # It also runs under a wall-clock budget (search_timeout_s) so an unreachable
    # REMOTE backend cannot stall submission. This only matters off localhost: a
    # dead local service refuses instantly, so the underlying 60s embed timeout
    # never binds, but a dead tailnet peer drops packets with no RST and the full
    # timeout elapses on every prompt (#106).
    search_cfg = {
        **cfg,
        "embed": {**cfg.get("embed", {}), "colpali_enabled": False},
        "search": {
            **cfg.get("search", {}),
            "rerank": {**cfg.get("search", {}).get("rerank", {}), "enabled": False},
        },
    }
    try:
        hits = run_search(query, search_cfg, timeout_s=search_timeout_s)
    except Exception as e:
        print(f"carta-hook: search error (fail-open): {e}", file=sys.stderr)
        sys.exit(0)

    # 7. Cap results
    hits = hits[:max_results]

    # 8. Gate on retrieval structure
    zone = _gate_zone(
        hits,
        agree_rank=agree_rank,
        low=low_threshold, high=high_threshold,
    )
    judge_verdict = None
    judge_out: dict = {}
    if zone == "judge":
        judge_verdict = _judge_with_timeout(prompt, hits, cfg, judge_timeout_s, out=judge_out)

    _emit_trace(query, hits, zone, judge_verdict, started_at, search_cfg,
                judge_backend=judge_out.get("backend"), judge_score=judge_out.get("score"))

    if zone == "inject" or judge_verdict:
        _inject(hits)
    sys.exit(0)


# ---------------------------------------------------------------------------
# Rank-and-agreement gate (replaces absolute RRF-score gating)
# ---------------------------------------------------------------------------

def _gate_zone(hits: list[dict], agree_rank: int = 3, low: float = 0.60,
               high: float = 0.85) -> str:
    """Decide inject / judge / silent: rank for AGREEMENT, cosine for RELEVANCE.

    Hybrid search returns RRF scores whose scale depends on k and lane count, so
    an absolute threshold on the FUSED score is meaningless (see the 2026-08-09
    spec). Rank is scale-independent, and lane agreement — "top-N in both the
    dense and the BM25 lane" — is strong evidence that a result is on topic.

    Rank cannot, however, express irrelevance. A rank is relative to the result
    set: `hits[0]` is the fusion argmax, so it always ranks well *among these
    results* however bad they all are. (Arithmetically, a dense rank-0-only hit
    scores 1/(2+0)=0.5 while a hit deep in both lanes tops out at 1/5+1/5=0.4,
    so a both-lanes-deep hit can never BE `hits[0]` — a "confident in neither
    lane" silent branch is dead code.) The dense lane's raw cosine IS an
    absolute measure, and `low` (0.60) was calibrated for exactly that before it
    was mistakenly applied to a fused RRF score.

    Hence the order below, and its deliberate asymmetry: go silent only when we
    can MEASURE low relevance. A sparse-only top hit carries no `dense_score`,
    so it reaches the judge instead of being dropped — dropping it is the recall
    bug this gate exists to fix and must not come back.

    Falls back to score thresholds when lane ranks are unavailable, which is the
    non-hybrid path where the cosine calibration is still valid as-is.

    `agree_rank` is 0-indexed like the ranks themselves: a rank is "confident"
    only when strictly less than agree_rank, so agree_rank=3 covers ranks
    0, 1, 2 (the top three) and a rank of exactly 3 (4th place) does not count.
    """
    if not hits:
        return "silent"

    ranks = hits[0].get("lane_ranks")
    if not ranks:
        score = hits[0].get("score")
        if score is None or score < low:
            return "silent"
        return "inject" if score > high else "judge"

    # `is not None`, never truthiness: 0.0 is a real (orthogonal) cosine and a
    # rank of 0 is the BEST rank — both would be discarded by a falsy check.
    dense_score = hits[0].get("dense_score")
    if dense_score is not None and dense_score < low:
        return "silent"

    confident = [r for r in (ranks.get("dense"), ranks.get("sparse"))
                 if r is not None and r < agree_rank]
    return "inject" if len(confident) >= 2 else "judge"


# ---------------------------------------------------------------------------
# Trace emission (calibration data for the gate above; issue #118)
# ---------------------------------------------------------------------------

def _emit_trace(
    query: str,
    hits: list[dict],
    zone: str,
    judge_verdict: bool | None,
    started_at: float,
    search_cfg: dict,
    judge_backend: str | None = None,
    judge_score: float | None = None,
) -> None:
    """Append one trace record. Swallows everything — the hook must fail open.

    Records land in `~/.carta/traces/<project_name>/`, outside every repo: the
    record's `query` is the prompt verbatim for prompts <=500 chars, and
    `.carta/` inside a project is a tracked directory. `project_name` is a real
    config key, so it is read off the cfg dict; the searched `collections` are
    not, so they are recomputed here via `get_search_collections(search_cfg,
    "repo")` — the same helper `run_search` uses internally, so a `ValueError`
    from an invalid scope is a real (if unlikely) failure mode that must stay
    inside this function's try/except.

    Opt out with `proactive_recall.trace: false`; absent, tracing is on (the
    trace IS the calibration data for the gate, issue #118).

    Takes `search_cfg` — the text-only, no-rerank cfg `_run` actually passed to
    `run_search` — not the project's raw `cfg`. `search_cfg` always forces
    `colpali_enabled: False` (step 6), so `get_search_collections` always
    excludes `_visual` from the traced list too, matching what was actually
    queried. Passing raw `cfg` here would report `_visual` as searched for any
    project that hasn't itself opted out of ColPali, even though the hook never
    queries it — a false "searched and missed" reading of a collection that was
    never touched. Deriving `score_kind`/`rrf_k` from the same `search_cfg` keeps
    that value tied to the dict the search actually used, for the same reason.
    """
    try:
        if not search_cfg.get("proactive_recall", {}).get("trace", True):
            return
        from carta.search.scoped import get_search_collections
        from carta.search.trace import build_trace_record, append_trace
        collections = get_search_collections(search_cfg, "repo")
        hybrid = search_cfg.get("search", {}).get("hybrid", {})
        # Hybrid can be enabled project-wide yet a legacy (non-named-vector)
        # collection still returns a plain cosine score with no lane_ranks.
        # `_gate_zone` already distinguishes these two worlds by lane_ranks
        # presence, not the cfg flag — use the same signal here, so a legacy
        # trace line never claims score_kind="rrf" alongside lanes=null.
        is_rrf = bool(hits and hits[0].get("lane_ranks"))
        rec = build_trace_record(
            query=query,
            collections=collections,
            hits=hits, zone=zone, judge=judge_verdict,
            latency_ms=int((time.monotonic() - started_at) * 1000),
            score_kind="rrf" if is_rrf else "cosine",
            rrf_k=hybrid.get("rrf_k", 2) if is_rrf else None,
            judge_backend=judge_backend,
            judge_score=judge_score,
        )
        append_trace(search_cfg.get("project_name", ""), rec)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Query extraction (D-06)
# ---------------------------------------------------------------------------

def _extract_query(prompt: str, cfg: dict) -> str:
    """Return a search query from the prompt.

    Short prompts (<=500 chars) are returned as-is.
    Long prompts are compressed via Ollama; on failure returns last 500 chars.
    """
    if len(prompt) <= 500:
        return prompt

    try:
        ollama_url = cfg["embed"]["ollama_url"]
        model = cfg["proactive_recall"]["ollama_model"]
        resp = requests.post(
            f"{ollama_url}/api/chat",
            json={
                "model": model,
                "messages": [
                    {
                        "role": "system",
                        "content": "Extract a concise 1-2 sentence search query from this text.",
                    },
                    {"role": "user", "content": prompt[:1000]},
                ],
                "stream": False,
                "think": False,  # reasoning-model judge; see carta/hook/judge.py _NO_THINK
                "keep_alive": ollama_keep_alive(),
            },
            timeout=4,
        )
        return resp.json()["message"]["content"].strip()
    except Exception as e:
        print(f"carta-hook: _extract_query fallback ({e})", file=sys.stderr)
        return prompt[-500:]


# ---------------------------------------------------------------------------
# Ollama judge (D-15 through D-18)
# ---------------------------------------------------------------------------

def _call_ollama_judge(prompt: str, hits: list[dict], cfg: dict, timeout_s: float = 4) -> bool:
    """Judge whether the documentation candidates are relevant. Returns True only
    on a 'yes'; any error or non-yes answer returns False (fail-open).

    ``timeout_s`` is the Ollama call budget; _judge_with_timeout passes the same
    value it enforces on the worker thread so the inner timeout never exceeds the
    outer one. An inner > outer let the hook block past judge_timeout_s, since
    ThreadPoolExecutor.__exit__ waits for the abandoned thread to finish."""
    from carta.hook.judge import ollama_yesno

    ollama_url = cfg["embed"]["ollama_url"]
    model = cfg["proactive_recall"]["ollama_model"]
    excerpts = "\n---\n".join(h["excerpt"][:200] for h in hits)
    user_msg = (
        f"Prompt: {prompt[:300]}\n\n"
        f"Documentation candidates:\n{excerpts}\n\n"
        f"Are any of these relevant?"
    )
    system = (
        "You decide if documentation is relevant to a coding prompt. "
        "Answer only 'yes' or 'no'."
    )
    return bool(ollama_yesno(ollama_url, model, system, user_msg, timeout_s=timeout_s))


# Excerpt budget for the cross-encoder judge. 400 chars beat 200 on the labelled set
# (AUC 0.915 vs 0.877 against random chunks, 0.676 vs 0.632 against plausible-but-wrong
# ones) for ~4ms more. The Ollama judge keeps 200: that is the shape its numbers were
# measured at, and this PR does not change that backend.
_CROSSENC_EXCERPT_CHARS = 400
_DEFAULT_JUDGE_BACKEND = "crossenc"


def _call_crossenc_judge(prompt: str, hits: list[dict], cfg: dict) -> bool:
    """True when the best candidate clears `judge_threshold` on a local cross-encoder.

    A cross-encoder scores "does this passage answer this query" directly — the judge's
    actual question — with no reasoning model to mis-handle, no Ollama dependency, and
    ~10ms warm / ~0.5s cold in a fresh process. Scores are raw logits, so the threshold
    is model-specific (see `carta/hook/eval/calibrate_gate.py` for how it was derived).

    The score is returned via `_judge_score_out` rather than the return value because
    callers (and the existing tests) treat the judge as a boolean; the trace needs the
    number to stay recalibratable (#118).
    """
    from carta.search.rerank import score_pairs

    if not hits:
        return False
    pr = cfg.get("proactive_recall", {})
    model = pr.get("judge_model", "Xenova/ms-marco-MiniLM-L-6-v2")
    threshold = pr.get("judge_threshold", -10.4)
    texts = [(h.get("excerpt") or "")[:_CROSSENC_EXCERPT_CHARS] for h in hits]
    scores = score_pairs(prompt[:300], texts, model)
    best = max(scores) if scores else None
    _judge_score_out["score"] = best
    return best is not None and best >= threshold


def _judge(prompt: str, hits: list[dict], cfg: dict, timeout_s: float) -> bool:
    """Route to the configured judge backend. An unknown name warns and uses the default
    rather than silently disabling recall or silently switching semantics."""
    backend = cfg.get("proactive_recall", {}).get("judge_backend", _DEFAULT_JUDGE_BACKEND)
    if backend not in ("crossenc", "ollama"):
        print(f"carta-hook: unknown proactive_recall.judge_backend {backend!r} — "
              f"using {_DEFAULT_JUDGE_BACKEND!r}", file=sys.stderr)
        backend = _DEFAULT_JUDGE_BACKEND
    _judge_score_out["backend"] = backend
    if backend == "ollama":
        return _call_ollama_judge(prompt, hits, cfg, timeout_s)
    return _call_crossenc_judge(prompt, hits, cfg)


# Written by the judge call, read by _judge_with_timeout's `out` param. A dict rather
# than a return value so the boolean contract every caller and test relies on is intact.
_judge_score_out: dict = {}


def _judge_with_timeout(
    prompt: str, hits: list[dict], cfg: dict, timeout_s: int, out: dict | None = None
) -> bool:
    """Run the judge in a thread; return False on timeout and on other errors.

    HOOK-05: on timeout we do NOT inject. The gray-zone hits are exactly the
    borderline ones the judge exists to vet, so injecting them unvetted would be
    the context noise Carta tries to avoid. The hook still exits 0 — the prompt
    proceeds unblocked — it just stays silent.

    `out` (optional) receives {"backend", "score"} for the trace record; the score is
    None for the ollama backend, which answers yes/no rather than scoring.
    """
    _judge_score_out.clear()
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(_judge, prompt, hits, cfg, timeout_s)
        try:
            return future.result(timeout=timeout_s)
        except concurrent.futures.TimeoutError:
            print(
                f"carta-hook: judge timeout after {timeout_s}s (skipping injection)",
                file=sys.stderr,
            )
            return False
        except Exception as e:
            print(f"carta-hook: judge exception (fail-open): {e}", file=sys.stderr)
            return False
        finally:
            if out is not None:
                out.update(_judge_score_out)


# ---------------------------------------------------------------------------
# Injection output (D-01, D-02, D-03)
# ---------------------------------------------------------------------------

def _inject(hits: list[dict]) -> None:
    """Write the context block as JSON to stdout.

    stdout is reserved exclusively for the hook JSON output block.
    All other output uses stderr.
    """
    context_lines = ["## Relevant documentation\n"]
    for h in hits:
        tag = f"[{h['doc_type']}] " if h.get("doc_type") in NOTE_DOC_TYPES else ""
        context_lines.append(
            f"**Source: {tag}{h['source']} (score: {h['score']:.2f})**\n"
            f"> {h['excerpt'][:200]}\n"
        )
    context_text = "\n".join(context_lines)

    output = json.dumps({"context": context_text})
    sys.__stdout__.write(output)
    sys.__stdout__.flush()
