"""Stage 2 of mention extraction, and the composed pure extractor.

``extract_mentions`` is a pure function of (doc path, doc text, set of repo
paths, settings). It never reads the filesystem, the clock, the environment
or git, which is what lets the replay eval run it against historical trees
read from git objects.

The numbered rules (N1 to N14) are defined in the design spec; the comments
below name the rule each block implements.
"""
from __future__ import annotations

import posixpath
import re
from typing import Dict, Iterable, List, Optional, Sequence, Tuple, Union
from urllib.parse import unquote

from carta.graph.idents import parse_identifier
from carta.graph.markdown import scan_text
from carta.graph.model import (
    MENTION_EXTRACTOR, MENTION_EXTRACTOR_VERSION, Candidate, Edge, canonical_meta,
)
from carta.graph.paths import PATHSET_EXCLUDED_ROOTS, PathIndex, expand_braces, is_doc_path
from carta.graph.settings import DEFAULT_SETTINGS, GraphSettings

MAX_CANDIDATE_LENGTH = 300
FENCE_WEIGHT = 0.5

_PATH_CONTEXTS = ("link", "code", "fence", "frontmatter")
_LINE_SUFFIX = re.compile(r":L?\d+(?:[:-]L?\d+)?$")
_NODE_SUFFIX = re.compile(r"^(.*?\.[A-Za-z0-9]{1,8})(?:::.*|:[A-Za-z_][A-Za-z0-9_.]*)$")
_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
_EMAIL = re.compile(r"^[^/@\s]+@[^/@\s]+\.[A-Za-z]{2,}$")
_EXTENSION = re.compile(r"\.([A-Za-z0-9]{1,8})$")
_FORBIDDEN = re.compile(r"""[\\$<>|"'…]|\.\.\.""")
_SPACE = re.compile(r"\s")
_GLOB = re.compile(r"[*?\[]")
_GLOB_CLASS = re.compile(r"\[[^\]]*\]")
_LITERAL = re.compile(r"[A-Za-z0-9]")
_PLACEHOLDER_SEGMENTS = frozenset(
    ("foo", "bar", "baz", "qux", "example", "placeholder", "xxx", "your"))
_PLACEHOLDER_PREFIXES = ("your-", "your_", "my-", "my_")


def _has_extension(segment: str) -> bool:
    """True for ``name.ext`` with a non-numeric extension (``1.5`` is not a filename)."""
    m = _EXTENSION.search(segment)
    return bool(m) and m.start() > 0 and not m.group(1).isdigit()


def _normalise(raw: str, ctx: str, keep_hash: bool = False
               ) -> Optional[List[Tuple[str, bool]]]:
    """Rules N1 to N6. Returns ``(candidate, rooted)`` pairs, or ``None`` to reject.

    ``keep_hash`` leaves a ``#`` in place: the reading in which it is part of
    a file or directory name rather than the start of a fragment.
    """
    is_link = ctx == "link"
    text = raw.strip()
    # N1: wrappers and trailing punctuation
    changed = True
    while changed and len(text) >= 2:
        changed = False
        for left, right in (("<", ">"), ('"', '"'), ("'", "'"), ("(", ")"), ("`", "`")):
            if text[0] == left and text[-1] == right:
                text = text[1:-1].strip()
                changed = True
                break
    # Bold markers that ended up inside the span: "**src/a.py**". A real glob
    # keeps its stars because they sit next to a slash ("**/x", "src/**").
    if text.startswith("**") and not text.startswith("**/"):
        text = text[2:]
    if text.endswith("**") and not text.endswith("/**"):
        text = text[:-2]
    text = text.rstrip(".,;:!")
    if text.endswith(")") and text.count(")") > text.count("("):
        text = text[:-1]
    # N2: fragments, queries, line numbers, node ids
    if "#" in text and not keep_hash:
        text = text.split("#", 1)[0]
    if is_link and "?" in text:
        text = text.split("?", 1)[0]
    text = _LINE_SUFFIX.sub("", text)
    node = _NODE_SUFFIX.match(text)
    if node:
        text = node.group(1)
    if is_link:
        text = unquote(text)
    # N3
    if not text or len(text) > MAX_CANDIDATE_LENGTH:
        return None
    if _FORBIDDEN.search(text):
        return None
    if not is_link and _SPACE.search(text):
        return None
    # N4
    if (_SCHEME.match(text) or ":" in text or text.startswith(("//", "www.", "~", "-", "@"))
            or _EMAIL.match(text)):
        return None
    # N5
    rooted = False
    if text.startswith("/"):
        if not is_link:
            return None
        text = text.lstrip("/")
        rooted = True
        if not text:
            return None
    # N6
    expanded = expand_braces(text)
    if expanded is None:
        return None
    return [(item, rooted) for item in expanded if item]


def normalise_candidate(raw: str, ctx: str) -> Optional[List[str]]:
    """Pure string normalisation of one candidate (N1 to N6); ``None`` rejects it."""
    out = _normalise(raw, ctx)
    return [item for item, _rooted in out] if out else None


def _join(base: str, cand: str) -> Optional[str]:
    """``base/cand`` normalised; ``None`` when it leaves the repo."""
    path = posixpath.normpath(posixpath.join(base, cand)) if base else posixpath.normpath(cand)
    if path == "." or path == ".." or path.startswith("../") or path.startswith("/"):
        return None
    return path


def _ancestors(directory: str) -> List[str]:
    """The directory, then each parent, nearest first, excluding the repo root."""
    out = []
    while directory:
        out.append(directory)
        directory = posixpath.dirname(directory)
    return out


def _is_placeholder(path: str, index: PathIndex) -> bool:
    """True when a not-found path is an illustration, not a reference (see N13)."""
    segments = path.rstrip("/").split("/")
    lowered = [s.lower() for s in segments]
    for i in range(len(lowered) - 1):
        if lowered[i] == "path" and lowered[i + 1] == "to":
            return True
    last = len(segments) - 1
    for i, seg in enumerate(lowered):
        if index.has_dir("/".join(segments[:i + 1])):
            continue                    # a real directory is never a placeholder
        word = seg.split(".", 1)[0] if i == last else seg
        if word in _PLACEHOLDER_SEGMENTS or seg.startswith(_PLACEHOLDER_PREFIXES):
            return True
    return False


Hit = Tuple[str, str, dict]


class _Resolver:
    """Resolves the candidates of one doc against one path index."""

    def __init__(self, doc_path: str, index: PathIndex, settings: GraphSettings) -> None:
        self.doc_path = doc_path
        self.doc_dir = posixpath.dirname(doc_path)
        self.index = index
        self.settings = settings
        self._type_cache: Dict[str, str] = {}

    def _file_type(self, path: str) -> str:
        hit = self._type_cache.get(path)
        if hit is None:
            hit = "doc" if is_doc_path(path, self.settings) else "file"
            self._type_cache[path] = hit
        return hit

    def _bases(self, cand: str, is_link: bool, rooted: bool, explicit: bool) -> List[str]:
        """N8: the directories a candidate is read against, in order."""
        if rooted:
            return [""]
        if is_link:
            return [self.doc_dir, ""] if self.doc_dir else [""]
        if explicit:
            # "./x" in code is as often written from the repo root as from the
            # doc's directory; "../x" only makes sense from the doc.
            if cand.startswith("./") and self.doc_dir:
                return [self.doc_dir, ""]
            return [self.doc_dir]
        return [""] + _ancestors(self.doc_dir)

    def _lookup(self, cand: str, bases: Sequence[str], dir_form: bool, plain: bool
                ) -> Tuple[List[str], Optional[Hit], bool]:
        """Read ``cand`` against each base: ``(in-repo readings, hit, rejected)``."""
        index = self.index
        inside: List[str] = []
        for base in bases:
            path = _join(base, cand)
            if path is None:
                continue
            inside.append(path)
            via = {} if (not base or plain) else {"via": "relative"}
            if index.has_file(path):
                if dir_form:
                    return inside, None, True       # "file.py/" names nothing
                return inside, (path, self._file_type(path), via), False        # N9
            if index.has_dir(path):
                return inside, (path + "/", "dir", via), False                  # N10
        return inside, None, False

    def resolve(self, cand: str, ctx: str, rooted: bool,
                literal_only: bool = False) -> List[Hit]:
        """Every ``(dst, dst_type, extra meta)`` one normalised candidate names.

        Usually one. A link that is not found has two readings (beside the doc
        and from the repo root) and is recorded as missing under both, the
        second marked ``alt``. With ``literal_only`` only a file or directory
        that exists under exactly this spelling is accepted.
        """
        index = self.index
        is_link = ctx == "link"
        explicit = cand.startswith(("./", "../")) or cand in (".", "..")
        dir_form = cand.endswith("/")
        bare = "/" not in cand.rstrip("/") and not dir_form
        has_glob = bool(_GLOB.search(cand))
        bases = self._bases(cand, is_link, rooted, explicit)
        plain = is_link or explicit

        if (has_glob or literal_only) and not bare:
            # A real name wins over a pattern: "src/app/[id]/page.tsx".
            _inside, hit, _rejected = self._lookup(cand, bases, dir_form, plain)
            if hit is not None:
                return [hit]
        if literal_only:
            return []

        # N7: globs
        if has_glob:
            if "/" not in cand and not cand.startswith("*."):
                return []
            pattern = _join(self.doc_dir, cand) if explicit else _join("", cand)
            if pattern is None:
                return []
            # After normalising it must still be anchored somewhere and name
            # something literal: "*/", "**/" and a pasted regex are not globs.
            if "/" not in pattern and not pattern.startswith("*."):
                return []
            if not _LITERAL.search(_GLOB_CLASS.sub("", pattern)):
                return []
            matches = index.glob_matches(pattern)
            if not matches:
                return []
            return [(pattern, "glob", {"matches": matches})]

        if bare:
            return self._resolve_bare(cand, ctx)

        inside, hit, rejected = self._lookup(cand, bases, dir_form, plain)
        if hit is not None:
            return [hit]
        if rejected or not inside:
            return []                               # or every reading leaves the repo

        # N11: a unique suffix
        if not explicit and not rooted:
            key = posixpath.normpath(cand) + ("/" if dir_form else "")
            if not key.startswith(".."):
                matches = index.suffix_matches(key)
                if len(matches) == 1:
                    dst = matches[0]
                    dst_type = "dir" if dst.endswith("/") else self._file_type(dst)
                    return [(dst, dst_type, {"via": "suffix"})]
                if len(matches) > 1:
                    return []                       # ambiguous: no edge, and not "missing"

        # N13: missing
        if ctx == "fence":
            return []
        if explicit and not is_link and len(set(inside)) > 1:
            return []                               # "./x" in code: which reading is unknown
        if is_link and not explicit:
            return self._missing_readings(inside, dir_form)
        return self._missing_readings(inside[:1], dir_form)

    def _resolve_bare(self, cand: str, ctx: str) -> List[Hit]:
        """N12: a bare filename. Links look beside the doc and at the root, nothing else."""
        index = self.index
        sibling = posixpath.join(self.doc_dir, cand) if self.doc_dir else cand
        if ctx == "link":
            for path in (sibling, cand):
                if index.has_file(path):
                    return [(path, self._file_type(path), {})]
                if index.has_dir(path):
                    return [(path + "/", "dir", {})]
            if _has_extension(cand):
                return self._missing_readings([sibling, cand], False)
            return []
        if ctx == "fence" or not _has_extension(cand):
            return []
        if index.has_file(cand):
            return [(cand, self._file_type(cand), {})]
        if sibling != cand and index.has_file(sibling):
            return [(sibling, self._file_type(sibling), {"via": "relative"})]
        matches = index.by_basename(cand)
        if len(matches) == 1:
            return [(matches[0], self._file_type(matches[0]), {"via": "basename"})]
        return []

    def _missing_readings(self, readings: Sequence[str], dir_form: bool) -> List[Hit]:
        """The first reading as ``missing``, and any other as an ``alt`` of it.

        Which reading the author meant cannot be known once the file is gone,
        so an impact query or a history lookup can find the doc under either.
        """
        out: List[Hit] = []
        seen = set()
        for path in readings:
            if path in seen:
                continue
            seen.add(path)
            hit = self._missing(path, dir_form)
            if hit is not None:
                out.append((hit[0], hit[1], {"alt": True}) if out or path != readings[0]
                           else hit)
        return out

    def _missing(self, path: str, dir_form: bool) -> Optional[Hit]:
        segments = path.split("/")
        if segments[0] in PATHSET_EXCLUDED_ROOTS:
            return None
        if dir_form:
            if segments[0] not in self.index.top_level_dirs:
                return None
        elif not _has_extension(segments[-1]):
            return None
        if _is_placeholder(path, self.index):
            return None
        return path + ("/" if dir_form else ""), "missing", {}


def resolve_candidates(doc_path: str, candidates: Sequence[Candidate], index: PathIndex,
                       settings: GraphSettings = DEFAULT_SETTINGS) -> List[Edge]:
    """Turn stage-1 candidates into edges for a doc at ``doc_path``.

    The result is sorted and holds one edge per ``(kind, src, dst, line, key)``:
    the one with the highest weight, ties broken by edge ordering.
    """
    resolver = _Resolver(doc_path, index, settings)
    best: Dict[tuple, Edge] = {}

    def keep(edge: Edge) -> None:
        current = best.get(edge.identity)
        if (current is None or edge.weight > current.weight
                or (edge.weight == current.weight and edge < current)):
            best[edge.identity] = edge

    def make(kind: str, dst: str, dst_type: str, cand: Candidate, weight: float,
             meta: dict) -> Edge:
        return Edge(kind=kind, src=doc_path, dst=dst, dst_type=dst_type, line=cand.line,
                    evidence=cand.evidence, weight=weight, meta=canonical_meta(meta),
                    extractor=MENTION_EXTRACTOR, extractor_version=MENTION_EXTRACTOR_VERSION)

    for cand in candidates:
        if cand.ctx == "pattern":
            keep(make("pattern", f"{cand.pattern}:{cand.raw}", "pattern", cand, 1.0,
                      {"pattern": cand.pattern}))
            continue
        if cand.ctx not in _PATH_CONTEXTS:
            continue
        became_path = False
        hits: List[Hit] = []
        if "#" in cand.raw:
            # N2: "#" starts a fragment, unless the text with it names a real path.
            for item, rooted in _normalise(cand.raw, cand.ctx, keep_hash=True) or ():
                if "#" in item:
                    hits.extend(resolver.resolve(item, cand.ctx, rooted, literal_only=True))
        if not hits:
            for item, rooted in _normalise(cand.raw, cand.ctx) or ():
                hits.extend(resolver.resolve(item, cand.ctx, rooted))
        for dst, dst_type, extra in hits:
            became_path = True
            if dst == doc_path:
                continue                                                    # N14
            meta = {"ctx": cand.ctx}
            if cand.raw != dst:
                meta["raw"] = cand.raw
            meta.update(extra)
            weight = FENCE_WEIGHT if cand.ctx == "fence" else 1.0
            keep(make("mention", dst, dst_type, cand, weight, meta))
        if (not became_path and cand.ctx == "code" and cand.single
                and settings.ident_enabled):
            parsed = parse_identifier(cand.raw, min_length=settings.ident_min_length,
                                      stoplist=settings.ident_stoplist)
            if parsed:
                token, was_call = parsed
                keep(make("ident", token, "token", cand, 1.0, {"call": True} if was_call else {}))
    return sorted(best.values())


def extract_mentions(doc_path: str, text: str, repo_paths: Union[PathIndex, Iterable[str]],
                     settings: GraphSettings = DEFAULT_SETTINGS) -> List[Edge]:
    """The pure extractor: (doc path, doc text, set of repo paths) -> sorted edges."""
    index = repo_paths if isinstance(repo_paths, PathIndex) else PathIndex(repo_paths)
    return resolve_candidates(doc_path, scan_text(text, settings.patterns), index, settings)
