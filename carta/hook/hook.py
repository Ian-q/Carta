"""carta-hook — UserPromptSubmit hook entry point.

Reads stdin JSON from Claude Code, extracts the prompt, queries Qdrant via
run_search, and routes through three zones gated on retrieval STRUCTURE
(rank agreement across lanes), not absolute score magnitude — see
`_gate_zone` for why: hybrid search returns RRF scores whose scale depends
on k and lane count, so an absolute threshold on them is meaningless.

  top hit ranked within agree_rank in BOTH lanes  → fast-path inject (no Ollama)
  top hit ranked within agree_rank in NEITHER lane → noise gate (silent exit)
  top hit confident in exactly ONE lane            → Ollama judge with timeout;
                                                      inject on yes, skip on no
                                                      or timeout (HOOK-05: no
                                                      injection on timeout)

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
        # Config lives at <repo_root>/.carta/config.yaml — used below to place
        # trace output at the real repo root without threading it through cfg.
        repo_root = cfg_path.parent.parent
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
    started_at = time.monotonic()
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
    if zone == "judge":
        judge_verdict = _judge_with_timeout(prompt, hits, cfg, judge_timeout_s)

    _emit_trace(query, hits, zone, judge_verdict, started_at, cfg, repo_root)

    if zone == "inject" or judge_verdict:
        _inject(hits)
    sys.exit(0)


# ---------------------------------------------------------------------------
# Rank-and-agreement gate (replaces absolute RRF-score gating)
# ---------------------------------------------------------------------------

def _gate_zone(hits: list[dict], agree_rank: int = 3, low: float = 0.60,
               high: float = 0.85) -> str:
    """Decide inject / judge / silent from retrieval STRUCTURE, not magnitude.

    Hybrid search returns RRF scores whose scale depends on k and lane count, so
    an absolute threshold on them is meaningless (see the 2026-08-09 spec). Rank
    is scale-independent: "top 3 in both lanes" means the same thing at any k.

    Falls back to score thresholds when lane ranks are unavailable, which is the
    non-hybrid path where the cosine calibration is still valid.

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

    confident = [r for r in (ranks.get("dense"), ranks.get("sparse"))
                 if r is not None and r < agree_rank]
    if len(confident) >= 2:
        return "inject"
    if len(confident) == 1:
        return "judge"
    return "silent"


# ---------------------------------------------------------------------------
# Trace emission (calibration data for the gate above; issue #118)
# ---------------------------------------------------------------------------

def _emit_trace(
    query: str,
    hits: list[dict],
    zone: str,
    judge_verdict: bool | None,
    started_at: float,
    cfg: dict,
    repo_root: Path,
) -> None:
    """Append one trace record. Swallows everything — the hook must fail open.

    `repo_root` and the searched `collections` are passed in explicitly rather
    than read off the cfg dict: cfg carries only real, user-facing config keys.
    `repo_root` is derived once in `_run` from `cfg_path.parent.parent` (config
    lives at `<repo_root>/.carta/config.yaml`); `collections` is recomputed here
    via `get_search_collections(cfg, "repo")`, the same helper `run_search` uses
    internally, so a `ValueError` from an invalid scope is a real (if unlikely)
    failure mode that must stay inside this function's try/except.
    """
    try:
        from carta.search.scoped import get_search_collections
        from carta.search.trace import build_trace_record, append_trace
        collections = get_search_collections(cfg, "repo")
        hybrid = cfg.get("search", {}).get("hybrid", {})
        hybrid_enabled = hybrid.get("enabled", True)
        rec = build_trace_record(
            query=query,
            collections=collections,
            hits=hits, zone=zone, judge=judge_verdict,
            latency_ms=int((time.monotonic() - started_at) * 1000),
            score_kind="rrf" if hybrid_enabled else "cosine",
            rrf_k=hybrid.get("rrf_k", 2) if hybrid_enabled else None,
        )
        append_trace(repo_root, rec)
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


def _judge_with_timeout(
    prompt: str, hits: list[dict], cfg: dict, timeout_s: int
) -> bool:
    """Run Ollama judge in a thread; return False on timeout and on other errors.

    HOOK-05: on timeout we do NOT inject. The gray-zone hits are exactly the
    borderline ones the judge exists to vet, so injecting them unvetted would be
    the context noise Carta tries to avoid. The hook still exits 0 — the prompt
    proceeds unblocked — it just stays silent.
    """
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(_call_ollama_judge, prompt, hits, cfg, timeout_s)
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
