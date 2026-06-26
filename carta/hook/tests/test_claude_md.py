from carta.hook.stale_scan import StaleFinding
from carta.hook import claude_md


def test_group_rolls_chunks_up_to_heading_and_dedupes():
    sections = [
        {"page": 1, "text": "### Surface\n\nbig table ...", "headings": ["### Surface"]},
        {"page": 2, "text": "### Other\n\n...", "headings": ["### Other"]},
    ]
    findings = [
        StaleFinding("CLAUDE.md", "### Surface", "snip", "docs/a.md", 0.81, "A says new."),
        StaleFinding("CLAUDE.md", "### Surface", "snip", "docs/a.md", 0.79, "A dup."),
        StaleFinding("CLAUDE.md", "### Surface", "snip", "docs/b.md", 0.77, "B says new."),
    ]
    grouped = claude_md.group_findings_by_heading(findings, sections)

    assert len(grouped) == 1
    entry = grouped[0]
    assert entry["heading"] == "### Surface"
    assert entry["section_text"].startswith("### Surface")
    # deduped by candidate_path: docs/a.md once, docs/b.md once
    assert [s["source"] for s in entry["superseding"]] == ["docs/a.md", "docs/b.md"]
    assert entry["superseding"][0]["excerpt"] == "A says new."
