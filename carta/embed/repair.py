"""carta embed --repair: fix corpus-integrity issues found by integrity.scan.

Per affected file: delete ALL of its points from the _doc collection (any
generation, any legacy ID), then force re-embed through the fixed pipeline.
Files that no longer exist on disk, or whose extraction yields nothing, end up
purged + flagged rather than re-upserted. Stuck-stale sidecars get their status
corrected in place.

For the _visual collection (#38 part 2): orphaned visual points (source gone)
are purged, while count mismatches for live files are re-queued for the
`--visual` drain rather than deleted — ColPali embeddings can't be re-created
deterministically, but the visual point IDs are idempotent so a re-drain
overwrites in place.
"""
from __future__ import annotations

from pathlib import Path

from qdrant_client import QdrantClient
from qdrant_client.models import FieldCondition, Filter, MatchValue

from carta.config import collection_name
from carta.embed.induct import read_sidecar, sidecar_path
from carta.embed.integrity import scan_corpus_integrity
from carta.embed.pipeline import run_embed_file


def _delete_file_points(client, coll: str, rel_path: str) -> None:
    selector = Filter(
        must=[FieldCondition(key="file_path", match=MatchValue(value=rel_path))]
    )
    client.delete(collection_name=coll, points_selector=selector)


def run_repair(repo_root: Path, cfg: dict, verbose: bool = True) -> dict:
    """Detect and repair corpus-integrity issues. Returns a summary dict."""
    client = QdrantClient(url=cfg["qdrant_url"], timeout=30)
    report = scan_corpus_integrity(cfg, repo_root, client=client)
    coll = collection_name(cfg, "doc")

    if (not report["affected_files"] and not report["stuck_stale"]
            and not report.get("orphaned_visual_files")
            and not report.get("visual_count_mismatches")):
        if verbose:
            print("Corpus integrity: nothing to repair.", flush=True)
        return {"affected": 0, "repaired": 0, "purged_only": 0,
                "flagged": 0, "queued_visual": 0, "failed": 0, "stale_fixed": 0,
                "visual_purged": 0, "visual_requeued": 0}

    repaired = purged_only = flagged = queued_visual = failed = 0
    for rel in report["affected_files"]:
        src = repo_root / rel
        if verbose:
            print(f"  repairing {rel}...", flush=True)
        try:
            _delete_file_points(client, coll, rel)
        except Exception as e:
            print(f"  Warning: could not purge points for {rel} — {e}", flush=True)
        if not src.exists():
            purged_only += 1
            if verbose:
                print("    purged (file no longer on disk)", flush=True)
            continue
        try:
            result = run_embed_file(src, cfg, force=True)
            if result.get("chunks", 0) > 0:
                repaired += 1
            else:
                # Zero chunks: distinguish a genuine extraction failure from a
                # healthy two-pass-visual PDF whose pages were queued for the
                # --visual drainer (sidecar stays "embedded" in that case).
                sc = read_sidecar(sidecar_path(src, repo_root)) or {}
                # Both are terminal zero-chunk verdicts, not queued-visual PDFs.
                if sc.get("status") in ("extraction_failed", "no_text_content"):
                    flagged += 1
                else:
                    queued_visual += 1
                    if verbose:
                        print(
                            "    re-embedded; visual pages queued for pass-2 "
                            "(run `carta embed --visual` to drain)",
                            flush=True,
                        )
        except Exception as e:
            failed += 1
            print(f"  Error: re-embed failed for {rel} — {e}", flush=True)

    # Stuck-stale sidecars not otherwise affected: fix status in place.
    stale_fixed = 0
    affected = set(report["affected_files"])
    if report["stuck_stale"]:
        from carta.embed.pipeline import _update_sidecar
        for rel in report["stuck_stale"]:
            if rel in affected:
                continue  # re-embed above already rewrote it
            sc = sidecar_path(repo_root / rel, repo_root)
            if sc.exists():
                _update_sidecar(sc, {"status": "embedded", "stale_as_of": None})
                stale_fixed += 1

    # _visual collection repair (#38 part 2).
    visual_coll = collection_name(cfg, "visual")
    visual_purged = visual_requeued = 0

    # Orphaned visual points (source gone): safe to purge — nothing to re-create.
    for rel in report.get("orphaned_visual_files", []):
        try:
            _delete_file_points(client, visual_coll, rel)
            visual_purged += 1
            if verbose:
                print(f"  purged orphaned _visual points for {rel}", flush=True)
        except Exception as e:
            print(f"  Warning: could not purge _visual points for {rel} — {e}", flush=True)

    # Count mismatches (source present): re-queue for re-drain, NEVER delete.
    # ColPali embeddings aren't deterministically reproducible, but the visual
    # point IDs are idempotent, so `carta embed --visual` overwrites in place.
    if report.get("visual_count_mismatches"):
        from carta.embed.pipeline import _update_sidecar
        for rel in report["visual_count_mismatches"]:
            sc_path = sidecar_path(repo_root / rel, repo_root)
            sc = read_sidecar(sc_path) or {}
            pages = sorted(set((sc.get("visual_done") or [])
                               + (sc.get("visual_pending") or [])))
            if not pages or not sc_path.exists():
                continue
            _update_sidecar(sc_path, {"visual_pending": pages, "visual_done": []})
            visual_requeued += 1
            if verbose:
                print(
                    f"  re-queued {len(pages)} visual page(s) for {rel}; "
                    f"run `carta embed --visual` to re-drain",
                    flush=True,
                )

    summary = {
        "affected": len(report["affected_files"]),
        "repaired": repaired,
        "purged_only": purged_only,
        "flagged": flagged,
        "queued_visual": queued_visual,
        "failed": failed,
        "stale_fixed": stale_fixed,
        "visual_purged": visual_purged,
        "visual_requeued": visual_requeued,
    }
    if verbose:
        print(
            f"Repair complete: {repaired} re-embedded, {purged_only} purged, "
            f"{flagged} flagged extraction_failed, {queued_visual} queued for "
            f"visual pass, {failed} failed, "
            f"{stale_fixed} stale sidecar(s) corrected, "
            f"{visual_requeued} visual re-queued, {visual_purged} visual purged.",
            flush=True,
        )
    return summary
