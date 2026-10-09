"""The ``missing_path_mention`` scan finding: fires only when the path was real."""
from __future__ import annotations

from datetime import date
from pathlib import Path
from unittest.mock import patch

import pytest

from carta.graph.scan import missing_path_findings
from carta.graph.tests.conftest import requires_git

BODY = "def parse():\n    return 'a body long enough for git to detect a rename'\n"


def write(root: Path, rel: str, text: str = "") -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))
    return path


def brief(findings):
    return [(f["doc"], f["path"], f["reason"]) for f in findings]


@pytest.fixture
def repo(git_repo):
    """History: src/parser/{old_frame,legacy}.py and tools/gen/ existed, then went away."""
    git_repo.write("src/parser/old_frame.py", BODY)
    git_repo.write("src/parser/legacy.py", "x = 1\n")
    git_repo.write("src/parser/keep.py", "k = 1\n")
    git_repo.write("tools/gen/tables.py", "t = 1\n")
    git_repo.write("tools/run.py", "r = 1\n")
    git_repo.commit("first", 0)
    git_repo.git("mv", "src/parser/old_frame.py", "src/parser/frame.py")
    git_repo.rm("src/parser/legacy.py")
    git_repo.rm("tools/gen/tables.py")
    git_repo.commit("second", 1)
    return git_repo


@requires_git
def test_deleted_file_mentioned_in_backticks_is_reported(repo):
    repo.write("docs/parser.md", "# Parser\n\nOld code was `src/parser/legacy.py`.\n")
    assert missing_path_findings(repo.root, {}) == [{
        "type": "missing_path_mention", "severity": "warning", "doc": "docs/parser.md",
        "detail": "mentions `src/parser/legacy.py` (line 3), which no longer exists; "
                  "deleted from the repository",
        "path": "src/parser/legacy.py", "line": 3, "reason": "deleted",
    }]


@requires_git
def test_renamed_file_reports_new_path(repo):
    repo.write("docs/parser.md", "See `src/parser/old_frame.py`.\n")
    (finding,) = missing_path_findings(repo.root, {})
    assert finding["reason"] == "renamed"
    assert finding["renamed_to"] == "src/parser/frame.py"
    assert finding["detail"] == ("mentions `src/parser/old_frame.py` (line 1), which no longer "
                                 "exists; renamed to src/parser/frame.py")


@requires_git
def test_rename_chain_is_followed_to_the_surviving_path(repo):
    repo.git("mv", "src/parser/frame.py", "src/parser/framing.py")
    repo.commit("third", 2)
    repo.write("docs/parser.md", "See `src/parser/old_frame.py`.\n")
    (finding,) = missing_path_findings(repo.root, {})
    assert finding["reason"] == "renamed" and finding["renamed_to"] == "src/parser/framing.py"


@requires_git
def test_renamed_then_new_path_deleted_reports_no_renamed_to(repo):
    repo.rm("src/parser/frame.py")
    repo.commit("third", 2)
    repo.write("docs/parser.md", "See `src/parser/old_frame.py`.\n")
    (finding,) = missing_path_findings(repo.root, {})
    assert finding["reason"] == "renamed" and "renamed_to" not in finding
    assert finding["detail"].endswith("which no longer exists; renamed, then removed")


@requires_git
def test_removed_directory_is_evidenced_by_the_files_deleted_under_it(repo):
    repo.write("docs/tools.md", "Generators live in `tools/gen/`.\n")
    assert brief(missing_path_findings(repo.root, {})) == [
        ("docs/tools.md", "tools/gen/", "deleted")]


def test_broken_relative_link_is_reported_without_history(tmp_path):
    root = tmp_path / "proj"
    write(root, "docs/guide.md", "Read [the setup notes](setup.md) and [x](../src/gone.py).\n")
    write(root, "docs/other.md", "# Other\n")
    write(root, "src/a.py", "a = 1\n")
    assert brief(missing_path_findings(root, {})) == [
        ("docs/guide.md", "docs/setup.md", "broken_link"),
        ("docs/guide.md", "src/gone.py", "broken_link")]
    assert missing_path_findings(root, {})[0]["detail"] == (
        "links to `docs/setup.md` (line 1), which does not exist")


@requires_git
def test_precision_table_gives_no_finding(repo):
    repo.write(".gitignore", "htmlcov/\nout/\n")
    repo.write("out/report.py", "generated = 1\n")                # ignored, present on disk
    repo.write("docs/space name.md", "# spaced\n")
    repo.write("docs/setup.md", "# setup\n")
    repo.write("src/parser/legacy.py", "recreated = 1\n")          # deleted, then back
    repo.write("docs/precise.md", "\n".join([
        "A URL: [site](https://example.invalid/src/parser/legacy2.py) and `http://x.invalid/a.py`.",
        "A placeholder: `path/to/file.py` and `foo/bar.py` and `your-project/main.py`.",
        "An absolute path: `/usr/local/lib/thing.py`.",
        "Outside the repo: `../../outside/x.py` and [o](../../outside/y.md).",
        "Never existed: `src/parser/planned.py`.",
        "A glob: `src/parser/old_*.py`.",
        "Present but ignored: `out/report.py`.",
        "Deleted then re-created: `src/parser/legacy.py`.",
        "A site-root link: [guide](/guide/setup/).",
        "A rendered name: [setup](setup.html).",
        "Generated output: [coverage](../htmlcov/index.html).",
        "A file with a space: [spaced](space%20name.md).",
        "An anchor: [here](#section) and a mail link [m](mailto:someone@example.invalid).",
        "```",
        "rm src/parser/old_frame.py tools/gen/tables.py",
        "```",
        "",
    ]))
    assert missing_path_findings(repo.root, {}) == []


@requires_git
def test_mentions_in_ignored_and_excluded_docs_give_no_finding(repo):
    text = "Removed `src/parser/legacy.py`.\n"
    repo.write("CHANGELOG.md", text)
    repo.write("docs/CHANGELOG-old.md", text)
    repo.write("docs/archive/old.md", text)
    repo.write("docs/live.md", text)
    cfg = {"excluded_paths": ["docs/archive/"]}
    assert brief(missing_path_findings(repo.root, cfg)) == [
        ("docs/live.md", "src/parser/legacy.py", "deleted")]
    cfg["graph"] = {"scan": {"ignore_docs": ["docs/live.md"]}}
    assert sorted(f["doc"] for f in missing_path_findings(repo.root, cfg)) == [
        "CHANGELOG.md", "docs/CHANGELOG-old.md"]


@requires_git
def test_one_finding_per_doc_and_path_on_first_line(repo):
    repo.write("docs/parser.md", "intro\n`src/parser/legacy.py`\nagain `src/parser/legacy.py`\n")
    (finding,) = missing_path_findings(repo.root, {})
    assert finding["line"] == 2


@requires_git
def test_findings_sorted(repo):
    repo.write("docs/b.md", "`tools/gen/tables.py`\n`src/parser/legacy.py`\n")
    repo.write("docs/a.md", "x\n`src/parser/old_frame.py`\n")
    assert [(f["doc"], f["line"]) for f in missing_path_findings(repo.root, {})] == [
        ("docs/a.md", 2), ("docs/b.md", 1), ("docs/b.md", 2)]


@requires_git
def test_history_limit_bounds_the_search(repo):
    repo.write("docs/parser.md", "Old code was `src/parser/legacy.py`.\n")
    repo.commit("third", 2)
    repo.write("docs/parser.md", "Old code was `src/parser/legacy.py`. Edited.\n")
    repo.commit("fourth", 3)
    cfg = {"graph": {"scan": {"history_limit": 1}}}
    assert missing_path_findings(repo.root, cfg) == []
    assert len(missing_path_findings(repo.root, {})) == 1


@requires_git
def test_disabled_by_config(repo):
    repo.write("docs/parser.md", "Old code was `src/parser/legacy.py`.\n")
    assert missing_path_findings(repo.root, {"graph": {"enabled": False}}) == []
    assert missing_path_findings(repo.root, {"graph": {"scan": {"missing_paths": False}}}) == []
    assert not (repo.root / ".carta").exists()


@requires_git
def test_works_when_the_carta_root_is_a_subdirectory_of_the_checkout(git_repo):
    git_repo.write("pkg/src/gone.py", "x = 1\n")
    git_repo.write("pkg/docs/a.md", "See `src/gone.py`.\n")
    git_repo.commit("first", 0)
    git_repo.rm("pkg/src/gone.py")
    git_repo.write("pkg/src/keep.py", "k = 1\n")
    git_repo.commit("second", 1)
    assert brief(missing_path_findings(git_repo.root / "pkg", {})) == [
        ("docs/a.md", "src/gone.py", "deleted")]


# -- through run_scan -------------------------------------------------------

def _scan(root: Path, cfg: dict) -> dict:
    from carta.scanner.scanner import run_scan
    with patch("carta.scanner.scanner.suggest_related_for_all", return_value=[]), \
            patch("carta.scanner.scanner.get_file_last_commit_date", return_value=None):
        return run_scan(root, cfg, root / "scan-results.json", reference_date=date(2026, 1, 1))


SCAN_CFG = {"docs_root": "docs/", "excluded_paths": [], "stale_threshold_days": 30}


@requires_git
def test_run_scan_includes_finding_and_stat(repo):
    repo.write("docs/parser.md",
               "---\nrelated: []\nlast_reviewed: 2026-01-01\n---\nSee `src/parser/legacy.py`.\n")
    result = _scan(repo.root, dict(SCAN_CFG))
    found = [i for i in result["issues"] if i["type"] == "missing_path_mention"]
    assert brief(found) == [("docs/parser.md", "src/parser/legacy.py", "deleted")]
    assert result["stats"]["missing_path_mentions"] == 1
    assert result["stats"]["issues_by_severity"]["warning"] >= 1


@requires_git
def test_run_scan_survives_graph_failure(repo):
    repo.write("docs/parser.md",
               "---\nrelated: []\nlast_reviewed: 2026-01-01\n---\nSee `src/parser/legacy.py`.\n")
    with patch("carta.graph.scan.missing_path_findings", side_effect=RuntimeError("boom")):
        result = _scan(repo.root, dict(SCAN_CFG))
    assert [i for i in result["issues"] if i["type"] == "missing_path_mention"] == []
    assert result["stats"]["missing_path_mentions"] == 0


@requires_git
def test_run_scan_with_the_finding_disabled(repo):
    repo.write("docs/parser.md",
               "---\nrelated: []\nlast_reviewed: 2026-01-01\n---\nSee `src/parser/legacy.py`.\n")
    result = _scan(repo.root, dict(SCAN_CFG, graph={"scan": {"missing_paths": False}}))
    assert result["stats"]["missing_path_mentions"] == 0
