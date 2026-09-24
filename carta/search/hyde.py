"""HyDE — blend a caller-written hypothetical answer into the dense query vector.

A short question and a long passage sit in different regions of embedding space. A
hypothetical answer — it need not be correct — is shaped like the passage, so blending it
in pulls the dense lane toward documents that read like the answer. Measured on the
84-query ET-embed set: +0.24 MRR on the dense-only MCP path with Claude-written
hypotheticals, while a local 9b writer did not help — so Carta never writes one itself;
the caller (Claude, via MCP or the /doc-search skill) does.
See docs/superpowers/specs/2026-09-21-hyde-hypothetical-query-design.md.
"""
from __future__ import annotations

import math

from carta.embed.embed import get_embedding

# A hypothetical is shaped like a document, so it is embedded as one.
HYPOTHETICAL_PREFIX = "search_document: "


def usable(hypothetical: str | None) -> bool:
    """True when there is an actual passage to blend (None / blank means no HyDE)."""
    return bool(hypothetical and hypothetical.strip())


def _unit(v: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def blend(query_vec: list[float], hyp_vec: list[float]) -> list[float]:
    """Equal-weight mean of the two unit vectors, renormalised.

    Keeping the question in the vector bounds the downside of an off-topic hypothetical:
    with a weak (local 9b) writer this was the only blend whose effect did not go negative.
    """
    q, h = _unit(query_vec), _unit(hyp_vec)
    return _unit([a + b for a, b in zip(q, h)])


def embed_hypothetical(text: str, ollama_url: str, model: str,
                       timeout: float | None = None) -> list[float]:
    """Embed a hypothetical answer with the document prefix.

    ``timeout`` is omitted rather than forwarded as None: requests treats timeout=None as
    "wait forever", which would remove get_embedding's ceiling (issue #106).
    """
    kwargs = {"ollama_url": ollama_url, "model": model, "prefix": HYPOTHETICAL_PREFIX}
    if timeout is not None:
        kwargs["timeout"] = timeout
    return get_embedding(text, **kwargs)
