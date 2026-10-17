"""The co-change arm of the replay: a pair table carried forward through history.

For the commit being scored the edges come from commits strictly before it.
The replay loop guarantees the order (rank first, then observe), and this
provider holds nothing but the running table, so there is no other way for a
commit, or anything after it, to reach its own ranking.
"""
from __future__ import annotations

from typing import List, Sequence

from carta.graph.cochange import PairTable
from carta.graph.eval.replay import EdgeProvider, ReplayContext, Snapshot
from carta.graph.gitio import LogEntry
from carta.graph.model import Edge


class CochangeProvider(EdgeProvider):
    """File-to-doc ``cochange`` edges as of each commit's parent."""

    kind = "cochange"

    def __init__(self, context: ReplayContext) -> None:
        self.table = PairTable(context.settings.cochange)
        self.observed: List[str] = []

    def _edges(self, snapshot: Snapshot, sources: Sequence[str]) -> List[Edge]:
        docs = frozenset(snapshot.docs)
        out: List[Edge] = []
        for src in sources:
            out.extend(self.table.edges_from(src, docs.__contains__, docs.__contains__))
        return out

    def edges_at(self, snapshot: Snapshot) -> List[Edge]:
        return self._edges(snapshot, sorted(self.table.files))

    def relevant_edges(self, snapshot: Snapshot, paths: Sequence[str]) -> List[Edge]:
        # A co-change row is only ever read from its source, so the rows out of
        # the queried paths rank exactly as the whole table does.
        return self._edges(snapshot, sorted(set(paths)))

    def observe_entry(self, entry: LogEntry) -> None:
        self.observed.append(entry.sha)
        self.table.observe(entry.sha, entry.changes, has_parent=bool(entry.parents),
                           blobs=entry.blobs, removed=entry.removed)

    def observe(self, commit: str, changes) -> None:
        self.observed.append(commit)
        self.table.observe(commit, changes)
