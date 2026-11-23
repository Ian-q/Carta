"""`carta hook explain "<prompt>"` — why the recall hook would (not) inject (#138).

Runs the same `decide()` the hook runs, plus the hook's health checks, and prints
the whole chain: was the hook even reachable, which query was searched, the top
hits with their scores, which zone the gate chose and why, what the judge said
(or that it timed out / could not run), and how long each step took.

It never injects and never writes a trace record.
"""
from __future__ import annotations

import json
from pathlib import Path

_BLOCKING = ("recall_hook_wrapper", "recall_hook_entry_point")
_ICON = {"pass": "ok  ", "warn": "WARN", "fail": "ERR ", "skip": "skip"}


def explain(prompt: str, cfg: dict, repo_root: Path, *, as_json: bool = False,
            health_probe: bool = False) -> int:
    """Print the explanation; return the exit code (1 when any health check fails)."""
    from carta.hook import health
    from carta.hook.hook import decide

    items = health.check_recall_hook(cfg, repo_root, probe=health_probe)
    enabled = bool(cfg.get("modules", {}).get("proactive_recall", False))
    decision = decide(prompt, cfg)
    failed = any(i.status == "fail" for i in items)
    # Failures that stop the hook before it ever reaches decide().
    blocked = any(i.status == "fail" and i.name in _BLOCKING for i in items)

    if as_json:
        doc = {
            "enabled": enabled,
            "health": [
                {"name": i.name, "status": i.status, "message": i.message,
                 "suggestion": i.suggestion}
                for i in items
            ],
            "decision": decision.to_dict(),
            # What a real prompt would get: the decision only reaches Claude when
            # the hook is enabled and its wrapper can actually run.
            "would_inject": enabled and not blocked and decision.inject,
        }
        print(json.dumps(doc, indent=2))
        return 1 if failed else 0

    print("Recall hook health")
    for i in items:
        print(f"  [{_ICON.get(i.status, '?   ')}] {i.message}")
        if i.suggestion and i.status in ("fail", "warn"):
            print(f"         fix: {i.suggestion}")

    d = decision
    t = d.thresholds
    print("\nDecision for this prompt")
    src = {"prompt": "prompt as-is", "ollama": "compressed by the LLM",
           "fallback": "last 500 chars (LLM compression failed)"}[d.query_source]
    print(f"  query ({src}): {d.query[:160]!r}")
    if d.query_error:
        print(f"  query extraction error: {d.query_error}")
    if d.search_error:
        print(f"  SEARCH FAILED: {d.search_error}")
    else:
        print(f"  thresholds: low={t['low']} high={t['high']} agree_rank={t['agree_rank']} "
              f"max_results={t['max_results']}")
        print(f"  hits ({len(d.hits)}):")
        for n, h in enumerate(d.hits):
            print(f"    {n}. score={_f(h.get('score'))} dense={_f(h.get('dense_score'))} "
                  f"lanes={h.get('lane_ranks')}  {h.get('source')}")
        print(f"  zone: {d.zone} — {d.zone_reason}")
        if d.judge_status != "not_called":
            line = f"  judge ({d.judge_backend}): {d.judge_status}"
            if d.judge_score is not None:
                thr = cfg.get("proactive_recall", {}).get("judge_threshold")
                line += f" score={_f(d.judge_score)} threshold={thr}"
            print(line)
            if d.judge_error:
                print(f"  judge error: {d.judge_error}")
            if d.judge_fallback:
                print(f"  judge unavailable -> {d.fallback_note()}")
    print("  timings: " + ", ".join(f"{k} {v} ms" for k, v in d.timings_ms.items()))

    verdict = "INJECT" if d.inject else "no injection"
    if not enabled:
        verdict += " (but proactive_recall is disabled, so nothing is injected)"
    elif blocked:
        verdict += " (but the hook never gets this far: see the errors above)"
    print(f"\nResult: {verdict}")
    return 1 if failed else 0


def _f(x) -> str:
    return "-" if x is None else f"{x:.3f}" if isinstance(x, float) else str(x)
