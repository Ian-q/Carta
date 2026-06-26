from carta.hook.stale_scan import StaleFinding
from carta.hook import claude_md
from carta.hook import claude_md_sidecar as sc


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


def _write_claude_md(repo_root, body):
    (repo_root / "CLAUDE.md").write_text(body, encoding="utf-8")


def _write_embed_sidecar(repo_root, name, indexed_at):
    p = repo_root / ".carta" / "sidecars" / "docs" / f"{name}.embed-meta.yaml"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"slug: {name}\nindexed_at: {indexed_at}\n", encoding="utf-8")


def test_scan_skips_pinned_and_flags_stale(tmp_path):
    _write_claude_md(tmp_path, (
        "## Constraints\n\nAlways use TDD.\n\n"
        "### Surface\n\nThe embed command does the old thing.\n"
    ))
    # pin the Constraints section
    sc.write_sync_sidecar(tmp_path, {
        "schema": 1, "last_synced": None,
        "sections": {"## Constraints": {"hash": "x", "pinned": True}},
    })

    seen = {}
    def search_fn(q):
        seen["q"] = q
        return [{"source": "docs/embed.md", "score": 0.9, "excerpt": "embed now does the new thing"}]
    judge_fn = lambda section_text, candidate: "Surface" in section_text or "embed" in section_text

    out = claude_md.scan_claude_md(tmp_path, {}, search_fn=search_fn, judge_fn=judge_fn)

    assert out["scanned"] is True
    assert out["skipped_pinned"] == 1
    headings = [f["heading"] for f in out["findings"]]
    assert "### Surface" in headings
    assert "## Constraints" not in headings  # pinned never scanned


def test_scan_returns_not_scanned_when_no_claude_md(tmp_path):
    out = claude_md.scan_claude_md(tmp_path, {})
    assert out["scanned"] is False
    assert out["findings"] == []


def test_scan_skips_unchanged_when_graph_unchanged(tmp_path):
    from carta.embed.parse import sections_from_markdown
    body = "### Surface\n\nstable text here\n"
    _write_claude_md(tmp_path, body)
    secs, _ = sections_from_markdown(body)
    section_text = secs[0]["text"]
    # A doc exists in the graph, but was embedded BEFORE last_synced → graph unchanged
    # since the last sync, so an unchanged section is safe to skip.
    _write_embed_sidecar(tmp_path, "old", "2026-06-20T00:00:00+00:00")
    sc.write_sync_sidecar(tmp_path, {
        "schema": 1,
        "last_synced": "2026-06-25T00:00:00+00:00",
        "sections": {"### Surface": {"hash": sc.section_hash(section_text), "pinned": False}},
    })

    called = {"n": 0}
    def search_fn(q):
        called["n"] += 1
        return []
    out = claude_md.scan_claude_md(tmp_path, {}, search_fn=search_fn, judge_fn=lambda *a: False)
    assert out["skipped_unchanged"] == 1
    assert called["n"] == 0  # unchanged + graph-unchanged → never searched


def test_record_sync_hashes_sections_and_preserves_pins(tmp_path):
    _write_claude_md(tmp_path, "## Constraints\n\nAlways TDD.\n\n### Surface\n\nNew text.\n")
    sc.write_sync_sidecar(tmp_path, {
        "schema": 1, "last_synced": None,
        "sections": {"## Constraints": {"hash": "old", "pinned": True}},
    })

    written = claude_md.record_sync(tmp_path, "2026-06-26T09:00:00+00:00")

    assert written["last_synced"] == "2026-06-26T09:00:00+00:00"
    assert written["sections"]["## Constraints"]["pinned"] is True   # pin preserved
    assert written["sections"]["## Constraints"]["hash"] != "old"    # re-hashed
    assert "### Surface" in written["sections"]                      # new section recorded
    # round-trips through disk
    assert sc.load_sync_sidecar(tmp_path)["sections"]["### Surface"]["hash"]


def test_skill_file_exists_and_names_the_commands():
    from pathlib import Path
    p = Path(__file__).resolve().parents[2] / "skills" / "claude-md-sync" / "SKILL.md"
    assert p.exists(), "claude-md-sync SKILL.md missing"
    body = p.read_text(encoding="utf-8")
    assert "carta claude-md check" in body
    assert "carta claude-md record" in body
    assert "name: claude-md-sync" in body
