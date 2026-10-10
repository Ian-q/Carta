"""The edge record and the constants every graph module shares."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

# On-disk layout of the edge store. A store with a lower version is rebuilt;
# a store with a higher one was written by a newer Carta and is left alone.
SCHEMA_VERSION = 1

# Versions are per extractor: bumping one arm never invalidates another.
# Bump this whenever the mention extractor would emit different edges for an
# unchanged input.
MENTION_EXTRACTOR_VERSION = 3
MENTION_EXTRACTOR = "mention"

# The symbol arm (#131): bump when it would emit different edges for the same
# identifiers and the same symbol index.
SYMBOL_EXTRACTOR_VERSION = 1
SYMBOL_EXTRACTOR = "symbol"

KINDS = ("mention", "ident", "pattern", "cochange", "symbol", "related")
DST_TYPES = ("file", "doc", "dir", "glob", "missing", "token", "pattern")
SRC_TYPES = ("doc", "file")


# Terminal control characters (C0, DEL, C1) and the bidirectional overrides.
# Evidence is stored without them, and anything else a repository names (a
# file name can hold any of them) is printed through ``printable``: a
# repository must not be able to drive the terminal or reorder what is shown.
CONTROL_CHARS = re.compile("[\x00-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069]")


def printable(text: str) -> str:
    """``text`` with each control character written out as its escape (``\\x1b``)."""
    return CONTROL_CHARS.sub(
        lambda m: m.group().encode("unicode_escape").decode("ascii"), str(text))


def canonical_meta(data: dict) -> str:
    """Serialise an edge's ``meta`` mapping in the one canonical form."""
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


@dataclass(frozen=True, order=True)
class Edge:
    """One typed edge. Field order is the sort order used everywhere."""

    kind: str
    src: str
    dst: str
    dst_type: str
    line: int = 0
    key: str = ""          # discriminator inside (kind, src, dst, line); '' for mentions
    evidence: str = ""
    weight: float = 1.0
    meta: str = "{}"       # canonical JSON, see canonical_meta
    src_type: str = "doc"
    extractor: str = MENTION_EXTRACTOR
    extractor_version: int = MENTION_EXTRACTOR_VERSION

    @property
    def identity(self) -> tuple:
        """The store's unique key for this edge."""
        return (self.kind, self.src, self.dst, self.line, self.key)


@dataclass(frozen=True)
class Candidate:
    """A piece of doc text that might name something (stage 1 output)."""

    raw: str               # the text as written
    ctx: str               # link | code | fence | frontmatter | pattern
    line: int              # 1-based
    evidence: str          # the line, whitespace collapsed and truncated
    single: bool = True    # False when it is one token out of a multi-token code span
    pattern: str = ""      # pattern name when ctx == 'pattern'
