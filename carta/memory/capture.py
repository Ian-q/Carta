"""Note capture — write a curated note as a repo markdown file and embed it.

Notes are knowledge artifacts: content-named files in the user's docs tree
(docs/quirks/, docs/notes/ by default), plain markdown with generic frontmatter,
useful with or without Carta. See the repo footprint policy in
docs/superpowers/specs/2026-06-10-note-capture-design.md.
"""
from __future__ import annotations

import re
import sys
from datetime import date
from pathlib import Path

import yaml

from carta.config import NOTE_DOC_TYPES, collection_for_doc_type


def _slugify(text: str, max_words: int = 6) -> str:
    words = re.sub(r"[^a-zA-Z0-9\s-]", "", text).split()[:max_words]
    slug = "-".join(w.lower() for w in words)
    return slug or "note"


def _note_dir(cfg: dict, note_type: str) -> str:
    mem = cfg.get("memory", {})
    if note_type == "quirk":
        return mem.get("quirks_dir", "docs/quirks")
    return mem.get("notes_dir", "docs/notes")


def capture_note(cfg: dict, repo_root: Path, text: str, *,
                 note_type: str, title: str = "",
                 tags: list[str] | None = None,
                 about: str | None = None) -> dict:
    """Write a note file with frontmatter and embed it via the standard pipeline.

    Args:
        cfg: carta config dict.
        repo_root: absolute repo root path.
        text: the note body (stored verbatim).
        note_type: one of NOTE_DOC_TYPES.
        title: optional title; drives the filename slug and frontmatter title.
        tags: optional list of tags for the frontmatter.
        about: optional path of the file this note is about; recorded in the
            frontmatter as a repo-relative path. Warns (does not fail) if the
            target does not exist.

    Returns:
        {"path": <repo-relative str>, "collection": <name>, "chunks": <int>}

    Raises:
        ValueError: invalid note_type or empty text.
        RuntimeError: file written but embedding failed (file is kept).
    """
    if note_type not in NOTE_DOC_TYPES:
        raise ValueError(
            f"invalid note_type {note_type!r} — must be one of {', '.join(NOTE_DOC_TYPES)}"
        )
    if not text or not text.strip():
        raise ValueError("note text is empty")

    target_dir = Path(repo_root) / _note_dir(cfg, note_type)
    target_dir.mkdir(parents=True, exist_ok=True)

    slug = _slugify(title or text)
    today = date.today().isoformat()
    path = target_dir / f"{today}-{slug}.md"
    n = 2
    while path.exists():
        path = target_dir / f"{today}-{slug}-{n}.md"
        n += 1

    frontmatter = {
        "doc_type": note_type,
        "title": title or " ".join(text.split()[:8]),
        "created": today,
    }
    if tags:
        frontmatter["tags"] = list(tags)

    if about:
        about_p = Path(about)
        if about_p.is_absolute():
            try:
                about_p = about_p.relative_to(repo_root)
            except ValueError:
                pass  # outside the repo — record as given
        about_rel = about_p.as_posix()
        if not (Path(repo_root) / about_rel).exists():
            print(f"Warning: --about target does not exist: {about_rel} "
                  f"(note captured anyway)", file=sys.stderr)
        frontmatter["about"] = about_rel

    content = (
        "---\n"
        + yaml.dump(frontmatter, default_flow_style=False, sort_keys=False)
        + "---\n\n"
        + text.strip()
        + "\n"
    )
    path.write_text(content)
    rel = str(path.relative_to(repo_root))

    from carta.embed.pipeline import run_embed_file
    try:
        result = run_embed_file(path, cfg) or {}
    except Exception as e:
        raise RuntimeError(
            f"note written to {rel} but embedding failed: {e} — "
            f"run `carta embed` to index it"
        ) from e

    return {
        "path": rel,
        "collection": collection_for_doc_type(cfg, note_type),
        "chunks": result.get("chunks", 0),
    }
