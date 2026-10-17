"""The symbol arm against a hostile repository, a hostile store and a ctags that misbehaves."""
from __future__ import annotations

import sqlite3
import stat
import sys
import time
from pathlib import Path

import pytest

from carta.graph import symbols
from carta.graph.build import build_graph, graph_status
from carta.graph.query import format_impact, run_impact
from carta.graph.settings import GraphConfigError, graph_settings
from carta.graph.store import EdgeStore, store_path
from carta.graph.symbol_scan import symbol_findings, words_in_code
from carta.graph.paths import PathIndex
from carta.graph.symbols import Ctags, SymbolsFailed, _file_field, parse_symbols, run_ctags
from carta.graph.tests.test_symbols import cfg_for, make_repo

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="needs a POSIX shell")
DOCS = {"docs/guide.md": "Call `render_table()` and `FrameParser.parse_frame()`.\n"}


def _script(path: Path, body: str) -> Path:
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def _tamper(root: Path, statement: str, *params) -> None:
    con = sqlite3.connect(str(store_path(root)))
    con.execute(statement, params)
    con.commit()
    con.close()


# -- ctags as a process ------------------------------------------------------------

@posix_only
def test_a_ctags_that_closes_its_output_and_lingers_is_still_stopped(tmp_path, monkeypatch):
    tool = _script(tmp_path / "lingering", "exec 1>&-\nsleep 30\n")
    monkeypatch.setattr(symbols, "CTAGS_TIMEOUT_SECONDS", 1)
    started = time.perf_counter()
    with pytest.raises(SymbolsFailed, match="did not finish"):
        run_ctags(tmp_path, Ctags(str(tool), "fake"), ["a.py"])
    assert time.perf_counter() - started < 15


@posix_only
def test_a_probe_whose_child_holds_the_pipe_ends_with_its_time_limit(tmp_path, monkeypatch):
    tool = _script(tmp_path / "wrapper", "sleep 30 &\nwait\n")
    monkeypatch.setattr(symbols.limits, "CTAGS_PROBE_TIMEOUT_SECONDS", 1)
    started = time.perf_counter()
    assert symbols._probe(str(tool), "--list-features") is None
    assert time.perf_counter() - started < 15


# -- a load that fails -------------------------------------------------------------

def test_a_failed_load_is_not_tried_again_until_its_input_changes(tmp_path, monkeypatch):
    root = make_repo(tmp_path / "proj", DOCS)
    index = root / "index" / "symbols.jsonl"
    index.write_bytes(b"this is not an index\n" * 3)
    calls = []
    real = symbols.parse_symbols
    monkeypatch.setattr(symbols, "parse_symbols", lambda *a, **k: calls.append(1) or real(*a, **k))
    first = build_graph(root, cfg_for())["symbols"]
    assert first["state"] == "failed" and len(calls) == 1
    again = build_graph(root, cfg_for())["symbols"]
    assert again["state"] == "failed" and again["detail"].startswith(first["detail"])
    assert "--force" in again["detail"] and len(calls) == 1
    assert "symbols_failed" in graph_status(root, cfg_for())["stale_reasons"]
    build_graph(root, cfg_for(), force=True)                # the caller can insist
    assert len(calls) == 2
    index.write_bytes(b"still not an index\n")              # and a changed input is read
    build_graph(root, cfg_for())
    assert len(calls) == 3


# -- stored state ------------------------------------------------------------------

def test_every_row_the_symbol_pass_writes_is_one_the_store_reads_back(tmp_path):
    root = make_repo(tmp_path / "proj", DOCS)
    first = build_graph(root, cfg_for())
    assert first["symbols"]["state"] == "ok" and first["symbols"]["edges"] > 0
    inode = store_path(root).stat().st_ino
    second = build_graph(root, cfg_for())
    assert (second["extracted"], second["symbols"]["unchanged"]) == (0, True)
    assert second["symbols"]["digest"] == first["symbols"]["digest"]
    assert store_path(root).stat().st_ino == inode          # not replaced as "not a store"
    assert graph_status(root, cfg_for())["stale"] is False
    assert EdgeStore.open_readonly(store_path(root)) is not None


PLANTED = {
    "doc state that is a list": ("UPDATE sources SET meta = '[]' WHERE extractor = 'symbol'",),
    "a count that is text": (
        "UPDATE sources SET meta = '{\"resolved\":\"many\"}' WHERE extractor = 'symbol'",),
    "ambiguous that is a number": (
        "UPDATE sources SET meta = '{\"ambiguous\":7}' WHERE extractor = 'symbol'",),
    "an ambiguous entry that is not a pair": (
        "UPDATE sources SET meta = '{\"ambiguous\":{\"tok\":5}}' WHERE extractor = 'symbol'",),
    "an ambiguous token with an escape": (
        "UPDATE sources SET meta = ? WHERE extractor = 'symbol'",
        '{"ambiguous":{"\\u001b[2Jtok":[1,2]}}'),
    "state nested past the parser": (
        "UPDATE sources SET meta = ? WHERE extractor = 'symbol'", "[" * 200_000),
    "rows in digits int() refuses": (
        "UPDATE meta SET value = ? WHERE key = 'symbol.rows'", "³"),
    "rows that are words": ("UPDATE meta SET value = 'plenty' WHERE key = 'symbol.rows'",),
    "a state that is none of ours": (
        "UPDATE meta SET value = ? WHERE key = 'symbol.state'", "\x1b[2Jok"),
}


@pytest.mark.parametrize("what", sorted(PLANTED))
def test_planted_symbol_state_is_rebuilt_not_trusted_and_not_a_crash(tmp_path, what):
    root = make_repo(tmp_path / "proj", DOCS)
    config = cfg_for()
    config["graph"]["scan"] = {"ambiguous_symbols": True}
    clean = build_graph(root, config)["symbols"]
    _tamper(root, *PLANTED[what])
    status = graph_status(root, config)                     # must not raise
    assert "symbols_changed" in status["stale_reasons"]
    assert "\x1b" not in str(status["symbols"])
    result = run_impact(root, config, paths=["src/pkg/table.py"], kinds=["symbol"], build=False)
    assert "\x1b" not in format_impact(result)
    report = build_graph(root, config)["symbols"]           # written again, by this code
    assert (report["state"], report["unchanged"]) == ("ok", False)
    assert report["digest"] == clean["digest"]
    assert graph_status(root, config)["stale"] is False
    findings, _stats = symbol_findings(root, config)
    assert "\x1b" not in str(findings)


def test_a_stored_detail_is_printed_with_its_control_characters_written_out(tmp_path):
    from carta.cli import _symbols_line
    root = make_repo(tmp_path / "proj", DOCS)
    build_graph(root, cfg_for())
    _tamper(root, "UPDATE meta SET value = ? WHERE key = 'symbol.detail'",
            "\x1b]0;owned\x07‮all good")
    status = graph_status(root, cfg_for())["symbols"]
    result = run_impact(root, cfg_for(), paths=["src/pkg/table.py"], build=False)
    for text in (status["detail"], _symbols_line(status), str(result.get("symbols"))):
        assert "\x1b" not in text and "\x07" not in text and "‮" not in text
    assert "\x1b" not in _symbols_line({"state": "failed", "detail": "\x1b[2J"})


# -- parsing ---------------------------------------------------------------------

def test_a_row_path_of_repeated_dot_slash_costs_its_length_once():
    line = b'{"name":"f","kind":"function","path":"' + b"./" * 8000 + b'a.py","line":1}\n'
    started = time.perf_counter()
    parsed = parse_symbols(line * 600)
    assert time.perf_counter() - started < 5.0
    assert len(parsed.rows) == 600 and parsed.rows[0].file == "a.py"
    assert _file_field("./././src/a.py") == "src/a.py"


def test_a_configured_symbols_file_is_measured_before_it_is_worked_on():
    started = time.perf_counter()
    with pytest.raises(GraphConfigError, match="graph.symbols.file"):
        graph_settings({"graph": {"symbols": {"file": "./" * 400_000 + "x"}}})
    assert time.perf_counter() - started < 2.0


def test_the_word_scan_finds_words_without_listing_every_word(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "big.py").write_text("alpha " * 200_000 + "needle_word\n")
    found, complete = words_in_code(root, PathIndex(["big.py"]), graph_settings({}),
                                    {"needle_word", "absent_word"})
    assert (found, complete) == ({"needle_word"}, True)
