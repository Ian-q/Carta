"""Corpus-integrity scanning: detect point-ID collisions, empty-text points,
sidecar/Qdrant count mismatches, and stuck-stale sidecars.

Read-only — used by `carta doctor` (report) and `carta embed --repair`
(which re-embeds/purges what this module finds).
"""
from __future__ import annotations

from collections import defaultdict
from pathlib import Path

from qdrant_client import QdrantClient

from carta.config import collection_name, collection_for_doc_type
from carta.embed.lifecycle import compute_file_hash
from carta.embed.induct import iter_canonical_sidecars
from carta.embed.enrichment import enrichment_is_stale


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
                              (informational; path-based IDs make this harmless)
        empty_files:          [file_path] — every point for the file has empty text
        partial_empty_files:  {file_path: n_empty} — some (not all) points empty
        count_mismatches:     {file_path: {"sidecar": n, "qdrant": n}}
        stuck_stale:          [rel_path] — status="stale" but file hash matches disk
        affected_files:       sorted union of _doc files needing re-embed/purge
                              (empty + partial + count mismatches; slug collisions
                               and stuck_stale excluded)
        visual_count_mismatches: {file_path: {"sidecar": n_done, "qdrant": n}} —
                              sidecar visual_done count vs _visual point count
        orphaned_visual_files: [file_path] — _visual points whose source is gone
        stale_enrichments:    [file_path] — source files whose enrichment_source_hash
                              no longer matches their current file_hash
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

    # _visual collection tallies: point count per file_path (#38 part 2).
    visual_coll = collection_name(cfg, "visual")
    visual_per_file_counts: dict[str, int] = defaultdict(int)
    if client.collection_exists(visual_coll):
        for payload in _scroll_all(client, visual_coll):
            visual_per_file_counts[payload.get("file_path", "")] += 1
    # Orphaned visual points: _visual points for a source no longer on disk.
    orphaned_visual_files = sorted(
        fp for fp in visual_per_file_counts
        if fp and not (repo_root / fp).exists()
    )

    # _notes collection tallies (CA-15): note doc_types (quirk/bug-note/helpful-note)
    # route to {project}_notes via collection_for_doc_type, NOT _doc. Counting their
    # sidecar chunk_count against _doc made every embedded note a false count-mismatch.
    notes_coll = collection_name(cfg, "notes")
    notes_per_file_counts: dict[str, int] = defaultdict(int)
    if notes_coll != coll and client.collection_exists(notes_coll):
        for payload in _scroll_all(client, notes_coll):
            notes_per_file_counts[payload.get("file_path", "")] += 1

    # Sidecar-side checks. iter_canonical_sidecars skips corrupt/non-dict
    # sidecars, those without a current_path, and misplaced/nested junk copies
    # (e.g. an accidental .carta/sidecars/.worktrees/.../.carta/sidecars/... tree)
    # whose current_path resolves to a real file they do not own (#40).
    count_mismatches: dict[str, dict] = {}
    visual_count_mismatches: dict[str, dict] = {}
    stuck_stale: list[str] = []
    stale_enrichments: list[str] = []

    for _sc_path, sc in iter_canonical_sidecars(repo_root):
        rel = sc["current_path"]

        status = sc.get("status")
        # Route the count comparison to the collection this sidecar's doc_type
        # actually lands in (notes -> _notes, everything else -> _doc), so notes
        # are not falsely flagged as "sidecar N vs qdrant 0" (CA-15).
        routed_counts = (
            notes_per_file_counts
            if collection_for_doc_type(cfg, sc.get("doc_type", "unknown")) == notes_coll
            else per_file_counts
        )
        # Missing entry means ZERO surviving points — a fully-lost file
        # (e.g. every chunk shadowed by a legacy ID collision) is the
        # strongest mismatch of all, not an exemption.
        qdrant_count = routed_counts.get(rel, 0)
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

        # Visual count mismatch: sidecar visual_done count vs _visual point
        # count. Files no longer on disk surface as orphaned_visual_files (#38).
        visual_done_count = len(sc.get("visual_done") or [])
        visual_qdrant = visual_per_file_counts.get(rel, 0)
        if (repo_root / rel).exists() and visual_done_count != visual_qdrant:
            visual_count_mismatches[rel] = {
                "sidecar": visual_done_count,
                "qdrant": visual_qdrant,
            }

        # Stale enrichment: the source's recorded enrichment_source_hash no
        # longer matches its current file_hash (the source changed since the
        # extraction doc was written).
        if enrichment_is_stale(sc):
            stale_enrichments.append(rel)

    # affected_files: union of everything needing re-embed or purge.
    # Slug collisions are intentionally EXCLUDED — with path-based point IDs
    # same-slug files coexist safely; genuine legacy-collision damage (chunks
    # shadowed by an old slug-keyed ID) still surfaces as a count_mismatch (#40).
    # (stuck_stale also excluded — repair marks them pending without full re-embed.)
    affected: set[str] = set(empty_files) | set(partial_empty_files) | set(count_mismatches)

    return {
        "slug_collisions": slug_collisions,
        "empty_files": empty_files,
        "partial_empty_files": partial_empty_files,
        "count_mismatches": count_mismatches,
        "stuck_stale": sorted(stuck_stale),
        "affected_files": sorted(affected),
        "visual_count_mismatches": visual_count_mismatches,
        "orphaned_visual_files": orphaned_visual_files,
        "stale_enrichments": sorted(stale_enrichments),
    }
