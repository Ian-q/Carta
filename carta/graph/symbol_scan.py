"""The ``dangling_symbol`` and ``ambiguous_symbol`` findings for ``carta scan``.

Both are off by default (``graph.scan.dangling_symbols`` and
``graph.scan.ambiguous_symbols``).

**dangling_symbol.** A doc names an identifier that has left the code. Most
identifiers a doc names and no index knows are simply not this repository's:
a library class, a shell command, a name from another project. So absence is
never enough. A finding needs both of:

1. the identifier's name occurs as a word in no code file today (a name that
   is still imported, called or configured somewhere is not gone, whatever
   the index says about where it is defined), and
2. git history shows it *was* in the code at a commit that changed this
   mention in the doc: when the doc was written to name it, the code had it.

An external name fails the second test, the same way ``missing_path_mention``
stays silent about a path that never existed. Without git there is no
finding. The check needs no symbol index: "is it still a word in the code"
is a text question, not a parsing one.

**ambiguous_symbol.** A doc names an identifier the symbol index defines in
more files than ``graph.symbols.max_definitions``, and neither a qualifier
nor a path the doc mentions narrows it. It is informational: the doc is not
wrong, the edge just could not be made.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from carta.graph.build import build_graph, list_repo_paths
from carta.graph.gitio import GitError, commits_changing_text, files_with_word, is_work_tree
from carta.graph.limits import (
    MAX_CODE_SCAN_BYTES, MAX_CODE_SCAN_FILE_BYTES, MAX_SYMBOL_HISTORY_CHECKS,
)
from carta.graph.model import MENTION_EXTRACTOR, SYMBOL_EXTRACTOR
from carta.graph.paths import PathIndex, in_path_set, is_doc_path, is_excluded_rel, matches_any_glob
from carta.graph.safeio import SkippedFile, read_regular_file
from carta.graph.settings import GraphSettings, graph_settings
from carta.graph.store import EdgeStore, store_path
from carta.graph.symbols import META_STATE, doc_state

DANGLING = "dangling_symbol"
AMBIGUOUS = "ambiguous_symbol"

_WORD = re.compile(rb"[A-Za-z_][A-Za-z0-9_]*")
_SEP = re.compile(r"\.|::|->|#")
_LEAF = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _is_code(path: str, settings: GraphSettings) -> bool:
    return (in_path_set(path) and not is_doc_path(path, settings)
            and not is_excluded_rel(path, settings.excluded_paths))


def words_in_code(repo_root: Path, index: PathIndex, settings: GraphSettings,
                  wanted: Set[str]) -> Tuple[Set[str], bool]:
    """Which of ``wanted`` occur as a whole word in some code file, and whether that is known.

    ``(found, complete)``. ``complete`` is False when a file could not be read
    or the total read passed ``MAX_CODE_SCAN_BYTES``: absence is then not
    proven, and the caller must report nothing. A file over
    ``MAX_CODE_SCAN_FILE_BYTES``, a link and a binary file are not code and are
    passed over.
    """
    remaining = {word.encode("ascii") for word in wanted if _LEAF.fullmatch(word)}
    found: Set[str] = set()
    total = 0
    for rel in index.paths():
        if not remaining:
            break
        if not _is_code(rel, settings):
            continue
        try:
            data = read_regular_file(repo_root, rel, MAX_CODE_SCAN_FILE_BYTES)
        except SkippedFile:
            continue
        except OSError:
            return found, False
        total += len(data)
        if total > MAX_CODE_SCAN_BYTES:
            return found, False
        if b"\0" in data[:8192]:
            continue
        # One word at a time: a list of every word of a 32 MiB file is gigabytes.
        hits = {match.group() for match in _WORD.finditer(data)
                if match.group() in remaining}
        if hits:
            remaining -= hits
            found.update(word.decode("ascii") for word in hits)
    return found, True


class _History:
    """Git lookups for the dangling check, counted against one scan's budget."""

    def __init__(self, repo_root: Path, settings: GraphSettings) -> None:
        self._root = repo_root
        self._settings = settings
        self._seen: Dict[Tuple[str, str], bool] = {}
        self.spent = 0
        self.exhausted = False

    def _spend(self) -> bool:
        if self.spent >= MAX_SYMBOL_HISTORY_CHECKS:
            self.exhausted = True
            return False
        self.spent += 1
        return True

    def _in_code_at(self, commit: str, leaf: str) -> Optional[bool]:
        key = (commit, leaf)
        hit = self._seen.get(key)
        if hit is None:
            if not self._spend():
                return None
            files = files_with_word(self._root, commit, leaf)
            hit = self._seen[key] = any(_is_code(path, self._settings) for path in files)
        return hit

    def last_seen(self, doc: str, token: str, leaf: str) -> Optional[str]:
        """A commit that changed ``token`` in ``doc`` while the code held ``leaf``, if any."""
        if not self._spend():
            return None
        commits = commits_changing_text(self._root, doc, token)
        if not commits:
            return None
        # The newest such commit, then the oldest: the last time the author
        # touched the mention, and the time it was first written.
        for commit in dict.fromkeys((commits[0], commits[-1])):
            known = self._in_code_at(commit, leaf)
            if known is None:
                return None
            if known:
                return commit
        return None


def _dangling(repo_root: Path, settings: GraphSettings, store: EdgeStore,
              stats: dict) -> List[dict]:
    first: Dict[Tuple[str, str], int] = {}
    for edge in store.iter_edges(kinds=("ident",), dst_types=("token",),
                                 extractors=(MENTION_EXTRACTOR,)):
        if matches_any_glob(edge.src, settings.scan_ignore_docs):
            continue
        key = (edge.src, edge.dst)
        if key not in first or edge.line < first[key]:
            first[key] = edge.line
    # An identifier with a symbol edge is defined somewhere, unless the edge
    # only reached its owner (`Parser.gone` resolved to where `Parser` is).
    for edge in store.iter_edges(kinds=("symbol",), extractors=(SYMBOL_EXTRACTOR,)):
        if json.loads(edge.meta).get("via") != "owner":
            first.pop((edge.src, edge.key), None)
    if not first:
        return []
    leaves = {key: _SEP.split(key[1])[-1] for key in first}
    index = PathIndex(list_repo_paths(repo_root))
    present, complete = words_in_code(repo_root, index, settings, set(leaves.values()))
    if not complete:
        stats["symbol_check"] = "incomplete"
        return []
    candidates = sorted((doc, first[(doc, token)], token)
                        for (doc, token) in first if leaves[(doc, token)] not in present)
    stats["dangling_candidates"] = len(candidates)
    if not candidates or not is_work_tree(repo_root):
        return []
    history = _History(repo_root, settings)
    findings = []
    for doc, line, token in candidates:
        commit = history.last_seen(doc, token, leaves[(doc, token)])
        if history.exhausted:
            stats["symbol_check"] = "incomplete"
            break
        if commit is None:
            continue
        findings.append({
            "type": DANGLING, "severity": "warning", "doc": doc,
            "detail": (f"names `{token}` (line {line}), which is no longer in the code; it was "
                       f"at {commit[:12]}, when this doc last changed that mention"),
            "symbol": token, "line": line, "last_seen": commit})
    return findings


def _ambiguous(settings: GraphSettings, store: EdgeStore) -> List[dict]:
    if store.get_meta(META_STATE) != "ok":
        return []
    findings = []
    for doc, raw in sorted(store.source_meta(SYMBOL_EXTRACTOR).items()):
        if matches_any_glob(doc, settings.scan_ignore_docs):
            continue
        try:
            entries = doc_state(raw)[2]
        except ValueError:                  # not a state the symbol pass wrote
            continue
        for token, (line, count) in sorted(entries.items()):
            findings.append({
                "type": AMBIGUOUS, "severity": "info", "doc": doc,
                "detail": (f"names `{token}` (line {line}), which is defined in {count} files; "
                           f"no symbol edge was made. Qualify it, or name the file"),
                "symbol": token, "line": line, "definitions": count})
    return findings


def symbol_findings(repo_root: Path, cfg: dict) -> Tuple[List[dict], dict]:
    """``(findings, stats)`` for the two symbol findings; both are off by default.

    ``stats["symbol_check"]`` is ``off`` (neither finding is switched on),
    ``ok``, or ``incomplete`` (absence from the code could not be proven, or
    the git budget ran out: the findings returned are right, and there may be
    more). Findings are sorted by doc, then line.
    """
    repo_root = Path(repo_root)
    settings = graph_settings(cfg)
    stats = {"symbol_check": "off", "dangling_symbols": 0, "ambiguous_symbols": 0}
    if not settings.enabled or not (settings.scan_dangling_symbols
                                    or settings.scan_ambiguous_symbols):
        return [], stats
    stats["symbol_check"] = "ok"
    build_graph(repo_root, cfg)
    store = EdgeStore.open_readonly(store_path(repo_root))
    if store is None:
        return [], stats
    findings: List[dict] = []
    with store:
        if settings.scan_dangling_symbols:
            try:
                found = _dangling(repo_root, settings, store, stats)
            except GitError:
                found, stats["symbol_check"] = [], "incomplete"
            stats["dangling_symbols"] = len(found)
            findings.extend(found)
        if settings.scan_ambiguous_symbols:
            found = _ambiguous(settings, store)
            stats["ambiguous_symbols"] = len(found)
            findings.extend(found)
    findings.sort(key=lambda f: (f["doc"], f["line"], f["type"], f["symbol"]))
    return findings, stats
