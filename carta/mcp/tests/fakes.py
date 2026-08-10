"""Qdrant test doubles that enforce the real server's contract.

The MCP carta_search bug (empty results on every hybrid collection) survived
because the existing fakes accepted any query_points() call. Real Qdrant
returns 400 "Not existing vector name error" when a collection has named
vectors and the query omits `using=`. This double does the same.
"""
from unittest.mock import MagicMock


class NamedVectorContractError(Exception):
    """Stand-in for Qdrant's 400 on an unnamed query against named vectors."""


class ContractFakeQdrant:
    def __init__(self, named_vectors: bool = True, points=None):
        self._named = named_vectors
        self._points = points if points is not None else []
        self.calls: list[dict] = []

    def get_collection(self, name):
        info = MagicMock()
        if self._named:
            info.config.params.vectors = {"dense": MagicMock()}
            info.config.params.sparse_vectors = {"bm25": MagicMock()}
        else:
            info.config.params.vectors = MagicMock()   # unnamed single vector
            info.config.params.sparse_vectors = None
        return info

    def query_points(self, **kwargs):
        self.calls.append(kwargs)
        if self._named and "using" not in kwargs and "prefetch" not in kwargs:
            raise NamedVectorContractError(
                "Wrong input: Not existing vector name error: "
            )
        resp = MagicMock()
        resp.points = self._points
        return resp

    def close(self):
        """No-op: _run_search_collection always closes the client in a finally block."""
