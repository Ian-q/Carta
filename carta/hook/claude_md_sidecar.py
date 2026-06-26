"""Out-of-band sync metadata for CLAUDE.md (pins, per-section hashes, last_synced).

Lives at .carta/sidecars/CLAUDE.md.sync.yaml — never written into CLAUDE.md itself,
which Claude Code injects verbatim into every session. Fails open: missing or corrupt
sidecar reads back as empty defaults so the scan simply treats every section as fresh."""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path

import yaml

SYNC_SIDECAR_REL = Path(".carta") / "sidecars" / "CLAUDE.md.sync.yaml"


def sync_sidecar_path(repo_root: Path) -> Path:
    return repo_root / SYNC_SIDECAR_REL


def section_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_sync_sidecar(repo_root: Path) -> dict:
    """Read the sync sidecar; always return a dict with schema/last_synced/sections."""
    path = sync_sidecar_path(repo_root)
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        data = None
    if not isinstance(data, dict):
        return {"schema": 1, "last_synced": None, "sections": {}}
    data.setdefault("schema", 1)
    data.setdefault("last_synced", None)
    if not isinstance(data.get("sections"), dict):
        data["sections"] = {}
    return data


def write_sync_sidecar(repo_root: Path, data: dict) -> None:
    path = sync_sidecar_path(repo_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def _parse_iso(ts) -> datetime | None:
    """Tolerant ISO-8601 parse (handles a trailing 'Z', or a datetime already parsed by
    PyYAML); naive → UTC. None on failure."""
    if isinstance(ts, datetime):
        return ts.replace(tzinfo=timezone.utc) if ts.tzinfo is None else ts
    if not isinstance(ts, str):
        return None
    s = ts.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def latest_embed_time(repo_root: Path) -> datetime | None:
    """Newest indexed_at across all embed sidecars, or None if none/unreadable."""
    sidecar_dir = repo_root / ".carta" / "sidecars"
    latest: datetime | None = None
    try:
        paths = list(sidecar_dir.rglob("*.embed-meta.yaml"))
    except OSError:
        return None
    for p in paths:
        try:
            meta = yaml.safe_load(p.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            continue
        if not isinstance(meta, dict):
            continue
        dt = _parse_iso(meta.get("indexed_at"))
        if dt is not None and (latest is None or dt > latest):
            latest = dt
    return latest


def graph_changed_since(repo_root: Path, last_synced: str | None) -> bool:
    """True if a doc may have been embedded after last_synced — so unchanged CLAUDE.md
    sections must still be re-scanned. Fails toward True (re-scan) on any missing data."""
    base = _parse_iso(last_synced)
    if base is None:
        return True
    latest = latest_embed_time(repo_root)
    if latest is None:
        return True
    return latest > base
