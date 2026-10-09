"""Build the mention edges of a repository into its edge store.

Incremental by content hash: a doc is re-extracted only when its bytes
change, unless the path set or the extraction settings changed, in which
case every doc is re-extracted (whether a candidate resolves depends on
which paths exist). There is no mtime shortcut; every doc is hashed on
every build.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from carta.graph.gitio import GitError, is_work_tree, ls_files
from carta.graph.mentions import extract_mentions
from carta.graph.model import MENTION_EXTRACTOR, MENTION_EXTRACTOR_VERSION
from carta.graph.paths import PathIndex, in_path_set, is_doc_path, is_excluded_rel
from carta.graph.settings import GraphSettings, graph_settings
from carta.graph.store import EdgeStore, StoreUnreadableError, StoreVersionError, store_path

META_SETTINGS = "mention.settings_sha"
META_PATHS = "mention.paths_sha"
META_VERSION = "mention.extractor_version"


def content_sha256(data: bytes) -> str:
    """sha256 of file content with CRLF folded to LF (same rule as the embedding arm)."""
    return hashlib.sha256(data.replace(b"\r\n", b"\n")).hexdigest()


def decode_text(data: bytes) -> str:
    """Bytes to text the same way everywhere: UTF-8, undecodable bytes replaced."""
    return data.decode("utf-8", "replace")


def _walk_paths(repo_root: Path) -> List[str]:
    out: List[str] = []
    root = str(repo_root)
    for current, dirs, files in os.walk(root):
        rel_dir = os.path.relpath(current, root).replace(os.sep, "/")
        rel_dir = "" if rel_dir == "." else rel_dir + "/"
        dirs[:] = sorted(
            d for d in dirs
            if in_path_set(rel_dir + d + "/x") and not is_excluded_rel(rel_dir + d + "/", ()))
        for name in files:
            out.append(rel_dir + name)
    return out


def list_repo_paths(repo_root: Path) -> List[str]:
    """Every path that can be named by a doc, repo-relative POSIX, sorted.

    From ``git ls-files`` (tracked plus untracked-but-not-ignored) when the
    directory is in a git work tree, otherwise from a directory walk. Nested
    repositories and everything under ``.carta/`` and ``.git/`` are left out.
    """
    repo_root = Path(repo_root)
    listed: Optional[List[str]] = None
    if is_work_tree(repo_root):
        try:
            listed = ls_files(repo_root)
        except GitError:
            listed = None
    if listed is None:
        return sorted(p for p in _walk_paths(repo_root) if in_path_set(p))
    out = []
    for rel in listed:
        if not in_path_set(rel):
            continue
        full = os.path.join(str(repo_root), rel)
        if os.path.isfile(full) or os.path.islink(full):
            out.append(rel)
    return sorted(out)


def _scan_tree(repo_root: Path, settings: GraphSettings) -> Tuple[PathIndex, List[str]]:
    index = PathIndex(list_repo_paths(repo_root))
    docs = [p for p in index.paths() if is_doc_path(p, settings)]
    return index, docs


def _read(repo_root: Path, rel: str) -> Optional[bytes]:
    try:
        return (repo_root / rel).read_bytes()
    except OSError:
        return None


def _disabled_report() -> dict:
    return {"enabled": False, "docs": 0, "extracted": 0, "unchanged": 0, "written": 0,
            "removed": 0, "skipped": 0, "edges": 0, "full": False, "digest": "", "by_kind": {}}


def build_graph(repo_root: Path, cfg: dict, *, force: bool = False) -> dict:
    """Bring the edge store at ``repo_root`` up to date with the working tree.

    Returns a report: ``docs`` (sources found), ``extracted`` (re-extracted
    this run), ``unchanged`` (not re-extracted), ``written`` (sources whose
    stored rows actually changed), ``removed`` (sources dropped), ``skipped``
    (docs that could not be read), ``edges`` and ``by_kind`` (store totals),
    ``full`` (every doc was re-extracted) and ``digest``.
    """
    repo_root = Path(repo_root)
    settings = graph_settings(cfg)
    if not settings.enabled:
        return _disabled_report()
    index, docs = _scan_tree(repo_root, settings)
    settings_sha = settings.fingerprint()
    paths_sha = index.fingerprint()
    version = str(MENTION_EXTRACTOR_VERSION)
    extracted = unchanged = written = skipped = 0

    with EdgeStore(store_path(repo_root)) as store, store.transaction():
        full = (
            force
            or store.get_meta(META_SETTINGS) != settings_sha
            or store.get_meta(META_PATHS) != paths_sha
            or store.get_meta(META_VERSION) != version
        )
        rewrite_all = force or store.get_meta(META_VERSION) != version
        known = store.source_hashes(MENTION_EXTRACTOR)
        present = set()
        for doc in docs:
            data = _read(repo_root, doc)
            if data is None:
                skipped += 1
                continue
            present.add(doc)
            sha = content_sha256(data)
            same_content = known.get(doc) == sha
            if same_content and not full:
                unchanged += 1
                continue
            extracted += 1
            # A NUL byte means the file is not text, whatever its name says.
            edges = [] if b"\0" in data else extract_mentions(
                doc, decode_text(data), index, settings)
            if (same_content and not rewrite_all
                    and store.source_edges(MENTION_EXTRACTOR, doc) == edges):
                continue
            store.replace_source(MENTION_EXTRACTOR, doc, sha, MENTION_EXTRACTOR_VERSION, edges)
            written += 1
        gone = sorted(set(known) - present)
        for doc in gone:
            store.remove_source(MENTION_EXTRACTOR, doc)
        store.set_meta(META_SETTINGS, settings_sha)
        store.set_meta(META_PATHS, paths_sha)
        store.set_meta(META_VERSION, version)
        counts = store.counts()
        digest = store.edges_digest((MENTION_EXTRACTOR,))

    return {"enabled": True, "docs": len(present), "extracted": extracted,
            "unchanged": unchanged, "written": written, "removed": len(gone),
            "skipped": skipped, "edges": counts["edges"], "full": bool(full),
            "digest": digest, "by_kind": counts["by_kind"]}


def graph_status(repo_root: Path, cfg: dict) -> dict:
    """Describe the store without building it or creating any file.

    ``stale`` is true when a build would change the store; ``stale_reasons``
    lists why, from ``no_store``, ``newer_schema``, ``extractor_changed``,
    ``settings_changed``, ``paths_changed``, ``docs_changed`` and ``unreadable``
    (the file is damaged or is not a store; it is left exactly as found, and
    the next build replaces it).
    """
    repo_root = Path(repo_root)
    settings = graph_settings(cfg)
    status = {"enabled": settings.enabled, "exists": False, "schema_version": None,
              "extractor_version": None, "docs": 0, "edges": 0, "by_kind": {},
              "by_dst_type": {}, "digest": "", "stale": True, "stale_reasons": ["no_store"]}
    try:
        store = EdgeStore.open_readonly(store_path(repo_root))
    except StoreVersionError:
        status.update(exists=True, stale_reasons=["newer_schema"])
        return status
    except StoreUnreadableError:
        status.update(exists=True, stale_reasons=["unreadable"])
        return status
    if store is None:
        return status
    with store:
        reasons: List[str] = []
        stored_version = store.get_meta(META_VERSION)
        if stored_version != str(MENTION_EXTRACTOR_VERSION):
            reasons.append("extractor_changed")
        if store.get_meta(META_SETTINGS) != settings.fingerprint():
            reasons.append("settings_changed")
        index, docs = _scan_tree(repo_root, settings)
        if store.get_meta(META_PATHS) != index.fingerprint():
            reasons.append("paths_changed")
        current: Dict[str, str] = {}
        for doc in docs:
            data = _read(repo_root, doc)
            if data is not None:
                current[doc] = content_sha256(data)
        known = store.source_hashes(MENTION_EXTRACTOR)
        if current != known:
            reasons.append("docs_changed")
        counts = store.counts()
        status.update(
            exists=True, schema_version=store.schema_version,
            extractor_version=int(stored_version) if (stored_version or "").isdigit() else None,
            docs=len(known), edges=counts["edges"], by_kind=counts["by_kind"],
            by_dst_type=counts["by_dst_type"], digest=store.edges_digest((MENTION_EXTRACTOR,)),
            stale=bool(reasons), stale_reasons=reasons)
    return status
