"""The impact query: given changed paths, which docs describe them?

``rank_impact`` is a pure function over an in-memory ``ImpactIndex``. It is
the function the replay eval scores, so the eval measures what ships.
``run_impact`` wraps it for the CLI and the MCP tool: it brings the edge store
up to date, resolves the queried paths (or a git range) and returns the JSON
object both surfaces print.
"""
from __future__ import annotations

import json
import math
import os
import posixpath
from bisect import bisect_left
from pathlib import Path
from typing import Collection, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from carta.graph.build import build_graph, decode_text, list_repo_paths, read_doc
from carta.graph.gitio import (
    GitError, default_branch, diff_name_status, rev_list_first_parent,
)
from carta.graph.mentions import extract_mentions
from carta.graph.model import KINDS, MENTION_EXTRACTOR, Edge, printable
from carta.graph.paths import PathIndex, glob_matcher, in_path_set, is_doc_path
from carta.graph.safeio import entry_kind
from carta.graph.settings import GraphSettings, graph_settings
from carta.graph.store import (
    EdgeStore, StoreUnreadableError, StoreUnsafeError, StoreVersionError, store_path,
)

IMPACT_SCHEMA = 1
DEFAULT_KIND_WEIGHTS: Dict[str, float] = {"mention": 1.0, "symbol": 0.8, "cochange": 1.0}
GLOB_SPEC = 0.5
EVIDENCE_LINES_SHOWN = 3
# How the rankings of several edge kinds become one. ``max``: one ranking in
# which a doc is worth its best kind per queried path. The others rank each
# kind on its own and fuse: ``sum`` adds the scores, ``norm`` adds each score
# as a share of its kind's best, ``rrf`` adds ``(RRF_K + 1) / (RRF_K + rank)``
# (reciprocal rank fusion, scaled so that first place in one kind is worth
# that kind's weight). A kind that returns nothing takes no part.
COMBINE_MODES = ("max", "sum", "norm", "rrf")
RRF_K = 60
# Edge kinds that record history, not something a doc says.
HISTORY_KINDS = frozenset(("cochange",))

# Target types a queried path can match. ``token`` and ``pattern`` targets are
# not paths and never take part in an impact query.
_PATH_TYPES = frozenset(("file", "doc", "dir", "glob", "missing"))
# Matches that name the queried path itself; directory and glob matches are
# the weaker kind and are aggregated per target (see rank_impact).
_STRONG = frozenset(("exact", "under", "cochange"))


_STORE = ".carta/graph/edges.db"


class ImpactError(ValueError):
    """The impact query cannot be answered as asked (bad input, or no store)."""


class _Mention:
    """Everything one doc says about one target, within one edge kind."""

    __slots__ = ("lines", "weight")

    def __init__(self) -> None:
        self.lines: Dict[int, str] = {}
        self.weight = 0.0

    def add(self, edge: Edge) -> None:
        current = self.lines.get(edge.line)
        if current is None or edge.evidence < current:
            self.lines[edge.line] = edge.evidence
        if edge.weight > self.weight:
            self.weight = edge.weight


class ImpactIndex:
    """Edges arranged for reverse lookup: target -> the docs that name it.

    ``n_docs`` is the size of the corpus (every doc, not only those with
    edges); it is the ``N`` of the rarity weight.
    """

    def __init__(self, edges: Iterable[Edge], n_docs: int) -> None:
        self.n_docs = int(n_docs)
        self._targets: Dict[Tuple[str, str], Dict[str, _Mention]] = {}
        self._types: Dict[Tuple[str, str], str] = {}
        self._by_src: Dict[Tuple[str, str], Dict[str, _Mention]] = {}
        docs = set()
        for edge in edges:
            if edge.src_type == "file":
                # A file-sourced row (co-change) points at a doc. Only that
                # direction is read; the writer stores both ordered pairs.
                if edge.dst_type != "doc":
                    continue
                bucket = self._by_src.setdefault((edge.kind, edge.src), {})
                doc = edge.dst
            else:
                if edge.dst_type not in _PATH_TYPES:
                    continue
                key = (edge.kind, edge.dst)
                bucket = self._targets.setdefault(key, {})
                known = self._types.get(key)
                if known is None or edge.dst_type < known:
                    self._types[key] = edge.dst_type
                doc = edge.src
            docs.add(doc)
            mention = bucket.get(doc)
            if mention is None:
                mention = bucket[doc] = _Mention()
            mention.add(edge)
        self._doc_count = len(docs)
        self._sorted: Dict[str, List[str]] = {}
        self._globs: Dict[str, List[str]] = {}
        for kind, dst in sorted(self._targets):
            self._sorted.setdefault(kind, []).append(dst)
            if self._types[(kind, dst)] == "glob":
                self._globs.setdefault(kind, []).append(dst)

    def df(self, kind: str, dst: str) -> int:
        """How many distinct docs have an edge of ``kind`` to ``dst``."""
        return len(self._targets.get((kind, dst), ()))

    def _idf(self, kind: str, dst: str) -> float:
        df = self.df(kind, dst)
        if not df:
            return 0.0
        n = max(self.n_docs, self._doc_count, df)
        return math.log(1.0 + n / df)

    def _path_matches(self, kind: str, q: str) -> List[Tuple[str, str, float]]:
        """``(target, match, spec)`` for every target of ``kind`` that matches ``q``."""
        out: List[Tuple[str, str, float]] = []
        dst_type = self._types.get((kind, q))
        if dst_type is not None and dst_type != "glob":
            out.append((q, "exact", 1.0))
        if q.endswith("/"):
            ordered = self._sorted.get(kind, [])
            i = bisect_left(ordered, q)
            while i < len(ordered) and ordered[i].startswith(q):
                if ordered[i] != q:
                    out.append((ordered[i], "under", 1.0))
                i += 1
        parts = q.rstrip("/").split("/")
        for i in range(1, len(parts)):
            parent = "/".join(parts[:i]) + "/"
            # A directory that no longer exists is stored as ``missing``; it
            # still covers the files that were under it.
            if self._types.get((kind, parent)) in ("dir", "missing"):
                out.append((parent, "dir", 1.0 / (1 + len(parts) - i)))
        if not q.endswith("/"):
            for glob in self._globs.get(kind, ()):
                if glob_matcher(glob).match(q):
                    out.append((glob, "glob", GLOB_SPEC))
        return out

    def is_matched(self, path: str, kinds: Sequence[str]) -> bool:
        """True when some doc names ``path`` closely enough to call it covered.

        An exact, glob or direct-parent directory match counts (and a file
        co-change row). A doc naming a directory further up does not: a
        mention of a top-level package directory would otherwise "match"
        every file beneath it.
        """
        for kind in kinds:
            if self._by_src.get((kind, path)):
                return True
            for _target, match, spec in self._path_matches(kind, path):
                if match != "dir" or spec >= 0.5:
                    return True
        return False


def _tf(mention: _Mention) -> float:
    return 1.0 + math.log(len(mention.lines))


def _best(cands: list) -> tuple:
    """Largest value; ties go to the smaller target, then the smaller kind."""
    return min(cands, key=lambda c: (-c[0], c[2], c[1]))


def rank_impact(index: ImpactIndex, paths: Sequence[str], *,
                kinds: Sequence[str] = ("mention",),
                weights: Mapping[str, float] = DEFAULT_KIND_WEIGHTS,
                top: Optional[int] = 10, min_score: float = 0.0,
                exclude_docs: Collection[str] = (), combine: str = "max") -> List[dict]:
    """Rank the docs that name the queried paths. Pure: no I/O, no clock.

    For a doc ``d``, a queried path ``q`` and a matching target ``t``::

        value(d, t, q) = w_kind * spec(t, q) * idf(t) * tf(d, t) * w_edge(d, t)

    with ``spec`` 1.0 for an exact match (or a target under a queried
    directory), ``1 / (1 + depth)`` for a directory target ``depth`` levels
    above ``q`` and 0.5 for a glob; ``idf = ln(1 + N / df)``; ``tf = 1 + ln``
    of the distinct lines of ``d`` that name ``t``; ``w_edge`` the largest
    stored weight. A file-sourced row (co-change) is worth ``w_kind * weight``
    with no ``idf``.

    A path the doc names exactly contributes its best value. Every other path
    is assigned to the doc's best directory or glob target, and each such
    target contributes once: its best value times ``1 + ln(paths assigned)``.
    So a doc naming a common directory is not paid once per changed file.

    Returns ``[{"rank", "doc", "score", "matches": [...]}]`` sorted by score
    descending then doc ascending. Scores are not rounded here. A directory or
    glob target's contribution is reported split evenly over the paths it
    covers, so a result's match contributions sum to its score. ``top=None``
    returns everything.

    ``combine`` only matters when more than one kind is read: see
    ``COMBINE_MODES``. Under ``norm`` and ``rrf`` the score is a fused value
    and no longer the sum of the match contributions, which stay in each
    kind's own units; ``by_kind`` on every result gives the doc's rank and
    score within each kind that found it.
    """
    kinds = sorted(set(kinds))
    if combine not in COMBINE_MODES:
        raise ValueError(f"unknown combine mode {combine!r}")
    if combine != "max" and len(kinds) > 1:
        return _rank_fused(index, paths, kinds, weights, top, min_score, exclude_docs, combine)
    queried = sorted(set(p for p in paths if p))
    excluded = set(exclude_docs)
    # doc -> path -> [(value, kind, target, match, mention)]
    per_doc: Dict[str, Dict[str, list]] = {}

    def offer(doc: str, q: str, cand: tuple) -> None:
        if cand[0] > 0.0 and doc not in excluded:
            per_doc.setdefault(doc, {}).setdefault(q, []).append(cand)

    for kind in kinds:
        w_kind = float(weights.get(kind, 1.0))
        for q in queried:
            for doc, mention in index._by_src.get((kind, q), {}).items():
                offer(doc, q, (w_kind * mention.weight, kind, q, "cochange", mention))
            for target, match, spec in index._path_matches(kind, q):
                base = w_kind * spec * index._idf(kind, target)
                for doc, mention in index._targets[(kind, target)].items():
                    offer(doc, q, (base * _tf(mention) * mention.weight, kind, target, match,
                                   mention))

    scored = []
    for doc, by_path in per_doc.items():
        plan: List[Tuple[str, tuple, float]] = []          # (path, candidate, contribution)
        assigned: Dict[Tuple[str, str], List[Tuple[str, tuple]]] = {}
        for q in sorted(by_path):
            strong = [c for c in by_path[q] if c[3] in _STRONG]
            if strong:
                best = _best(strong)
                plan.append((q, best, best[0]))
            else:
                best = _best(by_path[q])
                assigned.setdefault((best[1], best[2]), []).append((q, best))
        for key in sorted(assigned):
            items = assigned[key]
            total = max(c[0] for _q, c in items) * (1.0 + math.log(len(items)))
            for q, cand in items:
                plan.append((q, cand, total / len(items)))
        score = math.fsum(part for _q, _c, part in plan)
        if score > 0.0 and score >= min_score:
            scored.append((score, doc, plan))

    scored.sort(key=lambda item: (-item[0], item[1]))
    if top is not None:
        scored = scored[:max(0, int(top))]
    results = []
    for rank, (score, doc, plan) in enumerate(scored, 1):
        matches = []
        for q, (_value, kind, target, match, mention), part in plan:
            if match == "cochange":
                target_type, df = "file", len(index._by_src[(kind, q)])
            else:
                target_type, df = index._types[(kind, target)], index.df(kind, target)
            matches.append({
                "path": q, "target": target, "target_type": target_type, "kind": kind,
                "match": match, "df": df, "contribution": part,
                "evidence": [{"line": line, "text": mention.lines[line]}
                             for line in sorted(mention.lines)],
            })
        matches.sort(key=lambda x: (-x["contribution"], x["path"], x["target"], x["kind"]))
        by_kind = ({kinds[0]: {"rank": rank, "score": score}} if len(kinds) == 1 else {})
        results.append({"rank": rank, "doc": doc, "score": score, "matches": matches,
                        "by_kind": by_kind})
    return results


def _rank_fused(index: ImpactIndex, paths: Sequence[str], kinds: Sequence[str],
                weights: Mapping[str, float], top: Optional[int], min_score: float,
                exclude_docs: Collection[str], combine: str) -> List[dict]:
    """Rank each kind on its own, then fuse the rankings (see ``COMBINE_MODES``)."""
    fused: Dict[str, float] = {}
    found: Dict[str, Dict[str, dict]] = {}
    rankings = {kind: rank_impact(index, paths, kinds=(kind,), weights=weights, top=None,
                                  exclude_docs=exclude_docs) for kind in kinds}
    answered = [kind for kind in kinds if rankings[kind]]
    if len(answered) <= 1:
        # Fusing with an empty ranking is the identity: the one kind that
        # answered keeps its own scores.
        return rank_impact(index, paths, kinds=answered or kinds[:1], weights=weights,
                           top=top, min_score=min_score, exclude_docs=exclude_docs)
    for kind in kinds:
        ranked = rankings[kind]
        best = ranked[0]["score"] if ranked else 0.0
        w_kind = float(weights.get(kind, 1.0))
        for item in ranked:
            if combine == "sum":
                part = item["score"]
            elif combine == "norm":
                part = w_kind * item["score"] / best
            else:
                part = w_kind * (RRF_K + 1) / (RRF_K + item["rank"])
            fused[item["doc"]] = fused.get(item["doc"], 0.0) + part
            found.setdefault(item["doc"], {})[kind] = item
    scored = sorted(((score, doc) for doc, score in fused.items()
                     if score > 0.0 and score >= min_score),
                    key=lambda item: (-item[0], item[1]))
    if top is not None:
        scored = scored[:max(0, int(top))]
    results = []
    for rank, (score, doc) in enumerate(scored, 1):
        matches = [m for kind in kinds if kind in found[doc] for m in found[doc][kind]["matches"]]
        matches.sort(key=lambda x: (x["kind"], -x["contribution"], x["path"], x["target"]))
        results.append({
            "rank": rank, "doc": doc, "score": score, "matches": matches,
            "by_kind": {kind: {"rank": item["rank"], "score": item["score"]}
                        for kind, item in sorted(found[doc].items())}})
    return results


# -- query inputs ----------------------------------------------------------

def normalise_query_paths(repo_root: Path, cwd: Path,
                          raw_paths: Sequence[str]) -> Tuple[List[str], List[dict]]:
    """Turn paths as typed into sorted repo-relative POSIX paths.

    A path may be absolute, relative to ``cwd`` or relative to the repo root.
    ``./x`` and ``../x`` are always read from ``cwd``; otherwise the reading
    that exists on disk wins (``cwd`` first), and a path that exists under
    neither is taken as repo-relative, which is what a deleted file needs. A
    trailing ``/`` (or naming an existing directory) asks about everything
    under that directory.

    "Exists" and "is a directory" are asked of the repository's own tree and
    never through a symbolic link in it: a link can point anywhere, and the
    answer would say what is there.

    Returns ``(paths, skipped)``; ``skipped`` holds ``{"path", "reason"}`` with
    reason ``outside_repo`` or ``repo_root``.
    """
    root = os.path.realpath(str(repo_root))
    here = os.path.realpath(str(cwd))

    def inside_root(full: str) -> Optional[str]:
        rel = os.path.relpath(full, root).replace(os.sep, "/")
        if rel == ".." or rel.startswith("../"):
            return None
        return "" if rel == "." else rel
    paths, skipped = set(), {}
    for raw in raw_paths:
        text = str(raw).strip().replace(os.sep, "/")
        if not text:
            continue
        wants_dir = text.endswith("/")
        if os.path.isabs(text):
            candidates = [os.path.realpath(text)]
        elif text.startswith(("./", "../")) or text in (".", ".."):
            candidates = [os.path.normpath(os.path.join(here, text))]
        else:
            from_cwd = os.path.normpath(os.path.join(here, text))
            from_root = os.path.normpath(os.path.join(root, text))
            there = inside_root(from_cwd)
            candidates = ([from_cwd, from_root] if there == "" or (there and entry_kind(root, there))
                          else [from_root])
        rel = None
        for full in candidates:
            inside = inside_root(full)
            if inside is None:
                continue
            if inside and (wants_dir or entry_kind(root, inside) == "dir"):
                inside += "/"
            rel = inside
            break
        if rel is None:
            skipped[text] = "outside_repo"
        elif rel == "":
            skipped[text] = "repo_root"
        else:
            paths.add(rel)
    return sorted(paths), [{"path": p, "reason": skipped[p]} for p in sorted(skipped)]


def _diff_paths(repo_root: Path, diff: str) -> Tuple[str, Dict[str, str]]:
    """The range as used, and ``path -> A | D | M`` for what it changed."""
    range_spec = diff
    try:
        if not range_spec:
            range_spec = f"{default_branch(repo_root)}...HEAD"
        return range_spec, diff_name_status(repo_root, range_spec)
    except GitError as exc:
        # The hint is only for an entry of the repository's own tree, never
        # one reached through a link in it: anything else would tell the
        # caller whether an arbitrary file exists. And git's own message is
        # not passed on: it can name absolute paths, and say what is on disk.
        inside = posixpath.normpath(diff) if diff and "\0" not in diff else ""
        if inside and entry_kind(repo_root, inside):
            raise ImpactError(
                f"`{printable(diff)}` is a path, not a range; pass it without --diff") from exc
        raise ImpactError(
            f"`{printable(range_spec)}` is not a revision range git can diff here "
            f"(try `git diff --name-status` with it)") from exc


def _range_commits(repo_root: Path, range_spec: Optional[str]) -> frozenset:
    """The mainline commits a diff range covers; empty for a path query."""
    if not range_spec:
        return frozenset()
    try:
        return frozenset(rev_list_first_parent(repo_root, range_spec))
    except GitError:
        return frozenset()


def _history_edges(edges: Iterable[Edge], own_commits: frozenset) -> List[Edge]:
    """History rows, minus those that rest on the change being asked about.

    The store is mined up to ``HEAD``, so for ``--diff`` it already holds the
    commits of the range itself. A pair whose **first** shared commit lies in
    the range has no evidence older than the question: it says only that the
    change touched both files, which the caller knows. Such a row is left
    out. A pair with older evidence stays, its count including the range.
    """
    if not own_commits:
        return list(edges)
    kept = []
    for edge in edges:
        try:
            first = json.loads(edge.meta).get("first")
        except (ValueError, AttributeError, RecursionError):
            first = None
        if not isinstance(first, str) or first not in own_commits:
            kept.append(edge)
    return kept


def _edges_before(repo_root: Path, settings: GraphSettings, gone: Sequence[str],
                  came: Sequence[str]) -> List[Edge]:
    """Mention edges as they were before ``gone`` went away, extracted in memory.

    The store is built from the working tree, where a deleted path can no
    longer be resolved: a suffix or a bare filename names nothing, a glob
    matches no file, a link is read beside the doc. Those are exactly the docs
    a deletion leaves stale. So the docs are read once more against the path
    set with the deleted paths put back (and the paths the change added taken
    out), and the edges that gives are ranked together with the stored ones.
    Nothing is written.
    """
    current = list_repo_paths(repo_root)
    added = set(came)
    before = PathIndex(sorted((set(current) - added) | {p for p in gone if in_path_set(p)}))
    out: List[Edge] = []
    for doc in current:
        if not is_doc_path(doc, settings):
            continue
        data, _state = read_doc(repo_root, doc)
        if data is None or b"\0" in data:
            continue
        out.extend(e for e in extract_mentions(doc, decode_text(data), before, settings)
                   if e.kind == "mention")
    return out


def run_impact(repo_root: Path, cfg: dict, *, paths: Sequence[str] = (),
               diff: Optional[str] = None, top: Optional[int] = None,
               kinds: Optional[Sequence[str]] = None, include_docs: bool = False,
               min_score: Optional[float] = None, build: bool = True,
               cwd: Optional[Path] = None) -> dict:
    """Answer an impact query against the repository's edge store.

    Give either ``paths`` or ``diff`` (a git range; ``""`` means
    ``<default-branch>...HEAD``), not both. With ``build`` the store is brought
    up to date first; without it an existing store is read as it is. ``cwd``
    is what relative ``paths`` are read against (default: the repo root).

    Returns the object ``carta impact --json`` prints. Raises ``ImpactError``
    for a request that cannot be answered.
    """
    repo_root = Path(repo_root)
    settings = graph_settings(cfg)
    if not settings.enabled:
        raise ImpactError("the graph arm is disabled (graph.enabled is false in the config)")
    paths = [p for p in paths if str(p).strip()]
    if paths and diff is not None:
        raise ImpactError("give paths or a diff range, not both")
    if not paths and diff is None:
        raise ImpactError("give at least one path, or a diff range")
    # Naming a symbol provider is the opt-in: its edges are then read by
    # default, beside whatever graph.impact.kinds lists.
    default_kinds = list(settings.impact_kinds)
    if settings.symbols_provider != "none":
        default_kinds.append("symbol")
    use_kinds = sorted(set(kinds if kinds else default_kinds))
    unknown = [k for k in use_kinds if k not in KINDS]
    if unknown:
        raise ImpactError(
            f"unknown edge kind {unknown[0]!r} (known kinds: {', '.join(KINDS)})")
    use_top = settings.impact_top if top is None else int(top)
    use_min = settings.impact_min_score if min_score is None else float(min_score)
    if use_top < 1:
        raise ImpactError(f"--top must be at least 1, got {use_top}")
    if not math.isfinite(use_min):
        raise ImpactError(f"--min-score must be a finite number, got {use_min}")

    in_diff: frozenset = frozenset()
    range_spec: Optional[str] = None
    skipped: List[dict] = []
    if diff is not None:
        range_spec, changed = _diff_paths(repo_root, diff)
        in_diff = frozenset(changed)
        gone = [p for p, status in changed.items() if status == "D"]
        came = [p for p, status in changed.items() if status == "A"]
        query = []
        for path in changed:
            if not include_docs and is_doc_path(path, settings):
                skipped.append({"path": path, "reason": "doc"})
            else:
                query.append(path)
    else:
        query, skipped = normalise_query_paths(repo_root, cwd or repo_root, paths)
        gone = [q for q in query if not q.endswith("/") and not entry_kind(repo_root, q)]
        came = []

    try:
        built: dict = {}
        if build:
            # History is only read when a history kind is asked for.
            built = build_graph(
                repo_root, cfg,
                cochange=any(k in HISTORY_KINDS for k in use_kinds)).get("symbols", {})
        store = EdgeStore.open_readonly(store_path(repo_root))
    # The store's own messages name it by absolute path; this result also goes
    # to an MCP client, which is told where it is in the repository and no more.
    except StoreVersionError as exc:
        raise ImpactError(
            f"the edge store at {_STORE} was written by a newer Carta; upgrade Carta, or "
            f"delete the file to rebuild it") from exc
    except StoreUnsafeError as exc:
        raise ImpactError(
            f"the edge store at {_STORE} is, or is behind, a symbolic link, or is larger "
            f"than Carta opens; it is never read. Remove it and run `carta graph build`"
        ) from exc
    except StoreUnreadableError as exc:
        raise ImpactError(
            f"the edge store at {_STORE} cannot be read (damaged, or not a store Carta "
            f"wrote); run `carta graph build` to rebuild it") from exc
    if store is None:
        raise ImpactError("no edge store yet; run `carta graph build` (or drop --no-build)")
    with store:
        # A history row is only ever read from a changed file to a doc.
        edges = list(store.iter_edges(kinds=[k for k in use_kinds if k not in HISTORY_KINDS]))
        history = [k for k in use_kinds if k in HISTORY_KINDS]
        if history:
            edges.extend(_history_edges(
                store.iter_edges(kinds=history, dst_types=("doc",)),
                _range_commits(repo_root, range_spec)))
        n_docs = len(store.source_hashes(MENTION_EXTRACTOR))
        total_edges = store.counts()["edges"]
        digest = store.edges_digest((MENTION_EXTRACTOR,))
        symbols_state = built.get("state") or store.get_meta("symbol.state") or "off"
        symbols_detail = (built.get("detail") if built
                          else store.get_meta("symbol.detail")) or ""

    if build and gone and "mention" in use_kinds:
        edges.extend(_edges_before(repo_root, settings, gone, came))
    index = ImpactIndex(edges, n_docs)
    matched = rank_impact(index, query, kinds=use_kinds, weights=dict(settings.impact_weights),
                          top=None, combine=settings.impact_combine)
    ranked = [item for item in matched if item["score"] >= use_min][:use_top]
    results = []
    for item in ranked:
        matches = [dict(match, contribution=round(match["contribution"], 6))
                   for match in item["matches"]]
        results.append({"rank": item["rank"], "doc": item["doc"],
                        "score": round(item["score"], 6), "in_diff": item["doc"] in in_diff,
                        "matches": matches})
    out = {
        "schema": IMPACT_SCHEMA,
        "query": {"paths": query, "diff": range_spec, "kinds": use_kinds, "top": use_top,
                  "min_score": use_min},
        "store": {"docs": n_docs, "edges": total_edges, "digest": digest, "built": bool(build)},
        "matched_docs": len(matched),
        "results": results,
        "unmatched_paths": [q for q in query if not index.is_matched(q, use_kinds)],
        "skipped_paths": skipped,
    }
    if "symbol" in use_kinds:
        # The symbol edges of a failed load are kept, so they are read here
        # too; the answer has to say that they may no longer be true.
        out["symbols"] = {"provider": settings.symbols_provider, "state": symbols_state,
                          "stale": symbols_state == "failed", "detail": symbols_detail}
    return out


# -- human output ----------------------------------------------------------

def _count(n: int, noun: str) -> str:
    return f"{n:,} {noun}" + ("" if n == 1 else "s")


def _match_reason(match: dict) -> str:
    named = f"named by {_count(match['df'], 'doc')}"
    kind = match["match"]
    if match["kind"] == "symbol":
        # No doc names the path: it names an identifier this file defines.
        docs = _count(match["df"], "doc")
        return (f"symbol: the doc names an identifier defined here "
                f"({docs} {'does' if match['df'] == 1 else 'do'})")
    if kind == "exact":
        return f"exact, {named}"
    if kind == "under":
        return f"names {printable(match['target'])} under it, {named}"
    if kind == "cochange":
        said = match["evidence"][0]["text"] if match["evidence"] else "changed together"
        return f"{printable(said)} (git history; the doc need not name it)"
    return f"via {printable(match['target'])} ({kind}), {named}"


def _relation(match: dict) -> str:
    """Which of the two things an edge can mean: the doc says so, or history does."""
    return "history" if match["kind"] in HISTORY_KINDS else "named"


def format_impact(result: dict) -> str:
    """The human form of a ``run_impact`` result.

    Every name in it comes from the repository (a file name can hold an
    escape sequence), so each is printed through ``printable``.
    """
    query, store, results = result["query"], result["store"], result["results"]
    noun = "changed path" if query["diff"] else "path"
    corpus = f"({_count(store['docs'], 'doc')}, {_count(store['edges'], 'edge')})"
    lines: List[str] = []
    matched = result.get("matched_docs", len(results))
    n_paths = _count(len(query["paths"]), noun)
    # With a history kind in the query a result need not name anything, so the
    # wording changes and every reason is labelled with which of the two it is.
    labelled = any(match["kind"] in HISTORY_KINDS
                   for item in results for match in item["matches"])
    one, many = (("is tied to", "are tied to") if labelled else ("mentions", "mention"))
    if results and matched > len(results):
        lines.append(f"Top {len(results)} of {_count(matched, 'doc')} that {many} "
                     f"{n_paths} {corpus}")
    elif results:
        verb = one if len(results) == 1 else many
        lines.append(f"{_count(len(results), 'doc')} {verb} {n_paths} {corpus}")
    elif matched:
        verb = one if matched == 1 else many
        lines.append(f"{_count(matched, 'doc')} {verb} the "
                     f"{_count(len(query['paths']), 'queried ' + noun)}, but none scores at "
                     f"least {query['min_score']:g} (--min-score) {corpus}")
    else:
        lines.append(f"No doc mentions the {_count(len(query['paths']), 'queried ' + noun)} "
                     f"{corpus}")
    if query["diff"]:
        lines.append(f"Range: {printable(query['diff'])}")
    if labelled:
        lines.append("  named: the doc names the path.   history: they changed in the same "
                     "commits; that is correlation, not a claim that the doc describes it.")
    if results:
        lines.append("")
    for item in results:
        flag = "  [already in this diff]" if item["in_diff"] else ""
        lines.append(f"{item['rank']:>2}. {printable(item['doc'])}{flag}  "
                     f"score {item['score']:.6f}")
        for match in item["matches"]:
            label = f"[{_relation(match)}] " if labelled else ""
            lines.append(f"      {printable(match['path'])}  {label}{_match_reason(match)}")
            if match["match"] == "cochange":
                continue                    # its one evidence line is the reason above
            evidence = match["evidence"]
            for ev in evidence[:EVIDENCE_LINES_SHOWN]:
                where = f"L{ev['line']}: " if ev["line"] else ""
                lines.append(f"        {where}{printable(ev['text'])}")
            if len(evidence) > EVIDENCE_LINES_SHOWN:
                more = len(evidence) - EVIDENCE_LINES_SHOWN
                lines.append(f"        ... {_count(more, 'more line')}")
    tail: List[str] = []
    symbols = result.get("symbols") or {}
    if symbols.get("stale"):
        tail.append("Symbol matches may be out of date: the symbol index could not be loaded "
                    f"({symbols.get('detail', '')}); the edges of the last good build were used")
    if result["unmatched_paths"]:
        nothing = "No doc names or has changed with" if labelled else "No doc mentions"
        tail.append(f"{nothing}: " + ", ".join(
            printable(path) for path in result["unmatched_paths"]))
    if result["skipped_paths"]:
        tail.append("Skipped: " + ", ".join(
            f"{printable(s['path'])} ({s['reason']})" for s in result["skipped_paths"]))
    if tail:
        lines.append("")
        lines.extend(tail)
    return "\n".join(lines)
