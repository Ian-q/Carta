"""Corpus-integrity scanning: detect point-ID collisions, empty-text points,
sidecar/Qdrant count mismatches, and stuck-stale sidecars.

Read-only — used by `carta doctor` (report) and `carta embed --repair`
(which re-embeds/purges what this module finds).
"""
from __future__ import annotations

from collections import defaultdict
from pathlib import Path

from qdrant_client import QdrantClient

from carta.config import collection_name
from carta.embed.lifecycle import compute_file_hash
from carta.embed.induct import read_sidecar


def _scroll_all(client, coll: str):
    """Yield every point payload in a collection (no vectors)."""
    offset = None
    while True:
        points, offset = client.scroll(
            coll, limit=1000, offset=offset, with_payload=True, with_vectors=False
        )
        for p in points:
            yield p.payload or {}
        if offset is None:
            return


def scan_corpus_integrity(cfg: dict, repo_root: Path, client=None) -> dict:
    """Scan the project's _doc collection and sidecars for integrity issues.

    Args:
        cfg:       Carta config dict (must contain project_name, qdrant_url).
        repo_root: Absolute path to the repository root.
        client:    Optional pre-built QdrantClient (for testing / injection).

    Returns a dict with keys:
        slug_collisions:      {slug: [file_path, ...]} — slugs with >1 file_path
        empty_files:          [file_path] — every point for the file has empty text
        partial_empty_files:  {file_path: n_empty} — some (not all) points empty
        count_mismatches:     {file_path: {"sidecar": n, "qdrant": n}}
        stuck_stale:          [rel_path] — status="stale" but file hash matches disk
        affected_files:       sorted union of files needing re-embed/purge
                              (slug collision + empty + partial + mismatches;
                               stuck_stale excluded — repair handles those separately)
    """
    if client is None:
        client = QdrantClient(url=cfg["qdrant_url"], timeout=30)

    coll = collection_name(cfg, "doc")

    # Per-file tallies gathered from Qdrant payloads
    slug_files: dict[str, set] = defaultdict(set)
    per_file_counts: dict[str, int] = defaultdict(int)
    per_file_empty: dict[str, int] = defaultdict(int)

    if client.collection_exists(coll):
        for payload in _scroll_all(client, coll):
            fp = payload.get("file_path", "")
            slug = payload.get("slug", "")
            slug_files[slug].add(fp)
            per_file_counts[fp] += 1
            if not (payload.get("text") or "").strip():
                per_file_empty[fp] += 1

    # Slug collisions: same slug mapped to more than one file_path
    slug_collisions = {
        slug: sorted(files)
        for slug, files in slug_files.items()
        if len(files) > 1
    }

    # Files where every point has empty text
    empty_files = sorted(
        fp for fp, n in per_file_empty.items() if n == per_file_counts[fp]
    )

    # Files where some (but not all) points have empty text
    partial_empty_files = {
        fp: n
        for fp, n in sorted(per_file_empty.items())
        if 0 < n < per_file_counts[fp]
    }

    # Sidecar-side checks
    count_mismatches: dict[str, dict] = {}
    stuck_stale: list[str] = []

    sidecars_root = repo_root / ".carta" / "sidecars"
    if sidecars_root.exists():
        for sc_path in sidecars_root.rglob("*.embed-meta.yaml"):
            sc = read_sidecar(sc_path)
            if not isinstance(sc, dict):
                # Corrupt sidecar (valid YAML but not a mapping) — one bad file
                # must not blind the whole scan.
                continue
            rel = sc.get("current_path")
            if not rel:
                continue

            status = sc.get("status")
            # Missing entry means ZERO surviving points — a fully-lost file
            # (e.g. every chunk shadowed by a legacy ID collision) is the
            # strongest mismatch of all, not an exemption.
            qdrant_count = per_file_counts.get(rel, 0)
            sidecar_count = sc.get("chunk_count")

            # Stuck-stale: status is "stale" but file hash matches disk — a bug
            # artifact, not a pending re-embed.
            is_stuck = False
            if status == "stale":
                src = repo_root / rel
                if src.exists():
                    try:
                        if compute_file_hash(src) == sc.get("file_hash"):
                            is_stuck = True
                            stuck_stale.append(rel)
                    except OSError:
                        pass

            # Count mismatch: sidecar chunk_count differs from Qdrant point count.
            # Checked for "embedded" sidecars AND stuck-stale ones (their counts
            # should agree with Qdrant — nothing is pending). Genuinely-stale
            # sidecars (hash differs) are expected to be out of sync and exempt.
            if (
                (status == "embedded" or is_stuck)
                and sidecar_count is not None
                and sidecar_count != qdrant_count
            ):
                count_mismatches[rel] = {
                    "sidecar": sidecar_count,
                    "qdrant": qdrant_count,
                }

    # affected_files: union of everything needing re-embed or purge
    # (stuck_stale excluded — repair marks them pending without full re-embed)
    affected: set[str] = set(empty_files) | set(partial_empty_files) | set(count_mismatches)
    for files in slug_collisions.values():
        affected.update(files)

    return {
        "slug_collisions": slug_collisions,
        "empty_files": empty_files,
        "partial_empty_files": partial_empty_files,
        "count_mismatches": count_mismatches,
        "stuck_stale": sorted(stuck_stale),
        "affected_files": sorted(affected),
    }
