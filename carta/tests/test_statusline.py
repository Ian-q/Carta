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
