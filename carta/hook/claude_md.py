"""CLAUDE.md ↔ docs sync: scan CLAUDE.md against the docs graph and surface the
sections the docs have superseded, so the in-session agent can draft corrections.

Detection only — never edits CLAUDE.md. Reuses run_stale_scan with CLAUDE.md fed in
as a ChangedDoc built directly here (bypassing the git collectors). Fails open."""
from __future__ import annotations

from pathlib import Path

from carta.embed.parse import sections_from_markdown
from carta.hook.stale_scan import ChangedDoc, run_stale_scan
from carta.hook import claude_md_sidecar as sc

CLAUDE_MD = "CLAUDE.md"


def _read_sections(repo_root: Path) -> list[dict]:
    text = (repo_root / CLAUDE_MD).read_text(encoding="utf-8-sig", errors="replace")
    sections, _ = sections_from_markdown(text)
    return sections


def group_findings_by_heading(findings: list, sections: list[dict]) -> list[dict]:
    """Roll chunk-level findings up to one entry per heading, with the full section
    text and the superseding excerpts (deduped by candidate path, order preserved)."""
    text_by_heading = {s["headings"][0]: s["text"] for s in sections}
    grouped: dict[str, dict] = {}
    for f in findings:
        entry = grouped.setdefault(f.section, {
            "heading": f.section,
            "section_text": text_by_heading.get(f.section, ""),
            "superseding": [],
            "_seen": set(),
        })
        if f.candidate_path in entry["_seen"]:
            continue
        entry["_seen"].add(f.candidate_path)
        entry["superseding"].append({
            "source": f.candidate_path,
            "excerpt": f.candidate_excerpt,
            "score": round(f.candidate_score, 4),
        })
    out = []
    for entry in grouped.values():
        entry.pop("_seen", None)
        out.append(entry)
    return out
