"""Mention edges kept up to date across a history walk.

Resolving a doc's candidates depends on which paths exist, so a naive replay
re-resolves every doc at every commit. ``MentionState`` records, for each
doc, every question its resolution asked of the path index (does this file
exist, does this directory, what ends with this suffix, which files have this
basename) and re-resolves a doc only when a change to the path set could
alter one of those answers, or when the doc's own text changed. A glob is
re-counted only when an added or deleted path matches it. Nothing is inferred about the
resolver: a lookup it makes is a dependency, and a lookup the recorder does
not know raises. A test checks the result against extraction from scratch on
random histories.
"""
from __future__ import annotations

from typing import Dict, Iterable, List, Sequence, Set

from carta.graph.mentions import resolve_candidates
from carta.graph.model import Candidate, Edge
from carta.graph.paths import PathIndex, glob_to_regex
from carta.graph.settings import GraphSettings


class _TopDirs:
    """``name in index.top_level_dirs``, recorded as a directory lookup."""

    def __init__(self, recorder: "_Recorder") -> None:
        self._recorder = recorder

    def __contains__(self, name: object) -> bool:
        return self._recorder.has_dir(str(name))


class _Recorder:
    """A ``PathIndex`` stand-in that notes every lookup made through it.

    It offers exactly the lookups the resolver uses. If the resolver starts
    using another one, that call fails loudly here rather than becoming a
    dependency nobody tracks.
    """

    def __init__(self, index: PathIndex) -> None:
        self._index = index
        self.deps: Set[str] = set()
        self.globs: Set[str] = set()

    def has_file(self, path: str) -> bool:
        self.deps.add("f:" + path)
        return self._index.has_file(path)

    def has_dir(self, path: str) -> bool:
        self.deps.add("d:" + path.rstrip("/"))
        return self._index.has_dir(path)

    def by_basename(self, name: str):
        self.deps.add("b:" + name)
        return self._index.by_basename(name)

    def suffix_matches(self, candidate: str):
        self.deps.add("s:" + candidate.rstrip("/"))
        return self._index.suffix_matches(candidate)

    def glob_matches(self, pattern: str) -> int:
        self.globs.add(pattern)
        return self._index.glob_matches(pattern)

    @property
    def top_level_dirs(self) -> _TopDirs:
        return _TopDirs(self)


def _ancestors(path: str) -> List[str]:
    parts = path.split("/")
    return ["/".join(parts[:i]) for i in range(1, len(parts))]


def _suffixes(path: str) -> List[str]:
    parts = path.split("/")
    return ["/".join(parts[i:]) for i in range(1, len(parts))]


class MentionState:
    """Per-doc candidates and edges, with a reverse map from target to docs."""

    def __init__(self, settings: GraphSettings, incremental: bool = True) -> None:
        self.settings = settings
        self.incremental = incremental
        self._cands: Dict[str, Sequence[Candidate]] = {}
        self._deps: Dict[str, frozenset] = {}
        self._watch: Dict[str, Set[str]] = {}                     # lookup key -> docs
        self._globs: Dict[str, frozenset] = {}                    # doc -> glob patterns asked
        self._glob_watch: Dict[str, Set[str]] = {}                # glob pattern -> docs
        self._dir_count: Dict[str, int] = {}                      # directory -> files beneath
        self._edges: Dict[str, List[Edge]] = {}
        self._targets: Dict[str, Dict[str, List[Edge]]] = {}     # mention dst -> doc -> edges
        self._glob_targets: Dict[str, int] = {}                   # glob dst -> docs holding it
        self._dirty: Set[str] = set()

    # -- changes ----------------------------------------------------------

    def seed_paths(self, paths: Iterable[str]) -> None:
        """Tell the state which paths exist before the first change is reported."""
        counts = self._dir_count
        counts.clear()
        for path in paths:
            for directory in _ancestors(path):
                counts[directory] = counts.get(directory, 0) + 1

    def set_doc(self, doc: str, candidates: Sequence[Candidate]) -> None:
        """Add a doc, or replace its candidates. Resolved at the next ``refresh``."""
        self._cands[doc] = candidates
        self._dirty.add(doc)

    def remove_doc(self, doc: str) -> None:
        if doc not in self._cands:
            return
        del self._cands[doc]
        self._dirty.discard(doc)
        self._forget_deps(doc)
        self._replace_edges(doc, None)

    def paths_changed(self, added: Iterable[str] = (), deleted: Iterable[str] = ()) -> None:
        """Report files added to and deleted from the path set (each exactly once)."""
        added = [p for p in added if p]
        deleted = [p for p in deleted if p]
        if not added and not deleted:
            return
        counts = self._dir_count
        keys: Set[str] = set()
        for path in deleted:
            for directory in _ancestors(path):
                left = counts.get(directory, 0) - 1
                if left > 0:
                    counts[directory] = left
                else:
                    counts.pop(directory, None)
                    keys.add("d:" + directory)
                    keys.update("s:" + s for s in _suffixes(directory))
        for path in added:
            for directory in _ancestors(path):
                have = counts.get(directory, 0)
                if not have:
                    keys.add("d:" + directory)
                    keys.update("s:" + s for s in _suffixes(directory))
                counts[directory] = have + 1
        if not self.incremental:
            self._dirty.update(self._cands)
            return
        for path in (*added, *deleted):
            keys.add("f:" + path)
            keys.add("b:" + path.rsplit("/", 1)[-1])
            keys.update("s:" + s for s in _suffixes(path))
        changed = (*added, *deleted)
        for pattern, docs in self._glob_watch.items():
            regex = glob_to_regex(pattern)
            if any(regex.match(path) for path in changed):
                self._dirty.update(docs)
        watch = self._watch
        for key in keys:
            hit = watch.get(key)
            if hit:
                self._dirty.update(hit)

    def refresh(self, index: PathIndex) -> int:
        """Re-resolve the docs that may have changed; returns how many were resolved."""
        dirty = sorted(self._dirty)
        self._dirty.clear()
        for doc in dirty:
            recorder = _Recorder(index)
            edges = resolve_candidates(doc, self._cands[doc], recorder, self.settings)
            self._forget_deps(doc)
            deps = frozenset(recorder.deps)
            self._deps[doc] = deps
            for key in deps:
                self._watch.setdefault(key, set()).add(doc)
            if recorder.globs:
                self._globs[doc] = frozenset(recorder.globs)
                for pattern in recorder.globs:
                    self._glob_watch.setdefault(pattern, set()).add(doc)
            self._replace_edges(doc, edges)
        return len(dirty)

    def _forget_deps(self, doc: str) -> None:
        for key in self._deps.pop(doc, ()):
            bucket = self._watch.get(key)
            if bucket is not None:
                bucket.discard(doc)
                if not bucket:
                    del self._watch[key]
        for pattern in self._globs.pop(doc, ()):
            bucket = self._glob_watch.get(pattern)
            if bucket is not None:
                bucket.discard(doc)
                if not bucket:
                    del self._glob_watch[pattern]

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
        for target in sorted(wanted):               # a fixed order, without comparing edges
            bucket = self._targets.get(target)
            if bucket:
                for doc in sorted(bucket):
                    out.extend(bucket[doc])
        return out
