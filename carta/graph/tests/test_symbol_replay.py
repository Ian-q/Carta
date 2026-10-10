"""The symbol arm of the replay: the index a commit's parent had, or an explicit leak."""
from __future__ import annotations

import json
import stat
import sys
from pathlib import Path

import pytest

from carta.graph.eval.replay import ReplayError, format_replay, main, run_replay
from carta.graph.eval.symbol_replay import SymbolSourceError, make_source
from carta.graph.settings import DEFAULT_SETTINGS
from carta.graph.tests.conftest import requires_git

pytestmark = requires_git

OPTS = dict(use_disk_cache=False, resamples=50, per_commit=True,
            arms={"symbol": ["symbol"], "mention": ["mention"]})
SYMBOL = "graph[symbol]"


def index_line(name: str, file: str) -> str:
    return json.dumps({"name": name, "kind": "function", "file": file, "line": 1}) + "\n"


def build_history(repo) -> dict:
    """``render_table`` lives in table.py, then moves to grid.py; the doc names it throughout."""
    c = {}
    repo.write("src/table.py", "def render_table(rows):\n    return rows\n")
    repo.write("src/grid.py", "GRID = 1\n")
    repo.write("docs/tables.md", "# Tables\n\nCall `render_table()` to print one.\n")
    repo.write("docs/other.md", "# Other\n\nNothing named.\n")
    repo.write("index/symbols.jsonl", index_line("render_table", "src/table.py"))
    c["root"] = repo.commit("root", 0)

    repo.write("src/table.py", "def render_table(rows):\n    return list(rows)\n")
    repo.write("docs/tables.md", "# Tables\n\nCall `render_table()` to print one. v2\n")
    c["table"] = repo.commit("table and its doc", 1)

    repo.write("src/table.py", "TABLE = 1\n")
    repo.write("src/grid.py", "GRID = 1\n\ndef render_table(rows):\n    return list(rows)\n")
    repo.write("index/symbols.jsonl", index_line("render_table", "src/grid.py"))
    c["move"] = repo.commit("move render_table to grid", 2)

    repo.write("src/grid.py", "GRID = 2\n\ndef render_table(rows):\n    return list(rows)\n")
    repo.write("docs/tables.md", "# Tables\n\nCall `render_table()` to print one. v3\n")
    c["grid"] = repo.commit("grid and the doc", 3)
    return c


def ranks(report: dict) -> dict:
    return {row["commit"]: row["ranks"][SYMBOL] for row in report["per_commit"]}


@pytest.fixture
def history(git_repo):
    git_repo.commits = build_history(git_repo)
    return git_repo


def test_a_symbol_arm_without_an_index_is_refused_with_what_to_pass(history, capsys):
    with pytest.raises(ReplayError) as err:
        run_replay(history.root, **OPTS)
    assert "--symbols ctags" in str(err.value) and "--accept-symbol-leak" in str(err.value)
    assert main(["--repo", str(history.root), "--no-cache", "--arm", "s=symbol"]) == 1
    assert "symbol index" in capsys.readouterr().err


def test_a_tracked_index_is_read_as_of_each_parent(history):
    source = make_source(history.root, "tracked:index/symbols.jsonl", accept_leak=False,
                         settings=DEFAULT_SETTINGS)
    report = run_replay(history.root, symbols=source, **OPTS)
    c = history.commits
    got = ranks(report)
    # At each scored commit the parent's index says where render_table was then.
    assert got[c["table"]] == 1 and got[c["grid"]] == 1
    assert report["symbols"] == {
        "source": "tracked:index/symbols.jsonl", "leak": False, "max_definitions": 1,
        "note": "the symbol index is rebuilt for each commit's parent; nothing later is seen"}
    text = format_replay(report)
    assert "Symbols: tracked:index/symbols.jsonl" in text and "WARNING" not in text


def test_todays_file_needs_the_leak_to_be_accepted_and_the_report_says_so(history, tmp_path):
    today = tmp_path / "today.jsonl"
    today.write_text(index_line("render_table", "src/grid.py"))
    with pytest.raises(SymbolSourceError) as err:
        make_source(history.root, f"file:{today}", accept_leak=False, settings=DEFAULT_SETTINGS)
    assert "--accept-symbol-leak" in str(err.value)
    source = make_source(history.root, f"file:{today}", accept_leak=True,
                         settings=DEFAULT_SETTINGS)
    report = run_replay(history.root, symbols=source, **OPTS)
    c = history.commits
    got = ranks(report)
    # Today's index knows only where the symbol is now: the earlier commit,
    # which the per-parent index gets right, is missed.
    assert got[c["table"]] is None and got[c["grid"]] == 1
    assert report["symbols"]["leak"] is True and report["symbols"]["definitions"] == 1
    assert report["symbols"]["source"] == "file:today.jsonl"
    assert "WARNING, leaks" in format_replay(report)


def test_reports_without_a_symbol_arm_are_unchanged(history):
    plain = run_replay(history.root, use_disk_cache=False, resamples=50)
    assert "symbols" not in plain and "Symbols:" not in format_replay(plain)


def test_the_command_line_runs_a_symbol_arm_and_refuses_a_silent_leak(history, tmp_path, capsys):
    out = tmp_path / "report.json"
    code = main(["--repo", str(history.root), "--no-cache", "--no-config", "--resamples", "50",
                 "--arm", "both=mention+symbol", "--symbols", "tracked:index/symbols.jsonl",
                 "--max-definitions", "2", "--json", str(out)])
    assert code == 0
    report = json.loads(out.read_text())
    assert report["settings"]["arms"] == {"both": ["mention", "symbol"]}
    assert report["symbols"]["max_definitions"] == 2
    capsys.readouterr()
    today = tmp_path / "today.tsv"
    today.write_text("render_table\tfunction\tsrc/grid.py\t3\n")
    base = ["--repo", str(history.root), "--no-cache", "--arm", "s=symbol"]
    assert main([*base, "--symbols", f"file:{today}"]) == 1
    assert "--accept-symbol-leak" in capsys.readouterr().err
    assert main([*base, "--symbols", f"file:{today}", "--accept-symbol-leak"]) == 0
    assert "WARNING, leaks" in capsys.readouterr().out
    assert main([*base, "--accept-symbol-leak"]) == 1
    assert main([*base, "--symbols", "nonsense"]) == 1
    assert main([*base, "--symbols", "tracked:../x"]) == 1
    assert main([*base, "--symbols", "tracked:index/symbols.jsonl",
                 "--max-definitions", "0"]) == 1


@pytest.mark.parametrize("spec", ["tracked:", "tracked:/abs/path", "tracked:a/../b",
                                  "tracked:a\nb", "file:", "unknown:x", ""])
def test_a_bad_symbol_source_is_an_error_not_a_guess(history, spec):
    with pytest.raises(SymbolSourceError):
        make_source(history.root, spec, accept_leak=True, settings=DEFAULT_SETTINGS)


def test_a_symlinked_symbols_file_is_refused(history, tmp_path):
    real = tmp_path / "real.jsonl"
    real.write_text(index_line("render_table", "src/grid.py"))
    link = tmp_path / "link.jsonl"
    link.symlink_to(real)
    with pytest.raises(SymbolSourceError) as err:
        make_source(history.root, f"file:{link}", accept_leak=True, settings=DEFAULT_SETTINGS)
    assert "symlink" in str(err.value)


# A stand-in for universal-ctags: it answers the capability probe and "tags"
# every `def name(` in the files it is given. No test needs the real binary.
_FAKE = r'''#!{python}
import json, re, sys
args = sys.argv[1:]
if "--list-features" in args:
    print("json   supports json format output"); sys.exit(0)
if "--version" in args:
    print("Universal Ctags 0.0.0(fake), test"); sys.exit(0)
with open({log!r}, "a") as log:
    log.write("run\n")
for name in open(args[args.index("-L") + 1]).read().split("\n"):
    if not name:
        continue
    for n, line in enumerate(open(name).read().split("\n"), 1):
        m = re.match(r"def (\w+)\(", line)
        if m:
            print(json.dumps({{"_type": "tag", "name": m.group(1), "path": name, "line": n,
                              "kind": "function"}}))
'''


@pytest.mark.skipif(sys.platform == "win32", reason="needs an executable script")
def test_ctags_is_rerun_on_each_parent_from_git_objects(history, tmp_path, monkeypatch):
    tool = tmp_path / "bin" / "fake-ctags"
    tool.parent.mkdir()
    log = tmp_path / "runs.log"
    tool.write_text(_FAKE.format(python=sys.executable, log=str(log)))
    tool.chmod(tool.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("CARTA_CTAGS", str(tool))
    # The work tree is not what is indexed: break it and nothing changes.
    (history.root / "src" / "grid.py").write_text("def something_else(): pass\n")
    source = make_source(history.root, "ctags", accept_leak=False, settings=DEFAULT_SETTINGS)
    report = run_replay(history.root, symbols=source, **OPTS)
    c = history.commits
    got = ranks(report)
    assert got[c["table"]] == 1 and got[c["grid"]] == 1
    assert report["symbols"]["leak"] is False
    assert report["symbols"]["source"] == "ctags (Universal Ctags 0.0.0(fake))"
    # Indexed once per content: the three code files of the first tree, then
    # only what each later parent changed (1, then 3). Three full trees are 9.
    assert source.indexed_blobs == 7
    assert not list(Path(history.root).glob("**/carta-replay-ctags-*"))


def test_ctags_source_without_a_binary_says_what_else_to_pass(history, tmp_path, monkeypatch):
    monkeypatch.setenv("CARTA_CTAGS", str(tmp_path / "no-such-binary"))
    with pytest.raises(SymbolSourceError) as err:
        make_source(history.root, "ctags", accept_leak=False, settings=DEFAULT_SETTINGS)
    assert "universal-ctags" in str(err.value) and "tracked:PATH" in str(err.value)
