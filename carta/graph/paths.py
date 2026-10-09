"""Path primitives: globs, brace expansion, exclusion and the path index.

Everything here is pure string work on repo-relative POSIX paths. Nothing
reads the filesystem.
"""
from __future__ import annotations

import fnmatch
import hashlib
import re
from functools import lru_cache
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

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


@lru_cache(maxsize=4096)
def glob_to_regex(glob: str) -> "re.Pattern[str]":
    """Translate a path glob into a compiled regex that must match a whole path.

    ``**/`` is any number of directories (including none), ``**`` is anything,
    ``*`` and ``?`` stay within one segment, ``[...]`` is a character class.
    Every other character is literal.
    """
    out: List[str] = []
    i, n = 0, len(glob)
    while i < n:
        c = glob[i]
        if c == "*":
            if glob.startswith("**/", i):
                out.append("(?:.*/)?")
                i += 3
            elif glob.startswith("**", i):
                out.append(".*")
                i += 2
            else:
                out.append("[^/]*")
                i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        elif c == "[":
            j = glob.find("]", i + 2)
            if j == -1:
                out.append(re.escape(c))
                i += 1
            else:
                body = glob[i + 1:j]
                negate = body[:1] in ("!", "^")
                if negate:
                    body = body[1:]
                body = body.replace("\\", "\\\\").replace("[", "\\[").replace("^", "\\^")
                out.append("[" + ("^/" if negate else "") + body + "]")
                i = j + 1
        else:
            out.append(re.escape(c))
            i += 1
    return re.compile("".join(out) + r"\Z")


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
    return any(glob_to_regex(g).match(rel) for g in globs)


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
            regex = glob_to_regex(pattern)
            hit = sum(1 for p in self._files if regex.match(p))
            self._glob_cache[pattern] = hit
        return hit

    def fingerprint(self) -> str:
        """sha256 over the sorted path set."""
        return hashlib.sha256("\n".join(sorted(self._files)).encode("utf-8")).hexdigest()
