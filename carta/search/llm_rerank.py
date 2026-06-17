"""Listwise LLM reranker via a single Ollama /api/chat call.

Reorders the fused candidate pool by LLM-judged relevance to the query and
truncates to top_n. Fail-open: any error/timeout/parse failure returns the
input order unchanged (never worse than no rerank). Local Ollama only.
"""
from __future__ import annotations

import json
import re
import sys
import requests

from carta.config import ollama_keep_alive

_SYSTEM = (
    "You rank document passages by relevance to a search query. "
    "Return ONLY a JSON array of passage numbers, most relevant first. "
    "Include only clearly relevant passages."
)


def _build_prompt(query: str, hits: list[dict], max_excerpt_chars: int) -> str:
    lines = [f"Query: {query}", "", "Passages:"]
    for i, h in enumerate(hits):
        excerpt = (h.get("excerpt", "") or "")[:max_excerpt_chars].replace("\n", " ")
        lines.append(f"[{i}] {h.get('source', '')}: {excerpt}")
    lines.append("")
    lines.append("Return a JSON array of the passage numbers, most relevant first.")
    return "\n".join(lines)


def _parse_order(content: str, n: int) -> list[int]:
    """Parse the model reply into a de-duplicated list of valid in-range indices.

    Tolerant of small-model noise: a valid JSON array followed by trailing text,
    or wrapped in leading prose. Returns [] (→ fail-open) when no array is found.
    """
    content = (content or "").strip()
    try:
        data, _end = json.JSONDecoder().raw_decode(content)
    except json.JSONDecodeError:
        m = re.search(r"\[.*?\]", content, re.DOTALL)
        if not m:
            return []
        try:
            data = json.loads(m.group(0))
        except json.JSONDecodeError:
            return []
    if isinstance(data, dict):
        data = next((v for v in data.values() if isinstance(v, list)), [])
    if not isinstance(data, list):
        return []
    order: list[int] = []
    seen = set()
    for x in data:
        try:
            i = int(x)
        except (TypeError, ValueError):
            continue
        if 0 <= i < n and i not in seen:
            seen.add(i)
            order.append(i)
    return order


def llm_rerank_hits(query: str, hits: list[dict], *, model: str, ollama_url: str,
                    top_n: int, timeout_s: int = 20, max_excerpt_chars: int = 500) -> list[dict]:
    """Reorder *hits* by a single listwise Ollama call; return top_n. Fail-open."""
    if not hits or not query.strip():
        return hits[:top_n]
    try:
        resp = requests.post(
            f"{ollama_url}/api/chat",
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": _SYSTEM},
                    {"role": "user", "content": _build_prompt(query, hits, max_excerpt_chars)},
                ],
                "stream": False,
                "format": "json",
                # The default llm_model (qwen3.5:0.8b) is a reasoning model. Without
                # this, its answer streams to message.thinking and message.content
                # stays empty (or it thinks to the context limit and times out), so the
                # reranker silently fails open. A listwise reranker never needs to think.
                "think": False,
                "options": {"temperature": 0},
                "keep_alive": ollama_keep_alive(),
            },
            timeout=timeout_s,
        )
        resp.raise_for_status()
        content = resp.json()["message"]["content"]
        order = _parse_order(content, len(hits))
    except Exception as exc:  # fail-open — never worse than the fused order
        print(f"llm_rerank: fail-open ({type(exc).__name__}: {exc})", file=sys.stderr, flush=True)
        return hits[:top_n]

    if not order:
        return hits[:top_n]
    ranked = [hits[i] for i in order]
    remainder = [h for j, h in enumerate(hits) if j not in set(order)]
    merged = ranked + remainder
    for rank, h in enumerate(merged):
        h["rerank_score"] = float(len(merged) - rank)
    return merged[:top_n]
