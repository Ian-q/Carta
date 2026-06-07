"""Helpers for the deferred visual-embedding queue stored in sidecar state.

A sidecar gains two lists of 1-indexed page numbers:
  visual_pending: pages awaiting glm-ocr text + ColPali visual embedding
  visual_done:    pages already visual-embedded
Per-page transitions make the visual pass resumable/interrupt-safe.
"""
from __future__ import annotations

VISUAL_PENDING_KEY = "visual_pending"
VISUAL_DONE_KEY = "visual_done"


def add_pending_pages(sidecar: dict, pages: list[int]) -> None:
    """Add page numbers to visual_pending (deduped, sorted), excluding already-done."""
    done = set(sidecar.get(VISUAL_DONE_KEY, []) or [])
    cur = set(sidecar.get(VISUAL_PENDING_KEY, []) or [])
    cur.update(p for p in pages if p not in done)
    sidecar[VISUAL_PENDING_KEY] = sorted(cur)


def move_to_done(sidecar: dict, page: int) -> None:
    """Move one page from visual_pending to visual_done (idempotent)."""
    sidecar[VISUAL_PENDING_KEY] = [
        p for p in sidecar.get(VISUAL_PENDING_KEY, []) or [] if p != page
    ]
    done = sidecar.get(VISUAL_DONE_KEY, []) or []
    if page not in done:
        done = sorted(done + [page])
    sidecar[VISUAL_DONE_KEY] = done


def queue_summary(sidecars: list[dict]) -> dict:
    """Count files with >=1 pending page and total pending pages."""
    files = 0
    pages = 0
    for sc in sidecars:
        p = sc.get(VISUAL_PENDING_KEY, []) or []
        if p:
            files += 1
            pages += len(p)
    return {"files": files, "pages": pages}


def format_summary_line(summary: dict) -> str:
    """Human nudge printed at the end of pass-1; empty string when nothing queued."""
    if not summary.get("pages"):
        return ""
    return (
        f"Visual queue: {summary['pages']} page(s) across {summary['files']} file(s) "
        f"await visual embedding. Run `carta embed --visual` to process them."
    )
