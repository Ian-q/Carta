"""Stage 1 of mention extraction: doc text in, candidates out.

``scan_text`` depends on the text and the configured patterns and on nothing
else, so its result can be cached by content hash. It decides *where* in a
doc a mention can come from (frontmatter, fenced blocks, links, inline code,
project patterns); it does not decide what a candidate means. Plain prose is
never read for paths.
"""
from __future__ import annotations

import re
from bisect import bisect_right
from typing import List, Optional, Sequence, Tuple

from carta.graph.limits import (
    MAX_CANDIDATES_PER_DOC, MAX_DOC_BYTES, MAX_LINE_LENGTH, MAX_LINK_TARGET_LENGTH,
)
from carta.graph.model import CONTROL_CHARS, Candidate

EVIDENCE_LIMIT = 240

_FENCE_OPEN = re.compile(r"^\s*(`{3,}|~{3,})(.*)$")
_FENCE_SPLIT = re.compile(r"""[\s"'`()<>,;=\[\]{}|]+""")
_FRONTMATTER_STRIP = "-:'\"[],"
_BACKTICKS = re.compile(r"`+")
_SPACE = re.compile(r"\s")
# Any "](target)" or "](target "title")": inline links and images, including
# an image nested in a link's text. Every run is bounded, and the target is
# matched inside a lookahead and then by backreference, which makes it atomic
# (giving characters of a target back can never produce a match, since what
# follows it must be a space or the closing bracket). Without both, each "]("
# on a hostile line re-scans the rest of the line: quadratic in its length.
_N = str(MAX_LINK_TARGET_LENGTH)
_INLINE_LINK = re.compile(
    r"""\]\(\s*(?=(<[^>\n]{0,""" + _N + r"""}>|[^)\s]{1,""" + _N + r"""}))\1"""
    r"""(?:\s+(?:"[^"]{0,""" + _N + r"""}"|'[^']{0,""" + _N + r"""}'|\([^)]{0,""" + _N
    + r"""}\)))?\s*\)""")
_REF_DEF = re.compile(r"^ {0,3}\[([^\]]+)\]:\s+(<[^>]*>|\S+)")
# Evidence is printed, so it is stored without control characters.
_CONTROL = CONTROL_CHARS
_CALL_HEAD = re.compile(
    r"[&*@]?[A-Za-z_][A-Za-z0-9_]*(?:(?:\.|::|->|#)[A-Za-z_][A-Za-z0-9_]*)*\(")


def collapse_evidence(line: str, limit: int = EVIDENCE_LIMIT) -> str:
    """Collapse whitespace and cut to ``limit`` characters, ending ``…`` when cut."""
    text = _CONTROL.sub("", " ".join(line.split()))
    if len(text) > limit:
        text = text[:limit - 1] + "…"
    return text


def is_call_span(span: str) -> bool:
    """True when the whole span is ``identifier(...)`` with one balanced argument list."""
    head = _CALL_HEAD.match(span)
    if not head or not span.endswith(")"):
        return False
    depth = 0
    for i in range(head.end() - 1, len(span)):
        ch = span[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i == len(span) - 1
    return False


def _assigned_parts(token: str) -> List[str]:
    """The pieces of ``--flag=path`` or ``NAME=path``; empty when it is not one.

    Only a token that holds a ``/`` is split, and never a URL (its query
    string is not a list of paths).
    """
    if "=" not in token or "/" not in token or "://" in token:
        return []
    return [part for part in token.split("=") if part]


def _code_spans(line: str) -> List[Tuple[int, int, int, int]]:
    """Inline code spans on one line as (outer start, outer end, inner start, inner end)."""
    runs = [(m.start(), m.end()) for m in _BACKTICKS.finditer(line)]
    spans = []
    i = 0
    while i < len(runs):
        start, end = runs[i]
        width = end - start
        for j in range(i + 1, len(runs)):
            if runs[j][1] - runs[j][0] == width:
                spans.append((start, runs[j][1], end, runs[j][0]))
                i = j
                break
        i += 1
    return spans


def _inside(pos: int, spans: Sequence[Tuple[int, int, int, int]],
            starts: Optional[Sequence[int]] = None) -> bool:
    """True when ``pos`` is inside a code span. Spans are in order and do not overlap."""
    if not spans:
        return False
    if starts is None:
        starts = [s[0] for s in spans]
    i = bisect_right(starts, pos) - 1
    return i >= 0 and pos < spans[i][1]


def _frontmatter_end(lines: Sequence[str]) -> Optional[int]:
    """Index of the closing ``---`` line, or None when there is no frontmatter."""
    if not lines or lines[0].rstrip() != "---":
        return None
    for i in range(1, len(lines)):
        if lines[i].rstrip() == "---":
            return i
    return None


def scan_text(text: str, patterns: Sequence = ()) -> List[Candidate]:
    """Return every candidate in ``text``, ordered by line then column.

    Bounded before any work: a text over ``MAX_DOC_BYTES`` characters yields
    nothing, a line over ``MAX_LINE_LENGTH`` is not scanned (it still counts
    for line numbers), and at most ``MAX_CANDIDATES_PER_DOC`` are returned.
    """
    if len(text) > MAX_DOC_BYTES:
        return []
    if text.startswith("﻿"):
        text = text[1:]
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    found: List[Tuple[int, int, int, Candidate]] = []

    def emit(line_no: int, col: int, raw: str, ctx: str, evidence: str,
             single: bool = True, pattern: str = "") -> None:
        if len(found) >= MAX_CANDIDATES_PER_DOC:
            return
        found.append((line_no, col, len(found),
                      Candidate(raw, ctx, line_no, evidence, single, pattern)))

    def run_patterns(line_no: int, line: str, evidence: str, zone: str,
                     spans: Sequence[Tuple[int, int, int, int]] = (),
                     starts: Sequence[int] = ()) -> None:
        # zone: 'meta' (frontmatter), 'fence', or 'body'
        for spec in patterns:
            if spec.where == "prose" and zone != "body":
                continue
            if spec.where == "code" and zone == "meta":
                continue
            for m in spec.compiled.finditer(line):
                if zone == "body" and spec.where != "all":
                    in_code = _inside(m.start(), spans, starts)
                    if in_code != (spec.where == "code"):
                        continue
                raw = m.group(1) if spec.compiled.groups else m.group(0)
                if raw:
                    emit(line_no, m.start(), raw, "pattern", evidence, True, spec.name)

    fm_end = _frontmatter_end(lines)
    fence_char = ""
    fence_len = 0
    for idx, line in enumerate(lines):
        line_no = idx + 1
        if len(found) >= MAX_CANDIDATES_PER_DOC:
            break
        if len(line) > MAX_LINE_LENGTH or not line.strip():
            continue
        evidence = collapse_evidence(line)

        if fm_end is not None and idx <= fm_end:
            if 0 < idx < fm_end:
                col = 0
                for token in line.split():
                    col = line.index(token, col)
                    cleaned = token.strip(_FRONTMATTER_STRIP)
                    if "/" in cleaned or "." in cleaned:
                        emit(line_no, col, cleaned, "frontmatter", evidence)
                    col += len(token)
                run_patterns(line_no, line, evidence, "meta")
            continue

        if fence_char:
            stripped = line.strip()
            if (len(stripped) >= fence_len and set(stripped) == {fence_char}):
                fence_char = ""
                continue
            col = 0
            for token in _FENCE_SPLIT.split(line):
                if not token:
                    continue
                col = line.index(token, col)
                if "/" in token:
                    emit(line_no, col, token, "fence", evidence, False)
                col += len(token)
            run_patterns(line_no, line, evidence, "fence")
            continue

        opened = _FENCE_OPEN.match(line)
        if opened and not (opened.group(1)[0] == "`" and "`" in opened.group(2)):
            fence_char = opened.group(1)[0]
            fence_len = len(opened.group(1))
            continue

        spans = _code_spans(line)
        starts = [span[0] for span in spans]
        for outer_start, _outer_end, inner_start, inner_end in spans:
            content = line[inner_start:inner_end].strip()
            if not content:
                continue
            if is_call_span(content) or (
                    not _SPACE.search(content) and not _assigned_parts(content)):
                emit(line_no, outer_start, content, "code", evidence, True)
                continue
            col = inner_start
            for token in content.split():
                col = line.index(token, col)
                sub = col
                for part in _assigned_parts(token) or [token]:
                    sub = line.index(part, sub)
                    if "/" in part:
                        emit(line_no, sub, part, "code", evidence, False)
                    sub += len(part)
                col += len(token)
        for m in _INLINE_LINK.finditer(line):
            if not _inside(m.start(), spans, starts):
                emit(line_no, m.start(1), m.group(1), "link", evidence)
        ref = _REF_DEF.match(line)
        if ref and not ref.group(1).startswith("^") and not _inside(ref.start(), spans, starts):
            emit(line_no, ref.start(2), ref.group(2), "link", evidence)
        run_patterns(line_no, line, evidence, "body", spans, starts)

    found.sort(key=lambda item: item[:3])
    return [item[3] for item in found]
