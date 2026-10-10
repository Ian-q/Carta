"""PR replay: score the graph arm against a repository's own history.

Every mainline commit that touched both code and docs is a free labelled
example: the code it touched is the query, the docs it edited are the answer.
For each such commit the replay asks every method "which docs describe these
files?" using only what existed at the commit's **parent**, and scores the
answer against the docs the commit really edited.

``run_replay`` reads nothing from the working tree, the index or the edge
store. Trees and file contents come from git objects (``git log --raw``,
``git ls-tree``, ``git cat-file --batch``), so for given settings the result
depends on the history alone.

The settings are the one input that is not history. The command line
(``carta graph replay`` or ``python -m carta.graph.eval.replay``) takes them
from ``<repo>/.carta/config.yaml`` in the working tree when that file exists,
tracked or not, unless ``--no-config`` is given. ``excluded_paths`` and
``graph.doc_globs`` decide which paths are docs and so which commits are
eligible, which is why every report records where its settings came from and
lists the exclusions it used.

The repository being replayed is not trusted. Its config is read without
following a symbolic link, and its ``graph.patterns`` (regular expressions,
which can be written to take exponential time) are ignored unless
``--trust-config`` is given. The report file is written by renaming a new
file into place, never through a link.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import re
import statistics
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import (
    Any, Callable, Dict, List, Mapping, NamedTuple, Optional, Sequence, Set, Tuple,
)

from carta.config import ConfigError
from carta.graph.build import decode_text
from carta.graph.eval.baselines import (
    TOKENIZER_VERSION, BasenameIndex, PopularityWindow, basename_tokens,
)
from carta.graph.eval.cache import ReplayCache, default_cache_path
from carta.graph.eval.incremental import MentionState
from carta.graph.eval.metrics import (
    RESAMPLES, aggregate, bootstrap_ci, paired_bootstrap, score_ranking,
)
from carta.graph.gitio import (
    BlobReader, BlobTooLarge, GitError, LogEntry, first_parent_log, ls_tree,
    rev_list_first_parent, toplevel,
)
from carta.graph.limits import MAX_CONFIG_BYTES, MAX_DOC_BYTES
from carta.graph.markdown import scan_text
from carta.graph.model import Edge, printable
from carta.graph.paths import PathIndex, in_path_set, is_doc_glob_match, is_excluded_rel
from carta.graph.query import COMBINE_MODES, ImpactIndex, rank_impact
from carta.graph.safeio import SkippedFile, read_regular_file, write_text_replacing
from carta.graph.settings import DEFAULT_SETTINGS, GraphSettings, graph_settings
from carta.graph.limits import MAX_REPLAY_TEXT_BYTES

REPLAY_SCHEMA = 1
DEFAULT_KS = (1, 3, 5, 10)
BASELINES = ("popularity", "basename", "basename_idf")
SKIP_REASONS = ("root_commit", "empty", "bulk", "docs_only", "no_docs_edited", "only_new_docs")
DEFAULT_ARMS: Mapping[str, Sequence[str]] = {"mention": ("mention",)}

_ARM_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")

Changes = Tuple[Tuple[str, str], ...]


class ReplayError(ValueError):
    """The replay cannot run as asked (not a repository, bad range, unknown edge kind)."""


# -- providers -----------------------------------------------------------------

@dataclass(frozen=True)
class Snapshot:
    """The repository as it was at one commit's parent. Read-only for providers."""

    parent: str                              # '' for a commit with no parent
    paths: PathIndex                         # every path at the parent; do not mutate
    blobs: Mapping[str, str]                 # path -> blob id at the parent
    docs: Tuple[str, ...]                    # the docs at the parent, sorted
    read_text: Callable[[str], str]          # text of a path at the parent, from git objects
    prior_changes: Sequence[Tuple[str, Changes]]     # (commit, changes) strictly before, oldest first


@dataclass(frozen=True)
class ReplayContext:
    """What a provider factory is given: one per run."""

    settings: GraphSettings
    cache: ReplayCache
    incremental: bool
    counters: Dict[str, int]


class EdgeProvider:
    """The interface an edge kind implements to take part in a replay.

    A provider belongs to one run and is driven in commit order. To score a
    commit the loop calls ``edges_at`` with the state at that commit's parent,
    and only afterwards ``observe`` with the commit itself, so a provider that
    mines history (co-change) updates its counts once per commit and can never
    see the commit it is being scored on. ``observe`` is called for every
    mainline commit, including those before the scored window.

    A provider may also define ``relevant_edges(snapshot, paths)`` returning
    only the edges that can match ``paths``; ranking over them must equal
    ranking over ``edges_at(snapshot)``. And it may define
    ``observe_entry(entry)``, called with the whole ``LogEntry`` (parents and
    blob ids included) at the moment ``observe`` would be, and instead of it.
    """

    kind = ""

    def edges_at(self, snapshot: Snapshot) -> List[Edge]:
        raise NotImplementedError

    def observe(self, commit: str, changes: Changes) -> None:
        return None


class MentionProvider(EdgeProvider):
    """Doc-to-path mention edges, extracted from each doc as it was at the parent."""

    kind = "mention"

    def __init__(self, context: ReplayContext) -> None:
        self._settings = context.settings
        self._cache = context.cache
        self._counters = context.counters
        self._fingerprint = context.settings.fingerprint()
        self._state = MentionState(context.settings, incremental=context.incremental)
        self._started = False
        self._touched: Set[str] = set()

    def _load(self, snapshot: Snapshot, doc: str) -> None:
        blob = snapshot.blobs[doc]
        candidates = self._cache.get_scan(blob, self._fingerprint)
        if candidates is None:
            candidates = scan_text(snapshot.read_text(doc), self._settings.patterns)
            self._cache.put_scan(blob, self._fingerprint, candidates)
        self._state.set_doc(doc, candidates)

    def _sync(self, snapshot: Snapshot) -> MentionState:
        state = self._state
        if not self._started:
            self._started = True
            state.seed_paths(snapshot.paths.files)
            for doc in snapshot.docs:
                self._load(snapshot, doc)
        else:
            docs = set(snapshot.docs)
            for path in sorted(self._touched):
                if path in docs:
                    self._load(snapshot, path)
                else:
                    state.remove_doc(path)
        self._touched.clear()
        self._counters["docs_resolved"] += state.refresh(snapshot.paths)
        return state

    def edges_at(self, snapshot: Snapshot) -> List[Edge]:
        return self._sync(snapshot).edges()

    def relevant_edges(self, snapshot: Snapshot, paths: Sequence[str]) -> List[Edge]:
        return self._sync(snapshot).edges_for(paths)

    def observe(self, commit: str, changes: Changes) -> None:
        if not self._started:
            return                      # nothing resolved yet: the first sync reads the tree
        self._state.paths_changed(added=[p for s, p in changes if s == "A"],
                                  deleted=[p for s, p in changes if s == "D"])
        self._touched.update(p for _s, p in changes)


def _cochange_provider(context: ReplayContext) -> EdgeProvider:
    from carta.graph.eval.cochange_arm import CochangeProvider
    return CochangeProvider(context)


# kind -> factory(ReplayContext) -> EdgeProvider. #130 and #131 register theirs here.
PROVIDERS: Dict[str, Callable[[ReplayContext], EdgeProvider]] = {
    "mention": MentionProvider,
    "cochange": _cochange_provider,
}


def register_provider(kind: str, factory: Callable[[ReplayContext], EdgeProvider]) -> None:
    """Make an edge kind available to ``--arm``. ``factory`` is called once per run."""
    PROVIDERS[kind] = factory


# -- classification --------------------------------------------------------------

class Classified(NamedTuple):
    reason: Optional[str]            # why the commit is not scored; None when it is
    query: Tuple[str, ...]           # the non-doc paths it touched
    gold: Tuple[str, ...]            # the docs it modified or deleted
    new_docs: int                    # docs it created (never gold)
    excluded_docs: int               # doc-named paths under an excluded directory
    edited_docs: Tuple[str, ...]     # every doc it touched, created ones included


def _path_role(path: str, settings: GraphSettings) -> str:
    """``doc``, ``excluded`` (doc-named but excluded) or ``file``."""
    if not is_doc_glob_match(path, settings):
        return "file"
    return "excluded" if is_excluded_rel(path, settings.excluded_paths) else "doc"


def classify_commit(changes: Sequence[Tuple[str, str]], has_parent: bool,
                    settings: GraphSettings, max_files: int,
                    role: Optional[Callable[[str], str]] = None) -> Classified:
    """Split one commit's changes into query and gold, and decide whether it is scored.

    Pure. Exclusion applies to the doc side only: a doc-named path that is
    excluded is neither a query path nor gold, and every other path stays in
    the query whatever directory it is in. A commit is scored when query and
    gold are both non-empty; otherwise it is skipped under exactly one reason,
    checked in the order of ``SKIP_REASONS``.
    """
    role = role or (lambda path: _path_role(path, settings))
    query: List[str] = []
    gold: List[str] = []
    edited: List[str] = []
    new_docs = excluded = 0
    for status, path in changes:
        kind = role(path)
        if kind == "file":
            query.append(path)
        elif kind == "excluded":
            excluded += 1
        else:
            edited.append(path)
            if status == "A":
                new_docs += 1
            else:
                gold.append(path)
    if not has_parent:
        reason: Optional[str] = "root_commit"
    elif not changes:
        reason = "empty"
    elif len(changes) > max_files:
        reason = "bulk"
    elif not query:
        reason = "docs_only"
    elif not edited:
        reason = "no_docs_edited"
    elif not gold:
        reason = "only_new_docs"
    else:
        reason = None
    return Classified(reason, tuple(sorted(query)), tuple(sorted(gold)), new_docs, excluded,
                      tuple(sorted(edited)))


# -- the walk ----------------------------------------------------------------------

class _Prefix(Sequence):
    """The first ``n`` items of a list, without copying it."""

    def __init__(self, items: list, n: int) -> None:
        self._items, self._n = items, n

    def __len__(self) -> int:
        return self._n

    def __getitem__(self, i):
        if isinstance(i, slice):
            return self._items[:self._n][i]
        if i < 0:
            i += self._n
        if not 0 <= i < self._n:
            raise IndexError(i)
        return self._items[i]


def _arms(arms: Optional[Mapping[str, Sequence[str]]]) -> Dict[str, Tuple[str, ...]]:
    out: Dict[str, Tuple[str, ...]] = {}
    for name, kinds in (arms or DEFAULT_ARMS).items():
        if not _ARM_NAME.match(str(name)):
            raise ReplayError(f"arm name {name!r} must match [A-Za-z0-9_.-]+")
        kinds = tuple(sorted(set(str(k) for k in kinds if not str(k).startswith("@"))))
        if not kinds:
            raise ReplayError(f"arm {name!r} names no edge kind")
        for kind in kinds:
            if kind not in PROVIDERS:
                raise ReplayError(
                    f"arm {name!r}: no provider for edge kind {kind!r} "
                    f"(available: {', '.join(sorted(PROVIDERS))})")
        out[str(name)] = kinds
    return out


def _arm_combine(arms: Optional[Mapping[str, Sequence[str]]], default: str) -> Dict[str, str]:
    """How each arm fuses its kinds: an ``@mode`` item among its kinds, else ``default``."""
    out: Dict[str, str] = {}
    for name, kinds in (arms or DEFAULT_ARMS).items():
        modes = [str(k)[1:] for k in kinds if str(k).startswith("@")]
        if len(modes) > 1 or any(mode not in COMBINE_MODES for mode in modes):
            raise ReplayError(f"arm {name!r}: the combine mode must be one of "
                              f"{', '.join(COMBINE_MODES)}")
        out[str(name)] = modes[0] if modes else default
    return out


def _method_name(arm: str) -> str:
    return f"graph[{arm}]"


def _window(repo: Path, range_spec: Optional[str], last: int
            ) -> Tuple[List[LogEntry], Set[str], str, str]:
    """The mainline history, the commits to scan, the head commit and the range label."""
    if range_spec and last:
        raise ReplayError("give a range or a number of commits, not both")
    try:
        if range_spec:
            wanted = rev_list_first_parent(repo, range_spec)
            if not wanted:
                return [], set(), "", range_spec
            log = first_parent_log(repo, wanted[-1])
            on_line = {entry.sha for entry in log}
            off = [sha for sha in wanted if sha not in on_line]
            if off:
                raise ReplayError(
                    f"range {range_spec!r} names {len(wanted)} commits but {len(off)} of them "
                    f"are not on the first-parent line of {wanted[-1][:12]}; the replay walks "
                    f"one line of history, so give a range along it (A..B)")
            return log, set(wanted), wanted[-1], range_spec
        log = first_parent_log(repo, "HEAD")
    except GitError as exc:
        raise ReplayError(str(exc)) from exc
    if not log:
        return [], set(), "", "all"
    chosen = log[-last:] if last and last > 0 else log
    label = f"last {last}" if last and last > 0 else "all"
    return log, {e.sha for e in chosen}, log[-1].sha, label


def run_replay(repo: Path, *, range_spec: Optional[str] = None, last: int = 0,
               arms: Optional[Mapping[str, Sequence[str]]] = None,
               ks: Sequence[int] = DEFAULT_KS, settings: Optional[GraphSettings] = None,
               hub_share: Optional[float] = None, max_files: Optional[int] = None,
               popularity_window: Optional[int] = None, min_population: Optional[int] = None,
               cache_path: Optional[Path] = None, use_disk_cache: bool = True,
               per_commit: bool = False, incremental: bool = True,
               resamples: int = RESAMPLES, stats: Optional[dict] = None,
               config: Optional[str] = None) -> dict:
    """Replay a repository's mainline history and return the report as a dict.

    ``last=0`` scans the whole first-parent history. ``arms`` maps an arm name
    to the edge kinds it reads (default ``{"mention": ["mention"]}``); every
    arm and every baseline is scored on the same commits. ``incremental=False``
    turns off selective re-resolution and the reduced edge sets (the report
    must not change; a test relies on that). ``stats``, when given, is filled
    with run counters and timings that are deliberately not part of the report.
    ``config`` says where ``settings`` came from; it is recorded in the report
    (``defaults`` when no settings are given, else ``caller`` unless named).
    """
    started = time.perf_counter()
    config = "defaults" if settings is None else (config or "caller")
    settings = settings or DEFAULT_SETTINGS
    resamples = int(resamples)
    if resamples < 1:
        raise ReplayError(f"resamples must be at least 1, got {resamples}")
    if int(last or 0) < 0:
        raise ReplayError(f"last must be 0 (all commits) or a positive count, got {last}")
    ks = tuple(sorted(set(int(k) for k in ks)))
    if not ks or ks[0] < 1:
        raise ReplayError("k values must be positive integers")
    top = ks[-1]
    hub_share = settings.replay_hub_share if hub_share is None else float(hub_share)
    max_files = settings.replay_max_files if max_files is None else int(max_files)
    window = (settings.replay_popularity_window if popularity_window is None
              else int(popularity_window))
    min_population = (settings.replay_min_population if min_population is None
                      else int(min_population))
    arm_kinds = _arms(arms)
    arm_combine = _arm_combine(arms, settings.impact_combine)
    methods = [_method_name(a) for a in arm_kinds] + list(BASELINES)
    weights = dict(settings.impact_weights)

    root = toplevel(Path(repo))
    if root is None:
        raise ReplayError(f"{repo} is not inside a git repository")
    log, scanned, head, label = _window(root, range_spec, int(last or 0))

    counters = {"docs_resolved": 0, "blob_reads": 0, "ranked": 0}
    disk = None
    if use_disk_cache:
        disk = Path(cache_path) if cache_path is not None else default_cache_path(root)
    cache = ReplayCache(disk)
    cache_on_disk = cache.on_disk
    context = ReplayContext(settings, cache, incremental, counters)
    providers = {kind: PROVIDERS[kind](context)
                 for kind in sorted({k for kinds in arm_kinds.values() for k in kinds})}

    roles: Dict[str, str] = {}

    def role(path: str) -> str:
        hit = roles.get(path)
        if hit is None:
            hit = roles[path] = _path_role(path, settings)
        return hit

    index = PathIndex()
    blobs: Dict[str, str] = {}
    docs: Set[str] = set()
    docs_sorted: List[Optional[Tuple[str, ...]]] = [None]
    tree_ready = False
    reader = BlobReader(root)
    texts: Dict[str, str] = {}                       # blob -> text, for the current commit only
    held = [0, 0]                                    # characters in ``texts`` now, and at most

    def read_blob_text(blob: str) -> str:
        text = texts.get(blob)
        if text is None:
            try:
                data = reader.read(blob, max_bytes=MAX_DOC_BYTES)
            except BlobTooLarge:
                data = b""                  # the same rule as a build: too large is not read
            text = decode_text(data)
            counters["blob_reads"] += 1
            # MAX_DOC_BYTES bounds one doc; the first sync reads every doc of
            # the tree. What is kept is only ever a saving (the two readers
            # of a doc share one read), so it is dropped when it gets large.
            if held[0] + len(text) > MAX_REPLAY_TEXT_BYTES:
                texts.clear()
                held[0] = 0
            if len(text) <= MAX_REPLAY_TEXT_BYTES:
                texts[blob] = text
                held[0] += len(text)
                held[1] = max(held[1], held[0])
        return text

    def read_text(path: str) -> str:
        return read_blob_text(blobs[path])

    def doc_tuple() -> Tuple[str, ...]:
        if docs_sorted[0] is None:
            docs_sorted[0] = tuple(sorted(docs))
        return docs_sorted[0]

    universe = {p.rsplit("/", 1)[-1] for entry in log for _s, p in entry.changes}
    basenames = BasenameIndex(universe)
    basename_pending: Set[str] = set()
    basename_started = [False]

    def sync_basenames() -> None:
        if not basename_started[0]:
            basename_started[0] = True
            pending = set(docs)
        else:
            pending = set(basename_pending)
        basename_pending.clear()
        for doc in sorted(pending):
            if doc not in docs:
                basenames.remove_doc(doc)
                continue
            blob = blobs[doc]
            tokens = cache.get_tokens(blob, TOKENIZER_VERSION)
            if tokens is None:
                tokens = basename_tokens(read_blob_text(blob))
                cache.put_tokens(blob, TOKENIZER_VERSION, tokens)
            basenames.set_doc(doc, tokens)

    popularity = PopularityWindow(window)
    prior: List[Tuple[str, Changes]] = []

    def rank_all(parent: str, query: Sequence[str]) -> Dict[str, Tuple[str, ...]]:
        """Every method's full ranking for one commit, from the state at its parent."""
        counters["ranked"] += 1
        snapshot = Snapshot(parent, index, blobs, doc_tuple(), read_text,
                            _Prefix(prior, len(prior)))
        edges: Dict[str, List[Edge]] = {}
        for kind, provider in providers.items():
            lean = getattr(provider, "relevant_edges", None) if incremental else None
            edges[kind] = (lean(snapshot, query) if lean is not None
                           else provider.edges_at(snapshot))
        out: Dict[str, Tuple[str, ...]] = {}
        for arm, kinds in arm_kinds.items():
            pool = edges[kinds[0]] if len(kinds) == 1 else [
                e for kind in kinds for e in edges[kind]]
            ranked = rank_impact(ImpactIndex(pool, len(docs)), query, kinds=kinds,
                                 weights=weights, top=None, combine=arm_combine[arm])
            out[_method_name(arm)] = tuple(r["doc"] for r in ranked)
        sync_basenames()
        out["popularity"] = tuple(popularity.ranking(docs))
        out["basename"] = tuple(basenames.rank(query))
        out["basename_idf"] = tuple(basenames.rank_idf(query, len(docs)))
        return out

    skipped = {reason: 0 for reason in SKIP_REASONS}
    eligible: List[Tuple[str, int, Tuple[str, ...], Dict[str, Tuple[str, ...]]]] = []
    alarm_any = {m: 0 for m in methods}
    alarm_returned = {m: 0 for m in methods}
    alarm_commits = 0
    n_scanned = new_docs = excluded_docs = 0

    try:
        for entry in log:
            changes: Changes = tuple((s, p) for s, p in entry.changes if in_path_set(p))
            parent = entry.parents[0] if entry.parents else ""
            in_window = entry.sha in scanned
            if in_window and not tree_ready:
                tree_ready = True
                if parent:
                    try:
                        tree = ls_tree(root, parent)
                    except GitError:
                        tree, parent = {}, ""       # a shallow boundary: nothing before it
                    for path in sorted(tree):
                        if in_path_set(path):
                            index.add(path)
                            blobs[path] = tree[path]
                            if role(path) == "doc":
                                docs.add(path)
            got = classify_commit(changes, bool(parent), settings, max_files, role)
            if in_window:
                n_scanned += 1
                if got.reason != "root_commit":     # a first commit creates everything
                    new_docs += got.new_docs
                    excluded_docs += got.excluded_docs
                if got.reason is not None:
                    skipped[got.reason] += 1
                if got.reason is None or got.reason == "no_docs_edited":
                    texts.clear()
                    held[0] = 0
                    rankings = rank_all(parent, got.query)
                    if got.reason is None:
                        eligible.append((entry.sha, len(got.query), got.gold, rankings))
                    else:
                        alarm_commits += 1
                        for name in methods:
                            alarm_any[name] += 1 if rankings[name] else 0
                            alarm_returned[name] += min(len(rankings[name]), 5)
            # Only now does the commit itself become visible to anything.
            if tree_ready:
                for status, path in changes:
                    if status == "D":
                        index.remove(path)
                        blobs.pop(path, None)
                        if path in docs:
                            docs.discard(path)
                            docs_sorted[0] = None
                    else:
                        if status == "A":
                            index.add(path)
                        blobs[path] = entry.blobs[path]
                        if role(path) == "doc" and path not in docs:
                            docs.add(path)
                            docs_sorted[0] = None
                    if basename_started[0] and role(path) == "doc":
                        basename_pending.add(path)
            popularity.observe(got.edited_docs)
            prior.append((entry.sha, changes))
            for provider in providers.values():
                whole = getattr(provider, "observe_entry", None)
                if whole is not None:
                    whole(entry)
                else:
                    provider.observe(entry.sha, changes)
    except GitError as exc:
        raise ReplayError(str(exc)) from exc
    finally:
        reader.close()
        cache.close()
    walked = time.perf_counter()

    # -- scoring ------------------------------------------------------------
    n_eligible = len(eligible)
    gold_counts: Dict[str, int] = {}
    for _sha, _nq, gold, _rankings in eligible:
        for doc in gold:
            gold_counts[doc] = gold_counts.get(doc, 0) + 1
    hubs = sorted((doc for doc, n in gold_counts.items()
                   if n_eligible and n / n_eligible > hub_share),
                  key=lambda doc: (-gold_counts[doc], doc))
    hub_set = set(hubs)
    ci_k = 5 if 5 in ks else top

    def variant(exclude: Set[str]) -> Tuple[dict, dict, dict, int, List[Optional[dict]]]:
        rows: Dict[str, List[dict]] = {m: [] for m in methods}
        ranks: List[Optional[dict]] = []
        for _sha, _nq, gold, rankings in eligible:
            kept_gold = [d for d in gold if d not in exclude]
            if not kept_gold:
                ranks.append(None)
                continue
            row_ranks = {}
            for name in methods:
                # Hubs go before truncation: filtering a full ranking is the same
                # as never ranking them (see rank_impact's exclude_docs).
                ranked = ([d for d in rankings[name] if d not in exclude]
                          if exclude else rankings[name])
                row = score_ranking(ranked, kept_gold, ks)
                rows[name].append(row)
                row_ranks[name] = row["first_rank"]
            ranks.append(row_ranks)
        n = len(rows[methods[0]]) if methods else 0
        table = {}
        for name in methods:
            agg = aggregate(rows[name], ks)
            series = {
                f"recall_at_{ci_k}": [r["recall_at"][ci_k] for r in rows[name]],
                f"any_hit_at_{ci_k}": [float(r["any_hit_at"][ci_k]) for r in rows[name]],
                "mrr": [r["rr"] for r in rows[name]],
            }
            agg["ci95"] = {key: list(bootstrap_ci(values, resamples=resamples))
                           for key, values in series.items()}
            table[name] = agg
        paired: Dict[str, dict] = {}
        judgement: Dict[str, str] = {}
        arm_methods = [_method_name(a) for a in arm_kinds]
        for name in arm_methods:
            paired[name] = {}
            for other in [*BASELINES, *(m for m in arm_methods if m != name)]:
                paired[name][other] = {
                    "mrr": paired_bootstrap([r["rr"] for r in rows[name]],
                                            [r["rr"] for r in rows[other]],
                                            resamples=resamples),
                    f"recall_at_{ci_k}": paired_bootstrap(
                        [r["recall_at"][ci_k] for r in rows[name]],
                        [r["recall_at"][ci_k] for r in rows[other]], resamples=resamples),
                }
            if n < min_population:
                judgement[name] = "inconclusive"
            elif all(paired[name][b]["mrr"]["ci95"][0] > 0 for b in BASELINES):
                judgement[name] = "pass"
            else:
                judgement[name] = "fail"
        return table, paired, judgement, n, ranks

    table, paired, judgement, _n, ranks = variant(set())
    hub_table, hub_paired, hub_judgement, hub_n, hub_ranks = variant(hub_set)

    gold_sizes = [len(gold) for _sha, _nq, gold, _r in eligible]
    report: Dict[str, Any] = {
        "schema": REPLAY_SCHEMA,
        "repo": {"head": head, "range": label},
        "settings": {
            "arms": {arm: list(kinds) for arm, kinds in sorted(arm_kinds.items())},
            "combine": {arm: arm_combine[arm] for arm, kinds in sorted(arm_kinds.items())
                        if len(kinds) > 1},
            "cochange": (dataclasses.asdict(settings.cochange)
                         if "cochange" in providers else {}),
            "ks": list(ks), "hub_share": hub_share, "max_files": max_files,
            "popularity_window": window, "min_population": min_population,
            "resamples": int(resamples), "doc_globs": list(settings.doc_globs),
            "excluded_paths": list(settings.excluded_paths), "config": config,
            "fingerprint": settings.fingerprint(), "weights": dict(sorted(weights.items())),
        },
        "population": {
            "commits_scanned": n_scanned, "eligible": n_eligible, "skipped": skipped,
            "gold_docs_mean": statistics.fmean(gold_sizes) if gold_sizes else 0.0,
            "gold_docs_median": statistics.median(gold_sizes) if gold_sizes else 0,
            "docs_at_end": len(docs), "new_docs_not_gold": new_docs,
            "excluded_doc_paths": excluded_docs,
            "conclusive": n_eligible >= min_population,
        },
        "methods": table,
        "paired": paired,
        "judgement": judgement,
        # Predicting the hub docs is enough to score well on the main table, so
        # when there are hubs the table without them is the one to judge by.
        "verdict_table": "no_hub" if hubs else "all",
        "no_hub": {
            "hub_docs": [{"doc": doc, "commits": gold_counts[doc],
                          "share": gold_counts[doc] / n_eligible} for doc in hubs],
            "population": {"eligible": hub_n, "dropped": n_eligible - hub_n,
                           "conclusive": hub_n >= min_population},
            "methods": hub_table, "paired": hub_paired, "judgement": hub_judgement,
        },
        "alarm": {
            "commits": alarm_commits,
            "methods": {name: {
                "share_any": alarm_any[name] / alarm_commits if alarm_commits else 0.0,
                "mean_returned_at_5": (alarm_returned[name] / alarm_commits
                                       if alarm_commits else 0.0),
            } for name in methods},
        },
    }
    if per_commit:
        report["per_commit"] = [
            {"commit": sha, "n_query": n_query, "n_gold": len(gold), "gold": list(gold),
             "ranks": ranks[i], "no_hub_ranks": hub_ranks[i]}
            for i, (sha, n_query, gold, _rankings) in enumerate(eligible)]
    if stats is not None:
        done = time.perf_counter()
        stats.update(counters)
        stats.update({
            "commits_walked": len(log), "commits_scanned": n_scanned,
            "cache_hits": cache.hits, "cache_misses": cache.misses,
            "cache_on_disk": cache_on_disk, "text_held_peak": held[1],
            "walk_seconds": walked - started, "score_seconds": done - walked,
            "seconds": done - started,
        })
    return _rounded(report)


def _rounded(node):
    if isinstance(node, float):
        return round(node, 6)
    if isinstance(node, dict):
        return {key: _rounded(value) for key, value in node.items()}
    if isinstance(node, (list, tuple)):
        return [_rounded(value) for value in node]
    return node


# -- text report -------------------------------------------------------------------

def _plural(n: int, noun: str) -> str:
    return f"{n:,} {noun}" + ("" if n == 1 else "s")


def _fmt(value: float) -> str:
    return f"{value:.3f}"


def _signed(value: float) -> str:
    return f"{value:+.3f}"


def _tables(lines: List[str], methods: dict, paired: dict, judgement: dict, ks: Sequence[int],
            conclusive: bool, min_population: int, n: int, verdict: bool = True) -> None:
    if not n:
        lines.append("  (no eligible commits: nothing to score)")
        return
    names = list(methods)
    width = max(len(name) for name in names) + 2
    p_k = "5" if "5" in methods[names[0]]["precision_at"] else str(ks[-1])
    head = "".join(f"{'R@' + str(k):<7}" for k in ks)
    lines.append(f"{'':<{width}}{head}{'P@' + p_k:<7}{'any@' + p_k:<7}{'MRR':<7}returned")
    footnote = (f"(P@{p_k} divides by {p_k}, so a method that returns one right doc scores at "
                f"most {1 / int(p_k):.3g}; MRR reads the whole ranking)")
    for name in names:
        m = methods[name]
        cells = "".join(f"{_fmt(m['recall_at'][str(k)]):<7}" for k in ks)
        lines.append(f"{name:<{width}}{cells}{_fmt(m['precision_at'][p_k]):<7}"
                     f"{_fmt(m['any_hit_at'][p_k]):<7}{_fmt(m['mrr']):<7}"
                     f"{m['mean_returned']:.1f}")
    lines.append(footnote)
    lines.append("")
    lines.append("95% intervals (bootstrap over commits):")
    for name in names:
        ci = methods[name]["ci95"]
        parts = [f"{key.replace('_at_', '@').replace('recall', 'R').replace('any_hit', 'any')}"
                 f" [{_fmt(lo)}, {_fmt(hi)}]" for key, (lo, hi) in ci.items()]
        lines.append(f"  {name:<{width}}" + "   ".join(parts))
    lines.append("")
    pair_width = max((len(f"{a} vs {b}") for a in paired for b in paired[a]), default=0) + 2
    for name in paired:
        for other, cell in paired[name].items():
            mrr = cell["mrr"]
            lines.append(
                f"{name + ' vs ' + other:<{pair_width}}dMRR {_signed(mrr['delta'])} "
                f"[{_signed(mrr['ci95'][0])}, {_signed(mrr['ci95'][1])}]   "
                f"better {mrr['better']} / worse {mrr['worse']} / tied {mrr['tied']}")
    if not verdict:
        return
    for name, verdict in judgement.items():
        if verdict == "inconclusive":
            lines.append(f"{name}: INCONCLUSIVE ({n} eligible commits, fewer than the "
                         f"{min_population} needed to call it either way)")
        elif verdict == "pass":
            lines.append(f"{name}: beats every baseline on MRR (each paired interval "
                         f"excludes zero)")
        else:
            lines.append(f"{name}: does not beat every baseline on MRR (a paired interval "
                         f"includes zero or is negative)")


def format_replay(report: dict) -> str:
    """The report as text. The population always comes first."""
    pop = report["population"]
    cfg = report["settings"]
    ks = cfg["ks"]
    head = report["repo"]["head"][:12] or "(none)"
    scanned, n = pop["commits_scanned"], pop["eligible"]
    share = f" ({100.0 * n / scanned:.1f}%)" if scanned else ""
    lines = [f"Replay  repo={head}  range={printable(report['repo']['range'])}",
             f"Population: {_plural(scanned, 'commit')} scanned, {n:,} eligible{share}"]
    reasons = sorted(((count, reason) for reason, count in pop["skipped"].items() if count),
                     key=lambda item: (-item[0], item[1]))
    lines.append("  skipped: " + (", ".join(f"{reason} {count:,}" for count, reason in reasons)
                                  or "none"))
    lines.append(f"  gold docs per eligible commit: mean {pop['gold_docs_mean']:.1f}, "
                 f"median {pop['gold_docs_median']:g}")
    lines.append(f"  docs in corpus at range end: {pop['docs_at_end']:,}     "
                 f"created-in-commit docs not counted as gold: {pop['new_docs_not_gold']:,}")
    if pop["excluded_doc_paths"]:
        lines.append(f"  doc-named paths under excluded directories, ignored: "
                     f"{pop['excluded_doc_paths']:,}")
    # Globs come from the repository's config and hub docs are file names:
    # both are printed with their control characters written out.
    lines.append(f"Settings: config={cfg.get('config', 'defaults')}; "
                 f"docs: {printable(', '.join(cfg['doc_globs']))}")
    lines.append("  excluded_paths: "
                 f"{printable(', '.join(cfg.get('excluded_paths', []))) or '(none)'}")
    lines.append("")
    no_hub = report["no_hub"]
    hubs = bool(no_hub["hub_docs"])
    pct = f"{100 * cfg['hub_share']:g}%"
    _tables(lines, report["methods"], report["paired"], report["judgement"], ks,
            pop["conclusive"], cfg["min_population"], n, verdict=not hubs)
    if hubs and n:
        count = len(no_hub["hub_docs"])
        lines.append(f"Note: {_plural(count, 'hub doc')} {'is' if count == 1 else 'are'} gold "
                     f"in more than {pct} of eligible commits, so the table above mostly "
                     f"measures who predicts the hubs. The verdict is given on the table "
                     f"without them.")
    lines.append("")
    if hubs:
        named = ", ".join(f"{printable(h['doc'])} {100 * h['share']:.0f}%"
                          for h in no_hub["hub_docs"])
        lines.append(f"Without hub docs (>{pct} of eligible commits: {named}): "
                     f"{no_hub['population']['eligible']:,} eligible")
        _tables(lines, no_hub["methods"], no_hub["paired"], no_hub["judgement"], ks,
                no_hub["population"]["conclusive"], cfg["min_population"],
                no_hub["population"]["eligible"])
    else:
        lines.append(f"Without hub docs: no doc is gold in more than {pct} of eligible "
                     f"commits, so this variant equals the tables above")
    alarm = report["alarm"]
    if alarm["commits"]:
        lines.append("")
        lines.append(f"Source-only commits ({alarm['commits']:,}, unlabelled): "
                     f"share with any prediction / mean returned at 5")
        width = max(len(name) for name in alarm["methods"]) + 2
        for name, cell in alarm["methods"].items():
            lines.append(f"  {name:<{width}}{cell['share_any']:.3f} / "
                         f"{cell['mean_returned_at_5']:.2f}")
    return "\n".join(lines).rstrip() + "\n"


# -- command line ------------------------------------------------------------------

def build_parser(prog: str = "carta graph replay") -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=prog,
        description="Score the graph arm against a repository's own history: for each mainline "
                    "commit, predict the docs from the code it touched and compare with the "
                    "docs it edited. Reads git objects only.")
    parser.add_argument("--repo", default=".", metavar="PATH",
                        help="Any path inside the repository (default: the current directory)")
    parser.add_argument("--range", dest="range_spec", default=None, metavar="A..B",
                        help="Scan the mainline commits of a git range")
    parser.add_argument("--last", type=int, default=0, metavar="N",
                        help="Scan the most recent N mainline commits (default 0: all of them)")
    parser.add_argument("--arm", action="append", default=[], metavar="NAME=KIND[+KIND...][@MODE]",
                        help="An arm to score: a name and the edge kinds it reads, and for "
                             "several kinds how they are fused: max, sum, norm or rrf "
                             "(repeatable; default mention=mention)")
    parser.add_argument("--k", default=None, metavar="K[,K...]",
                        help="Cutoffs to report (default 1,3,5,10)")
    parser.add_argument("--hub-share", type=float, default=None, metavar="F",
                        help="A doc in gold for more than this share of eligible commits is a "
                             "hub (default: graph.replay.hub_share, 0.2)")
    parser.add_argument("--max-files", type=int, default=None, metavar="N",
                        help="Skip commits touching more paths than this as bulk "
                             "(default: graph.replay.max_files, 100)")
    parser.add_argument("--popularity-window", type=int, default=None, metavar="N",
                        help="Commits of history the popularity baseline counts (default 200)")
    parser.add_argument("--min-population", type=int, default=None, metavar="N",
                        help="Below this many eligible commits a result is inconclusive "
                             "(default 30)")
    parser.add_argument("--doc-glob", action="append", default=[], metavar="GLOB",
                        help="Which paths are docs (repeatable; default: graph.doc_globs)")
    parser.add_argument("--exclude", action="append", default=[], metavar="PATTERN",
                        help="An extra excluded_paths entry for the doc side (repeatable)")
    parser.add_argument("--no-config", action="store_true",
                        help="Ignore <repo>/.carta/config.yaml and use the built-in defaults "
                             "(the report records which was used)")
    parser.add_argument("--trust-config", action="store_true",
                        help="Also use graph.patterns from the repository's config. They are "
                             "regular expressions run over every doc line; only for a "
                             "repository you trust")
    parser.add_argument("--resamples", type=int, default=RESAMPLES, metavar="N",
                        help=f"Bootstrap resamples (default {RESAMPLES})")
    parser.add_argument("--per-commit", action="store_true",
                        help="Include one row per eligible commit in the JSON")
    parser.add_argument("--no-cache", action="store_true",
                        help="Do not read or write the on-disk blob cache")
    parser.add_argument("--cache-dir", default=None, metavar="DIR",
                        help="Keep the blob cache in DIR instead of the carta home")
    parser.add_argument("--json", nargs="?", const="-", default=None, metavar="PATH",
                        help="Print the report as JSON, or write it to PATH and print the text")
    return parser


def _read_repo_config(root: Path) -> Optional[dict]:
    """The repository's ``.carta/config.yaml`` as a mapping; ``None`` when it has none.

    Read without following a symbolic link (the file, or ``.carta`` itself,
    could point anywhere), and an error never quotes the file: what a YAML
    parser prints about a bad line is that line.
    """
    name = ".carta/config.yaml"
    try:
        data = read_regular_file(root, name, MAX_CONFIG_BYTES)
    except SkippedFile as skipped:
        if skipped.reason == "missing":
            return None
        why = {"symlink": "is (or is behind) a symbolic link",
               "too_large": f"is larger than {MAX_CONFIG_BYTES} bytes"}.get(
                   skipped.reason, "is not a regular file")
        raise ConfigError(f"{name} {why}; it was not read (pass --no-config to "
                          f"replay with the defaults)") from None
    except OSError as exc:
        raise ConfigError(f"could not read {name}: {exc.strerror or 'I/O error'}") from None
    import yaml
    try:
        raw = yaml.safe_load(data.decode("utf-8")) or {}
    except UnicodeDecodeError:
        raise ConfigError(f"{name} is not UTF-8 text") from None
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f" at line {mark.line + 1}" if mark is not None else ""
        raise ConfigError(f"{name} is not valid YAML{where}") from None
    if not isinstance(raw, dict):
        raise ConfigError(f"{name} must be a mapping")
    return raw


def _repo_settings(root: Path, doc_globs: Sequence[str], excludes: Sequence[str],
                   use_config: bool = True, trust_config: bool = False
                   ) -> Tuple[GraphSettings, str]:
    """Graph settings for a repository, and a label saying where they came from.

    The repository's Carta config when it has one (read from the work tree),
    else the defaults; command-line options on top of either. Unless
    ``trust_config`` is set the config's ``graph.patterns`` are dropped (and
    the label says so): they are regular expressions, and the repository
    being replayed need not be one the caller wrote.
    """
    cfg: Dict[str, Any] = {}
    source = "defaults"
    raw = _read_repo_config(root) if use_config else None
    if raw is not None:
        source = ".carta/config.yaml"
        for key in ("graph", "excluded_paths"):
            if raw.get(key) is not None:
                cfg[key] = raw[key]
        graph_cfg = cfg.get("graph")
        if not trust_config and isinstance(graph_cfg, dict) and graph_cfg.get("patterns"):
            cfg["graph"] = {k: v for k, v in graph_cfg.items() if k != "patterns"}
            source += " (graph.patterns ignored)"
    if doc_globs:
        graph = dict(cfg.get("graph") or {}) if isinstance(cfg.get("graph"), dict) else {}
        if "graph" in cfg and not isinstance(cfg["graph"], dict):
            raise ConfigError(f"graph must be a mapping, got {type(cfg['graph']).__name__}")
        graph["doc_globs"] = list(doc_globs)
        cfg["graph"] = graph
    if excludes:
        from carta.config import DEFAULTS
        base = cfg.get("excluded_paths", DEFAULTS["excluded_paths"]) or []
        cfg["excluded_paths"] = [*base, *excludes]
    if doc_globs or excludes:
        source += " + command line"
    return graph_settings(cfg), source


def _parse_arms(specs: Sequence[str]) -> Optional[Dict[str, List[str]]]:
    if not specs:
        return None
    arms: Dict[str, List[str]] = {}
    for spec in specs:
        name, sep, kinds = spec.partition("=")
        if not sep or not name.strip() or not kinds.strip():
            raise ReplayError(f"--arm {spec!r}: expected NAME=KIND[+KIND...]")
        kinds, at, mode = kinds.partition("@")
        arms[name.strip()] = [k.strip() for k in kinds.split("+") if k.strip()]
        if at:
            arms[name.strip()].append("@" + mode.strip())
    return arms


def main(argv: Optional[Sequence[str]] = None, prog: str = "carta graph replay") -> int:
    args = build_parser(prog).parse_args(argv)
    try:
        root = toplevel(Path(args.repo)) if Path(args.repo).is_dir() else None
        if root is None:
            raise ReplayError(f"{args.repo} is not inside a git repository")
        ks = DEFAULT_KS
        if args.k:
            try:
                ks = tuple(int(part) for part in args.k.split(",") if part.strip())
            except ValueError:
                raise ReplayError(f"--k {args.k!r}: expected positive integers") from None
        settings, source = _repo_settings(root, args.doc_glob, args.exclude,
                                          use_config=not args.no_config,
                                          trust_config=args.trust_config)
        cache_path = None
        if args.cache_dir:
            cache_path = Path(args.cache_dir) / default_cache_path(root).name
        stats: dict = {}
        report = run_replay(
            root, range_spec=args.range_spec, last=args.last, arms=_parse_arms(args.arm), ks=ks,
            settings=settings, hub_share=args.hub_share, max_files=args.max_files,
            popularity_window=args.popularity_window, min_population=args.min_population,
            cache_path=cache_path, use_disk_cache=not args.no_cache,
            per_commit=args.per_commit, resamples=args.resamples, stats=stats, config=source)
        if args.json and args.json != "-":
            out = Path(args.json)
            try:
                write_text_replacing(out, json.dumps(report, indent=2, sort_keys=True) + "\n")
            except OSError as exc:
                raise ReplayError(f"could not write the report to {out}: {exc}") from exc
    except (ReplayError, ConfigError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    if args.json == "-":
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        sys.stdout.write(format_replay(report))
    seconds = stats.get("seconds", 0.0)
    rate = stats.get("commits_scanned", 0) / seconds if seconds > 0 else 0.0
    print(f"replayed {stats.get('commits_scanned', 0):,} commits "
          f"({stats.get('ranked', 0):,} ranked, {stats.get('commits_walked', 0):,} walked for "
          f"warm-up) in {seconds:.2f} s: {rate:,.0f} commits/s; "
          f"{stats.get('blob_reads', 0):,} blobs read, "
          f"{stats.get('docs_resolved', 0):,} doc resolutions", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(prog="python -m carta.graph.eval.replay"))
