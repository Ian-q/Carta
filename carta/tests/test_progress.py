"""Tests for carta/ui/progress.py."""

import sys
from io import StringIO
from unittest.mock import patch

import pytest

from carta.ui.progress import Progress


def make_plain(total=3):
    """Return a Progress instance forced into plain mode (non-TTY)."""
    with patch("sys.stdout") as mock_stdout:
        mock_stdout.isatty.return_value = False
        p = Progress(total=total)
    p._tty = False  # ensure plain mode regardless of test runner TTY
    return p


class TestPlainMode:
    def test_file_prints_header(self, capsys):
        p = make_plain(total=3)
        p.file(idx=1, name="foo.pdf")
        captured = capsys.readouterr()
        assert "[1/3]" in captured.out
        assert "foo.pdf" in captured.out

    def test_step_prints_message(self, capsys):
        p = make_plain(total=3)
        p.file(idx=1, name="foo.pdf")
        p.step("extracting 5 pages")
        captured = capsys.readouterr()
        assert "extracting 5 pages" in captured.out

    def test_done_prints_ok_line(self, capsys):
        p = make_plain(total=3)
        p.file(idx=1, name="foo.pdf")
        p.done(chunks=42, elapsed=3.7)
        captured = capsys.readouterr()
        assert "OK" in captured.out
        assert "42" in captured.out
        assert "foo.pdf" in captured.out

    def test_skip_prints_skip_line(self, capsys):
        p = make_plain(total=3)
        p.file(idx=2, name="bar.pdf")
        p.skip(reason="LFS pointer")
        captured = capsys.readouterr()
        assert "SKIP" in captured.out
        assert "LFS pointer" in captured.out

    def test_error_prints_to_stderr(self, capsys):
        p = make_plain(total=3)
        p.file(idx=3, name="baz.pdf")
        p.error("Qdrant timeout")
        captured = capsys.readouterr()
        assert "ERROR" in captured.err
        assert "Qdrant timeout" in captured.err

    def test_summary_prints_counts(self, capsys):
        p = make_plain(total=3)
        p.summary(embedded=2, skipped=1, errors=0)
        captured = capsys.readouterr()
        assert "2" in captured.out
        assert "1" in captured.out

    def test_summary_shows_errors_when_nonzero(self, capsys):
        p = make_plain(total=3)
        p.summary(embedded=1, skipped=0, errors=2)
        captured = capsys.readouterr()
        assert "Errors: 2" in captured.out

    def test_scan_step_prints_message(self, capsys):
        p = make_plain(total=0)
        p.scan_step("checking frontmatter")
        captured = capsys.readouterr()
        assert "checking frontmatter" in captured.out

    def test_scan_done_prints_summary(self, capsys):
        p = make_plain(total=0)
        p.scan_done(elapsed=0.8, issue_count=5)
        captured = capsys.readouterr()
        assert "5 issue" in captured.out

    def test_context_manager_enters_and_exits(self):
        p = make_plain(total=1)
        with p as ctx:
            assert ctx is p

    def test_exit_does_not_raise_on_clean_exit(self):
        p = make_plain(total=1)
        with p:
            pass  # no active line written — should not crash
