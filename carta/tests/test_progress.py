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

    def test_summary_callable_after_context_exits(self, capsys):
        """summary() must work after __exit__ — matches actual cmd_embed usage pattern."""
        p = make_plain(total=2)
        with p:
            p.file(idx=1, name="a.pdf")
            p.done(chunks=10, elapsed=1.0)
        p.summary(embedded=1, skipped=1, errors=0)
        captured = capsys.readouterr()
        assert "Embedded: 1" in captured.out
        assert "Skipped: 1" in captured.out


class TestTTYMode:
    """TTY-mode methods write ANSI sequences to stdout — verify no exceptions and content."""

    def _make_tty(self, total=3):
        p = Progress(total=total)
        p._tty = True
        p._no_color = False
        return p

    def test_file_in_tty_does_not_print(self, capsys):
        p = self._make_tty()
        p.file(idx=1, name="foo.pdf")
        captured = capsys.readouterr()
        # file() is silent in TTY mode (first output comes from step/done)
        assert captured.out == ""

    def test_step_writes_to_stdout(self, capsys):
        p = self._make_tty()
        p.file(idx=1, name="foo.pdf")
        p.step("extracting 5 pages")
        captured = capsys.readouterr()
        assert "foo.pdf" in captured.out
        assert "extracting 5 pages" in captured.out
        assert "\r" in captured.out  # in-place rewrite

    def test_done_writes_newline(self, capsys):
        p = self._make_tty()
        p.file(idx=1, name="foo.pdf")
        p.done(chunks=10, elapsed=1.0)
        captured = capsys.readouterr()
        assert captured.out.endswith("\n")
        assert "foo.pdf" in captured.out

    def test_skip_writes_newline(self, capsys):
        p = self._make_tty()
        p.file(idx=2, name="bar.pdf")
        p.skip(reason="LFS pointer")
        captured = capsys.readouterr()
        assert captured.out.endswith("\n")
        assert "LFS pointer" in captured.out

    def test_error_writes_to_stderr(self, capsys):
        p = self._make_tty()
        p.file(idx=3, name="baz.pdf")
        p.error("Qdrant timeout")
        captured = capsys.readouterr()
        assert captured.err.endswith("\n")
        assert "Qdrant timeout" in captured.err

    def test_exit_clears_active_spinner_line(self, capsys):
        p = self._make_tty()
        p.file(idx=1, name="foo.pdf")
        p.step("working...")         # sets _active = True
        p.__exit__(None, None, None)
        captured = capsys.readouterr()
        # Should have written a newline to terminate the spinner line
        assert "\n" in captured.out

    def test_exit_no_extra_newline_when_not_active(self, capsys):
        p = self._make_tty()
        # No step() called — _active is False
        p.__exit__(None, None, None)
        captured = capsys.readouterr()
        assert captured.out == ""

    def test_no_color_suppresses_ansi(self):
        p = Progress(total=1)
        p._tty = True
        p._no_color = True
        result = p._c("\033[32m", "hello")
        assert result == "hello"
        assert "\033" not in result

    def test_scan_step_writes_spinner(self, capsys):
        p = self._make_tty(total=0)
        p.scan_step("checking frontmatter")
        captured = capsys.readouterr()
        assert "checking frontmatter" in captured.out
        assert "\r" in captured.out

    def test_scan_done_writes_newline(self, capsys):
        p = self._make_tty(total=0)
        p.scan_done(elapsed=0.8, issue_count=3)
        captured = capsys.readouterr()
        assert captured.out.endswith("\n")
        assert "3 issue" in captured.out
