"""Out-of-band sync metadata for CLAUDE.md (pins, per-section hashes, last_synced).

Lives at .carta/sidecars/CLAUDE.md.sync.yaml — never written into CLAUDE.md itself,
which Claude Code injects verbatim into every session. Fails open: missing or corrupt
sidecar reads back as empty defaults so the scan simply treats every section as fresh."""
from __future__ import annotations

import hashlib
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
