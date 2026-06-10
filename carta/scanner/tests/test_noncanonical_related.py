from pathlib import Path
from carta.scanner.scanner import check_noncanonical_related, check_broken_related, parse_frontmatter
from carta.search.graph import build_doc_index


def _repo(tmp_path: Path) -> Path:
    (tmp_path / "docs" / "hardware" / "vcu").mkdir(parents=True)
    (tmp_path / "docs" / "hardware" / "vcu" / "connector-map.md").write_text(
        "---\nid: connector-map\nrelated: []\n---\nbody\n")
    # one canonical entry, one bare-id (non-canonical), one broken
    (tmp_path / "docs" / "hardware" / "vcu" / "power.md").write_text(
        "---\nid: power\nrelated:\n"
        "  - docs/hardware/vcu/connector-map.md\n"   # canonical -> no finding
        "  - connector-map\n"                          # bare id -> noncanonical_related warning
        "  - nonexistent-doc\n"                        # broken -> broken_related error (not here)
        "---\nbody\n")
    return tmp_path


def test_flags_noncanonical_but_not_canonical_and_not_broken(tmp_path):
    """check_noncanonical_related only emits for resolvable-but-non-canonical entries."""
    repo = _repo(tmp_path)
    doc_index = build_doc_index(repo)
    power = repo / "docs" / "hardware" / "vcu" / "power.md"
    fm = parse_frontmatter(power)
    issues = check_noncanonical_related(power, fm, doc_index, repo)
    types_files = {(i["type"], i.get("related_file")) for i in issues}
    # bare id resolves but is non-canonical → warning with suggested canonical path
    assert ("noncanonical_related", "connector-map") in types_files
    nc = next(i for i in issues if i.get("related_file") == "connector-map")
    assert nc["suggested"] == "docs/hardware/vcu/connector-map.md"
    assert nc["severity"] == "warning"
    assert nc.get("resolves") is True
    # truly-broken entry must NOT produce a noncanonical_related finding
    assert ("noncanonical_related", "nonexistent-doc") not in types_files
    assert "nonexistent-doc" not in {i.get("related_file") for i in issues}
    # the canonical entry produces NO finding
    assert "docs/hardware/vcu/connector-map.md" not in {i.get("related_file") for i in issues}


def test_no_double_report_for_resolvable_bare_id(tmp_path):
    """A bare-id that resolves → exactly ONE finding total (noncanonical_related warning,
    zero broken_related errors)."""
    repo = _repo(tmp_path)
    doc_index = build_doc_index(repo)
    power = repo / "docs" / "hardware" / "vcu" / "power.md"
    fm = {"related": ["connector-map"]}  # bare id only

    nc_issues = check_noncanonical_related(power, fm, doc_index, repo)
    br_issues = check_broken_related(power, fm, repo, doc_index)

    all_issues = nc_issues + br_issues
    assert len(all_issues) == 1, f"Expected 1 finding, got {len(all_issues)}: {all_issues}"
    assert all_issues[0]["type"] == "noncanonical_related"
    assert all_issues[0]["severity"] == "warning"


def test_no_double_report_for_truly_broken_entry(tmp_path):
    """A truly-unresolvable entry → exactly ONE finding total (broken_related error,
    zero noncanonical_related)."""
    repo = _repo(tmp_path)
    doc_index = build_doc_index(repo)
    power = repo / "docs" / "hardware" / "vcu" / "power.md"
    fm = {"related": ["nonexistent-doc"]}  # broken only

    nc_issues = check_noncanonical_related(power, fm, doc_index, repo)
    br_issues = check_broken_related(power, fm, repo, doc_index)

    all_issues = nc_issues + br_issues
    assert len(all_issues) == 1, f"Expected 1 finding, got {len(all_issues)}: {all_issues}"
    assert all_issues[0]["type"] == "broken_related"
    assert all_issues[0]["severity"] == "error"
