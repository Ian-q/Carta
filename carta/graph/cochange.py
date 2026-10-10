"""Co-change edges: files that git history shows changing together.

One mainline commit is one transaction (one basket): on a squash-merge
repository that is one pull request, and on a merge-commit repository it is
one merge, because every commit is compared with its first parent. A pair of
files that shares enough commits becomes two ``cochange`` edges, one per
direction, scored so that a file which changes all the time does not look
coupled to everything.

``PairTable`` is the miner. It is pure (no I/O, no clock): feed it commits in
order and ask it for edges. ``build_cochange`` persists one in the edge store
and advances it from the last commit it saw; the replay eval carries one
forward through history, so both read the same counts by the same rules.

Nothing here claims causation. An edge says "historically changed together",
and every surface that shows one says so.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import (
    Callable, Dict, Iterable, Iterator, List, Mapping, Optional, Sequence, Set, Tuple,
)

from carta.graph.gitio import (
    GitError, LogEntry, first_parent_rows, is_shallow, is_unborn, iter_added_deleted,
    iter_log_entries, work_tree_root,
)
from carta.graph.limits import MAX_COCHANGE_PATH_LENGTH
from carta.graph.model import Edge, canonical_meta
from carta.graph.paths import in_path_set, is_doc_path
from carta.graph.settings import CochangeSettings, GraphSettings
from carta.graph.store import EdgeStore, store_path

COCHANGE_EXTRACTOR = "cochange"
# Bump whenever the miner would store different rows for an unchanged history.
COCHANGE_EXTRACTOR_VERSION = 2
COCHANGE_KIND = "cochange"

META_CURSOR = "cochange.last_commit"
META_SEQ = "cochange.seq"
META_SETTINGS = "cochange.settings_sha"
META_VERSION = "cochange.extractor_version"
META_TOTALS = "cochange.totals"
META_SUMMARY = "cochange.summary"

# The empty blob, as SHA-1 and as SHA-256. Every empty file shares it, so it
# says nothing about which file moved where.
_EMPTY_BLOBS = frozenset((
    "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391",
    "473a0f4c3be8a93681a267e3b1e9a7dcda1185436fe141f7749120a303721813",
))
_LOG_BATCH = 2000           # commits asked of git per call; entries are streamed, never held


def countable_path(path: str) -> bool:
    """True when a path from history may be counted and stored.

    File names are untrusted, and a counted name is stored and later printed.
    One is left out when it is outside the path set (``.git/``, ``.carta/``),
    is over-long, or holds anything but printable characters and the plain
    space: a control character (C0 or C1), a line or paragraph separator, a
    format character such as a bidirectional override, an unassigned code
    point, or a byte that was not valid UTF-8.
    """
    if not isinstance(path, str) or not in_path_set(path):
        return False
    if len(path) > MAX_COCHANGE_PATH_LENGTH:
        return False
    return path.isprintable()


class _File:
    __slots__ = ("commits", "w", "seq")

    def __init__(self, commits: int = 0, w: float = 0.0, seq: int = 0) -> None:
        self.commits = commits      # counted commits that touched the file
        self.w = w                  # the same count, decayed, as of ``seq``
        self.seq = seq              # position of the last commit that touched it


class _Pair:
    __slots__ = ("support", "w", "first", "last", "seq")

    def __init__(self, support: int = 0, w: float = 0.0, first: str = "", last: str = "",
                 seq: int = 0) -> None:
        self.support = support      # counted commits that touched both files
        self.w = w                  # decayed, as of ``seq``
        self.first = first          # first and last such commit
        self.last = last
        self.seq = seq


class PairTable:
    """Running co-change counts over a first-parent history.

    ``observe`` takes commits oldest first. The rules, all of them a function
    of the history and the settings alone:

    * A commit with no parent (the root, or the edge of a shallow clone)
      creates everything it holds; it is never counted.
    * A commit that changes more than ``max_files`` paths is a bulk change
      (a formatting sweep, a vendored drop, a mass move): it adds nothing to
      any count. The test is made before any pair is formed.
    * Otherwise each path the commit added or modified gains one commit, and
      each pair of them gains one co-change.
    * A deleted path loses its counts and its pairs. If it comes back later
      it starts from nothing.
    * A pure move keeps its history: when a commit deletes one path and adds
      another with the same content (the same blob, matched one to one, the
      empty blob excepted), the counts follow the file. A move that also
      edits the file is a delete and an add.
    * A pair still below ``min_support`` is forgotten once ``pending_window``
      commits pass without it recurring, which bounds the table.
    * With ``half_life`` set, a co-change counts half after that many commits
      (positions on the mainline, not dates: clocks in history are not to be
      trusted, and positions make the result reproducible).
    """

    def __init__(self, settings: CochangeSettings) -> None:
        self.settings = settings
        self.files: Dict[str, _File] = {}
        self.adj: Dict[str, Dict[str, _Pair]] = {}
        self.seq = 0                # mainline commits seen, counted or not
        self.total = 0              # commits counted
        self.total_w = 0.0          # the same, decayed, as of ``total_seq``
        self.total_seq = 0
        self.bulk = 0               # commits skipped as bulk
        self.dirty: Set[str] = set()    # files whose stored rows may have changed
        self.gone: Set[str] = set()     # files that no longer have rows

    # -- arithmetic ---------------------------------------------------------

    def _decay(self, value: float, since: int, now: int) -> float:
        half_life = self.settings.half_life
        if not half_life or now <= since:
            return value
        return value * 0.5 ** ((now - since) / half_life)

    # -- feeding ------------------------------------------------------------

    def _renames(self, deleted: Sequence[str], added: Sequence[str],
                 blobs: Mapping[str, str], removed: Mapping[str, str]) -> List[Tuple[str, str]]:
        by_blob: Dict[str, List[List[str]]] = {}
        for path in deleted:
            blob = removed.get(path)
            if blob and blob not in _EMPTY_BLOBS:
                by_blob.setdefault(blob, [[], []])[0].append(path)
        for path in added:
            blob = blobs.get(path)
            if blob in by_blob:
                by_blob[blob][1].append(path)
        return sorted((olds[0], news[0]) for olds, news in by_blob.values()
                      if len(olds) == 1 and len(news) == 1)

    def _move(self, old: str, new: str) -> None:
        self._drop(new)
        state = self.files.pop(old, None)
        if state is None:               # a file with no counts has nothing to carry over
            return
        partners = self.adj.pop(old, {})
        self.gone.add(old)
        self.dirty.discard(old)
        self.files[new] = state
        self.gone.discard(new)
        self.dirty.add(new)
        if partners:
            self.adj[new] = partners
        for other, pair in partners.items():
            links = self.adj[other]
            del links[old]
            links[new] = pair
            self.dirty.add(other)

    def _drop(self, path: str) -> None:
        if self.files.pop(path, None) is None:
            return
        self.gone.add(path)
        self.dirty.discard(path)
        for other in self.adj.pop(path, {}):
            links = self.adj[other]
            del links[path]
            if not links:
                del self.adj[other]
            self.dirty.add(other)

    def observe(self, sha: str, changes: Iterable[Tuple[str, str]], *, has_parent: bool = True,
                blobs: Optional[Mapping[str, str]] = None,
                removed: Optional[Mapping[str, str]] = None) -> None:
        """Count one mainline commit. ``changes`` is ``(status, path)`` with A, M or D.

        ``blobs`` (path to new blob id) and ``removed`` (deleted path to the
        blob id it had) are what lets a pure move be followed; without them
        every move is a delete and an add.
        """
        self.seq += 1
        seq = self.seq
        changes = list(changes)
        deleted = sorted({p for s, p in changes if s == "D" and countable_path(p)})
        live = sorted({p for s, p in changes if s != "D" and countable_path(p)})
        moved: Set[str] = set()
        if self.settings.follow_renames and blobs is not None and removed is not None:
            added = sorted({p for s, p in changes if s == "A" and countable_path(p)})
            for old, new in self._renames(deleted, added, blobs, removed):
                self._move(old, new)
                moved.add(old)
        for path in deleted:
            if path not in moved:
                self._drop(path)
        if not has_parent:
            return
        if len(changes) > self.settings.max_files:
            self.bulk += 1
            return
        if not live:
            return
        self.total += 1
        self.total_w = self._decay(self.total_w, self.total_seq, seq) + 1.0
        self.total_seq = seq
        for path in live:
            state = self.files.get(path)
            if state is None:
                state = self.files[path] = _File()
                self.gone.discard(path)
            state.w = self._decay(state.w, state.seq, seq) + 1.0
            state.commits += 1
            state.seq = seq
            self.dirty.add(path)
        floor, window = self.settings.min_support, self.settings.pending_window
        for i, a in enumerate(live):
            for b in live[i + 1:]:
                pair = self.adj.get(a, {}).get(b)
                if pair is None:
                    pair = _Pair(first=sha)
                    self.adj.setdefault(a, {})[b] = pair
                    self.adj.setdefault(b, {})[a] = pair
                elif window and pair.support < floor and seq - pair.seq > window:
                    pair.support, pair.w, pair.first = 0, 0.0, sha
                pair.w = self._decay(pair.w, pair.seq, seq) + 1.0
                pair.support += 1
                pair.last = sha
                pair.seq = seq

    def observe_bulk(self, sha: str, deleted: Iterable[str],
                     renames: Iterable[Tuple[str, str]] = (), *, has_parent: bool = True) -> None:
        """Count one mainline commit that is over ``max_files``, without its change list.

        What ``observe`` does with such a commit, given only what it uses:
        the paths it ``deleted`` and its pure moves as ``(old, new)``. Paths
        this table holds no counts for may be left out of both; they change
        nothing. ``sha`` is unused (a bulk commit is recorded on no pair) and
        is taken so the two calls read alike.
        """
        self.seq += 1
        moved: Set[str] = set()
        if self.settings.follow_renames:
            for old, new in sorted(set(renames)):
                self._move(old, new)
                moved.add(old)
        for path in sorted(set(deleted)):
            if path not in moved:
                self._drop(path)
        if has_parent:
            self.bulk += 1

    # -- reading ------------------------------------------------------------

    def score(self, src: str, dst: str) -> Tuple[int, float]:
        """``(support, score)`` for the ordered pair; ``(0, 0.0)`` when it has none.

        ``confidence`` is the share of ``src``'s commits that also changed
        ``dst``. ``lift`` divides that by how often ``dst`` changes at all.
        """
        pair = self.adj.get(src, {}).get(dst)
        state = self.files.get(src)
        if pair is None or state is None or state.w <= 0.0:
            return 0, 0.0
        confidence = self._decay(pair.w, pair.seq, state.seq) / state.w
        if self.settings.measure == "confidence":
            return pair.support, confidence
        other = self.files.get(dst)
        total = self._decay(self.total_w, self.total_seq, self.seq)
        if other is None or total <= 0.0:
            return pair.support, 0.0
        share = self._decay(other.w, other.seq, self.seq) / total
        return pair.support, (confidence / share if share > 0.0 else 0.0)

    def neighbours(self, src: str) -> List[Tuple[str, int, float]]:
        """``(dst, support, score)`` for every pair of ``src`` that passes both floors,
        best first (score, then support, then path)."""
        out = []
        floor, min_score = self.settings.min_support, self.settings.min_score
        for dst, pair in self.adj.get(src, {}).items():
            if pair.support < floor:
                continue
            support, score = self.score(src, dst)
            if score >= min_score and score > 0.0:
                out.append((dst, support, score))
        out.sort(key=lambda item: (-item[2], -item[1], item[0]))
        return out

    def edges_from(self, src: str, is_doc: Callable[[str], bool],
                   only: Optional[Callable[[str], bool]] = None) -> List[Edge]:
        """The ``cochange`` edges out of ``src``, sorted. ``only`` filters targets."""
        state = self.files.get(src)
        if state is None:
            return []
        edges = []
        for dst, support, score in self.neighbours(src):
            if only is not None and not only(dst):
                continue
            pair = self.adj[src][dst]
            edges.append(Edge(
                kind=COCHANGE_KIND, src=src, dst=dst,
                dst_type="doc" if is_doc(dst) else "file",
                evidence=f"changed together in {support} of {state.commits} commits",
                weight=round(score, 6),
                meta=canonical_meta({"support": support, "commits": state.commits,
                                     "first": pair.first, "last": pair.last}),
                src_type="file", extractor=COCHANGE_EXTRACTOR,
                extractor_version=COCHANGE_EXTRACTOR_VERSION))
        edges.sort()
        return edges

    def counts(self) -> dict:
        pairs = sum(len(links) for links in self.adj.values()) // 2
        return {"files": len(self.files), "pairs": pairs}

    # -- persistence ----------------------------------------------------------

    def file_meta(self, path: str) -> str:
        """Canonical JSON of one file's state, with the pairs it holds.

        A pair is written once, under the smaller of its two paths. A pair
        that is still below the floor and has already outlived the pending
        window as of this file's last commit is left out: the next co-change
        would reset it anyway, so dropping it changes nothing later.
        """
        state = self.files[path]
        floor, window = self.settings.min_support, self.settings.pending_window
        pairs = {}
        for other, pair in self.adj.get(path, {}).items():
            if other < path:
                continue
            if window and pair.support < floor and state.seq - pair.seq > window:
                continue
            pairs[other] = [pair.support, pair.w, pair.first, pair.last, pair.seq]
        return canonical_meta({"commits": state.commits, "w": state.w, "seq": state.seq,
                               "pairs": pairs})

    def totals(self) -> str:
        return canonical_meta({"total": self.total, "total_w": self.total_w,
                               "total_seq": self.total_seq, "bulk": self.bulk})

    @classmethod
    def load(cls, settings: CochangeSettings, seq: int, totals: str,
             files: Mapping[str, str]) -> "PairTable":
        """Rebuild a table from what ``file_meta`` and ``totals`` wrote.

        Raises ``ValueError`` on anything that is not exactly that shape; the
        caller then rebuilds from history instead of trusting the rows.
        """
        table = cls(settings)
        table.seq = _int(seq)
        head = _obj(totals)
        table.total = _int(head["total"])
        table.total_w = _float(head["total_w"])
        table.total_seq = _int(head["total_seq"])
        table.bulk = _int(head["bulk"])
        parsed = {path: _obj(text) for path, text in files.items()}
        for path in parsed:
            if not countable_path(path):
                raise ValueError("a stored row names a path that is never counted")
        for path, data in parsed.items():
            table.files[path] = _File(_int(data["commits"]), _float(data["w"]),
                                      _int(data["seq"]))
        for path, data in parsed.items():
            pairs = data["pairs"]
            if not isinstance(pairs, dict):
                raise ValueError("pairs must be a mapping")
            for other, row in pairs.items():
                if other not in table.files or not other > path or len(row) != 5:
                    raise ValueError("a stored pair names a file that has no row")
                support, w, first, last, pair_seq = row
                if not (_is_commit_id(first) and _is_commit_id(last)):
                    raise ValueError("a stored pair has no commit ids")
                pair = _Pair(_int(support), _float(w), first, last, _int(pair_seq))
                table.adj.setdefault(path, {})[other] = pair
                table.adj.setdefault(other, {})[path] = pair
        return table


def _is_commit_id(value: object) -> bool:
    return (isinstance(value, str) and len(value) in (40, 64)
            and all(ch in "0123456789abcdef" for ch in value))


def _obj(text: str) -> dict:
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("expected a JSON object")
    return data


def _int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"expected a non-negative integer, got {value!r}")
    return value


def _float(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"expected a number, got {value!r}")
    number = float(value)
    if number != number or number < 0.0 or number == float("inf"):
        raise ValueError(f"expected a finite non-negative number, got {value!r}")
    return number


# -- history ----------------------------------------------------------------------

def observe_entry(table: PairTable, entry: LogEntry, prefix: str = "") -> None:
    """Feed one ``LogEntry`` to a table, re-rooting its paths under ``prefix``.

    ``prefix`` is where the Carta root sits below the git root (``''`` when
    they are the same, else ``sub/dir/``). Paths outside it are not part of
    this project and are dropped before anything is counted.
    """
    if not prefix:
        table.observe(entry.sha, entry.changes, has_parent=bool(entry.parents),
                      blobs=entry.blobs, removed=entry.removed)
        return
    cut = len(prefix)
    table.observe(
        entry.sha,
        [(s, p[cut:]) for s, p in entry.changes if p.startswith(prefix)],
        has_parent=bool(entry.parents),
        blobs={p[cut:]: b for p, b in entry.blobs.items() if p.startswith(prefix)},
        removed={p[cut:]: b for p, b in entry.removed.items() if p.startswith(prefix)})


def _bulk_changes(top: Path, row: Sequence[str], prefix: str,
                  tables: Sequence[PairTable]) -> Tuple[List[str], List[Tuple[str, str]]]:
    """What a commit over the limit still does to ``tables``: ``(deleted, renames)``.

    Its change list was never collected, so the two things a bulk commit is
    used for are read from git again, each in one streamed pass that keeps
    only what a table can use: the deleted paths some table has counts for,
    and, for those, whether exactly one path was deleted and exactly one
    added with the same content. Memory is bounded by the tables, not by the
    commit.
    """
    cut = len(prefix)
    deleted: Dict[str, str] = {}
    for status, path, blob in iter_added_deleted(top, row, prefix=prefix):
        path = path[cut:]
        if status == "D" and countable_path(path) and any(path in t.files for t in tables):
            deleted[path] = blob
    wanted = {blob for blob in deleted.values() if blob and blob not in _EMPTY_BLOBS}
    if not wanted or not any(t.settings.follow_renames for t in tables):
        return sorted(deleted), []
    olds = dict.fromkeys(wanted, 0)
    news: Dict[str, List] = {blob: [0, ""] for blob in wanted}
    for status, path, blob in iter_added_deleted(top, row, prefix=prefix):
        path = path[cut:]
        if blob not in wanted or not countable_path(path):
            continue
        if status == "D":
            olds[blob] += 1
        else:
            news[blob][0] += 1
            news[blob][1] = path
    renames = sorted((old, news[blob][1]) for old, blob in deleted.items()
                     if blob in wanted and olds[blob] == 1 and news[blob][0] == 1)
    return sorted(deleted), renames


def mine_rows(top: Path, rows: Sequence[Sequence[str]], tables: Sequence[PairTable],
              prefix: str = "", *, limit: int = 0) -> Iterator[LogEntry]:
    """Feed the commits of ``rows`` to every table, oldest first, and yield each entry.

    The bulk limit is applied while git's answer is read, not after: a commit
    that changes more paths than every table's ``max_files`` (and than
    ``limit``, for a caller with a limit of its own) comes back ``truncated``,
    with a count and no paths, and is fed through ``observe_bulk``. So memory
    is one commit of at most that many paths, whatever the history holds.
    """
    cap = max([limit] + [table.settings.max_files for table in tables])
    for begin in range(0, len(rows), _LOG_BATCH):
        batch = rows[begin:begin + _LOG_BATCH]
        for row, entry in zip(batch, iter_log_entries(top, batch, max_changes=cap,
                                                      prefix=prefix)):
            if not tables:
                pass                    # a caller that only reads: nothing to feed
            elif not entry.truncated:
                for table in tables:
                    observe_entry(table, entry, prefix)
            elif not entry.parents:
                for table in tables:
                    table.observe_bulk(entry.sha, (), has_parent=False)
            else:
                deleted, renames = _bulk_changes(top, row, prefix, tables)
                for table in tables:
                    table.observe_bulk(entry.sha, deleted, renames)
            yield entry


def _history(repo_root: Path) -> Tuple[Optional[Path], str, List[List[str]]]:
    """The git root, the Carta root's prefix below it, and the mainline rows.

    ``(None, '', [])`` when ``repo_root`` is in no repository and ``(top,
    prefix, [])`` when the branch has no commit yet: both are "no edges",
    never an error. Anything else git cannot answer (a damaged ref, a missing
    object, git itself failing) raises ``GitError``: a history that cannot be
    read is not an empty one, and the caller must not act as if it were.
    """
    top = work_tree_root(repo_root)
    if top is None:
        return None, "", []
    try:
        rel = os.path.relpath(os.path.realpath(str(repo_root)), os.path.realpath(str(top)))
    except ValueError:
        return None, "", []
    rel = rel.replace(os.sep, "/")
    if rel == ".." or rel.startswith("../"):
        return None, "", []
    prefix = "" if rel == "." else rel + "/"
    try:
        rows = first_parent_rows(top, "HEAD")
    except GitError:
        if is_unborn(top):
            return top, prefix, []
        raise
    return top, prefix, rows


def _empty_report(status: str, enabled: bool) -> dict:
    return {"enabled": enabled, "status": status, "commits": 0, "new_commits": 0,
            "counted": 0, "bulk": 0, "files": 0, "pairs": 0, "edges": 0, "written": 0,
            "full": False, "shallow": False, "head": "", "digest": ""}


def _clear(store: EdgeStore) -> None:
    store.clear_extractor(COCHANGE_EXTRACTOR)
    for key in (META_CURSOR, META_SEQ, META_SETTINGS, META_VERSION, META_TOTALS, META_SUMMARY):
        store.set_meta(key, "")


def _stored_seq(store: EdgeStore, fingerprint: str,
                rows: Sequence[Sequence[str]]) -> Optional[int]:
    """How many of ``rows`` the store has already counted, or ``None`` to start over.

    A number when the store was written by this version with these settings
    and the commit it stopped at is still at the same position on the
    mainline. A rewritten or lengthened history (a rebase, a branch switch, a
    shallow clone that was deepened) fails that test and is mined again from
    the start.
    """
    if store.get_meta(META_SETTINGS) != fingerprint:
        return None
    if store.get_meta(META_VERSION) != str(COCHANGE_EXTRACTOR_VERSION):
        return None
    cursor = store.get_meta(META_CURSOR) or ""
    seq_text = store.get_meta(META_SEQ) or ""
    if not seq_text.isdigit():
        return None
    seq = int(seq_text)
    if seq == 0:
        if cursor:
            return None
    elif seq > len(rows) or rows[seq - 1][0] != cursor:
        return None
    return seq


def _stored_table(store: EdgeStore, settings: CochangeSettings, seq: int) -> Optional[PairTable]:
    """The table the store holds, or ``None`` when its rows are not what this code wrote."""
    try:
        return PairTable.load(settings, seq, store.get_meta(META_TOTALS) or "",
                              store.source_meta(COCHANGE_EXTRACTOR))
    except (ValueError, KeyError, TypeError):
        return None


def _summary(table: PairTable, edges: int, digest: str) -> str:
    sizes = table.counts()
    return canonical_meta({"counted": table.total, "bulk": table.bulk, "files": sizes["files"],
                           "pairs": sizes["pairs"], "edges": edges, "digest": digest})


def _stored_summary(store: EdgeStore) -> Optional[dict]:
    """What the last build reported about the store, or ``None`` if it is not on record."""
    try:
        data = _obj(store.get_meta(META_SUMMARY) or "")
        for key in ("counted", "bulk", "files", "pairs", "edges"):
            _int(data[key])
        if not isinstance(data["digest"], str):
            return None
    except (ValueError, KeyError, TypeError):
        return None
    return data


def build_cochange(repo_root: Path, settings: GraphSettings, *, force: bool = False) -> dict:
    """Bring the store's co-change edges up to date with the mainline history.

    Incremental: only commits after the last one seen are read, and only the
    files they touch are rewritten. The whole update is one store transaction,
    so a failure part-way (``GitError``) leaves the previous state, cursor
    included, and the store still knows it is behind.

    A history git cannot read raises ``GitError`` before anything is touched;
    only a directory in no repository and a branch with no commit count as an
    empty history. A build with no new commit returns what the last one
    recorded, without reading the pair state.

    Returns a report: ``status`` (``ok``, ``no_history`` or ``disabled``),
    ``commits`` (mainline commits seen in all), ``new_commits`` (read this
    run), ``counted`` and ``bulk`` (totals), ``files``, ``pairs``, ``edges``,
    ``written`` (files whose rows changed), ``full`` (mined from the start),
    ``shallow`` (the clone's history is cut off, so the counts are too),
    ``head`` and ``digest``.
    """
    repo_root = Path(repo_root)
    cs = settings.cochange
    if not (settings.enabled and cs.enabled):
        report = _empty_report("disabled", False)
        store = EdgeStore.open_existing(store_path(repo_root))
        if store is not None:
            with store, store.transaction():
                if store.get_meta(META_CURSOR) or store.source_meta(COCHANGE_EXTRACTOR):
                    _clear(store)
        return report

    top, prefix, rows = _history(repo_root)
    fingerprint = cs.fingerprint(settings, COCHANGE_EXTRACTOR_VERSION)
    head = rows[-1][0] if rows else ""
    status = "ok" if rows else "no_history"

    def is_doc(path: str) -> bool:
        return is_doc_path(path, settings)

    with EdgeStore(store_path(repo_root)) as store, store.transaction():
        seq = None if force else _stored_seq(store, fingerprint, rows)
        if seq is not None and seq == len(rows):
            # Nothing new on the mainline. The pair state is not read at all:
            # a build that has nothing to do must not cost what the state weighs.
            summary = _stored_summary(store)
            if summary is not None:
                return {"enabled": True, "status": status, "commits": seq, "new_commits": 0,
                        "counted": summary["counted"], "bulk": summary["bulk"],
                        "files": summary["files"], "pairs": summary["pairs"],
                        "edges": summary["edges"], "written": 0, "full": False,
                        "shallow": bool(top is not None and rows and is_shallow(top)),
                        "head": head, "digest": summary["digest"]}
        table = None if seq is None else _stored_table(store, cs, seq)
        full = table is None
        if table is None:
            had_rows = bool(store.source_meta(COCHANGE_EXTRACTOR))
            store.clear_extractor(COCHANGE_EXTRACTOR)
            table = PairTable(cs)
            written = 1 if had_rows else 0
        else:
            written = 0
        start = table.seq
        if top is not None:
            for _entry in mine_rows(top, rows[start:], [table], prefix):
                pass
        new_commits = len(rows) - start

        if cs.measure == "lift" and new_commits:
            # Lift reads every file's share of all commits, so every row moves.
            table.dirty.update(table.files)
        for path in sorted(table.gone - set(table.files)):
            store.remove_source(COCHANGE_EXTRACTOR, path)
            written += 1
        known = store.source_meta(COCHANGE_EXTRACTOR) if table.dirty else {}
        for path in sorted(table.dirty):
            if path not in table.files:
                continue
            meta = table.file_meta(path)
            edges = table.edges_from(path, is_doc)
            if known.get(path) == meta and store.source_edges(COCHANGE_EXTRACTOR, path) == edges:
                continue
            store.replace_source(COCHANGE_EXTRACTOR, path, "", COCHANGE_EXTRACTOR_VERSION,
                                 edges, meta)
            written += 1
        n_edges = store.counts()["by_kind"].get(COCHANGE_KIND, 0)
        digest = store.edges_digest((COCHANGE_EXTRACTOR,))
        store.set_meta(META_CURSOR, head)
        store.set_meta(META_SEQ, str(table.seq))
        store.set_meta(META_SETTINGS, fingerprint)
        store.set_meta(META_VERSION, str(COCHANGE_EXTRACTOR_VERSION))
        store.set_meta(META_TOTALS, table.totals())
        store.set_meta(META_SUMMARY, _summary(table, n_edges, digest))

    sizes = table.counts()
    return {"enabled": True, "status": status, "commits": table.seq,
            "new_commits": new_commits, "counted": table.total, "bulk": table.bulk,
            "files": sizes["files"], "pairs": sizes["pairs"], "edges": n_edges,
            "written": written, "full": full,
            "shallow": bool(top is not None and rows and is_shallow(top)),
            "head": head, "digest": digest}


def cochange_status(repo_root: Path, settings: GraphSettings, store: EdgeStore) -> dict:
    """What the store holds for this arm and whether a build would change it. Read-only."""
    cs = settings.cochange
    enabled = bool(settings.enabled and cs.enabled)
    cursor = store.get_meta(META_CURSOR) or ""
    seq_text = store.get_meta(META_SEQ) or ""
    held = bool(cursor or store.source_meta(COCHANGE_EXTRACTOR))
    status = {"enabled": enabled, "commits": int(seq_text) if seq_text.isdigit() else 0,
              "head": cursor, "stale": False, "reason": ""}
    if not enabled:
        if held:
            status.update(stale=True, reason="cochange_disabled")
        return status
    try:
        _top, _prefix, rows = _history(repo_root)
    except GitError:
        # The history cannot be read, so nobody can say the rows match it.
        status.update(stale=True, reason="cochange_git_failed")
        return status
    head = rows[-1][0] if rows else ""
    fingerprint = cs.fingerprint(settings, COCHANGE_EXTRACTOR_VERSION)
    mined = bool(store.get_meta(META_SETTINGS))
    if mined and (store.get_meta(META_SETTINGS) != fingerprint
                  or store.get_meta(META_VERSION) != str(COCHANGE_EXTRACTOR_VERSION)):
        status.update(stale=True, reason="cochange_settings_changed")
    elif cursor != head or status["commits"] != len(rows):
        status.update(stale=True, reason="cochange_behind")
    return status
