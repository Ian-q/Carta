"""Sidecar stub generation for carta embed induction."""

import re
from pathlib import Path
from typing import Optional

import yaml

from carta.config import collection_name


# Map parent directory names to doc_type values
_PATH_TYPE_MAP = {
    "datasheets": "datasheet",
    "manuals": "manual",
    "schematics": "schematic",
    "reference": "reference",
    "specs": "spec",
    "guides": "guide",
}


def slug_from_filename(filename: str) -> str:
    """Convert a filename (with extension) to a kebab-case slug."""
    stem = Path(filename).stem
    slug = re.sub(r"[^a-zA-Z0-9-]+", "-", stem)
    slug = re.sub(r"-+", "-", slug).strip("-")
    return slug.lower()


def infer_doc_type(file_path: Path) -> str:
    """Infer doc_type from the file's parent directory name."""
    for parent in file_path.parents:
        if parent.name in _PATH_TYPE_MAP:
            return _PATH_TYPE_MAP[parent.name]
    return "unknown"


def generate_sidecar_stub(
    file_path: Path,
    repo_root: Path,
    cfg: dict,
    notes: Optional[str] = None,
) -> dict:
    """Generate a sidecar stub dict for a file awaiting induction.

    Args:
        file_path: absolute path to the source file.
        repo_root: absolute path to the repo root.
        cfg: carta config dict (used to derive the collection name).
        notes: optional free-text notes.
    """
    rel_path = file_path.relative_to(repo_root)
    doc_type = infer_doc_type(rel_path)
    slug = slug_from_filename(file_path.name)

    stub = {
        "slug": slug,
        "doc_type": doc_type,
        "status": "pending",
        "indexed_at": None,
        "chunk_count": None,
        "collection": collection_name(cfg, "doc"),
        "spec_summary": None,
        "notes": notes or "",
    }

    return stub


def write_sidecar(file_path: Path, stub: dict) -> Path:
    """Write sidecar YAML next to the source file. Returns the sidecar path."""
    sidecar_path = file_path.parent / (file_path.stem + ".embed-meta.yaml")
    with open(sidecar_path, "w") as f:
        yaml.dump(stub, f, default_flow_style=False, sort_keys=False)
    return sidecar_path


def read_sidecar(sidecar_path: Path) -> Optional[dict]:
    """Read and parse a .embed-meta.yaml sidecar. Returns None on error."""
    try:
        with open(sidecar_path) as f:
            return yaml.safe_load(f)
    except (OSError, yaml.YAMLError):
        return None
