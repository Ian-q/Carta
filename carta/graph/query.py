"""The impact query: given changed paths, which docs describe them?

``rank_impact`` is a pure function over an in-memory ``ImpactIndex``. It is
the function the replay eval scores, so the eval measures what ships.
``run_impact`` wraps it for the CLI and the MCP tool: it brings the edge store
up to date, resolves the queried paths (or a git range) and returns the JSON
object both surfaces print.
"""
from __future__ import annotations

import math
import os
import posixpath
from bisect import bisect_left
from pathlib import Path
from typing import Collection, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from carta.graph.build import build_graph
from carta.graph.gitio import GitError, default_branch, diff_names
from carta.graph.model import KINDS, MENTION_EXTRACTOR, Edge
from carta.graph.paths import glob_to_regex, is_doc_path
from carta.graph.settings import graph_settings
from carta.graph.store import EdgeStore, StoreVersionError, store_path

IMPACT_SCHEMA = 1
DEFAULT_KIND_WEIGHTS: Dict[str, float] = {"mention": 1.0, "symbol": 0.8, "cochange": 0.5}
GLOB_SPEC = 0.5
EVIDENCE_LINES_SHOWN = 3

# Target types a queried path can match. ``token`` and ``pattern`` targets are
# not paths and never take part in an impact query.
_PATH_TYPES = frozenset(("file", "doc", "dir", "glob", "missing"))
# Matches that name the queried path itself; directory and glob matches are
# the weaker kind and are aggregated per target (see rank_impact).
_STRONG = frozenset(("exact", "under", "cochange"))


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
            if self._types.get((kind, parent)) == "dir":
                out.append((parent, "dir", 1.0 / (1 + len(parts) - i)))
        if not q.endswith("/"):
            for glob in self._globs.get(kind, ()):
                if glob_to_regex(glob).match(q):
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
                exclude_docs: Collection[str] = ()) -> List[dict]:
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
    """
    kinds = sorted(set(kinds))
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
        results.append({"rank": rank, "doc": doc, "score": score, "matches": matches})
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

    Returns ``(paths, skipped)``; ``skipped`` holds ``{"path", "reason"}`` with
    reason ``outside_repo`` or ``repo_root``.
    """
    root = os.path.realpath(str(repo_root))
    here = os.path.realpath(str(cwd))
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
            candidates = [from_cwd, from_root] if os.path.lexists(from_cwd) else [from_root]
        rel = None
        for full in candidates:
            inside = os.path.relpath(full, root).replace(os.sep, "/")
            if inside == ".." or inside.startswith("../"):
                continue
            if inside == ".":
                rel = ""
                break
            if wants_dir or os.path.isdir(full):
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


def _diff_paths(repo_root: Path, diff: str) -> Tuple[str, List[str]]:
    range_spec = diff
    try:
        if not range_spec:
            range_spec = f"{default_branch(repo_root)}...HEAD"
        return range_spec, diff_names(repo_root, range_spec)
    except GitError as exc:
        if diff and os.path.lexists(os.path.join(str(repo_root), diff)):
            raise ImpactError(
                f"`{diff}` is a path, not a range; pass it without --diff") from exc
        raise ImpactError(
            f"`{range_spec}` is not a revision range git can diff here: {exc}") from exc


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
    use_kinds = sorted(set(kinds if kinds else settings.impact_kinds))
    unknown = [k for k in use_kinds if k not in KINDS]
    if unknown:
        raise ImpactError(
            f"unknown edge kind {unknown[0]!r} (known kinds: {', '.join(KINDS)})")
    use_top = settings.impact_top if top is None else int(top)
    use_min = settings.impact_min_score if min_score is None else float(min_score)

    in_diff: frozenset = frozenset()
    range_spec: Optional[str] = None
    skipped: List[dict] = []
    if diff is not None:
        range_spec, changed = _diff_paths(repo_root, diff)
        in_diff = frozenset(changed)
        query = []
        for path in changed:
            if not include_docs and is_doc_path(path, settings):
                skipped.append({"path": path, "reason": "doc"})
            else:
                query.append(path)
    else:
        query, skipped = normalise_query_paths(repo_root, cwd or repo_root, paths)

    try:
        if build:
            build_graph(repo_root, cfg)
        store = EdgeStore.open_existing(store_path(repo_root))
    except StoreVersionError as exc:
        raise ImpactError(str(exc)) from exc
    if store is None:
        raise ImpactError("no edge store yet; run `carta graph build` (or drop --no-build)")
    with store:
        edges = list(store.iter_edges(kinds=use_kinds))
        n_docs = len(store.source_hashes(MENTION_EXTRACTOR))
        total_edges = store.counts()["edges"]
        digest = store.edges_digest((MENTION_EXTRACTOR,))

    index = ImpactIndex(edges, n_docs)
    ranked = rank_impact(index, query, kinds=use_kinds, weights=dict(settings.impact_weights),
                         top=use_top, min_score=use_min)
    results = []
    for item in ranked:
        matches = [dict(match, contribution=round(match["contribution"], 6))
                   for match in item["matches"]]
        results.append({"rank": item["rank"], "doc": item["doc"],
                        "score": round(item["score"], 6), "in_diff": item["doc"] in in_diff,
                        "matches": matches})
    return {
        "schema": IMPACT_SCHEMA,
        "query": {"paths": query, "diff": range_spec, "kinds": use_kinds, "top": use_top,
                  "min_score": use_min},
        "store": {"docs": n_docs, "edges": total_edges, "digest": digest, "built": bool(build)},
        "results": results,
        "unmatched_paths": [q for q in query if not index.is_matched(q, use_kinds)],
        "skipped_paths": skipped,
    }


# -- human output ----------------------------------------------------------

def _count(n: int, noun: str) -> str:
    return f"{n:,} {noun}" + ("" if n == 1 else "s")


def _match_reason(match: dict) -> str:
    named = f"named by {_count(match['df'], 'doc')}"
    kind = match["match"]
    if kind == "exact":
        return f"exact, {named}"
    if kind == "under":
        return f"names {match['target']} under it, {named}"
    if kind == "cochange":
        return f"changes with it ({_count(match['df'], 'doc')})"
    return f"via {match['target']} ({kind}), {named}"


def format_impact(result: dict) -> str:
    """The human form of a ``run_impact`` result."""
    query, store, results = result["query"], result["store"], result["results"]
    noun = "changed path" if query["diff"] else "path"
    corpus = f"({_count(store['docs'], 'doc')}, {_count(store['edges'], 'edge')})"
    lines: List[str] = []
    if results:
        verb = "mentions" if len(results) == 1 else "mention"
        lines.append(f"{_count(len(results), 'doc')} {verb} "
                     f"{_count(len(query['paths']), noun)} {corpus}")
    else:
        lines.append(f"No doc mentions the {_count(len(query['paths']), 'queried ' + noun)} "
                     f"{corpus}")
    if query["diff"]:
        lines.append(f"Range: {query['diff']}")
    if results:
        lines.append("")
    for item in results:
        flag = "  [already in this diff]" if item["in_diff"] else ""
        lines.append(f"{item['rank']:>2}. {item['doc']}{flag}  score {item['score']:.6f}")
        for match in item["matches"]:
            lines.append(f"      {match['path']}  {_match_reason(match)}")
            evidence = match["evidence"]
            for ev in evidence[:EVIDENCE_LINES_SHOWN]:
                where = f"L{ev['line']}: " if ev["line"] else ""
                lines.append(f"        {where}{ev['text']}")
            if len(evidence) > EVIDENCE_LINES_SHOWN:
                more = len(evidence) - EVIDENCE_LINES_SHOWN
                lines.append(f"        ... {_count(more, 'more line')}")
    tail: List[str] = []
    if result["unmatched_paths"]:
        tail.append("No doc mentions: " + ", ".join(result["unmatched_paths"]))
    if result["skipped_paths"]:
        tail.append("Skipped: " + ", ".join(
            f"{s['path']} ({s['reason']})" for s in result["skipped_paths"]))
    if tail:
        lines.append("")
        lines.extend(tail)
    return "\n".join(lines)
