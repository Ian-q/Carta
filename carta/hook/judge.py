"""Shared Ollama yes/no judge, used by the proactive-recall hook and the
stale-reference scan. Returns None on any error so callers can fail open."""
from __future__ import annotations

import requests


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
            },
            timeout=timeout_s,
        )
        answer = resp.json()["message"]["content"].strip().lower()
        return answer.startswith("yes")
    except Exception:
        return None
