"""carta-hook — UserPromptSubmit hook entry point.

Reads stdin JSON from Claude Code, extracts the prompt, queries Qdrant via
run_search, and routes through three score zones:

  score > high_threshold  → fast-path inject (no Ollama)
  score < low_threshold   → noise gate (silent exit)
  gray zone               → Ollama judge with timeout; inject on yes

All paths exit 0 (fail-open). stdout is reserved for the JSON context block.
All diagnostic output goes to stderr.
"""

import concurrent.futures
import json
import sys
from pathlib import Path

import requests

from carta.config import find_config, load_config
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

    # 5. Extract query
    query = _extract_query(prompt, cfg)

    # 6. Search
    try:
        hits = run_search(query, cfg)
    except Exception as e:
        print(f"carta-hook: search error (fail-open): {e}", file=sys.stderr)
        sys.exit(0)

    # 7. Cap results
    hits = hits[:max_results]

    # 8. Noise gate
    if not hits or hits[0]["score"] < low_threshold:
        sys.exit(0)

    # 9. Fast-path inject
    if hits[0]["score"] > high_threshold:
        _inject(hits)
        return

    # 10. Gray zone — call Ollama judge with timeout
    verdict = _judge_with_timeout(prompt, hits, cfg, judge_timeout_s)
    if verdict:
        _inject(hits)
    else:
        sys.exit(0)


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

def _call_ollama_judge(prompt: str, hits: list[dict], cfg: dict) -> bool:
    """Call Ollama to judge whether the documentation candidates are relevant.

    Returns True if the model answers with 'yes' (case-insensitive prefix match).
    Returns False on any error or non-yes response.
    """
    ollama_url = cfg["embed"]["ollama_url"]
    model = cfg["proactive_recall"]["ollama_model"]

    excerpts = "\n---\n".join(h["excerpt"][:200] for h in hits)
    user_msg = (
        f"Prompt: {prompt[:300]}\n\n"
        f"Documentation candidates:\n{excerpts}\n\n"
        f"Are any of these relevant?"
    )

    try:
        resp = requests.post(
            f"{ollama_url}/api/chat",
            json={
                "model": model,
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "You decide if documentation is relevant to a coding prompt. "
                            "Answer only 'yes' or 'no'."
                        ),
                    },
                    {"role": "user", "content": user_msg},
                ],
                "stream": False,
            },
            timeout=4,
        )
        answer = resp.json()["message"]["content"].strip().lower()
        return answer.startswith("yes")
    except Exception as e:
        print(f"carta-hook: judge error (fail-open): {e}", file=sys.stderr)
        return False


def _judge_with_timeout(
    prompt: str, hits: list[dict], cfg: dict, timeout_s: int
) -> bool:
    """Run Ollama judge in a thread; return True on timeout (fail-open per HOOK-05), False on other errors."""
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(_call_ollama_judge, prompt, hits, cfg)
        try:
            return future.result(timeout=timeout_s)
        except concurrent.futures.TimeoutError:
            print(
                f"carta-hook: judge timeout after {timeout_s}s (fail-open)",
                file=sys.stderr,
            )
            return True
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
        context_lines.append(
            f"**Source: {h['source']} (score: {h['score']:.2f})**\n"
            f"> {h['excerpt'][:200]}\n"
        )
    context_text = "\n".join(context_lines)

    output = json.dumps({"context": context_text})
    sys.__stdout__.write(output)
    sys.__stdout__.flush()
