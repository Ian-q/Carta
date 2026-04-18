"""Tests for carta.scanner.scanner."""

import json
import textwrap
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from carta.scanner.scanner import parse_frontmatter


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def write_doc(tmp_path, name, content):
    p = tmp_path / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(content))
    return p


def _make_tree(tmp_path, files):
    """Create files in tmp_path. files is list of relative path strings."""
    for f in files:
        p = tmp_path / f
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("# doc\n")
    return tmp_path


def _minimal_cfg(tmp_path, excluded_paths=None, stale_threshold_days=30):
    """Return a minimal config dict suitable for tests (no YAML file required)."""
    return {
        "docs_root": "docs/",
        "excluded_paths": excluded_paths if excluded_paths is not None else [],
        "stale_threshold_days": stale_threshold_days,
    }


# ---------------------------------------------------------------------------
# parse_frontmatter
# ---------------------------------------------------------------------------

def test_parse_frontmatter_with_valid_frontmatter(tmp_path):
    doc = write_doc(tmp_path, "test.md", """\
        ---
        related:
          - docs/CAN/TOPOLOGY.md
        last_reviewed: 2026-03-18
        ---
        # Doc content
        """)
    result = parse_frontmatter(doc)
    assert result == {
        "related": ["docs/CAN/TOPOLOGY.md"],
        "last_reviewed": "2026-03-18",
    }


def test_parse_frontmatter_no_frontmatter(tmp_path):
    doc = write_doc(tmp_path, "test.md", "# Just a doc\nNo frontmatter here.\n")
    assert parse_frontmatter(doc) is None


def test_parse_frontmatter_empty_related(tmp_path):
    doc = write_doc(tmp_path, "test.md", """\
        ---
        related: []
        last_reviewed: 2026-01-01
        ---
        """)
    result = parse_frontmatter(doc)
    assert result["related"] == []


def test_parse_frontmatter_missing_last_reviewed(tmp_path):
    doc = write_doc(tmp_path, "test.md", """\
        ---
        related:
          - docs/CAN/TOPOLOGY.md
        ---
        """)
    result = parse_frontmatter(doc)
    assert result is not None
    assert "last_reviewed" not in result


def test_parse_frontmatter_date_normalized_to_string(tmp_path):
    """YAML parses unquoted dates as date objects — scanner must normalize to str."""
    doc = write_doc(tmp_path, "test.md", """\
        ---
        last_reviewed: 2026-03-18
        ---
        """)
    result = parse_frontmatter(doc)
    assert isinstance(result["last_reviewed"], str)
    assert result["last_reviewed"] == "2026-03-18"


# ---------------------------------------------------------------------------
# is_excluded
# ---------------------------------------------------------------------------

from carta.scanner.scanner import is_excluded


def test_is_excluded_direct_match(tmp_path):
    cfg = {"excluded_paths": ["CLAUDE.md", ".cursor/"]}
    assert is_excluded(tmp_path / "CLAUDE.md", cfg, tmp_path) is True
    assert is_excluded(tmp_path / "docs" / "ARCHITECTURE.md", cfg, tmp_path) is False


def test_is_excluded_glob_pattern(tmp_path):
    cfg = {"excluded_paths": ["perplexity-*.md"]}
    assert is_excluded(tmp_path / "perplexity-advice.md", cfg, tmp_path) is True
    assert is_excluded(tmp_path / "advice.md", cfg, tmp_path) is False


def test_is_excluded_prefix_dir(tmp_path):
    cfg = {"excluded_paths": [".cursor/"]}
    assert is_excluded(tmp_path / ".cursor" / "rules.md", cfg, tmp_path) is True


# ---------------------------------------------------------------------------
# check_homeless_docs / check_nested_docs_folders
# ---------------------------------------------------------------------------

from carta.scanner.scanner import check_homeless_docs, check_nested_docs_folders


def test_homeless_doc_detected(tmp_path):
    _make_tree(tmp_path, [
        "docs/api/ENDPOINTS.md",
        "draft-notes.md",           # excluded by pattern — should NOT appear
        "stray-notes.md",           # homeless and not excluded — should appear
        "tools/scripts/README.md",  # OK — is README
    ])
    cfg = _minimal_cfg(tmp_path, excluded_paths=["draft-*.md"])
    issues = check_homeless_docs(tmp_path, cfg)
    doc_paths = [i["doc"] for i in issues]
    assert "stray-notes.md" in doc_paths
    assert "draft-notes.md" not in doc_paths
    assert "tools/scripts/README.md" not in doc_paths


def test_anchor_doc_exempt_from_homeless(tmp_path):
    """anchor_doc and anchor_docs entries must not be flagged as homeless_doc."""
    _make_tree(tmp_path, [
        "docs/api/ENDPOINTS.md",
        "CLAUDE.md",       # anchor_doc — should NOT be flagged
        "AGENTS.md",       # anchor_docs entry — should NOT be flagged
        "stray-notes.md",  # genuinely homeless — should be flagged
    ])
    cfg = _minimal_cfg(tmp_path)
    cfg["anchor_doc"] = "CLAUDE.md"
    cfg["anchor_docs"] = ["AGENTS.md"]
    issues = check_homeless_docs(tmp_path, cfg)
    doc_paths = [i["doc"] for i in issues]
    assert "CLAUDE.md" not in doc_paths
    assert "AGENTS.md" not in doc_paths
    assert "stray-notes.md" in doc_paths


def test_anchor_doc_path_normalized_to_basename(tmp_path):
    """anchor_doc may be a relative path; only the basename is used for exemption."""
    _make_tree(tmp_path, ["docs/x.md", "CLAUDE.md"])
    cfg = _minimal_cfg(tmp_path)
    cfg["anchor_doc"] = "some/prefix/../CLAUDE.md"
    issues = check_homeless_docs(tmp_path, cfg)
    assert not any(i["doc"] == "CLAUDE.md" for i in issues)


def test_default_root_whitelist_skips_common_root_docs(tmp_path):
    """Built-in whitelist exempts standard root markdown without extra config."""
    _make_tree(tmp_path, [
        "docs/x.md",
        "CHANGELOG.md",
        "CONTRIBUTING.md",
        "LICENSE.md",
    ])
    cfg = _minimal_cfg(tmp_path)
    issues = check_homeless_docs(tmp_path, cfg)
    doc_paths = [i["doc"] for i in issues]
    assert "CHANGELOG.md" not in doc_paths
    assert "CONTRIBUTING.md" not in doc_paths
    assert "LICENSE.md" not in doc_paths


def test_nested_docs_folder_detected(tmp_path):
    _make_tree(tmp_path, [
        "docs/ARCHITECTURE.md",
        "hardware/vcu/docs/power.md",  # nested /docs/ — flagged
    ])
    issues = check_nested_docs_folders(tmp_path)
    paths = [i["doc"] for i in issues]
    assert any("hardware/vcu/docs" in p for p in paths)
    assert not any(p == "docs" for p in paths)


# ---------------------------------------------------------------------------
# check_broken_related / check_missing_frontmatter / check_stale_last_reviewed
# ---------------------------------------------------------------------------

from carta.scanner.scanner import (
    check_broken_related,
    check_missing_frontmatter,
    check_prototype_doc,
    check_stale_last_reviewed,
)


def test_check_prototype_doc_returns_issue(tmp_path):
    doc = tmp_path / "docs" / "design.md"
    doc.parent.mkdir(parents=True, exist_ok=True)
    doc.write_text("# Design\n")
    fm = {"status": "prototype", "related": []}
    issue = check_prototype_doc(doc, fm, tmp_path)
    assert issue is not None
    assert issue["type"] == "prototype_doc"
    assert issue["severity"] == "info"
    assert "design.md" in issue["doc"]


def test_check_prototype_doc_returns_none_for_non_prototype(tmp_path):
    doc = tmp_path / "docs" / "design.md"
    doc.parent.mkdir(parents=True, exist_ok=True)
    doc.write_text("# Design\n")
    assert check_prototype_doc(doc, {"status": "active"}, tmp_path) is None
    assert check_prototype_doc(doc, {"related": []}, tmp_path) is None


def test_broken_related_detected(tmp_path):
    _make_tree(tmp_path, ["docs/PCB/DESIGN_CHECKLIST.md", "CLAUDE.md"])
    fm = {"related": ["CLAUDE.md", "docs/NONEXISTENT.md"], "last_reviewed": "2026-03-01"}
    doc = tmp_path / "docs/PCB/DESIGN_CHECKLIST.md"
    issues = check_broken_related(doc, fm, tmp_path)
    assert len(issues) == 1
    assert "docs/NONEXISTENT.md" in issues[0]["detail"]


def test_broken_related_all_valid(tmp_path):
    _make_tree(tmp_path, ["docs/PCB/DESIGN_CHECKLIST.md", "CLAUDE.md"])
    fm = {"related": ["CLAUDE.md"], "last_reviewed": "2026-03-01"}
    doc = tmp_path / "docs/PCB/DESIGN_CHECKLIST.md"
    assert check_broken_related(doc, fm, tmp_path) == []


def test_missing_frontmatter_flagged(tmp_path):
    doc = tmp_path / "docs/ARCHITECTURE.md"
    doc.parent.mkdir(parents=True, exist_ok=True)
    doc.write_text("# No frontmatter\n")
    issue = check_missing_frontmatter(doc, None)
    assert issue is not None
    assert issue["type"] == "missing_frontmatter"


def test_missing_frontmatter_not_flagged_when_present(tmp_path):
    doc = tmp_path / "docs/ARCHITECTURE.md"
    doc.parent.mkdir(parents=True, exist_ok=True)
    doc.write_text("---\nrelated: []\nlast_reviewed: 2026-03-01\n---\n# Content\n")
    fm = {"related": [], "last_reviewed": "2026-03-01"}
    assert check_missing_frontmatter(doc, fm) is None


def test_stale_last_reviewed_flagged(tmp_path):
    doc = tmp_path / "docs/ARCHITECTURE.md"
    doc.parent.mkdir(parents=True, exist_ok=True)
    doc.write_text("# doc\n")
    fm = {"last_reviewed": "2026-01-01"}
    reference = date(2026, 3, 18)
    issues = check_stale_last_reviewed(doc, fm, threshold_days=30, reference_date=reference)
    assert len(issues) == 1


def test_stale_last_reviewed_ok(tmp_path):
    doc = tmp_path / "docs/ARCHITECTURE.md"
    doc.parent.mkdir(parents=True, exist_ok=True)
    doc.write_text("# doc\n")
    fm = {"last_reviewed": "2026-03-10"}
    reference = date(2026, 3, 18)
    assert check_stale_last_reviewed(doc, fm, 30, reference) == []


def test_stale_last_reviewed_no_field(tmp_path):
    doc = tmp_path / "docs/ARCHITECTURE.md"
    doc.parent.mkdir(parents=True, exist_ok=True)
    doc.write_text("# doc\n")
    fm = {"related": []}
    assert check_stale_last_reviewed(doc, fm, 30, date(2026, 3, 18)) == []


# ---------------------------------------------------------------------------
# get_file_last_commit_date / check_related_drift / build_inverted_index / check_orphaned_doc
# ---------------------------------------------------------------------------

from carta.scanner.scanner import (
    get_file_last_commit_date,
    check_related_drift,
    build_inverted_index,
    check_orphaned_doc,
)


def test_get_file_last_commit_date_success(tmp_path):
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(
            returncode=0, stdout="2026-03-15\n", stderr=""
        )
        result = get_file_last_commit_date(tmp_path, Path("CLAUDE.md"))
    assert result == date(2026, 3, 15)


def test_get_file_last_commit_date_untracked(tmp_path):
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        result = get_file_last_commit_date(tmp_path, Path("new-file.md"))
    assert result is None


def test_related_drift_detected(tmp_path):
    _make_tree(tmp_path, ["docs/PCB/DESIGN_CHECKLIST.md", "CLAUDE.md"])
    doc = tmp_path / "docs/PCB/DESIGN_CHECKLIST.md"
    fm = {"related": ["CLAUDE.md"], "last_reviewed": "2026-02-01"}

    def mock_commit_date(repo_root, file_path):
        if file_path == Path("CLAUDE.md"):
            return date(2026, 3, 15)
        return date(2026, 1, 1)

    with patch("carta.scanner.scanner.get_file_last_commit_date", side_effect=mock_commit_date):
        issues = check_related_drift(doc, fm, tmp_path)
    assert len(issues) == 1
    assert "CLAUDE.md" in issues[0]["detail"]


def test_related_drift_not_detected_when_current(tmp_path):
    _make_tree(tmp_path, ["docs/PCB/DESIGN_CHECKLIST.md", "CLAUDE.md"])
    doc = tmp_path / "docs/PCB/DESIGN_CHECKLIST.md"
    fm = {"related": ["CLAUDE.md"], "last_reviewed": "2026-03-17"}

    with patch("carta.scanner.scanner.get_file_last_commit_date", return_value=date(2026, 3, 10)):
        issues = check_related_drift(doc, fm, tmp_path)
    assert issues == []


def test_build_inverted_index():
    docs = {
        "docs/PCB/DESIGN_CHECKLIST.md": {"related": ["CLAUDE.md", "docs/CAN/TOPOLOGY.md"]},
        "docs/CAN/TOPOLOGY.md": {"related": ["docs/ARCHITECTURE.md"]},
        "docs/ARCHITECTURE.md": None,
    }
    idx = build_inverted_index(docs)
    assert "CLAUDE.md" in idx
    assert "docs/PCB/DESIGN_CHECKLIST.md" in idx["CLAUDE.md"]
    assert "docs/PCB/DESIGN_CHECKLIST.md" in idx["docs/CAN/TOPOLOGY.md"]
    assert "docs/ARCHITECTURE.md" in idx


def test_orphaned_doc_detected(tmp_path):
    _make_tree(tmp_path, ["docs/hardware/VCU-Power-Consumption.md"])
    doc = tmp_path / "docs/hardware/VCU-Power-Consumption.md"
    idx = {}
    issue = check_orphaned_doc(doc, {}, idx, tmp_path)
    assert issue is not None
    assert issue["type"] == "orphaned_doc"


def test_orphaned_doc_not_flagged_if_referenced(tmp_path):
    _make_tree(tmp_path, ["docs/hardware/VCU-Power-Consumption.md"])
    doc = tmp_path / "docs/hardware/VCU-Power-Consumption.md"
    idx = {"docs/hardware/VCU-Power-Consumption.md": {"docs/PCB/DESIGN_CHECKLIST.md"}}
    assert check_orphaned_doc(doc, {}, idx, tmp_path) is None


def test_orphaned_doc_not_flagged_if_has_siblings(tmp_path):
    _make_tree(tmp_path, [
        "docs/hardware/VCU-Power-Consumption.md",
        "docs/hardware/VCU-Schematic.md",
    ])
    doc = tmp_path / "docs/hardware/VCU-Power-Consumption.md"
    idx = {}
    assert check_orphaned_doc(doc, {}, idx, tmp_path) is None


# ---------------------------------------------------------------------------
# get_current_git_hash / get_changed_since_hash
# ---------------------------------------------------------------------------

from carta.scanner.scanner import get_current_git_hash, get_changed_since_hash


def test_get_current_git_hash(tmp_path):
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="abc123def456\n", stderr="")
        result = get_current_git_hash(tmp_path)
    assert result == "abc123def456"


def test_get_changed_since_hash_filters_excluded(tmp_path):
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="docs/CAN/TOPOLOGY.md\nperplexity-advice.md\ntools/doc_audit/config.yaml\n",
            stderr=""
        )
        cfg = _minimal_cfg(tmp_path, excluded_paths=["perplexity-*.md", "tools/doc_audit/"])
        result = get_changed_since_hash(tmp_path, "abc123", cfg)
    assert "docs/CAN/TOPOLOGY.md" in result
    assert "perplexity-advice.md" not in result


# ---------------------------------------------------------------------------
# run_scan integration
# ---------------------------------------------------------------------------

from carta.scanner.scanner import run_scan


def test_run_scan_produces_valid_json(tmp_path):
    """Integration test: run_scan on a minimal repo tree returns valid JSON structure."""
    (tmp_path / "docs" / "CAN").mkdir(parents=True)
    (tmp_path / "docs" / "CAN" / "TOPOLOGY.md").write_text(
        "---\nrelated: []\nlast_reviewed: 2026-03-18\n---\n# Topology\n"
    )
    (tmp_path / "stray.md").write_text("# stray\n")

    cfg = _minimal_cfg(tmp_path, excluded_paths=[], stale_threshold_days=30)
    output_path = tmp_path / "scan-results.json"

    with patch("carta.scanner.scanner.get_current_git_hash", return_value="abc123"), \
         patch("carta.scanner.scanner.get_file_last_commit_date", return_value=None):
        result = run_scan(tmp_path, cfg, output_path, reference_date=date(2026, 3, 18))

    assert output_path.exists()
    data = json.loads(output_path.read_text())
    assert "run_at" in data
    assert "run_at_git_hash" in data
    assert "git_branch" in data
    assert "issues" in data
    assert "stats" in data
    assert "changed_since_last_audit" in data
    # No previous scan — all tracked docs should be in changed_since_last_audit
    assert "docs/CAN/TOPOLOGY.md" in data["changed_since_last_audit"]
    # stray.md should be a homeless_doc issue
    assert any(i["type"] == "homeless_doc" for i in data["issues"])


# ---------------------------------------------------------------------------
# Embed file type checks
# ---------------------------------------------------------------------------

from carta.scanner.scanner import (
    check_embed_induction_needed,
    check_embed_lfs_not_pulled,
    check_embed_transcript_unprocessed,
)


def test_check_embed_induction_needed_no_sidecar(tmp_path):
    ds = tmp_path / "docs" / "reference" / "datasheets"
    ds.mkdir(parents=True)
    (ds / "ads1263.pdf").write_bytes(b"%PDF-fake")

    cfg = _minimal_cfg(tmp_path)
    issues = check_embed_induction_needed(tmp_path, cfg)

    assert len(issues) == 1
    assert issues[0]["type"] == "embed_induction_needed"
    assert "ads1263.pdf" in issues[0]["doc"]


def test_check_embed_induction_needed_with_pending_sidecar(tmp_path):
    ds = tmp_path / "docs" / "reference" / "datasheets"
    ds.mkdir(parents=True)
    (ds / "ads1263.pdf").write_bytes(b"%PDF-fake")
    (ds / "ads1263.embed-meta.yaml").write_text("slug: ads1263\nstatus: pending\n")

    cfg = _minimal_cfg(tmp_path)
    issues = check_embed_induction_needed(tmp_path, cfg)

    assert len(issues) == 1
    assert issues[0]["type"] == "embed_induction_needed"


def test_check_embed_induction_needed_embedded_ok(tmp_path):
    ds = tmp_path / "docs" / "reference" / "datasheets"
    ds.mkdir(parents=True)
    (ds / "ads1263.pdf").write_bytes(b"%PDF-fake")
    (ds / "ads1263.embed-meta.yaml").write_text("slug: ads1263\nstatus: embedded\n")

    cfg = _minimal_cfg(tmp_path)
    issues = check_embed_induction_needed(tmp_path, cfg)
    assert len(issues) == 0


def test_check_embed_lfs_not_pulled(tmp_path):
    ds = tmp_path / "docs" / "reference" / "datasheets"
    ds.mkdir(parents=True)
    (ds / "big.pdf").write_text(
        "version https://git-lfs.github.com/spec/v1\noid sha256:abc123\nsize 12345\n"
    )
    (ds / "big.embed-meta.yaml").write_text("slug: big\nstatus: pending\n")

    cfg = _minimal_cfg(tmp_path)
    issues = check_embed_lfs_not_pulled(tmp_path, cfg)

    assert len(issues) == 1
    assert issues[0]["type"] == "embed_lfs_not_pulled"


def test_check_embed_transcript_unprocessed(tmp_path):
    audio_in = tmp_path / "docs" / "audio" / "inputs"
    audio_in.mkdir(parents=True)
    transcripts = tmp_path / "docs" / "audio" / "transcripts"
    transcripts.mkdir(parents=True)
    processed = tmp_path / "docs" / "audio" / "processed"
    processed.mkdir(parents=True)

    (audio_in / "meeting.m4a").write_bytes(b"fake-audio")
    (audio_in / "meeting.embed-meta.yaml").write_text("slug: meeting\nstatus: embedded\ndoc_type: audio\n")
    (transcripts / "meeting.txt").write_text("Speaker 1: hello")
    # No processed summary → should flag

    cfg = _minimal_cfg(tmp_path)
    issues = check_embed_transcript_unprocessed(tmp_path, cfg)

    assert len(issues) == 1
    assert issues[0]["type"] == "embed_transcript_unprocessed"


def test_check_embed_transcript_unprocessed_skips_fulfilled_without_summary(tmp_path):
    """Status fulfilled should suppress the issue even without a summary file."""
    audio_in = tmp_path / "docs" / "audio" / "inputs"
    audio_in.mkdir(parents=True)
    transcripts = tmp_path / "docs" / "audio" / "transcripts"
    transcripts.mkdir(parents=True)
    processed = tmp_path / "docs" / "audio" / "processed"
    processed.mkdir(parents=True)

    (audio_in / "meeting.m4a").write_bytes(b"fake-audio")
    (audio_in / "meeting.embed-meta.yaml").write_text("slug: meeting\nstatus: fulfilled\ndoc_type: audio\n")
    (transcripts / "meeting.txt").write_text("Speaker 1: hello")

    cfg = _minimal_cfg(tmp_path)
    issues = check_embed_transcript_unprocessed(tmp_path, cfg)
    assert len(issues) == 0


def test_check_embed_transcript_unprocessed_has_summary(tmp_path):
    audio_in = tmp_path / "docs" / "audio" / "inputs"
    audio_in.mkdir(parents=True)
    transcripts = tmp_path / "docs" / "audio" / "transcripts"
    transcripts.mkdir(parents=True)
    processed = tmp_path / "docs" / "audio" / "processed"
    processed.mkdir(parents=True)

    (audio_in / "meeting.m4a").write_bytes(b"fake-audio")
    (audio_in / "meeting.embed-meta.yaml").write_text("slug: meeting\nstatus: integrated\ndoc_type: audio\n")
    (transcripts / "meeting.txt").write_text("Speaker 1: hello")
    (processed / "meeting-summary.md").write_text("# Summary\nStuff was discussed.")

    cfg = _minimal_cfg(tmp_path)
    issues = check_embed_transcript_unprocessed(tmp_path, cfg)
    assert len(issues) == 0


# ---------------------------------------------------------------------------
# check_one_way_links
# ---------------------------------------------------------------------------

from carta.scanner.scanner import check_one_way_links


def test_one_way_link_detected(tmp_path):
    """A -> B where B does not list A back should emit one_way_link."""
    (tmp_path / "docs" / "CAN").mkdir(parents=True)
    doc_a = tmp_path / "docs" / "CAN" / "MESSAGE_FLOW.md"
    doc_b = tmp_path / "docs" / "CAN" / "TOPOLOGY.md"
    doc_a.write_text("# A\n")
    doc_b.write_text("# B\n")

    fm_a = {"related": ["docs/CAN/TOPOLOGY.md"], "last_reviewed": "2026-03-01"}
    fm_b = {"related": [], "last_reviewed": "2026-03-01"}
    docs_fm = {
        "docs/CAN/MESSAGE_FLOW.md": fm_a,
        "docs/CAN/TOPOLOGY.md": fm_b,
    }

    issues = check_one_way_links(doc_a, fm_a, docs_fm, tmp_path)
    assert len(issues) == 1
    assert issues[0]["type"] == "one_way_link"
    assert issues[0]["severity"] == "warning"
    assert "TOPOLOGY.md" in issues[0]["detail"]
    assert issues[0]["related_file"] == "docs/CAN/TOPOLOGY.md"


def test_bidirectional_link_no_issue(tmp_path):
    """A -> B and B -> A: no one_way_link should be emitted."""
    (tmp_path / "docs" / "CAN").mkdir(parents=True)
    doc_a = tmp_path / "docs" / "CAN" / "MESSAGE_FLOW.md"
    doc_b = tmp_path / "docs" / "CAN" / "TOPOLOGY.md"
    doc_a.write_text("# A\n")
    doc_b.write_text("# B\n")

    fm_a = {"related": ["docs/CAN/TOPOLOGY.md"]}
    fm_b = {"related": ["docs/CAN/MESSAGE_FLOW.md"]}
    docs_fm = {
        "docs/CAN/MESSAGE_FLOW.md": fm_a,
        "docs/CAN/TOPOLOGY.md": fm_b,
    }

    issues = check_one_way_links(doc_a, fm_a, docs_fm, tmp_path)
    assert issues == []


def test_one_way_link_skips_nonexistent_target(tmp_path):
    """Broken related entries (file missing) should be skipped — check_broken_related handles them."""
    (tmp_path / "docs").mkdir(parents=True)
    doc_a = tmp_path / "docs" / "ARCH.md"
    doc_a.write_text("# A\n")

    fm_a = {"related": ["docs/NONEXISTENT.md"]}
    docs_fm = {"docs/ARCH.md": fm_a}

    issues = check_one_way_links(doc_a, fm_a, docs_fm, tmp_path)
    assert issues == []


def test_one_way_link_skips_target_without_frontmatter(tmp_path):
    """Target without parseable frontmatter is skipped (not a bidirectionality error)."""
    (tmp_path / "docs" / "CAN").mkdir(parents=True)
    doc_a = tmp_path / "docs" / "CAN" / "MESSAGE_FLOW.md"
    doc_b = tmp_path / "docs" / "CAN" / "TOPOLOGY.md"
    doc_a.write_text("# A\n")
    doc_b.write_text("# B — no frontmatter\n")

    fm_a = {"related": ["docs/CAN/TOPOLOGY.md"]}
    docs_fm = {
        "docs/CAN/MESSAGE_FLOW.md": fm_a,
        "docs/CAN/TOPOLOGY.md": None,
    }

    issues = check_one_way_links(doc_a, fm_a, docs_fm, tmp_path)
    assert issues == []


def test_one_way_link_multiple_targets(tmp_path):
    """Doc with two related entries where only one is reciprocated."""
    (tmp_path / "docs" / "CAN").mkdir(parents=True)
    for name in ["SOURCE.md", "RECIPROCAL.md", "ONE_WAY.md"]:
        (tmp_path / "docs" / "CAN" / name).write_text("# doc\n")

    fm_source = {"related": ["docs/CAN/RECIPROCAL.md", "docs/CAN/ONE_WAY.md"]}
    fm_recip = {"related": ["docs/CAN/SOURCE.md"]}
    fm_one_way = {"related": []}

    docs_fm = {
        "docs/CAN/SOURCE.md": fm_source,
        "docs/CAN/RECIPROCAL.md": fm_recip,
        "docs/CAN/ONE_WAY.md": fm_one_way,
    }

    issues = check_one_way_links(
        tmp_path / "docs" / "CAN" / "SOURCE.md", fm_source, docs_fm, tmp_path
    )
    assert len(issues) == 1
    assert "ONE_WAY.md" in issues[0]["related_file"]


def test_run_scan_includes_one_way_link(tmp_path):
    """Integration: run_scan emits one_way_link issues when back-links are missing."""
    from unittest.mock import patch

    (tmp_path / "docs" / "CAN").mkdir(parents=True)
    (tmp_path / "docs" / "CAN" / "MESSAGE_FLOW.md").write_text(
        "---\nrelated:\n  - docs/CAN/TOPOLOGY.md\nlast_reviewed: 2026-03-18\n---\n# A\n"
    )
    (tmp_path / "docs" / "CAN" / "TOPOLOGY.md").write_text(
        "---\nrelated: []\nlast_reviewed: 2026-03-18\n---\n# B\n"
    )

    cfg = {
        "docs_root": "docs/",
        "excluded_paths": [],
        "stale_threshold_days": 30,
    }
    output_path = tmp_path / "scan-results.json"

    with patch("carta.scanner.scanner.get_current_git_hash", return_value="abc123"), \
         patch("carta.scanner.scanner.get_file_last_commit_date", return_value=None):
        result = run_scan(tmp_path, cfg, output_path, reference_date=date(2026, 3, 18))

    one_way = [i for i in result["issues"] if i["type"] == "one_way_link"]
    assert len(one_way) >= 1
    assert any("TOPOLOGY.md" in i["detail"] for i in one_way)


# ---------------------------------------------------------------------------
# suggest_related_for_doc / suggest_related_for_all
# ---------------------------------------------------------------------------

from carta.scanner.scanner import suggest_related_for_doc, suggest_related_for_all


def _make_cfg_with_qdrant(qdrant_url: str = "http://localhost:6333") -> dict:
    return {
        "project_name": "test",
        "qdrant_url": qdrant_url,
        "docs_root": "docs/",
        "excluded_paths": [],
        "stale_threshold_days": 30,
        "embed": {
            "ollama_url": "http://localhost:11434",
            "ollama_model": "nomic-embed-text:latest",
        },
    }


def test_suggest_related_skips_doc_with_existing_links(tmp_path):
    """Docs that already have related: entries must not be queried."""
    (tmp_path / "docs").mkdir()
    doc = tmp_path / "docs" / "ARCH.md"
    doc.write_text("# Arch\n")
    fm = {"related": ["docs/OTHER.md"]}

    results = suggest_related_for_doc(doc, fm, tmp_path, _make_cfg_with_qdrant())
    assert results == []


def test_suggest_related_degrades_gracefully_when_qdrant_unavailable(tmp_path):
    """suggest_related_for_doc must return [] when Qdrant is not running."""
    (tmp_path / "docs").mkdir()
    doc = tmp_path / "docs" / "ARCH.md"
    doc.write_text("# Arch — some content\n")
    fm = {"related": []}

    results = suggest_related_for_doc(doc, fm, tmp_path, _make_cfg_with_qdrant("http://localhost:19999"))
    assert results == []


def test_suggest_related_returns_suggestions_above_threshold(tmp_path):
    """When Qdrant returns hits above threshold, they should be included."""
    from unittest.mock import MagicMock, patch

    (tmp_path / "docs" / "CAN").mkdir(parents=True)
    doc = tmp_path / "docs" / "CAN" / "MESSAGE_FLOW.md"
    doc.write_text("# CAN message flow\n")

    mock_hit = MagicMock()
    mock_hit.score = 0.91
    mock_hit.payload = {"file_path": "docs/CAN/TOPOLOGY.md"}

    mock_low = MagicMock()
    mock_low.score = 0.70
    mock_low.payload = {"file_path": "docs/CAN/SAFETY.md"}

    mock_result = MagicMock()
    mock_result.points = [mock_hit, mock_low]

    cfg = _make_cfg_with_qdrant()
    with patch("carta.scanner.scanner.QdrantClient") as mock_qc, \
         patch("carta.scanner.scanner.get_embedding", return_value=[0.1] * 768), \
         patch("carta.scanner.scanner.collection_name", return_value="test_doc"):
        mock_qc.return_value.query_points.return_value = mock_result
        results = suggest_related_for_doc(doc, {}, tmp_path, cfg)

    assert len(results) == 1
    assert results[0]["suggested"] == "docs/CAN/TOPOLOGY.md"
    assert results[0]["score"] == 0.91
    assert results[0]["doc"] == "docs/CAN/MESSAGE_FLOW.md"


def test_suggest_related_excludes_self(tmp_path):
    """The doc itself should not appear in its own suggestions."""
    from unittest.mock import MagicMock, patch

    (tmp_path / "docs" / "CAN").mkdir(parents=True)
    doc = tmp_path / "docs" / "CAN" / "MESSAGE_FLOW.md"
    doc.write_text("# Self\n")

    mock_self = MagicMock()
    mock_self.score = 1.0
    mock_self.payload = {"file_path": "docs/CAN/MESSAGE_FLOW.md"}

    mock_result = MagicMock()
    mock_result.points = [mock_self]

    cfg = _make_cfg_with_qdrant()
    with patch("carta.scanner.scanner.QdrantClient") as mock_qc, \
         patch("carta.scanner.scanner.get_embedding", return_value=[0.1] * 768), \
         patch("carta.scanner.scanner.collection_name", return_value="test_doc"):
        mock_qc.return_value.query_points.return_value = mock_result
        results = suggest_related_for_doc(doc, {}, tmp_path, cfg)

    assert results == []


def test_suggest_related_for_all_aggregates(tmp_path):
    """suggest_related_for_all should aggregate results from all docs without related."""
    from unittest.mock import MagicMock, patch

    (tmp_path / "docs" / "CAN").mkdir(parents=True)
    doc_a = tmp_path / "docs" / "CAN" / "A.md"
    doc_b = tmp_path / "docs" / "CAN" / "B.md"
    doc_a.write_text("# A\n")
    doc_b.write_text("# B\n")

    frontmatters = {
        "docs/CAN/A.md": {"related": []},
        "docs/CAN/B.md": {"related": ["docs/CAN/A.md"]},  # already has links — skipped
    }

    mock_hit = MagicMock()
    mock_hit.score = 0.92
    mock_hit.payload = {"file_path": "docs/CAN/B.md"}
    mock_result = MagicMock()
    mock_result.points = [mock_hit]

    cfg = _make_cfg_with_qdrant()
    with patch("carta.scanner.scanner.QdrantClient") as mock_qc, \
         patch("carta.scanner.scanner.get_embedding", return_value=[0.1] * 768), \
         patch("carta.scanner.scanner.collection_name", return_value="test_doc"):
        mock_qc.return_value.query_points.return_value = mock_result
        results = suggest_related_for_all([doc_a, doc_b], frontmatters, tmp_path, cfg)

    assert len(results) == 1
    assert results[0]["doc"] == "docs/CAN/A.md"


def test_run_scan_includes_related_suggestions_key(tmp_path):
    """run_scan result dict must always contain 'related_suggestions' key."""
    from unittest.mock import patch

    (tmp_path / "docs" / "CAN").mkdir(parents=True)
    (tmp_path / "docs" / "CAN" / "TOPOLOGY.md").write_text(
        "---\nrelated: []\nlast_reviewed: 2026-03-18\n---\n# Topology\n"
    )

    cfg = {
        "docs_root": "docs/",
        "excluded_paths": [],
        "stale_threshold_days": 30,
    }
    output_path = tmp_path / "scan-results.json"

    with patch("carta.scanner.scanner.get_current_git_hash", return_value="abc123"), \
         patch("carta.scanner.scanner.get_file_last_commit_date", return_value=None):
        result = run_scan(tmp_path, cfg, output_path, reference_date=date(2026, 3, 18))

    assert "related_suggestions" in result
    assert isinstance(result["related_suggestions"], list)
