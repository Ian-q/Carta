"""``dangling_symbol`` and ``ambiguous_symbol``: gone from the code, not merely unknown."""
from __future__ import annotations

import os
import sys
from datetime import date
from pathlib import Path
from unittest.mock import patch

import pytest

from carta.graph import gitio, symbol_scan
from carta.graph.gitio import GitError, commits_changing_text, files_with_word
from carta.graph.paths import PathIndex
from carta.graph.settings import graph_settings
from carta.graph.symbol_scan import symbol_findings, words_in_code
from carta.graph.tests.conftest import requires_git

pytestmark = requires_git

ON = {"graph": {"scan": {"dangling_symbols": True}}}

FRAME_V1 = ("class FrameParser:\n    def flush_all(self):\n        return 1\n\n\n"
            "def decode_header(data):\n    return data\n\n\ndef render_table(rows):\n"
            "    return fetch_rows(rows)\n")
FRAME_V2 = ("class FrameParser:\n    pass\n\n\ndef decode_hdr(data):\n    return data\n\n\n"
            "def render_table(rows):\n    return fetch_rows(rows)\n")
DOC = ("# Frames\n\n`decode_header()` reads the header, then `render_table()` prints it.\n"
       "`FrameParser.flush_all` empties the queue.\n"
       "Vectors go through `QdrantClient`; install with `pip_install`.\n"
       "Rows come from `fetch_rows()`.\n")


@pytest.fixture
def repo(git_repo):
    """The code had ``decode_header`` and ``flush_all`` when the doc named them; then lost both."""
    git_repo.write("src/pkg/frame.py", FRAME_V1)
    git_repo.write("docs/frames.md", DOC)
    git_repo.write("CHANGELOG.md", "Added `decode_header()`.\n")
    git_repo.first = git_repo.commit("frames", 0)
    git_repo.write("src/pkg/frame.py", FRAME_V2)
    git_repo.commit("rename the decoder, drop the flush", 1)
    return git_repo


def by_symbol(findings):
    return {f["symbol"]: f for f in findings}


def test_a_deleted_symbol_is_dangling_and_names_the_commit_that_had_it(repo):
    findings, stats = symbol_findings(repo.root, ON)
    assert sorted(by_symbol(findings)) == ["FrameParser.flush_all", "decode_header"]
    found = by_symbol(findings)["decode_header"]
    assert found == {
        "type": "dangling_symbol", "severity": "warning", "doc": "docs/frames.md",
        "detail": (f"names `decode_header` (line 3), which is no longer in the code; it was at "
                   f"{repo.first[:12]}, when this doc last changed that mention"),
        "symbol": "decode_header", "line": 3, "last_seen": repo.first}
    assert stats == {"symbol_check": "ok", "dangling_symbols": 2, "ambiguous_symbols": 0,
                     "dangling_candidates": 4}


def test_an_external_name_is_never_dangling(repo):
    symbols = by_symbol(symbol_findings(repo.root, ON)[0])
    # Never in this repository's code: a library class and a shell-ish name.
    assert "QdrantClient" not in symbols and "pip_install" not in symbols
    # Called here and defined elsewhere: still a word in the code, so not gone.
    assert "fetch_rows" not in symbols
    assert "render_table" not in symbols


def test_a_member_whose_owner_survives_is_still_dangling(repo):
    cfg = {"graph": {"scan": {"dangling_symbols": True},
                     "symbols": {"provider": "file", "file": "symbols.tsv"}}}
    repo.write("symbols.tsv", "FrameParser\tclass\tsrc/pkg/frame.py\t1\n"
                              "render_table\tfunction\tsrc/pkg/frame.py\t9\n")
    symbols = by_symbol(symbol_findings(repo.root, cfg)[0])
    assert "FrameParser.flush_all" in symbols and "render_table" not in symbols


def test_both_findings_are_off_by_default(repo):
    assert symbol_findings(repo.root, {}) == (
        [], {"symbol_check": "off", "dangling_symbols": 0, "ambiguous_symbols": 0})
    assert not (repo.root / ".carta").exists()          # off means nothing was built either


def test_a_mention_written_after_the_symbol_left_is_not_dangling(repo):
    repo.write("src/pkg/frame.py", FRAME_V2.replace("decode_hdr", "read_hdr"))
    repo.write("docs/notes.md", "We removed `decode_hdr()` in favour of `read_hdr()`.\n")
    repo.commit("rename again, and say so", 2)
    repo.write("docs/later.md", "`decode_hdr()` used to live here.\n")
    repo.commit("a note written later", 3)
    symbols = [(f["doc"], f["symbol"]) for f in symbol_findings(repo.root, ON)[0]]
    assert ("docs/notes.md", "decode_hdr") not in symbols
    assert ("docs/later.md", "decode_hdr") not in symbols


def test_ignored_docs_are_not_reported(repo):
    docs = {f["doc"] for f in symbol_findings(repo.root, ON)[0]}
    assert docs == {"docs/frames.md"}                   # CHANGELOG.md names it too


def test_without_git_history_there_is_no_finding(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("x = 1\n")
    (tmp_path / "doc.md").write_text("`decode_header()` is gone.\n")
    findings, stats = symbol_findings(tmp_path, ON)
    assert findings == [] and stats["symbol_check"] == "ok"
    assert stats["dangling_candidates"] == 1


def test_an_untracked_doc_has_no_history_to_vouch_for_it(repo):
    repo.write("docs/draft.md", "`decode_header()` is described here.\n")
    assert {f["doc"] for f in symbol_findings(repo.root, ON)[0]} == {"docs/frames.md"}


def test_the_git_budget_is_a_bound_and_running_out_is_reported(repo, monkeypatch):
    monkeypatch.setattr(symbol_scan, "MAX_SYMBOL_HISTORY_CHECKS", 1)
    findings, stats = symbol_findings(repo.root, ON)
    assert findings == [] and stats["symbol_check"] == "incomplete"


def test_a_git_failure_reports_incomplete_not_clean(repo, monkeypatch):
    def broken(*_args, **_kwargs):
        raise GitError("boom")

    monkeypatch.setattr(symbol_scan, "commits_changing_text", broken)
    findings, stats = symbol_findings(repo.root, ON)
    assert findings == [] and stats["symbol_check"] == "incomplete"


@pytest.mark.skipif(not hasattr(os, "geteuid") or os.geteuid() == 0,
                    reason="needs a file the process cannot read")
def test_an_unreadable_code_file_means_absence_is_not_proven(repo):
    locked = repo.write("src/pkg/locked.py", "def decode_header(): pass\n")
    locked.chmod(0)
    try:
        findings, stats = symbol_findings(repo.root, ON)
    finally:
        locked.chmod(0o644)
    assert findings == [] and stats["symbol_check"] == "incomplete"


@pytest.mark.skipif(sys.platform == "win32", reason="needs symbolic links")
def test_words_in_code_reads_code_only_and_whole_words(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("decode_header_v2 = 1\nrender_table()\n")
    (tmp_path / "doc.md").write_text("decode_header\n")
    (tmp_path / "blob.bin").write_bytes(b"\0\0decode_header\0")
    os.symlink(tmp_path / "doc.md", tmp_path / "src" / "link.py")
    index = PathIndex(["src/a.py", "doc.md", "blob.bin", "src/link.py"])
    found, complete = words_in_code(tmp_path, index, graph_settings({}),
                                    {"decode_header", "render_table", "not a word"})
    assert (found, complete) == ({"render_table"}, True)


def test_ambiguous_symbol_reports_a_name_defined_in_too_many_files(repo):
    repo.write("symbols.tsv", "init\tfunction\tsrc/pkg/frame.py\t1\ninit\tfunction\tsrc/b.py\t1\n"
                              "render_table\tfunction\tsrc/pkg/frame.py\t9\n")
    repo.write("src/b.py", "def init(): pass\n")
    repo.write("docs/boot.md", "Call `init()` then `render_table()`.\nAgain `init()`.\n")
    cfg = {"graph": {"scan": {"ambiguous_symbols": True},
                     "symbols": {"provider": "file", "file": "symbols.tsv"}}}
    findings, stats = symbol_findings(repo.root, cfg)
    assert findings == [{
        "type": "ambiguous_symbol", "severity": "info", "doc": "docs/boot.md",
        "detail": ("names `init` (line 1), which is defined in 2 files; no symbol edge was "
                   "made. Qualify it, or name the file"),
        "symbol": "init", "line": 1, "definitions": 2}]
    assert stats["ambiguous_symbols"] == 1 and stats["dangling_symbols"] == 0
    # Without an index nothing is ambiguous: there is nothing to count.
    off = {"graph": {"scan": {"ambiguous_symbols": True}}}
    assert symbol_findings(repo.root, off)[0] == []


# -- the git helpers ------------------------------------------------------------

def test_commits_changing_text_is_newest_first_and_literal(repo):
    repo.write("docs/frames.md", DOC + "`decode_header()` again.\n")
    newer = repo.commit("mention it twice", 2)
    assert commits_changing_text(repo.root, "docs/frames.md", "decode_header") == [
        newer, repo.first]
    assert commits_changing_text(repo.root, "docs/frames.md", "never_written") == []
    # A path is a path: a glob that would match the doc names no file.
    assert commits_changing_text(repo.root, "docs/*.md", "decode_header") == []
    assert commits_changing_text(repo.root, ":(glob)docs/*.md", "decode_header") == []


@pytest.mark.parametrize("needle", ["", "a\nb", "x" * 201, "a\x00b"])
def test_commits_changing_text_refuses_a_needle_it_cannot_pass(repo, needle):
    with pytest.raises((GitError, ValueError)):
        commits_changing_text(repo.root, "docs/frames.md", needle)


def test_files_with_word_lists_names_at_a_commit(repo):
    assert files_with_word(repo.root, repo.first, "decode_header") == [
        "CHANGELOG.md", "docs/frames.md", "src/pkg/frame.py"]
    head = repo.git("rev-parse", "HEAD").strip()
    assert files_with_word(repo.root, head, "decode_header") == ["CHANGELOG.md", "docs/frames.md"]
    assert files_with_word(repo.root, head, "decode") == []          # whole words only
    assert files_with_word(repo.root, head, "no_such_word_here") == []


@pytest.mark.parametrize("commit,word", [
    ("HEAD", "decode_header"), ("--all", "decode_header"), ("0" * 39, "decode_header"),
    ("0" * 40 + "\n", "decode_header"), (None, "--help"), (None, "a b"), (None, "-e"),
    (None, ""), (None, "x" * 201),
])
def test_files_with_word_takes_only_a_full_object_name_and_an_identifier(repo, commit, word):
    with pytest.raises(GitError):
        files_with_word(repo.root, commit or repo.first, word)


def test_files_with_word_survives_a_file_name_with_a_newline(repo):
    try:
        repo.write("src/odd\nname.py", "decode_header = 1\n")
    except OSError:
        pytest.skip("the filesystem refuses the name")
    head = repo.commit("an odd name", 2)
    assert "src/odd\nname.py" in files_with_word(repo.root, head, "decode_header")
    assert gitio._is_oid(head)


# -- through run_scan -----------------------------------------------------------

def scan(root: Path, cfg: dict) -> dict:
    from carta.scanner.scanner import run_scan
    full = {"docs_root": "docs/", "excluded_paths": [], "stale_threshold_days": 30}
    full.update(cfg)
    with patch("carta.scanner.scanner.suggest_related_for_all", return_value=[]), \
            patch("carta.scanner.scanner.get_file_last_commit_date", return_value=None):
        return run_scan(root, full, root / "scan-results.json", reference_date=date(2026, 1, 1))


def test_run_scan_carries_the_findings_and_says_when_they_are_off(repo):
    result = scan(repo.root, {})
    assert result["stats"]["symbol_check"] == "off"
    assert result["stats"]["dangling_symbols"] == result["stats"]["ambiguous_symbols"] == 0
    assert not [i for i in result["issues"] if i["type"].endswith("_symbol")]
    result = scan(repo.root, ON)
    found = [i["symbol"] for i in result["issues"] if i["type"] == "dangling_symbol"]
    assert found == ["decode_header", "FrameParser.flush_all"]
    assert result["stats"]["dangling_symbols"] == 2 and result["stats"]["symbol_check"] == "ok"


def test_run_scan_records_a_symbol_check_that_could_not_run(repo):
    with patch("carta.graph.symbol_scan.symbol_findings", side_effect=RuntimeError("boom")):
        result = scan(repo.root, ON)
    assert result["stats"]["symbol_check"] == "failed"
    assert result["stats"]["dangling_symbols"] == 0
