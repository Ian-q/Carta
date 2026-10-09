"""Mention edges kept up to date across a history walk.

Resolving a doc's candidates depends on which paths exist, so a naive replay
re-resolves every doc at every commit. ``MentionState`` re-resolves only the
docs a change can affect: a doc whose text changed, a doc holding a glob, and
a doc with a candidate that shares a path segment with an added or deleted
path. Every lookup the resolver makes (file, directory, suffix, basename,
top-level directory) is keyed by segments of the candidate, so a path sharing
none of them cannot change the outcome. A test checks the result against
extraction from scratch at every step.
"""
from __future__ import annotations

import re
from typing import Dict, Iterable, List, Sequence, Set, Tuple

from carta.graph.mentions import normalise_candidate, resolve_candidates
from carta.graph.model import Candidate, Edge
from carta.graph.paths import PathIndex
from carta.graph.settings import GraphSettings

_PATH_CONTEXTS = ("link", "code", "fence", "frontmatter")
_GLOB = re.compile(r"[*?\[]")


def candidate_segments(candidates: Sequence[Candidate]) -> Tuple[frozenset, bool]:
    """The path segments a doc's candidates depend on, and whether any is a glob."""
    segments: Set[str] = set()
    has_glob = False
    for cand in candidates:
        if cand.ctx not in _PATH_CONTEXTS:
            continue
        for item in normalise_candidate(cand.raw, cand.ctx) or ():
            if _GLOB.search(item):
                has_glob = True
            segments.update(s for s in item.split("/") if s and s not in (".", ".."))
    return frozenset(segments), has_glob


class MentionState:
    """Per-doc candidates and edges, with a reverse map from target to docs."""

    def __init__(self, settings: GraphSettings, incremental: bool = True) -> None:
        self.settings = settings
        self.incremental = incremental
        self._cands: Dict[str, Sequence[Candidate]] = {}
        self._segs: Dict[str, frozenset] = {}
        self._by_seg: Dict[str, Set[str]] = {}
        self._glob_docs: Set[str] = set()
        self._edges: Dict[str, List[Edge]] = {}
        self._targets: Dict[str, Dict[str, List[Edge]]] = {}     # mention dst -> doc -> edges
        self._glob_targets: Dict[str, int] = {}                   # glob dst -> docs holding it
        self._dirty: Set[str] = set()

    # -- changes ----------------------------------------------------------

    def set_doc(self, doc: str, candidates: Sequence[Candidate]) -> None:
        """Add a doc, or replace its candidates. Resolved at the next ``refresh``."""
        self._forget_segments(doc)
        segments, has_glob = candidate_segments(candidates)
        self._cands[doc] = candidates
        self._segs[doc] = segments
        for seg in segments:
            self._by_seg.setdefault(seg, set()).add(doc)
        if has_glob:
            self._glob_docs.add(doc)
        self._dirty.add(doc)

    def remove_doc(self, doc: str) -> None:
        if doc not in self._cands:
            return
        self._forget_segments(doc)
        del self._cands[doc]
        self._dirty.discard(doc)
        self._replace_edges(doc, None)

    def paths_changed(self, paths: Iterable[str]) -> None:
        """Tell the state which paths were added to or deleted from the path set."""
        paths = [p for p in paths if p]
        if not paths:
            return
        if not self.incremental:
            self._dirty.update(self._cands)
            return
        self._dirty.update(self._glob_docs)
        by_seg = self._by_seg
        for path in paths:
            for seg in path.split("/"):
                hit = by_seg.get(seg)
                if hit:
                    self._dirty.update(hit)

    def refresh(self, index: PathIndex) -> int:
        """Re-resolve the docs that may have changed; returns how many were resolved."""
        dirty = sorted(self._dirty)
        self._dirty.clear()
        for doc in dirty:
            self._replace_edges(
                doc, resolve_candidates(doc, self._cands[doc], index, self.settings))
        return len(dirty)

    def _forget_segments(self, doc: str) -> None:
        for seg in self._segs.pop(doc, ()):
            bucket = self._by_seg.get(seg)
            if bucket is not None:
                bucket.discard(doc)
                if not bucket:
                    del self._by_seg[seg]
        self._glob_docs.discard(doc)

    def _replace_edges(self, doc: str, edges) -> None:
        for edge in self._edges.pop(doc, ()):
            if edge.kind != "mention":
                continue
            bucket = self._targets.get(edge.dst)
            if bucket is None or doc not in bucket:
                continue
            del bucket[doc]
            if not bucket:
                del self._targets[edge.dst]
            if edge.dst_type == "glob":
                left = self._glob_targets.get(edge.dst, 0) - 1
                if left > 0:
                    self._glob_targets[edge.dst] = left
                else:
                    self._glob_targets.pop(edge.dst, None)
        if edges is None:
            return
        self._edges[doc] = edges
        seen_globs: Set[str] = set()
        for edge in edges:
            if edge.kind != "mention":
                continue
            self._targets.setdefault(edge.dst, {}).setdefault(doc, []).append(edge)
            if edge.dst_type == "glob" and edge.dst not in seen_globs:
                seen_globs.add(edge.dst)
                self._glob_targets[edge.dst] = self._glob_targets.get(edge.dst, 0) + 1

    # -- reads ------------------------------------------------------------

    def docs(self) -> List[str]:
        return sorted(self._cands)

    def doc_edges(self, doc: str) -> List[Edge]:
        return list(self._edges.get(doc, ()))

    def edges(self) -> List[Edge]:
        """Every edge of every doc, sorted."""
        return sorted(edge for edges in self._edges.values() for edge in edges)

    def edges_for(self, paths: Iterable[str]) -> List[Edge]:
        """The mention edges that can match ``paths`` in an impact query.

        Every edge to a target that is one of the paths, a directory above
        one, or a glob. Ranking over this subset equals ranking over all
        edges: rarity counts are per target, and every edge of each returned
        target is included.
        """
        wanted: Set[str] = set(self._glob_targets)
        for path in paths:
            wanted.add(path)
            parts = path.rstrip("/").split("/")
            for i in range(1, len(parts)):
                wanted.add("/".join(parts[:i]) + "/")
        out: List[Edge] = []
        for target in wanted:
            for edges in self._targets.get(target, {}).values():
                out.extend(edges)
        out.sort()
        return out
