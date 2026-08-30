"""A cut-short scan must not read as a clean one (#121 follow-up).

`max_judge_calls` bounds cost, so overflow is expected — but it means the rest of
the diff was never looked at. A scan that burns its budget, finds nothing, and
skips the remainder produced byte-identical output to a scan that genuinely found
nothing. The stale-scan design doc states the requirement explicitly ("No silent
caps: max_judge_calls overflow is surfaced"); these pin it.
"""
from pathlib import Path

from carta.hook.stale_scan import StaleScanResult


def test_overflow_is_reported_even_when_there_are_no_findings(capsys):
    from carta.cli import _print_stale_result

    result = StaleScanResult(scanned=3, judge_calls=30, skipped_overflow=17)
    _print_stale_result(result, {})

    err = capsys.readouterr().err
    assert "17" in err and "max_judge_calls" in err, (
        f"a scan cut short by its judge budget reported nothing:\n{err!r}"
    )


def test_truncated_candidates_are_reported_even_when_there_are_no_findings(capsys):
    from carta.cli import _print_stale_result

    result = StaleScanResult(scanned=1, candidates_truncated=4)
    _print_stale_result(result, {})

    err = capsys.readouterr().err
    assert "4" in err, f"silent retrieval-depth ceiling:\n{err!r}"


def test_clean_scan_stays_quiet(capsys):
    """The counterweight: nothing to report must still print nothing, or the
    pre-push hook becomes noise on every push."""
    from carta.cli import _print_stale_result

    _print_stale_result(StaleScanResult(scanned=3), {})
    assert capsys.readouterr().err == ""


def test_scan_claude_md_propagates_overflow(monkeypatch, tmp_path):
    """`carta claude-md check` emits this dict as JSON and the /claude-md-sync
    skill stops on empty findings. Without an overflow field it cannot tell a
    synced CLAUDE.md from one whose scan ran out of budget partway through."""
    import carta.hook.claude_md as claude_md

    monkeypatch.setattr(
        claude_md, "run_stale_scan",
        lambda *a, **k: StaleScanResult(scanned=1, judge_calls=30,
                                        skipped_overflow=9, candidates_truncated=2),
    )

    md = tmp_path / "CLAUDE.md"
    md.write_text("# T\n\n## A\nbody a\n\n## B\nbody b\n")
    out = claude_md.scan_claude_md(tmp_path, {"embed": {"chunking": {"max_tokens": 400}}},
                                   search_fn=lambda q: [], judge_fn=lambda s, c: False)

    assert out.get("skipped_overflow") == 9, (
        f"overflow dropped before the JSON consumer: {out}"
    )
    assert out.get("candidates_truncated") == 2
