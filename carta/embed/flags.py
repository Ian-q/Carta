"""Sidecar priority flags — demand-driven deep-scan requests.

Agents mark a document high-priority via `carta flag`; the visual drain
processes flagged files first and applies the deep extraction tier.
Design: docs/superpowers/specs/2026-07-29-demand-driven-deep-scan-design.md
"""
from datetime import datetime, timezone
from pathlib import Path

from carta.embed.induct import (
    generate_sidecar_stub,
    iter_canonical_sidecars,
    read_sidecar,
    sidecar_path,
    write_sidecar,
)
from carta.embed.visual_queue import VISUAL_DONE_KEY, VISUAL_PENDING_KEY

FLAG_FIELDS = ("priority", "deep_scan", "deep_scan_reason", "deep_scan_requested_at")


def _pdf_page_count(path: Path) -> int:
    import fitz  # lazy: keep module import cheap (test_import_cost)

    with fitz.open(path) as doc:
        return doc.page_count


def flag_file(repo_root: Path, cfg: dict, rel: Path, reason: str) -> dict:
    """Mark *rel* high-priority for deep scanning. Creates a sidecar stub if none exists.

    For PDFs, deep scan is a redo: visual_done resets and every page is queued so
    the drain picks the file up regardless of past classification (point IDs
    overwrite in place, so re-draining is idempotent).
    """
    src = repo_root / rel
    if not src.is_file():
        raise FileNotFoundError(f"not a file under the repo: {rel}")
    sc = read_sidecar(sidecar_path(src, repo_root)) or generate_sidecar_stub(
        src, repo_root, cfg
    )
    sc["priority"] = "high"
    sc["deep_scan"] = "requested"
    sc["deep_scan_reason"] = reason
    sc["deep_scan_requested_at"] = datetime.now(timezone.utc).isoformat()
    if src.suffix.lower() == ".pdf":
        sc[VISUAL_DONE_KEY] = []
        sc[VISUAL_PENDING_KEY] = list(range(1, _pdf_page_count(src) + 1))
    write_sidecar(src, sc, repo_root)
    return sc


def clear_flag(repo_root: Path, rel: Path) -> bool:
    """Remove flag fields from *rel*'s sidecar. Returns False when no sidecar exists."""
    src = repo_root / rel
    sc_path = sidecar_path(src, repo_root)
    sc = read_sidecar(sc_path)
    if sc is None:
        return False
    for f in FLAG_FIELDS:
        sc.pop(f, None)
    write_sidecar(src, sc, repo_root)
    return True


def list_flagged(repo_root: Path) -> list[dict]:
    """All sidecars with priority: high, oldest deep_scan_requested_at first."""
    rows = [
        sc
        for _, sc in iter_canonical_sidecars(repo_root)
        if sc.get("priority") == "high"
    ]
    rows.sort(key=lambda s: str(s.get("deep_scan_requested_at") or ""))
    return rows
