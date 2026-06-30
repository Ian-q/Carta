"""Shared Ollama yes/no judge, used by the proactive-recall hook and the
stale-reference scan. Returns None on any error so callers can fail open."""
from __future__ import annotations

import json
import requests

from carta.config import ollama_keep_alive


def ollama_yesno(
    ollama_url: str,
    model: str,
    system: str,
    user: str,
    *,
    timeout_s: float = 4,
) -> bool | None:
    """Ask Ollama a yes/no question via /api/chat.

    Returns True if the answer begins with 'yes', False for any other answer,
    and None on any network/parse error (so the caller can fail open).
    """
    try:
        resp = requests.post(
            f"{ollama_url}/api/chat",
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "stream": False,
                "keep_alive": ollama_keep_alive(),
            },
            timeout=timeout_s,
        )
        answer = resp.json()["message"]["content"].strip().lower()
        return answer.startswith("yes")
    except Exception:
        return None


def _extract_json(content) -> dict | None:
    """Parse a JSON object from model output, tolerating prose/markdown wrapping.
    Returns a dict, or None if no JSON object can be parsed."""
    if not isinstance(content, str):
        return None
    try:
        obj = json.loads(content)
        return obj if isinstance(obj, dict) else None
    except (ValueError, TypeError):
        pass
    start = content.find("{")
    while start != -1:
        depth = 0
        for i in range(start, len(content)):
            if content[i] == "{":
                depth += 1
            elif content[i] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(content[start:i + 1])
                        return obj if isinstance(obj, dict) else None
                    except ValueError:
                        break
        start = content.find("{", start + 1)
    return None


def ollama_json(ollama_url, model, system, user, *, timeout_s: float = 20, schema=None) -> dict | None:
    """Ask Ollama for a JSON object via /api/chat (format-constrained).

    Returns the parsed dict, or None on any network/parse/format error so the
    caller can fail open.
    """
    try:
        resp = requests.post(
            f"{ollama_url}/api/chat",
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "stream": False,
                "format": schema if schema else "json",
                "keep_alive": ollama_keep_alive(),
            },
            timeout=timeout_s,
        )
        return _extract_json(resp.json()["message"]["content"])
    except Exception:
        return None
