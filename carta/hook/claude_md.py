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


def scan_claude_md(repo_root: Path, cfg: dict, *, search_fn=None, judge_fn=None) -> dict:
    """Scan CLAUDE.md against the docs graph and return superseded sections.

    Skips pinned sections, and sections whose text is unchanged AND whose graph is
    unchanged since last_synced. Builds a ChangedDoc from the remaining sections and
    reuses run_stale_scan. Detection only; never edits CLAUDE.md."""
    if not (repo_root / CLAUDE_MD).exists():
        return {"scanned": False, "reason": "no CLAUDE.md", "findings": []}

    sections = _read_sections(repo_root)
    sidecar = sc.load_sync_sidecar(repo_root)
    meta = sidecar.get("sections", {})
    graph_changed = sc.graph_changed_since(repo_root, sidecar.get("last_synced"))

    to_scan: list[dict] = []
    skipped_pinned = 0
    skipped_unchanged = 0
    for s in sections:
        heading = s["headings"][0]
        entry = meta.get(heading, {})
        if entry.get("pinned"):
            skipped_pinned += 1
            continue
        if not graph_changed and entry.get("hash") == sc.section_hash(s["text"]):
            skipped_unchanged += 1
            continue
        to_scan.append(s)

    findings: list = []
    judge_calls = 0
    if to_scan:
        scan_text = "\n\n".join(s["text"] for s in to_scan)
        result = run_stale_scan(
            repo_root, cfg, [ChangedDoc(path=CLAUDE_MD, text=scan_text)],
            search_fn=search_fn, judge_fn=judge_fn,
        )
        findings = result.findings
        judge_calls = result.judge_calls

    return {
        "scanned": True,
        "findings": group_findings_by_heading(findings, sections),
        "skipped_pinned": skipped_pinned,
        "skipped_unchanged": skipped_unchanged,
        "judge_calls": judge_calls,
    }


def record_sync(repo_root: Path, now_iso: str) -> dict:
    """Re-hash all current CLAUDE.md sections and stamp last_synced. Preserves pins;
    drops sections that no longer exist. Call after approved edits are applied."""
    sidecar = sc.load_sync_sidecar(repo_root)
    prev = sidecar.get("sections", {})
    new_sections: dict[str, dict] = {}
    for s in _read_sections(repo_root):
        heading = s["headings"][0]
        new_sections[heading] = {
            "hash": sc.section_hash(s["text"]),
            "pinned": bool(prev.get(heading, {}).get("pinned", False)),
            "last_reviewed": now_iso,
        }
    sidecar["schema"] = 1
    sidecar["sections"] = new_sections
    sidecar["last_synced"] = now_iso
    sc.write_sync_sidecar(repo_root, sidecar)
    return sidecar
