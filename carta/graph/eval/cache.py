"""A content-addressed cache for the replay eval.

Keys are git blob ids, so an entry is stale only when the code that read the
blob changes: the ``fingerprint`` column covers the extractor and its
settings, the ``version`` column covers the basename tokenizer. The cache is
an optimisation and never a requirement: if the file cannot be opened or
written, the run continues in memory.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import zlib
from collections import OrderedDict
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from carta.graph.model import Candidate

_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS scan (blob TEXT NOT NULL, fingerprint TEXT NOT NULL, "
    "payload BLOB NOT NULL, PRIMARY KEY (blob, fingerprint)) WITHOUT ROWID",
    "CREATE TABLE IF NOT EXISTS tokens (blob TEXT NOT NULL, version INTEGER NOT NULL, "
    "payload BLOB NOT NULL, PRIMARY KEY (blob, version)) WITHOUT ROWID",
)


def default_cache_path(repo: Path) -> Path:
    """``<carta home>/cache/graph-replay/<hash of the repository's absolute path>.db``."""
    from carta.registry import carta_home

    key = hashlib.sha256(str(Path(repo).resolve()).encode("utf-8")).hexdigest()[:16]
    return carta_home() / "cache" / "graph-replay" / f"{key}.db"


# The memory layer is small on purpose: a long history has tens of thousands
# of doc blobs, and the walk only ever needs the ones it is looking at.
MEMORY_ENTRIES = 512


class _Recent(OrderedDict):
    """A bounded mapping that forgets its oldest entry."""

    def put(self, key, value) -> None:
        self[key] = value
        self.move_to_end(key)
        while len(self) > MEMORY_ENTRIES:
            self.popitem(last=False)


def _pack(obj) -> bytes:
    text = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    return zlib.compress(text.encode("utf-8", "surrogatepass"), 6)


def _unpack(payload):
    return json.loads(zlib.decompress(payload).decode("utf-8", "surrogatepass"))


class ReplayCache:
    """Stage-1 scan results and basename token counts, by blob id."""

    def __init__(self, path: Optional[Path] = None) -> None:
        self._scan = _Recent()          # (blob, fingerprint) -> candidates
        self._tokens = _Recent()        # (blob, version) -> token counts
        self._conn: Optional[sqlite3.Connection] = None
        self.hits = 0
        self.misses = 0
        if path is not None:
            try:
                Path(path).parent.mkdir(parents=True, exist_ok=True)
                conn = sqlite3.connect(str(path))
                for statement in _SCHEMA:
                    conn.execute(statement)
                conn.commit()
                self._conn = conn
            except (sqlite3.Error, OSError):
                self._conn = None

    @property
    def on_disk(self) -> bool:
        return self._conn is not None

    def __enter__(self) -> "ReplayCache":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        conn, self._conn = self._conn, None
        if conn is None:
            return
        try:
            conn.commit()
            conn.close()
        except sqlite3.Error:
            pass

    def _load(self, table: str, column: str, blob: str, key):
        if self._conn is None:
            return None
        try:
            row = self._conn.execute(
                f"SELECT payload FROM {table} WHERE blob = ? AND {column} = ?",
                (blob, key)).fetchone()
            return _unpack(row[0]) if row else None
        except (sqlite3.Error, zlib.error, ValueError, TypeError):
            return None

    def _save(self, table: str, blob: str, key, obj) -> None:
        if self._conn is None:
            return
        try:
            self._conn.execute(f"INSERT OR REPLACE INTO {table} VALUES (?, ?, ?)",
                               (blob, key, _pack(obj)))
        except sqlite3.Error:
            self._conn = None           # a cache that cannot be written is not worth a failure

    def get_scan(self, blob: str, fingerprint: str) -> Optional[List[Candidate]]:
        hit = self._scan.get((blob, fingerprint))
        if hit is None:
            rows = self._load("scan", "fingerprint", blob, fingerprint)
            if rows is not None:
                try:
                    hit = [Candidate(str(r[0]), str(r[1]), int(r[2]), str(r[3]), bool(r[4]),
                                     str(r[5])) for r in rows]
                except (TypeError, ValueError, IndexError):
                    hit = None
                if hit is not None:
                    self._scan.put((blob, fingerprint), hit)
        if hit is None:
            self.misses += 1
        else:
            self.hits += 1
        return hit

    def put_scan(self, blob: str, fingerprint: str, candidates: Sequence[Candidate]) -> None:
        self._scan.put((blob, fingerprint), list(candidates))
        self._save("scan", blob, fingerprint,
                   [[c.raw, c.ctx, c.line, c.evidence, c.single, c.pattern] for c in candidates])

    def get_tokens(self, blob: str, version: int) -> Optional[Dict[str, int]]:
        hit = self._tokens.get((blob, version))
        if hit is None:
            loaded = self._load("tokens", "version", blob, version)
            if isinstance(loaded, dict):
                hit = loaded
                self._tokens.put((blob, version), hit)
        if hit is None:
            self.misses += 1
        else:
            self.hits += 1
        return hit

    def put_tokens(self, blob: str, version: int, tokens: Dict[str, int]) -> None:
        self._tokens.put((blob, version), dict(tokens))
        self._save("tokens", blob, version, tokens)
