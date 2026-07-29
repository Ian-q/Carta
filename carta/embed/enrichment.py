"""Enrichment docs — Claude/human-authored structured extractions of visual sources.

Canonical location is per-project config (embed.enrichment.repo_visible);
the mechanism is Carta's. Staleness is tracked against the SOURCE file hash.
Design: docs/superpowers/specs/2026-07-29-demand-driven-deep-scan-design.md
"""
from pathlib import Path
from typing import Optional

from carta.embed.induct import read_sidecar, sidecar_path, write_sidecar

_COMPANIONS = (".carta", "companions")


def _enrichment_cfg(cfg: dict) -> dict:
    return (cfg.get("embed", {}) or {}).get("enrichment", {}) or {}


def enrichment_suffix(cfg: dict) -> str:
    return _enrichment_cfg(cfg).get("suffix", ".extraction.md")


def enrichment_rel_path(source_rel: Path, cfg: dict) -> Path:
    """Canonical repo-relative enrichment path for *source_rel*.

    The full source NAME is appended (a.pdf -> a.pdf.extraction.md) so two
    sources differing only by extension cannot collide — same rule as
    tabular.companion_rel_path.
    """
    name = source_rel.name + enrichment_suffix(cfg)
    if _enrichment_cfg(cfg).get("repo_visible", False):
        return source_rel.parent / name
    return Path(*_COMPANIONS) / source_rel.parent / name


def source_rel_for_enrichment(path: Path, cfg: dict) -> Optional[Path]:
    """Inverse of enrichment_rel_path, or None when *path* is not an enrichment."""
    suffix = enrichment_suffix(cfg)
    if not path.name.endswith(suffix):
        return None
    src_name = path.name[: -len(suffix)]
    parent = path.parent
    if parent.parts[:2] == _COMPANIONS:
        parent = Path(*parent.parts[2:]) if len(parent.parts) > 2 else Path(".")
    return parent / src_name


def record_enrichment(repo_root: Path, source_rel: Path, enrichment_rel: Path) -> None:
    """Stamp the SOURCE sidecar: enrichment ingested at the current source hash."""
    src = repo_root / source_rel
    sc_path = sidecar_path(src, repo_root)
    sc = read_sidecar(sc_path) or {}
    sc["enrichment_path"] = str(enrichment_rel)
    sc["enrichment_source_hash"] = sc.get("file_hash", "")
    if sc.get("deep_scan") == "requested":
        sc["deep_scan"] = "done"
    write_sidecar(src, sc, repo_root)


def enrichment_is_stale(sc: dict) -> bool:
    rec = sc.get("enrichment_source_hash")
    return bool(rec) and rec != sc.get("file_hash", "")
