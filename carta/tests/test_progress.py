"""Tests for carta/ui/progress.py."""

import sys
from io import StringIO
from unittest.mock import patch

import pytest

from carta.ui.progress import Progress, _format_page_ranges


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


class TestFormatPageRanges:
    def test_empty_list_returns_empty_string(self):
        assert _format_page_ranges([]) == ""

    def test_single_page(self):
        assert _format_page_ranges([5]) == "5"

    def test_two_consecutive(self):
        assert _format_page_ranges([3, 4]) == "3-4"

    def test_non_consecutive(self):
        assert _format_page_ranges([1, 3, 5]) == "1, 3, 5"

    def test_mixed_ranges(self):
        # pages 1-3, gap, 5-6, gap, 8
        assert _format_page_ranges([1, 2, 3, 5, 6, 8]) == "1-3, 5-6, 8"

    def test_unsorted_input_is_sorted(self):
        assert _format_page_ranges([10, 1, 2]) == "1-2, 10"


class TestVisionDonePlainMode:
    def _make_plain(self):
        p = Progress(total=3)
        p._tty = False
        return p

    def test_empty_events_prints_nothing(self, capsys):
        p = self._make_plain()
        p.vision_done([])
        captured = capsys.readouterr()
        assert captured.out == ""

    def test_pure_text_pages_label(self, capsys):
        p = self._make_plain()
        events = [
            {"page": 1, "page_class": "pure_text", "model_used": "skip", "char_count": 0},
            {"page": 2, "page_class": "pure_text", "model_used": "skip", "char_count": 0},
        ]
        p.vision_done(events)
        captured = capsys.readouterr()
        assert "pure-text" in captured.out
        assert "2 pages" in captured.out
        assert "1-2" in captured.out

    def test_glm_ocr_label_and_suffix(self, capsys):
        p = self._make_plain()
        events = [
            {"page": 5, "page_class": "structured_text", "model_used": "glm-ocr", "char_count": 400},
        ]
        p.vision_done(events)
        captured = capsys.readouterr()
        assert "structured" in captured.out
        assert "1 page" in captured.out
        assert "5" in captured.out
        assert "GLM-OCR" in captured.out

    def test_llava_label_and_suffix(self, capsys):
        p = self._make_plain()
        events = [
            {"page": 3, "page_class": "text_with_images", "model_used": "llava", "char_count": 250},
        ]
        p.vision_done(events)
        captured = capsys.readouterr()
        assert "image" in captured.out
        assert "LLaVA" in captured.out

    def test_mixed_strategies_all_present(self, capsys):
        p = self._make_plain()
        events = [
            {"page": 1, "page_class": "pure_text", "model_used": "skip", "char_count": 0},
            {"page": 2, "page_class": "structured_text", "model_used": "glm-ocr", "char_count": 300},
            {"page": 3, "page_class": "text_with_images", "model_used": "llava", "char_count": 200},
        ]
        p.vision_done(events)
        captured = capsys.readouterr()
        assert "pure-text" in captured.out
        assert "structured" in captured.out
        assert "image" in captured.out

    def test_display_order_skip_before_glm_before_llava(self, capsys):
        p = self._make_plain()
        events = [
            {"page": 3, "page_class": "text_with_images", "model_used": "llava", "char_count": 200},
            {"page": 1, "page_class": "pure_text", "model_used": "skip", "char_count": 0},
            {"page": 2, "page_class": "structured_text", "model_used": "glm-ocr", "char_count": 300},
        ]
        p.vision_done(events)
        out = capsys.readouterr().out
        skip_pos = out.index("pure-text")
        glm_pos = out.index("structured")
        llava_pos = out.index("image")
        assert skip_pos < glm_pos < llava_pos
