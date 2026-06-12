"""carta embed --repair: fix corpus-integrity issues found by integrity.scan.

Per affected file: delete ALL of its points (any generation, any legacy ID),
then force re-embed through the fixed pipeline. Files that no longer exist on
disk, or whose extraction yields nothing, end up purged + flagged rather than
re-upserted. Stuck-stale sidecars get their status corrected in place.
"""
from __future__ import annotations

from pathlib import Path

from qdrant_client import QdrantClient
from qdrant_client.models import FieldCondition, Filter, MatchValue

from carta.config import collection_name
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

    if not report["affected_files"] and not report["stuck_stale"]:
        if verbose:
            print("Corpus integrity: nothing to repair.", flush=True)
        return {"affected": 0, "repaired": 0, "purged_only": 0,
                "flagged": 0, "failed": 0, "stale_fixed": 0}

    repaired = purged_only = flagged = failed = 0
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
                flagged += 1   # extraction_failed: purged + sidecar flagged
        except Exception as e:
            failed += 1
            print(f"  Error: re-embed failed for {rel} — {e}", flush=True)

    # Stuck-stale sidecars not otherwise affected: fix status in place.
    stale_fixed = 0
    affected = set(report["affected_files"])
    if report["stuck_stale"]:
        from carta.embed.induct import sidecar_path
        from carta.embed.pipeline import _update_sidecar
        for rel in report["stuck_stale"]:
            if rel in affected:
                continue  # re-embed above already rewrote it
            sc = sidecar_path(repo_root / rel, repo_root)
            if sc.exists():
                _update_sidecar(sc, {"status": "embedded", "stale_as_of": None})
                stale_fixed += 1

    summary = {
        "affected": len(report["affected_files"]),
        "repaired": repaired,
        "purged_only": purged_only,
        "flagged": flagged,
        "failed": failed,
        "stale_fixed": stale_fixed,
    }
    if verbose:
        print(
            f"Repair complete: {repaired} re-embedded, {purged_only} purged, "
            f"{flagged} flagged extraction_failed, {failed} failed, "
            f"{stale_fixed} stale sidecar(s) corrected.",
            flush=True,
        )
    return summary
