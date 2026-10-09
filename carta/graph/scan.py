"""The ``missing_path_mention`` finding for ``carta scan``.

A doc that names a repo path which is not there is only reported when there
is positive evidence the path was real: git history deleted or renamed it, or
the mention is a markdown link that plainly points into the repository. A
path that never existed (a planned file, an output name, an illustration)
produces nothing. That is the precision choice.
"""
from __future__ import annotations

import json
import os
import posixpath
from pathlib import Path
from typing import Dict, FrozenSet, List, Optional, Set, Tuple

from carta.graph.build import build_graph, list_repo_paths
from carta.graph.gitio import GitError, deleted_and_renamed, ignored_paths, is_work_tree
from carta.graph.model import MENTION_EXTRACTOR
from carta.graph.paths import PathIndex, is_doc_glob_match, matches_any_glob
from carta.graph.settings import GraphSettings, graph_settings
from carta.graph.store import EdgeStore, store_path

FINDING_TYPE = "missing_path_mention"
_MAX_RENAME_HOPS = 32


class _History:
    """What git history says went away, read once and only if needed."""

    def __init__(self, repo_root: Path, limit: int) -> None:
        self._root = repo_root
        self._limit = limit
        self._loaded = False
        self.deleted: FrozenSet[str] = frozenset()
        self.renamed: Dict[str, str] = {}
        self._gone_dirs: Set[str] = set()

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if not is_work_tree(self._root):
            return
        try:
            self.deleted, self.renamed = deleted_and_renamed(self._root, self._limit)
        except GitError:
            return
        for path in list(self.deleted) + list(self.renamed):
            parts = path.split("/")
            for i in range(1, len(parts)):
                self._gone_dirs.add("/".join(parts[:i]) + "/")

    def lookup(self, path: str) -> Optional[Tuple[str, Optional[str]]]:
        """``(reason, final path a rename chain leads to)`` or ``None`` when history is silent."""
        self._load()
        if path in self.renamed:
            current, hops = path, 0
            while current in self.renamed and hops < _MAX_RENAME_HOPS:
                current = self.renamed[current]
                hops += 1
            return "renamed", current
        if path in self.deleted:
            return "deleted", None
        if path.endswith("/") and path in self._gone_dirs:
            return "deleted", None
        return None


def _present(repo_root: Path, path: str) -> bool:
    """True when the path is on disk. A directory with no file under it is not."""
    full = os.path.join(str(repo_root), path.rstrip("/"))
    if not os.path.lexists(full):
        return False
    if not path.endswith("/") or not os.path.isdir(full):
        return True
    # git does not track directories: an empty one left behind after its files
    # were deleted is still a removed directory.
    for _current, _dirs, files in os.walk(full):
        if files:
            return True
    return False


def _link_candidates(edges, settings: GraphSettings, index: PathIndex) -> Set[str]:
    """Missing link targets that plainly point into the repository."""
    doc_stems = {(posixpath.dirname(p), posixpath.splitext(posixpath.basename(p))[0])
                 for p in index.paths() if is_doc_glob_match(p, settings)}
    out = set()
    for edge in edges:
        path = edge.dst
        if path.endswith("/") or json.loads(edge.meta).get("ctx") != "link":
            continue
        parent = posixpath.dirname(path)
        if is_doc_glob_match(path, settings):
            out.add(path)
            continue
        stem = posixpath.splitext(posixpath.basename(path))[0]
        if (parent, stem) in doc_stems:
            continue                    # the rendered name of a doc (setup.html for setup.md)
        if not parent or index.has_dir(parent):
            out.add(path)
    return out


def _detail(path: str, line: int, reason: str, renamed_to: Optional[str]) -> str:
    if reason == "broken_link":
        return f"links to `{path}` (line {line}), which does not exist"
    head = f"mentions `{path}` (line {line}), which no longer exists; "
    if reason == "deleted":
        return head + "deleted from the repository"
    if renamed_to:
        return head + f"renamed to {renamed_to}"
    return head + "renamed, then removed"


def missing_path_findings(repo_root: Path, cfg: dict) -> List[dict]:
    """Scan findings for docs that name a repo path which no longer exists.

    Brings the edge store up to date, then reports each ``missing`` mention
    that history (or a link) shows was meant as a real path. One finding per
    (doc, path), on the first line that names it, sorted by doc then line.
    Returns ``[]`` when the graph arm or this finding is switched off.
    """
    repo_root = Path(repo_root)
    settings = graph_settings(cfg)
    if not settings.enabled or not settings.scan_missing_paths:
        return []
    build_graph(repo_root, cfg)
    store = EdgeStore.open_existing(store_path(repo_root))
    if store is None:
        return []
    with store:
        edges = [e for e in store.iter_edges(kinds=("mention",), dst_types=("missing",),
                                             extractors=(MENTION_EXTRACTOR,))
                 if not matches_any_glob(e.src, settings.scan_ignore_docs)
                 and not _present(repo_root, e.dst)]
    if not edges:
        return []

    history = _History(repo_root, settings.scan_history_limit)
    index = PathIndex(list_repo_paths(repo_root))
    link_ok = _link_candidates(edges, settings, index)
    link_ok -= ignored_paths(repo_root, sorted(link_ok))

    first: Dict[Tuple[str, str], int] = {}
    linked: Set[Tuple[str, str]] = set()
    for edge in edges:
        key = (edge.src, edge.dst)
        if key not in first or edge.line < first[key]:
            first[key] = edge.line
        if json.loads(edge.meta).get("ctx") == "link":
            linked.add(key)

    findings = []
    for (doc, path), line in first.items():
        renamed_to: Optional[str] = None
        hit = history.lookup(path)
        if hit is not None:
            reason, final = hit
            if final and os.path.lexists(os.path.join(str(repo_root), final)):
                renamed_to = final
        elif (doc, path) in linked and path in link_ok:
            reason = "broken_link"
        else:
            continue
        finding = {"type": FINDING_TYPE, "severity": "warning", "doc": doc,
                   "detail": _detail(path, line, reason, renamed_to),
                   "path": path, "line": line, "reason": reason}
        if renamed_to:
            finding["renamed_to"] = renamed_to
        findings.append(finding)
    findings.sort(key=lambda f: (f["doc"], f["line"], f["path"]))
    return findings
