"""The baselines of the replay eval: what the graph arm has to beat.

``popularity`` suggests the docs people edit most. ``basename`` is what
``grep -wF "$(basename f)"`` over the docs gives, with no path understanding.
``basename_idf`` is the same token match scored with the same rarity and
frequency weighting as ``rank_impact``, so a win over it is a win for path
understanding and not for the weighting.

All of it is pure: rankings depend on their arguments and nothing else.
"""
from __future__ import annotations

import math
import re
from collections import Counter, deque
from typing import (
    Callable, Collection, Deque, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple,
)

# Bump whenever basename_tokens would return something different for an
# unchanged text: cached token counts are keyed by it.
TOKENIZER_VERSION = 1

_SPLIT = re.compile(r"[^\w.+\-]+")


def basename_tokens(text: str) -> Dict[str, int]:
    """Count the filename-shaped tokens of a text.

    Tokens are split on anything that cannot be part of a file name (letters,
    digits, ``_ . + -`` stay), and trailing dots are dropped, so ``frame.py``
    is found in "see frame.py." and in ``src/parser/frame.py`` but not in
    ``subframe.pyc``.
    """
    counts: Counter = Counter()
    for token in _SPLIT.split(text):
        token = token.rstrip(".")
        if token:
            counts[token] += 1
    return dict(counts)


# -- popularity -------------------------------------------------------------

class PopularityWindow:
    """How often each doc was edited in the last ``window`` mainline commits."""

    def __init__(self, window: int) -> None:
        self.window = max(0, int(window))
        self._recent: Deque[Tuple[str, ...]] = deque()
        self._counts: Counter = Counter()

    def observe(self, docs: Iterable[str]) -> None:
        """Record one commit's edited docs (possibly none). Call in commit order."""
        if not self.window:
            return
        edited = tuple(sorted(set(docs)))
        self._recent.append(edited)
        self._counts.update(edited)
        if len(self._recent) > self.window:
            for doc in self._recent.popleft():
                self._counts[doc] -= 1
                if not self._counts[doc]:
                    del self._counts[doc]

    def ranking(self, docs_at_parent: Collection[str], top: Optional[int] = None,
                exclude: Collection[str] = ()) -> List[str]:
        """Docs by edit count descending, then path; only docs present at the parent."""
        ranked = sorted((-count, doc) for doc, count in self._counts.items()
                        if doc in docs_at_parent and doc not in exclude)
        out = [doc for _neg, doc in ranked]
        return out if top is None else out[:max(0, int(top))]


def popularity_ranking(prior_doc_edits: Sequence[Collection[str]],
                       docs_at_parent: Collection[str], window: int,
                       top: Optional[int] = None, exclude: Collection[str] = ()) -> List[str]:
    """Rank docs by how many of the previous ``window`` commits edited them.

    ``prior_doc_edits`` holds one collection of doc paths per mainline commit
    strictly before the one being scored, oldest first.
    """
    win = PopularityWindow(window)
    for docs in prior_doc_edits[-win.window:] if win.window else ():
        win.observe(docs)
    return win.ranking(docs_at_parent, top, exclude)


# -- basename ---------------------------------------------------------------

Lookup = Callable[[str], Mapping[str, int]]


def _basenames(changed_paths: Iterable[str]) -> List[str]:
    return sorted({p.rstrip("/").rsplit("/", 1)[-1] for p in changed_paths if p.rstrip("/")})


def _rank_plain(changed_paths: Iterable[str], lookup: Lookup, top: Optional[int],
                exclude: Collection[str]) -> List[str]:
    distinct: Dict[str, int] = {}
    total: Dict[str, int] = {}
    for name in _basenames(changed_paths):
        for doc, count in lookup(name).items():
            if count > 0 and doc not in exclude:
                distinct[doc] = distinct.get(doc, 0) + 1
                total[doc] = total.get(doc, 0) + count
    out = sorted(distinct, key=lambda doc: (-distinct[doc], -total[doc], doc))
    return out if top is None else out[:max(0, int(top))]


def _rank_idf(changed_paths: Iterable[str], lookup: Lookup, n_docs: int, top: Optional[int],
              exclude: Collection[str], with_scores: bool) -> list:
    parts: Dict[str, List[float]] = {}
    for name in _basenames(changed_paths):
        found = {doc: count for doc, count in lookup(name).items() if count > 0}
        if not found:
            continue
        idf = math.log(1.0 + max(n_docs, len(found)) / len(found))
        for doc, count in found.items():
            if doc not in exclude:
                parts.setdefault(doc, []).append(idf * (1.0 + math.log(count)))
    scores = {doc: math.fsum(values) for doc, values in parts.items()}
    out = sorted(scores, key=lambda doc: (-scores[doc], doc))
    if top is not None:
        out = out[:max(0, int(top))]
    return [(doc, scores[doc]) for doc in out] if with_scores else out


def _invert(doc_tokens: Mapping[str, Mapping[str, int]]) -> Lookup:
    def lookup(name: str) -> Mapping[str, int]:
        return {doc: tokens[name] for doc, tokens in doc_tokens.items() if name in tokens}
    return lookup


def basename_ranking(changed_paths: Sequence[str], doc_tokens: Mapping[str, Mapping[str, int]],
                     top: Optional[int] = None, exclude: Collection[str] = ()) -> List[str]:
    """Docs containing the basename of a changed file as a whole token.

    Ranked by the number of distinct changed basenames found, then total
    occurrences, then path. Case-sensitive; the directory part is ignored.
    """
    return _rank_plain(changed_paths, _invert(doc_tokens), top, exclude)


def basename_idf_ranking(changed_paths: Sequence[str],
                         doc_tokens: Mapping[str, Mapping[str, int]], n_docs: int,
                         top: Optional[int] = None, exclude: Collection[str] = (),
                         with_scores: bool = False) -> list:
    """The same matches, scored ``sum(ln(1 + N / df) * (1 + ln(occurrences)))``.

    ``df`` is the number of docs containing the basename and ``N`` the corpus
    size: the weighting ``rank_impact`` uses, applied to a token match.
    """
    return _rank_idf(changed_paths, _invert(doc_tokens), n_docs, top, exclude, with_scores)


class BasenameIndex:
    """Token -> docs, kept up to date as docs change during a history walk.

    Only tokens in ``universe`` are kept (the replay passes every basename
    that occurs in the history), which bounds memory without changing any
    ranking: a query only ever asks for the basename of a changed file.
    """

    def __init__(self, universe: Optional[Collection[str]] = None) -> None:
        self._universe = None if universe is None else frozenset(universe)
        self._by_token: Dict[str, Dict[str, int]] = {}
        self._doc_tokens: Dict[str, Tuple[str, ...]] = {}

    def set_doc(self, doc: str, tokens: Mapping[str, int]) -> None:
        self.remove_doc(doc)
        universe = self._universe
        kept = [t for t in tokens if universe is None or t in universe]
        for token in kept:
            self._by_token.setdefault(token, {})[doc] = int(tokens[token])
        self._doc_tokens[doc] = tuple(kept)

    def remove_doc(self, doc: str) -> None:
        for token in self._doc_tokens.pop(doc, ()):
            bucket = self._by_token.get(token)
            if bucket is not None:
                bucket.pop(doc, None)
                if not bucket:
                    del self._by_token[token]

    def docs(self) -> Set[str]:
        return set(self._doc_tokens)

    def df(self, token: str) -> int:
        return len(self._by_token.get(token, ()))

    def _lookup(self, name: str) -> Mapping[str, int]:
        return self._by_token.get(name, {})

    def rank(self, changed_paths: Sequence[str], top: Optional[int] = None,
             exclude: Collection[str] = ()) -> List[str]:
        return _rank_plain(changed_paths, self._lookup, top, exclude)

    def rank_idf(self, changed_paths: Sequence[str], n_docs: int, top: Optional[int] = None,
                 exclude: Collection[str] = ()) -> List[str]:
        return _rank_idf(changed_paths, self._lookup, n_docs, top, exclude, False)
