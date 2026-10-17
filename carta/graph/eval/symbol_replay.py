"""The symbol arm of the PR replay.

To score a commit, the replay may only use what existed at its parent. For
symbol edges that means the symbol index as it was at the parent, and there
are three ways to have one:

* ``ctags``: universal-ctags is run over the code files of each parent, read
  from git objects into a scratch directory outside the repository. A file
  is indexed once per content (results are kept by blob id), so after the
  first commit only the files a commit changed are indexed. Nothing leaks.
* ``tracked:PATH``: the repository tracks its own symbols file; each parent's
  version of it is read from git objects. Nothing leaks.
* ``file:PATH``: one symbols file, as it is today, used for every commit.
  This **leaks**: a name is resolved to the file that defines it now, which
  the parent could not know when the symbol has since moved or been added.
  Definitions in files the parent did not have are left out, which removes
  part of the leak and not all of it. The caller has to say so explicitly
  (``--accept-symbol-leak``) and the report carries the warning.

The identifiers come from the mention extractor run on each doc as it was at
the parent, exactly as the mention arm does it.
"""
from __future__ import annotations

import dataclasses
import os
import re
import tempfile
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from carta.graph.limits import MAX_SYMBOLS_FILE_BYTES
from carta.graph.model import Edge
from carta.graph.paths import is_doc_path, is_excluded_rel
from carta.graph.safeio import SkippedFile, read_regular_file
from carta.graph.settings import GraphSettings
from carta.graph.symbols import (
    Ctags, SymbolRow, SymbolsFailed, SymbolTable, find_universal_ctags, parse_symbols,
    require_rows, resolve_doc, run_ctags,
)

LEAK_NOTE = ("the symbol index is today's, used for every commit: a symbol that has moved or "
             "been added since a commit's parent is seen early. Definitions in files the "
             "parent did not have are left out. This is not a clean measurement.")

_SAFE_NAME = re.compile(r"\.?[A-Za-z0-9][A-Za-z0-9._+-]{0,127}")
_SAFE_SUFFIX = re.compile(r"(?:\.[A-Za-z0-9_+-]{1,16}){1,2}")
_TRACKED_PATH = re.compile(r"[^\x00-\x1f\x7f]{1,1024}")
_BATCH = 2000                               # files handed to one scratch ctags run


class SymbolSourceError(ValueError):
    """The symbol source asked for cannot be used; the message says what to pass instead."""


def scratch_name(basename: str) -> str:
    """The name a blob is written under for ctags, which tells a language by it.

    The basename itself when it is made of plain characters (a leading dot is
    one: ``.eslintrc.js`` is a JavaScript file). Otherwise ``file`` plus the
    extension, so that an oddly named file is still read as its language.
    """
    if _SAFE_NAME.fullmatch(basename):
        return basename
    if len(basename) <= 1024:
        dot = basename.rfind(".", max(0, len(basename) - 34))
        suffix = basename[dot:] if dot > 0 else ""
        if _SAFE_SUFFIX.fullmatch(suffix):
            return "file" + suffix
    return "file"


class SymbolSource:
    """Where a replay's symbol index comes from. One per run; see the module docstring."""

    def __init__(self, mode: str, *, label: str, rows: Sequence[SymbolRow] = (),
                 tracked: str = "", ctags: Optional[Ctags] = None) -> None:
        self.mode = mode                    # ctags | tracked | file
        self.label = label
        self.leak = mode == "file"
        self._tracked = tracked
        self._ctags = ctags
        self._today = SymbolTable(rows) if mode == "file" else None
        self._by_blob: Dict[Tuple[str, str], Tuple[tuple, ...]] = {}
        self._tables: Dict[str, SymbolTable] = {}
        self.indexed_blobs = 0
        self.parents = 0                    # commit parents an index was asked for
        self.parents_with_index = 0         # ...of which the tracked file gave at least one row

    def describe(self, max_definitions: int) -> dict:
        out = {"source": self.label, "leak": self.leak, "max_definitions": max_definitions,
               "note": LEAK_NOTE if self.leak else
               "the symbol index is rebuilt for each commit's parent; nothing later is seen"}
        if self._today is not None:
            out["definitions"] = len(self._today)
        if self.mode == "tracked":
            out["parents"] = self.parents
            out["parents_with_index"] = self.parents_with_index
        return out

    def check(self) -> None:
        """Raise ``SymbolSourceError`` when the run measured nothing.

        A tracked path that no parent had (a typo, a directory, a file that
        is not an index) would otherwise score the symbol arm as all zeros
        and report it as a clean measurement.
        """
        if self.mode == "tracked" and self.parents and not self.parents_with_index:
            raise SymbolSourceError(
                f"--symbols tracked:{self._tracked} gave no symbol rows at any of the "
                f"{self.parents:,} commit parents read: the repository did not track a "
                f"symbols file at that path. Check the path, or use --symbols ctags")

    # -- per commit ---------------------------------------------------------

    def table_at(self, snapshot, is_code: Callable[[str], bool]
                 ) -> Tuple[SymbolTable, Optional[Callable[[str], bool]]]:
        """``(table, alive)`` for the tree at ``snapshot``."""
        def alive(path: str) -> bool:
            return snapshot.paths.has_file(path) and is_code(path)

        if self._today is not None:
            return self._today, alive
        if self.mode == "tracked":
            self.parents += 1
            blob = snapshot.blobs.get(self._tracked)
            if blob is None:
                return SymbolTable(), None
            table = self._tables.get(blob)
            if table is None:
                if len(self._tables) >= 8:
                    self._tables.clear()
                data = snapshot.read_text(self._tracked).encode("utf-8", "replace")
                try:
                    rows = parse_symbols(data).rows
                except SymbolsFailed:
                    rows = []
                table = self._tables[blob] = SymbolTable(rows)
            self.parents_with_index += 1 if len(table) else 0
            return table, alive
        return self._ctags_table(snapshot, is_code), None

    def _ctags_table(self, snapshot, is_code: Callable[[str], bool]) -> SymbolTable:
        assert self._ctags is not None
        code = [p for p in snapshot.paths.paths() if is_code(p)]
        missing: Dict[Tuple[str, str], str] = {}
        for path in code:
            key = (snapshot.blobs[path], path.rsplit("/", 1)[-1])
            if key not in self._by_blob and key not in missing:
                missing[key] = path
        keys = sorted(missing)
        for start in range(0, len(keys), _BATCH):
            self._index(snapshot, {key: missing[key] for key in keys[start:start + _BATCH]})
        rows: List[SymbolRow] = []
        for path in code:
            key = (snapshot.blobs[path], path.rsplit("/", 1)[-1])
            rows.extend(SymbolRow(name, kind, path, line, scope)
                        for name, kind, line, scope in self._by_blob.get(key, ()))
        return SymbolTable(rows)

    def _index(self, snapshot, batch: Dict[Tuple[str, str], str]) -> None:
        """Index the blobs of ``batch`` (``(blob, basename) -> a path holding it``).

        The content is written under generated names in a new directory
        outside the repository, so no name from the repository reaches the
        filesystem except a basename, or an extension, made only of plain
        characters (``scratch_name``).
        """
        with tempfile.TemporaryDirectory(prefix="carta-replay-ctags-") as scratch:
            placed: Dict[str, Tuple[str, str]] = {}
            for i, key in enumerate(sorted(batch)):
                text = snapshot.read_text(batch[key])
                self._by_blob[key] = ()
                if not text or "\0" in text:
                    continue
                name = scratch_name(key[1])
                os.mkdir(os.path.join(scratch, str(i)))
                rel = f"{i}/{name}"
                with open(os.path.join(scratch, str(i), name), "w", encoding="utf-8",
                          errors="replace", newline="") as handle:
                    handle.write(text)
                placed[rel] = key
            if not placed:
                return
            parsed = run_ctags(Path(scratch), self._ctags, sorted(placed))
        self.indexed_blobs += len(placed)
        grouped: Dict[Tuple[str, str], List[tuple]] = {}
        for row in parsed.rows:
            key = placed.get(row.file)
            if key is not None:
                grouped.setdefault(key, []).append((row.name, row.kind, row.line, row.scope))
        for key, found in grouped.items():
            self._by_blob[key] = tuple(found)


def make_source(root: Path, spec: str, *, accept_leak: bool,
                settings: GraphSettings) -> SymbolSource:
    """A ``SymbolSource`` from the ``--symbols`` option of the replay.

    ``ctags`` | ``tracked:<path in the repository>`` | ``file:<any path>``.
    Raises ``SymbolSourceError`` with what to do about it.
    """
    spec = spec.strip()
    if spec == "ctags":
        ctags = find_universal_ctags(root)
        if ctags is None:
            raise SymbolSourceError(
                "--symbols ctags: no universal-ctags binary was found (the ctags on PATH, if "
                "any, cannot write JSON). Install universal-ctags, or use --symbols "
                "tracked:PATH or --symbols file:PATH")
        return SymbolSource("ctags", label=f"ctags ({ctags.version})", ctags=ctags)
    mode, sep, value = spec.partition(":")
    if sep and mode == "tracked":
        rel = value.strip().replace(os.sep, "/")
        while rel.startswith("./"):
            rel = rel[2:]
        if (_TRACKED_PATH.fullmatch(rel) is None or rel.startswith("/")
                or any(part in ("", ".", "..") for part in rel.split("/"))):
            raise SymbolSourceError("--symbols tracked:PATH needs a path inside the repository, "
                                    "relative to its root")
        return SymbolSource("tracked", label=f"tracked:{rel}", tracked=rel)
    if sep and mode == "file":
        if not accept_leak:
            raise SymbolSourceError(
                "--symbols file:PATH uses one symbols file, as it is today, for every commit, "
                "so the replay sees symbols a commit's parent did not have. Pass "
                "--accept-symbol-leak to run it anyway (the report says so), or use "
                "--symbols ctags or --symbols tracked:PATH, which rebuild the index per commit")
        path = Path(value.strip() or ".")
        try:
            data = read_regular_file(path.parent, path.name, MAX_SYMBOLS_FILE_BYTES)
        except SkippedFile as skipped:
            raise SymbolSourceError(f"--symbols file: {path.name} was not read "
                                    f"({skipped.reason.replace('_', ' ')})") from None
        except OSError as exc:
            raise SymbolSourceError(f"--symbols file: {path.name} could not be read: "
                                    f"{exc.strerror or 'I/O error'}") from None
        try:
            parsed = parse_symbols(data)
            require_rows(parsed, path.name)
        except SymbolsFailed as exc:
            raise SymbolSourceError(f"--symbols file: {exc}") from None
        return SymbolSource("file", label=f"file:{path.name}", rows=parsed.rows)
    raise SymbolSourceError(f"--symbols {spec!r}: expected ctags, tracked:PATH or file:PATH")


def make_provider(context, base: type, mention_provider: Callable):
    """The ``symbol`` entry of the replay's provider registry.

    ``base`` and ``mention_provider`` are the replay's ``EdgeProvider`` and
    ``MentionProvider``, handed in so this module never imports the replay
    (which may be running as ``__main__``).
    """
    source = getattr(context, "symbols", None)
    if source is None:
        raise SymbolSourceError(
            "the symbol arm needs a symbol index: pass --symbols ctags (universal-ctags, "
            "re-run for each commit's parent), --symbols tracked:PATH (a symbols file the "
            "repository tracks) or --symbols file:PATH with --accept-symbol-leak")

    class SymbolProvider(base):
        """Doc-to-defining-file edges from the index as it was at each commit's parent."""

        kind = "symbol"

        def __init__(self) -> None:
            self._settings: GraphSettings = context.settings
            self._source: SymbolSource = source
            # Its own mention state: the identifiers are a product of mention
            # extraction, and an arm may read symbols without reading mentions.
            self._mentions = mention_provider(
                dataclasses.replace(context, counters={"docs_resolved": 0}))
            self._roles: Dict[str, bool] = {}

        def _is_code(self, path: str) -> bool:
            hit = self._roles.get(path)
            if hit is None:
                settings = self._settings
                hit = self._roles[path] = not (
                    is_doc_path(path, settings) or is_excluded_rel(path, settings.excluded_paths))
            return hit

        def edges_at(self, snapshot) -> List[Edge]:
            state = self._mentions._sync(snapshot)
            try:
                table, alive = self._source.table_at(snapshot, self._is_code)
            except SymbolsFailed as exc:
                raise SymbolSourceError(f"the symbol index could not be built: {exc}") from None
            limit = self._settings.symbols_max_definitions
            edges: List[Edge] = []
            for doc in state.docs():
                edges.extend(resolve_doc(doc, state.doc_edges(doc), table, limit, alive).edges)
            context.counters["symbol_edges"] = (context.counters.get("symbol_edges", 0)
                                                + len(edges))
            return edges

        def relevant_edges(self, snapshot, paths: Sequence[str]) -> List[Edge]:
            # A symbol edge only ever matches the file it points at, and rarity
            # is counted per target, so the edges to the queried files suffice.
            wanted = set(paths)
            return [e for e in self.edges_at(snapshot) if e.dst in wanted]

        def observe(self, commit: str, changes) -> None:
            self._mentions.observe(commit, changes)

    return SymbolProvider()
