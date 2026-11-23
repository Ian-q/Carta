"""A small invented corpus of past sessions, indexed into an in-memory Qdrant.

The embedder is a bag-of-words hash, so cosine similarity tracks shared words and
recall tests can assert a ranking without Ollama. Every text here is made up.
"""
from __future__ import annotations

import hashlib
import math
import re
import time
from contextlib import contextmanager
from unittest.mock import patch

from qdrant_client import QdrantClient

from carta.config import DEFAULTS, _deep_merge
from carta.embed.sparse import SparseVec
from carta.sessions import index as sidx
from carta.sessions.project import transcript_slug
from carta.sessions.tests.synth import Transcript, standard_session

DIM = 768
DAY = 24 * 60  # minutes


def _words(text: str) -> list[str]:
    out = []
    for w in re.findall(r"[a-z0-9]+", text.lower()):
        if len(w) < 3:
            continue
        out.append(w[:-1] if w.endswith("s") and len(w) > 3 else w)
    return out


def bow_embedding(text, **kwargs):
    v = [0.0] * DIM
    for w in _words(text):
        h = int(hashlib.md5(w.encode()).hexdigest()[:8], 16)
        v[h % DIM] += 1.0 if (h >> 16) & 1 else -1.0
    n = math.sqrt(sum(x * x for x in v))
    if not n:
        v[0], n = 1.0, 1.0
    return [x / n for x in v]


def bow_sparse(text, model_name=None):
    counts: dict[int, float] = {}
    for w in _words(text):
        i = int(hashlib.md5(w.encode()).hexdigest()[:6], 16)
        counts[i] = counts.get(i, 0.0) + 1.0
    idx = sorted(counts)
    return SparseVec(indices=idx, values=[counts[i] for i in idx])


def make_cfg(root, tmp_path, **sessions) -> dict:
    cfg = _deep_merge(DEFAULTS, {
        "project_name": "widgets",
        "qdrant_url": "http://unused.invalid:6333",
        "sessions": {"transcripts_dir": str(tmp_path / "projects"),
                     "state_dir": str(tmp_path / "state"), **sessions},
    })
    cfg["embed"]["embedding_workers"] = 1
    return cfg


def sessions(root) -> list[Transcript]:
    """Four sessions in three worktrees and the main checkout, a day or more apart."""
    root = str(root)
    out = [standard_session(root, session_id="sess-counter", branch="feat/widgets")]

    tr = Transcript("sess-exporter", f"{root}/.worktrees/exporter",
                    branch="fix/exporter-retry", start_minutes=2 * DAY)
    tr.prompt("The CSV exporter keeps timing out on large files. Add retries with backoff, "
              "see issue #61")
    tr.tool("Edit", {"file_path": f"{root}/.worktrees/exporter/src/export.py"}, "e1")
    tr.say("Added three retries with exponential backoff around the CSV export upload.")
    out.append(tr)

    tr = Transcript("sess-theme", f"{root}/.worktrees/theme", branch="fix/dark-mode",
                    start_minutes=3 * DAY)
    tr.prompt("Colours on the settings page look wrong in dark mode, fix the palette")
    tr.say("Fixed the dark mode palette for the settings page; PR #70 is open.")
    out.append(tr)

    tr = Transcript("sess-readme", root, branch="main", start_minutes=5 * DAY)
    tr.prompt("Update the README install section for the new release")
    tr.say("README install steps updated.")
    out.append(tr)
    return out


def write_all(cfg, transcripts) -> list:
    tdir = cfg["sessions"]["transcripts_dir"]
    from pathlib import Path
    return [tr.write(Path(tdir) / transcript_slug(tr.cwd) / f"{tr.session_id}.jsonl")
            for tr in transcripts]


@contextmanager
def utc_tz(monkeypatch):
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    try:
        yield
    finally:
        monkeypatch.delenv("TZ", raising=False)
        time.tzset()


def build_index(cfg, root, client: QdrantClient | None = None, *, hybrid: bool = False,
                transcripts=None) -> QdrantClient:
    """Write the corpus transcripts and index them; returns the client."""
    client = client or QdrantClient(":memory:")
    write_all(cfg, transcripts if transcripts is not None else sessions(root))
    with patch("carta.embed.embed.get_embedding", side_effect=bow_embedding), \
         patch("carta.embed.embed._fastembed_available", return_value=hybrid), \
         patch("carta.embed.sparse.embed_sparse_document", side_effect=bow_sparse):
        s = sidx.run_index(cfg, root, client=client, backfill=True, verbose=False)
    assert s["indexed"] >= 1, s
    return client
