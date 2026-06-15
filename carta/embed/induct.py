"""Sidecar stub generation for carta embed induction."""

import re
from pathlib import Path
from typing import Iterator, Optional

import yaml

from carta.config import collection_name, collection_for_doc_type


# Map parent directory names to doc_type values
_PATH_TYPE_MAP = {
    "datasheets": "datasheet",
    "manuals": "manual",
    "schematics": "schematic",
    "reference": "reference",
    "specs": "spec",
    "guides": "guide",
    "quirks": "quirk",
    "notes": "helpful-note",
}


def sidecar_path(file_path: Path, repo_root: Path) -> Path:
    """Return the canonical .carta/sidecars/ path for a source file's sidecar."""
    rel = file_path.relative_to(repo_root)
    return repo_root / ".carta" / "sidecars" / rel.with_suffix(".embed-meta.yaml")


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


def resolve_doc_type(file_path: Path, rel_path: Path) -> str:
    """Resolve a file's doc_type: explicit frontmatter wins, else parent-dir inference.

    Args:
        file_path: absolute path (read for frontmatter when markdown).
        rel_path: repo-relative path (parent names drive inference).
    """
    if file_path.suffix == ".md":
        from carta.scanner.scanner import parse_frontmatter
        try:
            # parse_frontmatter handles malformed YAML itself (returns None);
            # the except is fail-open for unreadable/binary/non-UTF-8 files,
            # which fall through to path inference like any other file.
            fm = parse_frontmatter(file_path) or {}
        except Exception:
            fm = {}
        fm_type = fm.get("doc_type")
        if isinstance(fm_type, str) and fm_type.strip():
            return fm_type.strip()
    return infer_doc_type(rel_path)


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
    doc_type = resolve_doc_type(file_path, rel_path)
    slug = slug_from_filename(file_path.name)
    file_type = "markdown" if file_path.suffix == ".md" else "pdf"

    stub = {
        "slug": slug,
        "doc_type": doc_type,
        "file_type": file_type,
        "current_path": str(rel_path),
        "status": "pending",
        "indexed_at": None,
        "chunk_count": None,
        "image_count": None,
        "image_chunks": None,
        "file_mtime": None,
        "collection": collection_for_doc_type(cfg, doc_type),
        "spec_summary": None,
        "notes": notes or "",
        # Lifecycle fields (Plan 999.1-02)
        "file_hash": None,
        "hash_algorithm": "sha256",
        "generation": 0,
        "last_hash_check_at": None,
        "version_history": [],
        # Vision extraction metadata (Plan 999.4-04)
        "vision": {
            "enabled": False,
            "pages_analyzed": 0,
            "extraction_summary": {
                "glm_ocr_pages": 0,
                "llava_pages": 0,
                "hybrid_pages": 0,
            },
            "page_details": [],  # List of per-page extraction info
        },
    }

    return stub


def write_sidecar(file_path: Path, stub: dict, repo_root: Path) -> Path:
    """Write sidecar YAML to .carta/sidecars/ mirroring repo structure. Returns the sidecar path."""
    path = sidecar_path(file_path, repo_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        yaml.dump(stub, f, default_flow_style=False, sort_keys=False)
    return path


def read_sidecar(sidecar_path: Path) -> Optional[dict]:
    """Read and parse a .embed-meta.yaml sidecar. Returns None on error."""
    try:
        with open(sidecar_path) as f:
            return yaml.safe_load(f)
    except (OSError, yaml.YAMLError):
        return None


def iter_canonical_sidecars(repo_root: Path) -> Iterator[tuple[Path, dict]]:
    """Yield ``(sidecar_path, data)`` for every canonically-located sidecar.

    Walks ``.carta/sidecars/`` and yields only sidecars that:
      - parse to a mapping (corrupt/non-dict files are skipped),
      - carry a ``current_path``, and
      - sit at the canonical location their ``current_path`` maps to — i.e.
        their path under ``sidecars/`` equals ``sidecar_path(...)`` would
        produce for that source.

    This skips misplaced/nested junk copies (e.g. an accidental
    ``.carta/sidecars/.worktrees/x/.carta/sidecars/foo.embed-meta.yaml`` whose
    ``current_path`` resolves to a real repo file it does not own), which would
    otherwise produce phantom duplicate entries in any sidecar scan.
    """
    sidecars_root = repo_root / ".carta" / "sidecars"
    if not sidecars_root.exists():
        return
    for sc_path in sidecars_root.rglob("*.embed-meta.yaml"):
        data = read_sidecar(sc_path)
        if not isinstance(data, dict):
            continue
        current_path = data.get("current_path")
        if not current_path:
            continue
        expected_rel = Path(current_path).with_suffix(".embed-meta.yaml")
        try:
            actual_rel = sc_path.relative_to(sidecars_root)
        except ValueError:
            continue
        if actual_rel != expected_rel:
            continue
        yield sc_path, data
