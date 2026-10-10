"""Path primitives: globs, brace expansion, exclusion and the path index.

Everything here is pure string work on repo-relative POSIX paths. Nothing
reads the filesystem.
"""
from __future__ import annotations

import fnmatch
import hashlib
from functools import lru_cache
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from carta.graph.limits import MAX_GLOB_LENGTH, MAX_SUFFIX_DEPTH

# The same list as carta.scanner.scanner._ALWAYS_EXCLUDED_DIRS. It is repeated
# here because importing the scanner loads the whole vector stack, which this
# package must never do; test_paths.py pins the two to stay equal.
ALWAYS_EXCLUDED_DIRS = (
    ".claude/worktrees/", "build/", "dist/", "temp/", "tmp/",
    "node_modules/", ".venv/", "venv/", ".pixi/", "site-packages/", ".tox/",
)

# Never part of the path set, in git mode or in the walk fallback: git's own
# directory, and Carta's (the edge store and runtime files live there, and
# letting them in would make every build change the path set).
PATHSET_EXCLUDED_ROOTS = (".git", ".carta")


_LITERAL, _ANY_ONE, _CLASS, _STAR, _DEEP, _DEEP_DIR = range(6)


def _class_ranges(body: str) -> Optional[List[Tuple[str, str]]]:
    """``a-z`` ranges and single characters of a class body; ``None`` for a reversed range."""
    ranges: List[Tuple[str, str]] = []
    k, n = 0, len(body)
    while k < n:
        low = body[k]
        if k + 2 < n and body[k + 1] == "-":
            high = body[k + 2]
            if high < low:
                return None
            ranges.append((low, high))
            k += 3
        else:
            ranges.append((low, low))
            k += 1
    return ranges


def _glob_tokens(glob: str) -> Optional[list]:
    """The glob as tokens, or ``None`` when it holds a class that can match nothing."""
    out: list = []
    i, n = 0, len(glob)
    while i < n:
        c = glob[i]
        if c == "*":
            if glob.startswith("**/", i):
                out.append((_DEEP_DIR, None))
                i += 3
            elif glob.startswith("**", i):
                out.append((_DEEP, None))
                i += 2
            else:
                out.append((_STAR, None))
                i += 1
        elif c == "?":
            out.append((_ANY_ONE, None))
            i += 1
        elif c == "[":
            j = glob.find("]", i + 2)
            if j == -1:
                out.append((_LITERAL, c))
                i += 1
            else:
                body = glob[i + 1:j]
                negate = body[:1] in ("!", "^")
                ranges = _class_ranges(body[1:] if negate else body)
                if ranges is None:
                    return None
                out.append((_CLASS, (tuple(ranges), negate)))
                i = j + 1
        else:
            out.append((_LITERAL, c))
            i += 1
    return out


def glob_problem(glob: str) -> Optional[str]:
    """Why a glob can never match (so a config that holds it is a mistake), else ``None``."""
    if len(glob) > MAX_GLOB_LENGTH:
        return f"is longer than {MAX_GLOB_LENGTH} characters"
    if _glob_tokens(glob) is None:
        return "has a character class with a reversed range"
    return None


class GlobMatcher:
    """One compiled path glob. ``match(path)`` is true when the whole path matches.

    The glob is run as a small state machine, every state it could be in
    advanced one character at a time, so a match costs at most
    ``len(glob) * len(path)`` steps whatever the glob looks like. Nothing
    backtracks, and nothing here can raise on a strange glob: one that is too
    long, or holds a reversed class range such as ``[z-a]``, matches nothing.
    """

    __slots__ = ("glob", "_never", "_literal", "_prefix", "_suffix", "_start", "_accept",
                 "_chars", "_any_one", "_classes", "_loop_deep", "_loop_segment",
                 "_skip_one", "_skip_three", "_masks")

    def __init__(self, glob: str) -> None:
        self.glob = glob
        self._literal: Optional[str] = None
        self._prefix = self._suffix = ""
        self._masks: Dict[str, Tuple[int, int]] = {}
        tokens = None if len(glob) > MAX_GLOB_LENGTH else _glob_tokens(glob)
        self._never = tokens is None
        if tokens is None:
            return
        if all(kind == _LITERAL for kind, _ in tokens):
            self._literal = glob
            return
        chars: Dict[str, int] = {}
        classes: List[Tuple[int, tuple, bool]] = []
        any_one = loop_deep = loop_segment = skip_one = skip_three = 0
        state = 0
        prefix_done = False
        prefix: List[str] = []
        for kind, value in tokens:
            bit = 1 << state
            if kind == _LITERAL:
                chars[value] = chars.get(value, 0) | bit
                if not prefix_done:
                    prefix.append(value)
            elif kind == _ANY_ONE:
                any_one |= bit
            elif kind == _CLASS:
                classes.append((bit, value[0], value[1]))
            elif kind == _STAR:
                loop_segment |= bit
                skip_one |= bit
            elif kind == _DEEP:
                loop_deep |= bit
                skip_one |= bit
            else:
                # "**/" is "nothing, or anything that ends in a slash": an
                # entry state that may skip the whole group, then a loop that
                # can only be left through the slash after it. The skip is on
                # the entry alone, so it cannot be taken after the loop ran.
                skip_one |= bit
                skip_three |= bit
                state += 1
                loop_deep |= 1 << state
                skip_one |= 1 << state
                state += 1
                chars["/"] = chars.get("/", 0) | (1 << state)
            if kind != _LITERAL:
                prefix_done = True
            state += 1
        suffix: List[str] = []
        for kind, value in reversed(tokens):
            if kind != _LITERAL:
                break
            suffix.append(value)
        self._prefix = "".join(prefix)
        self._suffix = "".join(reversed(suffix))
        self._chars, self._any_one, self._classes = chars, any_one, tuple(classes)
        self._loop_deep, self._loop_segment = loop_deep, loop_segment
        self._skip_one, self._skip_three = skip_one, skip_three
        self._accept = 1 << state
        self._start = self._closure(1 << len(prefix))

    def _closure(self, states: int) -> int:
        skip_one, skip_three = self._skip_one, self._skip_three
        while True:
            grown = states | ((states & skip_one) << 1) | ((states & skip_three) << 3)
            if grown == states:
                return states
            states = grown

    def _char_masks(self, c: str) -> Tuple[int, int]:
        advance = self._chars.get(c, 0)
        if c != "/":
            advance |= self._any_one
        for bit, ranges, negate in self._classes:
            inside = any(low <= c <= high for low, high in ranges)
            if (not inside and c != "/") if negate else inside:
                advance |= bit
        stay = 0 if c == "\n" else self._loop_deep
        if c != "/":
            stay |= self._loop_segment
        if len(self._masks) >= 512:
            self._masks.clear()
        self._masks[c] = (advance, stay)
        return advance, stay

    def match(self, path: str) -> bool:
        if self._never:
            return False
        if self._literal is not None:
            return path == self._literal
        prefix = self._prefix
        if not path.startswith(prefix) or not path.endswith(self._suffix):
            return False
        states = self._start
        masks = self._masks
        skips = self._skip_one
        for c in path[len(prefix):] if prefix else path:
            pair = masks.get(c)
            if pair is None:
                pair = self._char_masks(c)
            states = ((states & pair[0]) << 1) | (states & pair[1])
            if not states:
                return False
            if states & skips:
                states = self._closure(states)
        return bool(states & self._accept)


@lru_cache(maxsize=4096)
def glob_matcher(glob: str) -> GlobMatcher:
    """Compile a path glob.

    ``**/`` is any number of directories (including none), ``**`` is anything,
    ``*`` and ``?`` stay within one segment, ``[...]`` is a character class
    (``[!...]`` negates and never matches a slash). Every other character is
    literal.
    """
    return GlobMatcher(glob)


# The name this had while it compiled to a regular expression; callers only
# ever used ``.match``.
glob_to_regex = glob_matcher


def expand_braces(text: str, limit: int = 16) -> Optional[List[str]]:
    """Expand one ``{a,b,c}`` group. ``None`` means reject the candidate.

    Rejected: nested or unbalanced braces, more than one group, a group with
    no comma (``{name}`` is a template placeholder), more than ``limit``
    alternatives. Text without braces is returned alone.
    """
    if "{" not in text and "}" not in text:
        return [text]
    if text.count("{") != 1 or text.count("}") != 1:
        return None
    start, end = text.index("{"), text.index("}")
    if end < start:
        return None
    parts = text[start + 1:end].split(",")
    if len(parts) < 2 or len(parts) > limit or any(not p for p in parts):
        return None
    return [text[:start] + part + text[end + 1:] for part in parts]


def is_excluded_rel(rel: str, excluded_paths: Sequence[str]) -> bool:
    """True when a repo-relative path is excluded as a doc source.

    Same semantics as the scanner's ``is_excluded``: a pattern ending in ``/``
    matches that directory anywhere in the path; any other pattern is a glob
    tried against the basename and against the whole path.
    """
    name = rel.rsplit("/", 1)[-1]
    for pattern in (*ALWAYS_EXCLUDED_DIRS, *excluded_paths):
        if pattern.endswith("/"):
            if rel.startswith(pattern) or ("/" + pattern.rstrip("/") + "/" in "/" + rel):
                return True
        elif fnmatch.fnmatchcase(name, pattern) or fnmatch.fnmatchcase(rel, pattern):
            return True
    return False


def matches_any_glob(rel: str, globs: Sequence[str]) -> bool:
    return any(glob_matcher(g).match(rel) for g in globs)


def is_doc_glob_match(rel: str, settings) -> bool:
    """True when the path has a doc's name, whether or not it is excluded."""
    return matches_any_glob(rel, settings.doc_globs)


def is_doc_path(rel: str, settings) -> bool:
    """True when the path is a doc: it matches a doc glob and is not excluded."""
    return is_doc_glob_match(rel, settings) and not is_excluded_rel(rel, settings.excluded_paths)


def in_path_set(rel: str) -> bool:
    """True when a listed entry belongs in the path set at all."""
    if not rel or rel.endswith("/"):
        return False            # a nested repository or worktree, listed by git as one entry
    return rel.split("/", 1)[0] not in PATHSET_EXCLUDED_ROOTS


class PathIndex:
    """The set of repo paths, indexed for the lookups the extractor needs.

    Mutable (``add`` / ``remove``) so a history replay can carry one index
    across commits. Every lookup is a dict access; nothing scans the path set
    except ``glob_matches``, whose results are cached until the set changes.
    """

    def __init__(self, paths: Iterable[str] = ()) -> None:
        self._files: Set[str] = set()
        self._dirs: Dict[str, int] = {}                 # dir (no trailing slash) -> files beneath
        self._base: Dict[str, Set[str]] = {}
        self._suffix: Dict[str, Set[str]] = {}          # proper multi-segment suffix -> files
        self._dir_suffix: Dict[str, Set[str]] = {}      # same, for directories
        self._glob_cache: Dict[str, int] = {}
        self._frozen: Optional[frozenset] = None
        self._top: Optional[frozenset] = None
        for path in paths:
            self.add(path)

    # -- mutation ---------------------------------------------------------

    @staticmethod
    def _proper_suffixes(path: str) -> List[str]:
        parts = path.split("/")
        if len(parts) > MAX_SUFFIX_DEPTH:
            return []                   # bounded: suffixes of a deep path would be cubic
        return ["/".join(parts[i:]) for i in range(1, len(parts) - 1)]

    def _changed(self) -> None:
        self._glob_cache.clear()
        self._frozen = None
        self._top = None

    def add(self, path: str) -> None:
        if path in self._files:
            return
        self._files.add(path)
        self._base.setdefault(path.rsplit("/", 1)[-1], set()).add(path)
        for suffix in self._proper_suffixes(path):
            self._suffix.setdefault(suffix, set()).add(path)
        parts = path.split("/")
        for i in range(1, len(parts)):
            d = "/".join(parts[:i])
            count = self._dirs.get(d, 0)
            if count == 0:
                for suffix in self._proper_suffixes(d):
                    self._dir_suffix.setdefault(suffix, set()).add(d)
            self._dirs[d] = count + 1
        self._changed()

    def remove(self, path: str) -> None:
        if path not in self._files:
            return
        self._files.discard(path)
        self._drop(self._base, path.rsplit("/", 1)[-1], path)
        for suffix in self._proper_suffixes(path):
            self._drop(self._suffix, suffix, path)
        parts = path.split("/")
        for i in range(1, len(parts)):
            d = "/".join(parts[:i])
            count = self._dirs[d] - 1
            if count == 0:
                del self._dirs[d]
                for suffix in self._proper_suffixes(d):
                    self._drop(self._dir_suffix, suffix, d)
            else:
                self._dirs[d] = count
        self._changed()

    @staticmethod
    def _drop(table: Dict[str, Set[str]], key: str, value: str) -> None:
        bucket = table.get(key)
        if bucket is None:
            return
        bucket.discard(value)
        if not bucket:
            del table[key]

    # -- lookups ----------------------------------------------------------

    def __len__(self) -> int:
        return len(self._files)

    def __contains__(self, path: object) -> bool:
        return path in self._files

    @property
    def files(self) -> frozenset:
        if self._frozen is None:
            self._frozen = frozenset(self._files)
        return self._frozen

    def paths(self) -> List[str]:
        return sorted(self._files)

    def has_file(self, path: str) -> bool:
        return path in self._files

    def has_dir(self, path: str) -> bool:
        return path.rstrip("/") in self._dirs

    @property
    def top_level_dirs(self) -> frozenset:
        if self._top is None:
            self._top = frozenset(d for d in self._dirs if "/" not in d)
        return self._top

    def by_basename(self, name: str) -> Tuple[str, ...]:
        return tuple(sorted(self._base.get(name, ())))

    def suffix_matches(self, candidate: str) -> Tuple[str, ...]:
        """Files and directories that end with ``/candidate`` on a segment boundary.

        Directories are returned in canonical form, with a trailing slash. A
        candidate ending in ``/`` matches directories only. The candidate must
        have two or more segments.
        """
        dir_only = candidate.endswith("/")
        key = candidate.rstrip("/")
        if "/" not in key:
            return ()
        out = [] if dir_only else list(self._suffix.get(key, ()))
        out.extend(d + "/" for d in self._dir_suffix.get(key, ()))
        return tuple(sorted(out))

    def glob_matches(self, pattern: str) -> int:
        """How many files a root-anchored glob matches."""
        hit = self._glob_cache.get(pattern)
        if hit is None:
            matcher = glob_matcher(pattern)
            hit = sum(1 for p in self._files if matcher.match(p))
            self._glob_cache[pattern] = hit
        return hit

    def fingerprint(self) -> str:
        """sha256 over the sorted path set."""
        return hashlib.sha256("\n".join(sorted(self._files)).encode("utf-8")).hexdigest()
