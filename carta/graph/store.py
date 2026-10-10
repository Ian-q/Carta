"""The edge store: one SQLite file holding every edge the graph arm knows.

The store is a derived cache of the repository. Deleting it and rebuilding
reproduces the same ``edges_digest()``. There are no migrations: a file with
an older schema is rebuilt, and a file written by a newer Carta is left
alone and reported.

The file lives in the repository's own ``.carta/``, so a repository can ship
one. Nothing read back from it is trusted: the schema must be exactly the one
this module creates (no trigger, view or extra index can ride along), and
every row must be one this module could have written, checked where it is
read. A store that fails either is not a store: a build replaces it, and a
reader that must not write raises ``StoreUnreadableError``.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path
from typing import Dict, FrozenSet, Iterator, List, Optional, Sequence

from carta.graph.limits import (
    MAX_STORE_BYTES, MAX_STORED_EDGE_META_LENGTH, MAX_STORED_TEXT_LENGTH,
)
from carta.graph.model import (
    CONTROL_CHARS, DST_TYPES, KINDS, SCHEMA_VERSION, SRC_TYPES, Edge,
)
from carta.graph.safeio import create_exclusive, is_symlink

_SCHEMA = """
CREATE TABLE meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
) WITHOUT ROWID;

CREATE TABLE sources (
  extractor         TEXT    NOT NULL,
  path              TEXT    NOT NULL,
  sha256            TEXT    NOT NULL,
  extractor_version INTEGER NOT NULL,
  meta              TEXT    NOT NULL DEFAULT '{}',
  PRIMARY KEY (extractor, path)
) WITHOUT ROWID;

CREATE TABLE edges (
  id                INTEGER PRIMARY KEY,
  kind              TEXT    NOT NULL,
  src               TEXT    NOT NULL,
  src_type          TEXT    NOT NULL DEFAULT 'doc',
  dst               TEXT    NOT NULL,
  dst_type          TEXT    NOT NULL,
  line              INTEGER NOT NULL DEFAULT 0,
  key               TEXT    NOT NULL DEFAULT '',
  evidence          TEXT    NOT NULL DEFAULT '',
  weight            REAL    NOT NULL DEFAULT 1.0,
  meta              TEXT    NOT NULL DEFAULT '{}',
  extractor         TEXT    NOT NULL,
  extractor_version INTEGER NOT NULL,
  UNIQUE (kind, src, dst, line, key)
);
CREATE INDEX edges_dst ON edges (dst, kind);
CREATE INDEX edges_src ON edges (src, kind);
CREATE INDEX edges_kind_type ON edges (kind, dst_type);
"""

_EDGE_COLUMNS = ("kind, src, src_type, dst, dst_type, line, key, evidence, weight, meta, "
                 "extractor, extractor_version")
_EDGE_ORDER = "ORDER BY kind, src, dst, line, key"


class StoreVersionError(RuntimeError):
    """The store on disk was written by a newer Carta than this one."""


class StoreUnreadableError(RuntimeError):
    """The file is there but is not a store this Carta can read (damaged, or not SQLite)."""


class StoreUnsafeError(StoreUnreadableError):
    """The store is one Carta will not open at all, and will not replace either.

    It, or a directory of its own, is a symbolic link (never followed), or it
    is larger than ``MAX_STORE_BYTES``.
    """


_SIDECARS = ("", "-journal", "-wal", "-shm")


def _refuse_symlinks(db_path: Path) -> None:
    """Raise ``StoreUnsafeError`` if opening ``db_path`` would go through a symbolic link.

    A repository can ship ``.carta/graph``, the database or a file beside it
    as a link to anywhere, and SQLite (and ``mkdir``, and a plain write)
    would follow it. Checked: the database and the files SQLite keeps beside
    it, and the store's own two directories when the path is laid out as
    ``store_path`` lays it out. The repository root is the user's choice and
    is not checked.
    """
    path = Path(db_path)
    suspects = [Path(str(path) + suffix) for suffix in _SIDECARS]
    parent = path.parent
    if parent.name == "graph" and parent.parent.name == ".carta":
        suspects += [parent, parent.parent]
    for suspect in suspects:
        if is_symlink(suspect):
            raise StoreUnsafeError(
                f"{suspect} is a symbolic link; the edge store is never read or written "
                f"through one. Remove the link and run `carta graph build`.")


def _refuse_oversize(db_path: Path) -> None:
    """Raise ``StoreUnsafeError`` if the store and its journal are over ``MAX_STORE_BYTES``.

    Checked before SQLite opens the file: a read holds what it reads, so the
    file's size is the bound on it. A store that large is not replaced
    either; the same repository would only grow it again.
    """
    total = 0
    for suffix in _SIDECARS:
        try:
            total += os.lstat(str(db_path) + suffix).st_size
        except OSError:
            continue
    if total > MAX_STORE_BYTES:
        raise StoreUnsafeError(
            f"{db_path} is larger than {MAX_STORE_BYTES} bytes; an edge store that large "
            f"is not opened. Delete it and run `carta graph build`.")


def store_path(repo_root: Path) -> Path:
    """Where a repository's edge store lives."""
    return Path(repo_root) / ".carta" / "graph" / "edges.db"


# -- what a row may hold ----------------------------------------------------------
#
# SQLite types are per value, not per column, and a file can be written by
# anything: a column declared TEXT can come back as bytes, REAL as a string
# or an infinity. Each reader below returns what the writer above it wrote, or
# raises ``_BadRow``.

class _BadRow(ValueError):
    """A row this module did not write."""


_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_.:-]{0,127}")
_PATH_TARGETS = frozenset(("file", "doc", "dir", "missing"))
_MAX_LINE = 2 ** 31


def _text(value, limit: int = MAX_STORED_TEXT_LENGTH) -> str:
    if not isinstance(value, str) or len(value) > limit or "\0" in value:
        raise _BadRow("text")
    return value


def _name(value) -> str:
    if not isinstance(value, str) or _NAME.fullmatch(value) is None:
        raise _BadRow("name")
    return value


def _count(value) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= _MAX_LINE:
        raise _BadRow("number")
    return value


def _repo_path(value, directory: bool = False) -> str:
    """A repo-relative POSIX path: never absolute, never climbing out."""
    text = _text(value)
    body = text[:-1] if directory and text.endswith("/") else text
    if not body or any(part in ("", ".", "..") for part in body.split("/")):
        raise _BadRow("path")
    return text


def _is_object(text: str) -> bool:
    if text == "{}":
        return True
    try:
        return isinstance(json.loads(text), dict)
    except (ValueError, RecursionError):
        return False


def _row_to_edge(row: tuple) -> Edge:
    (kind, src, src_type, dst, dst_type, line, key, evidence, weight, meta,
     extractor, extractor_version) = row
    if kind not in KINDS or src_type not in SRC_TYPES or dst_type not in DST_TYPES:
        raise _BadRow("type")
    if (CONTROL_CHARS.search(_text(evidence)) or not isinstance(weight, float)
            or not math.isfinite(weight) or weight < 0.0):
        raise _BadRow("value")
    if not _is_object(_text(meta, MAX_STORED_EDGE_META_LENGTH)):
        raise _BadRow("meta")
    dst = _repo_path(dst, directory=True) if dst_type in _PATH_TARGETS else _text(dst)
    return Edge(kind=kind, src=_repo_path(src), dst=dst, dst_type=dst_type, line=_count(line),
                key=_text(key), evidence=evidence, weight=weight, meta=meta, src_type=src_type,
                extractor=_name(extractor), extractor_version=_count(extractor_version))


def _source_row(row: tuple) -> tuple:
    extractor, path, sha256, extractor_version, meta = row
    return (_name(extractor), _repo_path(path), _text(sha256), _count(extractor_version),
            _text(meta, MAX_STORE_BYTES))


def _meta_row(row: tuple) -> tuple:
    key, value = row
    return _name(key), _text(value, MAX_STORE_BYTES)


def _schema_rows(con: sqlite3.Connection) -> FrozenSet[tuple]:
    return frozenset(con.execute("SELECT type, name, tbl_name, sql FROM sqlite_master"))


@lru_cache(maxsize=1)
def _expected_schema() -> FrozenSet[tuple]:
    """``sqlite_master`` exactly as ``_SCHEMA`` leaves it."""
    con = sqlite3.connect(":memory:", isolation_level=None)
    try:
        EdgeStore._create_schema(con)
        return _schema_rows(con)
    finally:
        con.close()


def _edge_to_row(edge: Edge) -> tuple:
    return (edge.kind, edge.src, edge.src_type, edge.dst, edge.dst_type, edge.line, edge.key,
            edge.evidence, float(edge.weight), edge.meta, edge.extractor, edge.extractor_version)


def _in_clause(column: str, values: Sequence[str]) -> str:
    return f"{column} IN ({','.join('?' * len(values))})"


class EdgeStore:
    """Read and write access to one edge store file."""

    def __init__(self, db_path: Path) -> None:
        self.path = Path(db_path)
        self._depth = 0
        self._con = None  # type: ignore[assignment]
        _refuse_symlinks(self.path)
        _refuse_oversize(self.path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        _refuse_symlinks(self.path)
        # Created only if the name is free, and never through a link.
        create_exclusive(self.path.parent / ".gitignore", "*\n")
        self._con = self._open()

    @classmethod
    def open_existing(cls, db_path: Path) -> Optional["EdgeStore"]:
        """Open the store only if the file is already there; never create anything."""
        _refuse_symlinks(Path(db_path))
        if not Path(db_path).is_file():
            return None
        return cls(db_path)

    @classmethod
    def open_readonly(cls, db_path: Path) -> Optional["EdgeStore"]:
        """Open an existing store for reading, and never write: not the file, not beside it.

        ``None`` when there is no file. Raises ``StoreUnreadableError`` for a
        file that is not a usable store (where a normal open would rebuild it)
        and ``StoreVersionError`` for one written by a newer Carta. A file
        holding a row this module did not write is not a usable store.
        """
        path = Path(db_path)
        _refuse_symlinks(path)
        if not path.is_file():
            return None
        _refuse_oversize(path)
        con = None
        try:
            con = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5.0,
                                  isolation_level=None)
            version = con.execute("PRAGMA user_version").fetchone()[0]
            schema = _schema_rows(con)
        except sqlite3.Error as exc:
            if con is not None:
                con.close()
            raise StoreUnreadableError(f"{path} is not a readable edge store: {exc}") from exc
        if version > SCHEMA_VERSION:
            con.close()
            raise StoreVersionError(
                f"{path} was written by a newer Carta (schema {version}, this version "
                f"reads {SCHEMA_VERSION}). Upgrade Carta, or delete the file to rebuild it.")
        if version != SCHEMA_VERSION or schema != _expected_schema():
            con.close()
            raise StoreUnreadableError(
                f"{path} is not an edge store this Carta can read (schema {version})")
        try:
            intact = cls._rows_intact(con)
        except sqlite3.DatabaseError as exc:
            con.close()
            raise StoreUnreadableError(f"{path} is not a readable edge store: {exc}") from exc
        if not intact:
            con.close()
            raise StoreUnreadableError(
                f"{path} holds a row Carta did not write; it is not read")
        store = cls.__new__(cls)
        store.path = path
        store._depth = 0
        store._con = con
        return store

    # -- opening ----------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        # isolation_level=None: no implicit transactions on any supported
        # Python; BEGIN IMMEDIATE / COMMIT / ROLLBACK are issued explicitly.
        con = sqlite3.connect(str(self.path), timeout=5.0, isolation_level=None)
        con.execute("PRAGMA busy_timeout = 5000")
        return con

    def _open(self) -> sqlite3.Connection:
        con = None
        try:
            con = self._connect()
            version = con.execute("PRAGMA user_version").fetchone()[0]
            schema = _schema_rows(con)
            # A store that will be written is read in full first: a row this
            # module did not write means none of the file is to be believed.
            intact = (version == SCHEMA_VERSION and schema == _expected_schema()
                      and self._rows_intact(con))
        except sqlite3.DatabaseError:
            if con is not None:
                con.close()
            return self._recreate()
        if version > SCHEMA_VERSION:
            con.close()
            raise StoreVersionError(
                f"{self.path} was written by a newer Carta (schema {version}, this version "
                f"reads {SCHEMA_VERSION}). Upgrade Carta, or delete the file to rebuild it.")
        if intact:
            return con
        if not schema and version == 0:
            self._create_schema(con)
            return con
        con.close()
        return self._recreate()

    @staticmethod
    def _rows_intact(con: sqlite3.Connection) -> bool:
        """True when every row of every table is one this module could have written."""
        try:
            for row in con.execute(f"SELECT {_EDGE_COLUMNS} FROM edges"):
                _row_to_edge(row)
            for row in con.execute(
                    "SELECT extractor, path, sha256, extractor_version, meta FROM sources"):
                _source_row(row)
            for row in con.execute("SELECT key, value FROM meta"):
                _meta_row(row)
        except _BadRow:
            return False
        return True

    def _recreate(self) -> sqlite3.Connection:
        _refuse_symlinks(self.path)
        for suffix in _SIDECARS:
            stale = str(self.path) + suffix
            if os.path.lexists(stale):
                os.unlink(stale)
        con = self._connect()
        self._create_schema(con)
        return con

    @staticmethod
    def _create_schema(con: sqlite3.Connection) -> None:
        con.execute("BEGIN IMMEDIATE")
        try:
            for statement in _SCHEMA.split(";"):
                if statement.strip():
                    con.execute(statement)
            con.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            con.execute("COMMIT")
        except BaseException:
            con.execute("ROLLBACK")
            raise

    def close(self) -> None:
        if self._con is not None:
            self._con.close()
            self._con = None  # type: ignore[assignment]

    def __enter__(self) -> "EdgeStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    @property
    def schema_version(self) -> int:
        return self._con.execute("PRAGMA user_version").fetchone()[0]

    # -- transactions -----------------------------------------------------

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """One ``BEGIN IMMEDIATE`` transaction. Nested uses join the outer one."""
        if self._depth:
            self._depth += 1
            try:
                yield
            finally:
                self._depth -= 1
            return
        self._con.execute("BEGIN IMMEDIATE")
        self._depth = 1
        try:
            yield
        except BaseException:
            self._depth = 0
            self._con.execute("ROLLBACK")
            raise
        else:
            self._depth = 0
            self._con.execute("COMMIT")

    @contextmanager
    def _reading(self) -> Iterator[None]:
        """Turn a row this module did not write, or a damaged page, into one error."""
        try:
            yield
        except _BadRow:
            raise StoreUnreadableError(
                f"{self.path} holds a row Carta did not write; it is not read") from None
        except sqlite3.OperationalError:
            raise                           # busy or locked: nothing is wrong with the file
        except sqlite3.DatabaseError as exc:
            raise StoreUnreadableError(f"{self.path} is not a readable edge store: {exc}") from exc

    # -- meta -------------------------------------------------------------

    def get_meta(self, key: str) -> Optional[str]:
        with self._reading():
            row = self._con.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
            return _text(row[0], MAX_STORE_BYTES) if row else None

    def set_meta(self, key: str, value: str) -> None:
        with self.transaction():
            self._con.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) "
                "ON CONFLICT (key) DO UPDATE SET value = excluded.value", (key, value))

    # -- sources ----------------------------------------------------------

    def source_hashes(self, extractor: str) -> Dict[str, str]:
        """``path -> sha256`` for every source an extractor has written."""
        with self._reading():
            return {_repo_path(path): _text(sha256) for path, sha256 in self._con.execute(
                "SELECT path, sha256 FROM sources WHERE extractor = ? ORDER BY path",
                (extractor,))}

    def source_meta(self, extractor: str) -> Dict[str, str]:
        """``path -> canonical JSON`` of per-source state for an extractor."""
        with self._reading():
            return {_repo_path(path): _text(meta, MAX_STORE_BYTES) for path, meta in
                    self._con.execute(
                        "SELECT path, meta FROM sources WHERE extractor = ? ORDER BY path",
                        (extractor,))}

    def set_source_meta(self, extractor: str, path: str, meta: str) -> None:
        """Update a source's state without touching its edges (creates the row if absent)."""
        with self.transaction():
            self._con.execute(
                "INSERT INTO sources (extractor, path, sha256, extractor_version, meta) "
                "VALUES (?, ?, '', 0, ?) "
                "ON CONFLICT (extractor, path) DO UPDATE SET meta = excluded.meta",
                (extractor, path, meta))

    def source_edges(self, extractor: str, path: str) -> List[Edge]:
        """The edges one source wrote, in store order."""
        with self._reading():
            rows = self._con.execute(
                f"SELECT {_EDGE_COLUMNS} FROM edges WHERE extractor = ? AND src = ? "
                f"{_EDGE_ORDER}", (extractor, path))
            return [_row_to_edge(r) for r in rows]

    def replace_source(self, extractor: str, path: str, sha256: str, extractor_version: int,
                       edges: Sequence[Edge], meta: str = "{}") -> None:
        """Replace everything one source wrote: its edges and its bookkeeping row.

        Every edge must have ``src == path`` and ``extractor == extractor``. If
        two edges share an identity ``(kind, src, dst, line, key)`` the last
        one wins.
        """
        for edge in edges:
            if edge.src != path or edge.extractor != extractor:
                raise ValueError(
                    f"edge {edge.identity} does not belong to source ({extractor}, {path})")
        unique = {edge.identity: edge for edge in edges}
        with self.transaction():
            self._con.execute(
                "DELETE FROM edges WHERE extractor = ? AND src = ?", (extractor, path))
            self._con.executemany(
                f"INSERT INTO edges ({_EDGE_COLUMNS}) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                [_edge_to_row(unique[k]) for k in sorted(unique)])
            self._con.execute(
                "INSERT INTO sources (extractor, path, sha256, extractor_version, meta) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT (extractor, path) DO UPDATE SET "
                "sha256 = excluded.sha256, extractor_version = excluded.extractor_version, "
                "meta = excluded.meta", (extractor, path, sha256, extractor_version, meta))

    def remove_source(self, extractor: str, path: str) -> None:
        with self.transaction():
            self._con.execute(
                "DELETE FROM edges WHERE extractor = ? AND src = ?", (extractor, path))
            self._con.execute(
                "DELETE FROM sources WHERE extractor = ? AND path = ?", (extractor, path))

    def clear_extractor(self, extractor: str) -> None:
        with self.transaction():
            self._con.execute("DELETE FROM edges WHERE extractor = ?", (extractor,))
            self._con.execute("DELETE FROM sources WHERE extractor = ?", (extractor,))

    # -- reads ------------------------------------------------------------

    def iter_edges(self, kinds: Optional[Sequence[str]] = None,
                   dst_types: Optional[Sequence[str]] = None,
                   extractors: Optional[Sequence[str]] = None) -> Iterator[Edge]:
        """Edges in the one canonical order: kind, src, dst, line, key.

        Each filter is ``None`` for "no filter"; an empty sequence matches nothing.
        """
        clauses, params = [], []
        for column, values in (("kind", kinds), ("dst_type", dst_types), ("extractor", extractors)):
            if values is None:
                continue
            values = list(values)
            if not values:
                return
            clauses.append(_in_clause(column, values))
            params.extend(values)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._reading():
            rows = self._con.execute(
                f"SELECT {_EDGE_COLUMNS} FROM edges {where} {_EDGE_ORDER}", params).fetchall()
            for row in rows:
                yield _row_to_edge(row)

    def counts(self) -> dict:
        con = self._con
        with self._reading():
            by_kind = dict(con.execute(
                "SELECT kind, COUNT(*) FROM edges GROUP BY kind ORDER BY kind"))
            by_dst_type = dict(con.execute(
                "SELECT dst_type, COUNT(*) FROM edges GROUP BY dst_type ORDER BY dst_type"))
            if not (set(by_kind) <= set(KINDS) and set(by_dst_type) <= set(DST_TYPES)):
                raise _BadRow("type")
            return {
                "sources": con.execute("SELECT COUNT(*) FROM sources").fetchone()[0],
                "edges": con.execute("SELECT COUNT(*) FROM edges").fetchone()[0],
                "by_kind": by_kind,
                "by_dst_type": by_dst_type,
            }

    def edges_digest(self, extractors: Optional[Sequence[str]] = None) -> str:
        """sha256 over a canonical dump of edges and sources (row ids excluded).

        With ``extractors`` only those arms are hashed, so each arm's
        determinism can be asserted on its own.
        """
        digest = hashlib.sha256()

        def feed(row: Sequence) -> None:
            digest.update(json.dumps(list(row), ensure_ascii=False,
                                     separators=(",", ":")).encode("utf-8"))
            digest.update(b"\n")

        for e in self.iter_edges(extractors=extractors):
            feed((e.kind, e.src, e.src_type, e.dst, e.dst_type, e.line, e.key, e.evidence,
                  e.weight, e.meta, e.extractor, e.extractor_version))
        query = "SELECT extractor, path, sha256, extractor_version, meta FROM sources"
        params: list = []
        if extractors is not None:
            if not list(extractors):
                return digest.hexdigest()
            query += " WHERE " + _in_clause("extractor", list(extractors))
            params = list(extractors)
        with self._reading():
            for row in self._con.execute(query + " ORDER BY extractor, path", params):
                feed(("source",) + _source_row(row))
        return digest.hexdigest()
