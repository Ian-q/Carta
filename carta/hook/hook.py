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

When the judge cannot answer (timeout, missing model, Ollama down, fastembed
missing), that is NOT treated as "no": a top hit whose absolute relevance
(dense cosine, or the legacy cosine) is above high_threshold still injects; the
grey zone below it stays silent (#138). Set CARTA_HOOK_DEBUG=1 for one stderr
line per prompt with scores, thresholds, judge status and timings, or run
`carta hook explain "<prompt>"`.

Non-hybrid (cosine-score) searches fall back to the legacy high_threshold /
low_threshold gate, which remains valid there.

All paths exit 0 (the prompt always proceeds unblocked). stdout is reserved for
the JSON context block.
All diagnostic output goes to stderr.
"""

import concurrent.futures
import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import requests

from carta.config import (find_config, load_config, NOTE_DOC_TYPES, ollama_keep_alive,
                          query_ollama_url)
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
        _debug(lambda: "carta-hook: proactive_recall disabled in config — nothing to do")
        sys.exit(0)

    decision = _decide_bounded(prompt, cfg)
    if decision is None:
        # The hard ceiling fired: something below ignored its own timeout. Leave
        # now — the worker is a daemon thread, and os._exit skips waiting on any
        # non-daemon helper (a judge executor) it may have started.
        print(f"carta-hook: recall gave no answer within {_hard_timeout_s(prompt, cfg):g}s "
              "— skipping (fail-open)", file=sys.stderr)
        sys.stderr.flush()
        os._exit(0)
    if decision.backends_down:
        print("carta-hook: " + "; ".join(decision.backends_down)
              + " — skipping recall (fail-open)", file=sys.stderr)
        sys.exit(0)
    if decision.search_error is not None:
        print(f"carta-hook: search error (fail-open): {_one_line(decision.search_error)}",
              file=sys.stderr)
        sys.exit(0)

    if decision.judge_status in ("timeout", "error"):
        print(f"carta-hook: judge {decision.judge_status} "
              f"({decision.judge_error or 'no detail'}) — {decision.fallback_note()}",
              file=sys.stderr)
    _debug(decision.summary_line)

    _emit_trace(decision.query, decision.hits, decision.zone, decision.judge_verdict,
                decision.started_at, decision.search_cfg,
                judge_backend=decision.judge_backend, judge_score=decision.judge_score,
                judge_status=decision.judge_status, injected=decision.inject)

    if decision.inject:
        _inject(decision.hits)
    sys.exit(0)


def _one_line(msg: str, limit: int = 240) -> str:
    """First line of `msg`, truncated: hook stderr is shown to the user, so a
    multi-line requests/urllib3 error chain is noise, not diagnosis."""
    line = (msg or "").strip().splitlines()[0] if (msg or "").strip() else ""
    return line if len(line) <= limit else line[: limit - 3] + "..."


def _hard_timeout_s(prompt: str, cfg: dict) -> float:
    """Ceiling for the whole decision, in seconds (#106).

    `proactive_recall.hook_timeout_s` when set; otherwise the sum of the budgets
    the steps already have — connect probe, long-prompt extraction (4 s, only for
    prompts over 500 chars), search, judge — plus one second of slack. Each step
    is supposed to honour its own budget; this exists for the one that does not.
    """
    pr = cfg.get("proactive_recall", {}) or {}
    explicit = pr.get("hook_timeout_s")
    if isinstance(explicit, (int, float)) and explicit > 0:
        return float(explicit)
    from carta.hook.reach import DEFAULT_CONNECT_TIMEOUT_S
    total = float(pr.get("connect_timeout_s", DEFAULT_CONNECT_TIMEOUT_S) or 0)
    total += 4.0 if len(prompt) > 500 else 0.0
    total += float(pr.get("search_timeout_s", 3) or 0)
    total += float(pr.get("judge_timeout_s", 3) or 0)
    return total + 1.0


def _decide_bounded(prompt: str, cfg: dict) -> "Decision | None":
    """`decide()` under a hard wall-clock ceiling; None when the ceiling fires.

    Runs in a daemon thread so an abandoned call cannot keep the process alive.
    An exception inside decide() is re-raised here, so main()'s fail-open handler
    still reports it.
    """
    import threading

    box: dict = {}

    def work():
        try:
            box["d"] = decide(prompt, cfg)
        except BaseException as e:  # noqa: BLE001 - re-raised in the caller
            box["e"] = e

    t = threading.Thread(target=work, name="carta-hook-decide", daemon=True)
    t.start()
    t.join(_hard_timeout_s(prompt, cfg))
    if t.is_alive():
        return None
    if "e" in box:
        raise box["e"]
    return box["d"]


def _debug(make_line) -> None:
    """One stderr line per prompt when CARTA_HOOK_DEBUG is set (#138).

    Claude Code shows hook stderr in its transcript/verbose view, so this is the
    in-situ answer to "why did nothing get injected?" without a separate command.
    Built lazily so the default path pays nothing.
    """
    if os.environ.get("CARTA_HOOK_DEBUG", "").strip() not in ("", "0", "false", "no"):
        try:
            print(make_line(), file=sys.stderr)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# The decision, as data (shared by the hook and `carta hook explain`)
# ---------------------------------------------------------------------------

@dataclass
class Decision:
    """Everything the hook decided for one prompt, and why.

    `carta hook explain` prints this; the hook acts on it. Keeping one code path
    for both means the explanation cannot drift from what the hook really does.
    """
    query: str = ""
    query_source: str = "prompt"          # "prompt" | "ollama" | "fallback"
    query_error: str | None = None
    hits: list = field(default_factory=list)
    search_error: str | None = None
    backends_down: list = field(default_factory=list)  # one line per unreachable remote (#106)
    zone: str = "silent"                  # "silent" | "judge" | "inject"
    zone_reason: str = ""
    thresholds: dict = field(default_factory=dict)
    judge_backend: str | None = None
    judge_status: str = "not_called"      # "not_called" | "yes" | "no" | "timeout" | "error"
    judge_error: str | None = None
    judge_score: float | None = None
    judge_fallback: str | None = None     # set when the judge was unavailable
    inject: bool = False
    timings_ms: dict = field(default_factory=dict)
    started_at: float = 0.0
    search_cfg: dict = field(default_factory=dict)

    @property
    def judge_verdict(self) -> bool | None:
        """The trace's legacy `judge` field: None when the judge never ran."""
        if self.judge_status == "not_called":
            return None
        return self.judge_status == "yes"

    def fallback_note(self) -> str:
        if self.judge_fallback == "inject_high_confidence":
            return "top hit is above high_threshold, injecting without the judge"
        return "grey-zone hits not injected"

    def summary_line(self) -> str:
        top = self.hits[0] if self.hits else {}
        bits = [
            f"carta-hook: {'INJECT' if self.inject else 'silent'}",
            f"zone={self.zone}",
            f"hits={len(self.hits)}",
        ]
        if top:
            bits.append(f"top_score={_fmt(top.get('score'))}")
            bits.append(f"top_dense={_fmt(top.get('dense_score'))}")
            if top.get("lane_ranks"):
                bits.append(f"lanes={top['lane_ranks']}")
        t = self.thresholds
        bits.append(f"low={t.get('low')} high={t.get('high')} agree_rank={t.get('agree_rank')}")
        if self.judge_status != "not_called":
            bits.append(f"judge={self.judge_backend}:{self.judge_status}")
            if self.judge_score is not None:
                bits.append(f"judge_score={_fmt(self.judge_score)}")
            if self.judge_error:
                bits.append(f"judge_error={self.judge_error!r}")
            if self.judge_fallback:
                bits.append(f"fallback={self.judge_fallback}")
        bits.append("ms=" + ",".join(f"{k}:{v}" for k, v in self.timings_ms.items()))
        bits.append(f"reason={self.zone_reason!r}")
        return " ".join(bits)

    def to_dict(self) -> dict:
        return {
            "inject": self.inject,
            "zone": self.zone,
            "zone_reason": self.zone_reason,
            "query": self.query,
            "query_source": self.query_source,
            "query_error": self.query_error,
            "search_error": self.search_error,
            "backends_down": list(self.backends_down),
            "thresholds": self.thresholds,
            "judge": {
                "backend": self.judge_backend,
                "status": self.judge_status,
                "error": self.judge_error,
                "score": self.judge_score,
                "fallback": self.judge_fallback,
            },
            "timings_ms": self.timings_ms,
            "hits": [
                {k: h.get(k) for k in ("source", "score", "dense_score", "lane_ranks", "doc_type")}
                for h in self.hits
            ],
        }


def _fmt(x) -> str:
    return "none" if x is None else f"{x:.3f}" if isinstance(x, float) else str(x)


def decide(prompt: str, cfg: dict) -> Decision:
    """Run query extraction, search, gate and judge for one prompt — no side effects.

    Does not consult `modules.proactive_recall` (the caller does), does not inject
    and does not write a trace. Search errors are returned in `search_error`.
    """
    pr = cfg.get("proactive_recall", {})
    high_threshold = pr.get("high_threshold", 0.85)
    low_threshold = pr.get("low_threshold", 0.60)
    max_results = pr.get("max_results", 5)
    judge_timeout_s = pr.get("judge_timeout_s", 3)
    search_timeout_s = pr.get("search_timeout_s", 3)
    agree_rank = pr.get("agree_rank", 3)

    d = Decision(thresholds={
        "low": low_threshold, "high": high_threshold, "agree_rank": agree_rank,
        "judge_timeout_s": judge_timeout_s, "search_timeout_s": search_timeout_s,
        "max_results": max_results,
    })

    # Remote backends first (#106): one parallel TCP connect per non-loopback
    # endpoint under connect_timeout_s. A tailnet that is down makes every later
    # step wait out its full timeout, so find out in one short, bounded step and
    # skip the rest. Loopback endpoints are not probed (they fail instantly anyway).
    from carta.hook.reach import DEFAULT_CONNECT_TIMEOUT_S, probe
    connect_timeout_s = pr.get("connect_timeout_s", DEFAULT_CONNECT_TIMEOUT_S)
    d.thresholds["connect_timeout_s"] = connect_timeout_s
    t_probe = time.monotonic()
    d.started_at = t_probe
    if connect_timeout_s and connect_timeout_s > 0:
        down = probe({"Qdrant": cfg.get("qdrant_url", ""),
                      "Ollama (query)": query_ollama_url(cfg)}, connect_timeout_s)
        d.timings_ms["connect"] = int((time.monotonic() - t_probe) * 1000)
        if down:
            d.backends_down = [str(u) for u in down]
            d.search_error = "backend unreachable: " + "; ".join(d.backends_down)
            d.zone_reason = "backend unreachable"
            d.timings_ms["total"] = d.timings_ms["connect"]
            return d

    # Extract query.
    # started_at was set before the connect probe, not just before run_search:
    # for prompts >500 chars, _extract_query makes its own Ollama call (up to 4s)
    # that can dominate the latency the hook adds to prompt submission. Starting
    # the clock after it would make that cost invisible in the trace record used
    # for calibration (issue #118). The probe is hook latency too.
    t_extract = time.monotonic()
    qinfo: dict = {}
    d.query = _extract_query(prompt, cfg, info=qinfo)
    d.query_source = qinfo.get("source", "prompt")
    d.query_error = qinfo.get("error")
    t_search = time.monotonic()
    d.timings_ms["extract"] = int((t_search - t_extract) * 1000)

    # Search — text-only, never reranked. Proactive recall fires on every
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
    d.search_cfg = {
        **cfg,
        "embed": {**cfg.get("embed", {}), "colpali_enabled": False},
        "search": {
            **cfg.get("search", {}),
            "rerank": {**cfg.get("search", {}).get("rerank", {}), "enabled": False},
        },
    }
    try:
        hits = run_search(d.query, d.search_cfg, timeout_s=search_timeout_s)
    except Exception as e:
        d.search_error = f"{type(e).__name__}: {e}"
        d.zone_reason = "search failed"
        d.timings_ms["search"] = int((time.monotonic() - t_search) * 1000)
        return d
    d.timings_ms["search"] = int((time.monotonic() - t_search) * 1000)

    d.hits = hits[:max_results]
    d.zone = _gate_zone(d.hits, agree_rank=agree_rank, low=low_threshold, high=high_threshold)
    d.zone_reason = _zone_reason(d.hits, d.zone, agree_rank=agree_rank,
                                 low=low_threshold, high=high_threshold)

    if d.zone == "judge":
        judge_out: dict = {}
        verdict = _judge_with_timeout(prompt, d.hits, cfg, judge_timeout_s, out=judge_out)
        d.judge_backend = judge_out.get("backend")
        d.judge_score = judge_out.get("score")
        d.judge_status = judge_out.get("status") or ("yes" if verdict else "no")
        d.judge_error = judge_out.get("error")
        d.timings_ms["judge"] = judge_out.get("elapsed_ms", 0)
        if d.judge_status in ("timeout", "error"):
            # The judge is unavailable for this prompt (#138). A top hit whose
            # absolute relevance clears high_threshold is strong enough to inject
            # unvetted; the grey zone below it is exactly what the judge exists
            # to vet, so it stays silent (HOOK-05).
            if _clears_high(d.hits, high_threshold):
                d.judge_fallback = "inject_high_confidence"
                d.inject = True
            else:
                d.judge_fallback = "silent_grey_zone"
        else:
            d.inject = d.judge_status == "yes"
    else:
        d.inject = d.zone == "inject"

    d.timings_ms["total"] = int((time.monotonic() - d.started_at) * 1000)
    return d


def _clears_high(hits: list[dict], high: float) -> bool:
    """True when the top hit's ABSOLUTE relevance is above `high`.

    Hybrid hits are measured by the dense lane's raw cosine (the fused RRF score
    has no absolute scale); legacy cosine hits by `score`. A hit with no absolute
    measure (sparse-only) never clears it.
    """
    if not hits:
        return False
    top = hits[0]
    measure = top.get("dense_score") if top.get("lane_ranks") else top.get("score")
    return measure is not None and measure > high


def _zone_reason(hits: list[dict], zone: str, *, agree_rank: int, low: float,
                 high: float) -> str:
    """Plain-language reason for `_gate_zone`'s answer (explain/debug only)."""
    if not hits:
        return "no search results"
    top = hits[0]
    ranks = top.get("lane_ranks")
    if not ranks:
        score = top.get("score")
        if zone == "silent":
            return f"top cosine {_fmt(score)} < low_threshold {low}"
        if zone == "inject":
            return f"top cosine {_fmt(score)} > high_threshold {high}"
        return f"top cosine {_fmt(score)} in grey zone [{low}, {high}] -> judge"
    dense = top.get("dense_score")
    if zone == "silent":
        return f"top dense cosine {_fmt(dense)} < low_threshold {low}"
    if zone == "inject":
        return (f"top hit ranks {ranks} are both < agree_rank {agree_rank} "
                "(lanes agree) -> inject without judge")
    if dense is None:
        return f"top hit has no dense cosine (lanes {ranks}) -> judge"
    return (f"top dense cosine {_fmt(dense)} >= low_threshold {low} but lanes {ranks} "
            f"do not both rank < {agree_rank} -> judge")


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
    judge_status: str | None = None,
    injected: bool | None = None,
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
        # `judge` alone cannot tell a "no" from a timeout or a missing model —
        # both were False — which is how a dead judge hid in the traces (#138).
        if judge_status is not None:
            rec["judge_status"] = judge_status
        if injected is not None:
            rec["injected"] = injected
        append_trace(search_cfg.get("project_name", ""), rec)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Query extraction (D-06)
# ---------------------------------------------------------------------------

def _extract_query(prompt: str, cfg: dict, info: dict | None = None) -> str:
    """Return a search query from the prompt.

    Short prompts (<=500 chars) are returned as-is.
    Long prompts are compressed via Ollama; on failure returns last 500 chars.
    `info` (optional) receives {"source": "prompt"|"ollama"|"fallback", "error"}.
    """
    if info is None:
        info = {}
    info["source"] = "prompt"
    if len(prompt) <= 500:
        return prompt

    try:
        ollama_url = query_ollama_url(cfg)
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
        body = resp.json()
        if isinstance(body, dict) and body.get("error"):
            raise RuntimeError(f"ollama: {body['error']}")
        query = body["message"]["content"].strip()
        info["source"] = "ollama"
        return query
    except Exception as e:
        print(f"carta-hook: _extract_query fallback ({e})", file=sys.stderr)
        info["source"] = "fallback"
        info["error"] = f"{type(e).__name__}: {e}"
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

    ollama_url = query_ollama_url(cfg)
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
    err: dict = {}
    verdict = ollama_yesno(ollama_url, model, system, user_msg, timeout_s=timeout_s, err=err)
    if verdict is None:
        # Not a "no": the judge could not answer (model missing, Ollama down, bad
        # reply). Say so, so the caller can treat it as unavailable (#138).
        _judge_score_out["status"] = "error"
        _judge_score_out["error"] = err.get("error", "ollama judge returned no answer")
    return bool(verdict)


# Excerpt budget for the cross-encoder judge. 400 chars beat 200 on the labelled set
# (AUC 0.915 vs 0.877 against random chunks, 0.676 vs 0.632 against plausible-but-wrong
# ones) for ~4ms more. The Ollama judge keeps 200: that is the shape its numbers were
# measured at, and this PR does not change that backend.
_CROSSENC_EXCERPT_CHARS = 400
_JUDGE_BACKENDS = ("auto", "crossenc", "ollama")


def _resolve_judge_backend(cfg: dict) -> str:
    """Which judge actually runs: "crossenc" or "ollama".

    The default is "auto" because fastembed — which the cross-encoder needs — is an
    optional extra (`carta-cc[hybrid]`). A plain install has no cross-encoder, and
    hard-defaulting to it there would leave the gray zone permanently silent, which is
    exactly the silent-failure mode this gate has been bitten by before. So: prefer the
    cross-encoder when it is installed, fall back to the Ollama judge when it is not.
    An unknown name warns and resolves as "auto" rather than disabling recall.
    """
    import importlib.util

    backend = cfg.get("proactive_recall", {}).get("judge_backend", "auto")
    if backend not in _JUDGE_BACKENDS:
        print(f"carta-hook: unknown proactive_recall.judge_backend {backend!r} — "
              f"expected one of {_JUDGE_BACKENDS}; resolving as 'auto'", file=sys.stderr)
        backend = "auto"
    if backend == "auto":
        try:
            found = importlib.util.find_spec("fastembed") is not None
        except Exception:
            found = False   # find_spec can raise (e.g. a missing parent package)
        return "crossenc" if found else "ollama"
    return backend


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
    """Route to the resolved judge backend (see `_resolve_judge_backend`)."""
    backend = _resolve_judge_backend(cfg)
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

    `out` (optional) receives {"backend", "score", "status", "error", "elapsed_ms"}.
    The score is None for the ollama backend, which answers yes/no rather than
    scoring. `status` is "yes" / "no" for a real verdict and "timeout" / "error"
    when the judge could not answer — the boolean return cannot tell those apart,
    and conflating them is what let a dead judge look like a string of "no"s (#138).
    """
    _judge_score_out.clear()
    status: dict = {}
    t0 = time.monotonic()
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(_judge, prompt, hits, cfg, timeout_s)
        try:
            verdict = future.result(timeout=timeout_s)
            status = {"status": "yes" if verdict else "no"}
            return verdict
        except concurrent.futures.TimeoutError:
            print(
                f"carta-hook: judge timeout after {timeout_s}s (skipping injection)",
                file=sys.stderr,
            )
            status = {"status": "timeout",
                      "error": f"no answer within judge_timeout_s={timeout_s}s"}
            return False
        except Exception as e:
            print(f"carta-hook: judge exception (fail-open): {e}", file=sys.stderr)
            status = {"status": "error", "error": f"{type(e).__name__}: {e}"}
            return False
        finally:
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            if out is not None:
                snap = dict(_judge_score_out)
                # An inner "error" (the ollama judge answered None) outranks the
                # outer "no" it was coerced to; a timeout outranks everything.
                if status.get("status") == "no" and snap.get("status") == "error":
                    status = {"status": "error", "error": snap.get("error")}
                snap.update(status)
                snap["elapsed_ms"] = elapsed_ms
                out.update(snap)


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
