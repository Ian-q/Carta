import re

from carta import statusline as sl

_STRIP = re.compile(r"\x1b\[[0-9;]*m")


def _plain(s):
    return _STRIP.sub("", s)


def _running_status(**over):
    base = {
        "schema": 1, "phase": "running", "host": "h", "pid": 123,
        "total": 47, "current_idx": 24, "current_file": "EN_UM_N32WB03x.pdf",
        "current_file_started_at": 1000.0, "updated_at": 1000.0,
        "started_at": 0.0, "finished_at": None,
        "embedded": 23, "skipped": 1, "errors": 0, "chunks": 2334,
    }
    base.update(over)
    return base


def test_fmt_elapsed():
    assert sl._fmt_elapsed(4) == "4s"
    assert sl._fmt_elapsed(125) == "2m"
    assert sl._fmt_elapsed(3 * 3600 + 5 * 60) == "3h5m"
    assert sl._fmt_elapsed(-3) == "0s"


def test_fmt_chunks():
    assert sl._fmt_chunks(0) == "0"
    assert sl._fmt_chunks(999) == "999"
    assert sl._fmt_chunks(2334) == "2.3k"


def test_resolve_running_alive():
    st = _running_status()
    assert sl.resolve_state(st, now=1000.0, hostname="h",
                            pid_alive_fn=lambda p: True) == "running"


def test_resolve_running_dead_pid_is_idle():
    st = _running_status()
    assert sl.resolve_state(st, now=1000.0, hostname="h",
                            pid_alive_fn=lambda p: False) == "idle"


def test_resolve_running_other_host_uses_updated_at():
    st = _running_status(host="other", updated_at=1000.0)
    # within window -> running; pid_alive_fn must NOT be consulted for other host
    assert sl.resolve_state(st, now=1030.0, hostname="h",
                            pid_alive_fn=lambda p: (_ for _ in ()).throw(AssertionError())) == "running"
    assert sl.resolve_state(st, now=2000.0, hostname="h",
                            pid_alive_fn=lambda p: False) == "idle"


def test_resolve_done_flash_then_idle():
    st = _running_status(phase="done", finished_at=1000.0)
    assert sl.resolve_state(st, now=1010.0, hostname="h", pid_alive_fn=lambda p: True) == "done"
    assert sl.resolve_state(st, now=1100.0, hostname="h", pid_alive_fn=lambda p: True) == "idle"


def test_resolve_failed_flash():
    st = _running_status(phase="failed", finished_at=1000.0)
    assert sl.resolve_state(st, now=1010.0, hostname="h", pid_alive_fn=lambda p: True) == "failed"


def test_format_running_plain():
    st = _running_status()
    out = sl.format_segment(st, "running", now=1000.0 + 19 * 60, color=False)
    # spinner frame varies with now; assert the stable parts:
    assert "carta 24/47" in out
    assert "EN_UM_N32WB03x.pdf" in out
    assert "19m" in out


def test_format_long_filename_truncated():
    st = _running_status(current_file="a-really-extremely-long-document-name-that-overflows.pdf")
    out = sl.format_segment(st, "running", now=1000.0, color=False)
    assert "…" in out


def test_format_done_plain():
    st = _running_status(phase="done", embedded=45, skipped=1, chunks=2334)
    out = sl.format_segment(st, "done", now=1.0, color=False)
    assert out == "✓ carta 46 files · 2.3k chunks"


def test_format_failed_plain():
    st = _running_status(phase="failed", current_idx=12, total=47, errors=3)
    out = sl.format_segment(st, "failed", now=1.0, color=False)
    assert out == "✗ carta 12/47 · 3 errors"


def test_format_idle_is_empty():
    assert sl.format_segment(_running_status(), "idle", now=1.0, color=False) == ""


def test_format_color_includes_ansi():
    st = _running_status()
    out = sl.format_segment(st, "running", now=1000.0, color=True)
    assert "\x1b[" in out
    assert "carta 24/47" in _plain(out)


# ---------------------------------------------------------------------------
# Task 5: IO functions
# ---------------------------------------------------------------------------
import io
import json as _json
import os
import socket
import sys


def test_read_status_walks_up(tmp_path):
    (tmp_path / ".carta").mkdir()
    (tmp_path / ".carta" / "embed-status.json").write_text('{"phase": "running"}')
    sub = tmp_path / "a" / "b"
    sub.mkdir(parents=True)
    assert sl.read_status(sub) == {"phase": "running"}


def test_read_status_missing_returns_none(tmp_path):
    assert sl.read_status(tmp_path) is None


def test_read_status_corrupt_returns_none(tmp_path):
    (tmp_path / ".carta").mkdir()
    (tmp_path / ".carta" / "embed-status.json").write_text("{not json")
    assert sl.read_status(tmp_path) is None


def test_pid_alive_self_true():
    assert sl._pid_alive(os.getpid()) is True


def test_pid_alive_dead_false():
    # PID 2**31-1 is essentially never a live process
    assert sl._pid_alive(2**31 - 1) is False


def test_print_segment_running(tmp_path, monkeypatch, capsys):
    (tmp_path / ".carta").mkdir()
    status = {
        "schema": 1, "phase": "running", "host": socket.gethostname(),
        "pid": os.getpid(), "total": 47, "current_idx": 24,
        "current_file": "big.pdf", "current_file_started_at": 0.0,
        "updated_at": 0.0, "finished_at": None, "embedded": 0,
        "skipped": 0, "errors": 0, "chunks": 0,
    }
    (tmp_path / ".carta" / "embed-status.json").write_text(_json.dumps(status))
    stdin = io.StringIO(_json.dumps({"workspace": {"current_dir": str(tmp_path)}}))
    monkeypatch.setattr(sys, "stdin", stdin)
    sl.print_segment()
    assert "carta 24/47" in _STRIP.sub("", capsys.readouterr().out)


def test_print_segment_no_status_is_empty(tmp_path, monkeypatch, capsys):
    stdin = io.StringIO(_json.dumps({"cwd": str(tmp_path)}))
    monkeypatch.setattr(sys, "stdin", stdin)
    sl.print_segment()
    assert capsys.readouterr().out == ""


def test_print_segment_never_raises_on_garbage(monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", io.StringIO("this is not json"))
    sl.print_segment()  # must not raise
    assert capsys.readouterr().out == ""
