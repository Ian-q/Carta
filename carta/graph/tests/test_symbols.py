"""The symbol arm: reading an index another tool wrote, and resolving identifiers with it."""
from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import pytest

from carta.graph import symbols
from carta.graph.build import build_graph, graph_status
from carta.graph.limits import MAX_SYMBOL_LINE_BYTES
from carta.graph.model import SYMBOL_EXTRACTOR, SYMBOL_EXTRACTOR_VERSION
from carta.graph.query import format_impact, run_impact
from carta.graph.settings import GraphConfigError, graph_settings
from carta.graph.store import EdgeStore, store_path
from carta.graph.symbols import (
    Mentioned, SymbolRow, SymbolsFailed, SymbolTable, parse_symbols, resolve_token,
)

FIXTURES = Path(__file__).parent / "fixtures" / "symbols"

CODE = {
    "src/pkg/frame.py": "class FrameParser:\n    MAX_LEN = 8\n\n    def parse_frame(self, data):\n"
                        "        return data\n\n\ndef parse_frame(data):\n    return data\n\n"
                        "DEFAULT_TIMEOUT = 3\n",
    "src/pkg/table.py": "import os as operating\n\ndef render_table(rows):\n    return rows\n\n"
                        "def init():\n    pass\n",
    "src/wire.c": "#define FRAME_MAX 8\nstruct frame_header { int len; };\n"
                  "static int init(void) { return 0; }\n",
}


def fixture_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def table_from(name: str = "symbols.jsonl", accept=None) -> SymbolTable:
    parsed = parse_symbols(fixture_bytes(name))
    return SymbolTable(parsed.rows, accept=accept)


def make_repo(root: Path, docs: dict, *, symbols_file: str = "symbols.jsonl") -> Path:
    for rel, text in {**CODE, **docs}.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    if symbols_file:
        target = root / "index" / symbols_file
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(fixture_bytes(symbols_file))
    return root


def cfg_for(symbols_file: str = "index/symbols.jsonl", **symbol_keys) -> dict:
    section = {"provider": "file", "file": symbols_file}
    section.update(symbol_keys)
    return {"graph": {"symbols": section}}


def symbol_edges(root: Path) -> list:
    with EdgeStore(store_path(root)) as store:
        return list(store.iter_edges(kinds=("symbol",)))


# -- reading an index ---------------------------------------------------------

def test_recorded_ctags_json_parses_to_rows():
    parsed = parse_symbols(fixture_bytes("ctags_sample.json"))
    assert SymbolRow("render_table", "function", "src/pkg/table.py", 3, "") in parsed.rows
    assert SymbolRow("parse_frame", "member", "src/pkg/frame.py", 5, "FrameParser") in parsed.rows
    # The pseudo-tag is not a symbol, and a heading with a backtick is not a name.
    assert all(row.name != "JSON_OUTPUT_VERSION" for row in parsed.rows)
    assert parsed.skipped == 1
    assert parsed.format == "jsonl"


def test_jsonl_tsv_and_ctags_json_agree():
    def code(name):
        return table_from(name, accept=lambda f: not f.endswith(".md")).rows()

    assert code("symbols.jsonl") == code("symbols.tsv") == code("ctags_sample.json")
    assert parse_symbols(fixture_bytes("symbols.tsv")).format == "tsv"


def test_table_fingerprint_ignores_row_order_and_duplicates():
    rows = parse_symbols(fixture_bytes("symbols.jsonl")).rows
    assert SymbolTable(rows).sha == SymbolTable(list(reversed(rows)) + rows[:3]).sha
    assert SymbolTable(rows).sha != SymbolTable(rows[1:]).sha


@pytest.mark.parametrize("line", [
    b'{"name": "x_y", "kind": "function", "file": "../outside.py", "line": 1}',
    b'{"name": "x_y", "kind": "function", "file": "/etc/passwd", "line": 1}',
    b'{"name": "x_y", "kind": "function", "file": "a//b.py", "line": 1}',
    b'{"name": "x_y", "kind": "function", "file": "a/\\u0000b.py", "line": 1}',
    b'{"name": "x_y", "kind": "function", "file": 7, "line": 1}',
    b'{"name": "", "kind": "function", "file": "a.py", "line": 1}',
    b'{"name": "has space", "kind": "function", "file": "a.py", "line": 1}',
    b'{"name": ["x_y"], "kind": "function", "file": "a.py", "line": 1}',
    b'["x_y", "function", "a.py", 1]',
    b'{"name": "x_y"',
    b"[" * 5000,
])
def test_a_bad_row_is_skipped_never_an_error(line):
    parsed = parse_symbols(line + b"\n" + b'{"name": "ok_name", "kind": "f", "file": "a.py", '
                           b'"line": 2}\n')
    assert [row.name for row in parsed.rows] == ["ok_name"]
    assert parsed.skipped == 1


def test_an_overlong_line_is_skipped_before_it_is_parsed(monkeypatch):
    def boom(_text):
        raise AssertionError("an over-long line reached the JSON parser")

    huge = b'{"name": "' + b"a" * (MAX_SYMBOL_LINE_BYTES + 1) + b'"}'
    monkeypatch.setattr(symbols.json, "loads", boom)
    parsed = parse_symbols(huge + b"\n")
    assert parsed.rows == [] and parsed.skipped == 1


def test_too_many_rows_is_a_failed_load_not_a_truncated_index(monkeypatch):
    monkeypatch.setattr(symbols, "MAX_SYMBOL_ROWS", 3)
    with pytest.raises(SymbolsFailed):
        parse_symbols(fixture_bytes("symbols.jsonl"))


def test_line_and_kind_are_normalised():
    data = (b'{"name": "a_b", "kind": "Function\\u001b[31m", "file": "./a.py", "line": -4}\n'
            b'{"name": "c_d", "kind": "METHOD", "file": "a.py", "line": true}\n'
            b'{"name": "ns::e_f", "kind": "function", "file": "a.py", "line": 9}\n')
    rows = parse_symbols(data).rows
    assert rows == [SymbolRow("a_b", "", "a.py", 0, ""), SymbolRow("c_d", "method", "a.py", 0, ""),
                    SymbolRow("e_f", "function", "a.py", 9, "ns")]


# -- resolving one identifier --------------------------------------------------

def test_a_unique_symbol_resolves_to_its_file():
    got = resolve_token(table_from(), "render_table", Mentioned(), 2)
    assert got.status == "resolved"
    assert [(r.file, r.line, r.kind) for r in got.rows] == [("src/pkg/table.py", 3, "function")]
    assert got.candidates == 1 and got.via == ""


def test_two_definitions_in_one_file_are_one_target():
    got = resolve_token(table_from(), "parse_frame", Mentioned(), 1)
    assert got.status == "resolved" and got.candidates == 1
    assert [(r.file, r.line) for r in got.rows] == [("src/pkg/frame.py", 5)]


def test_a_name_defined_in_more_files_than_the_limit_is_ambiguous():
    table = table_from()
    assert resolve_token(table, "init", Mentioned(), 1).status == "ambiguous"
    within = resolve_token(table, "init", Mentioned(), 2)
    assert within.status == "resolved" and within.candidates == 2
    assert [r.file for r in within.rows] == ["src/pkg/table.py", "src/wire.c"]


def test_a_path_the_doc_mentions_narrows_an_ambiguous_name():
    table = table_from()
    by_file = resolve_token(table, "init", Mentioned(files=("src/wire.c",)), 1)
    assert (by_file.status, by_file.via) == ("resolved", "mentioned")
    assert [r.file for r in by_file.rows] == ["src/wire.c"] and by_file.candidates == 2
    by_dir = resolve_token(table, "init", Mentioned(dirs=("src/pkg/",)), 1)
    assert [r.file for r in by_dir.rows] == ["src/pkg/table.py"]
    # A mention that covers every candidate narrows nothing.
    assert resolve_token(table, "init", Mentioned(dirs=("src/",)), 1).status == "ambiguous"


def test_a_qualifier_narrows_by_scope_then_by_file_name():
    table = table_from()
    scoped = resolve_token(table, "FrameParser.parse_frame", Mentioned(), 1)
    assert scoped.status == "resolved" and scoped.rows[0].line == 5
    stem = resolve_token(table, "table.init", Mentioned(), 1)
    assert (stem.status, stem.via) == ("resolved", "qualifier")
    assert [r.file for r in stem.rows] == ["src/pkg/table.py"]
    c_style = resolve_token(table, "wire::init", Mentioned(), 1)
    assert [r.file for r in c_style.rows] == ["src/wire.c"]


def test_a_foreign_qualifier_on_a_plain_word_does_not_resolve():
    table = SymbolTable([SymbolRow("run", "function", "src/job.py", 1, ""),
                         SymbolRow("replace_source", "method", "src/db.py", 4, "EdgeStore")])
    # `subprocess.run` is somebody else's `run`: the qualifier matches nothing here
    # and the leaf has no shape of its own to vouch for it.
    assert resolve_token(table, "subprocess.run", Mentioned(), 2).status == "unknown"
    # `store.replace_source` is an instance, not a scope, but the leaf is distinctive.
    got = resolve_token(table, "store.replace_source", Mentioned(), 2)
    assert (got.status, got.via) == ("resolved", "leaf")
    assert resolve_token(table, "run", Mentioned(), 2).status == "resolved"


def test_a_missing_member_falls_back_to_its_owner():
    got = resolve_token(table_from(), "FrameParser.decode_all", Mentioned(), 2)
    assert (got.status, got.via) == ("resolved", "owner")
    assert [(r.name, r.file) for r in got.rows] == [("FrameParser", "src/pkg/frame.py")]


def test_locals_parameters_and_import_aliases_are_not_definitions():
    table = SymbolTable([SymbolRow("row_count", "local", "a.py", 3, "f"),
                         SymbolRow("row_count", "parameter", "b.py", 3, "g"),
                         SymbolRow("np_alias", "namespace", "c.py", 1, "")])
    assert len(table) == 0
    assert resolve_token(table, "row_count", Mentioned(), 2).status == "unknown"
    assert resolve_token(table_from(), "operating", Mentioned(), 2).status == "unknown"


def test_an_external_name_is_unknown():
    assert resolve_token(table_from(), "QdrantClient", Mentioned(), 2).status == "unknown"


def test_alive_filters_rows_for_a_tree_that_lacks_the_file():
    got = resolve_token(table_from(), "init", Mentioned(), 1,
                        alive=lambda path: path != "src/wire.c")
    assert got.status == "resolved" and [r.file for r in got.rows] == ["src/pkg/table.py"]


# -- settings -------------------------------------------------------------------

def test_symbols_are_off_by_default_and_do_not_touch_the_mention_fingerprint():
    off = graph_settings({})
    on = graph_settings(cfg_for())
    assert off.symbols_provider == "none" and not off.scan_dangling_symbols
    assert not off.scan_ambiguous_symbols
    assert off.fingerprint() == on.fingerprint()
    assert off.symbol_fingerprint() != on.symbol_fingerprint()


@pytest.mark.parametrize("section", [
    {"provider": "auto"}, {"provider": "file", "file": "/etc/passwd"},
    {"provider": "file", "file": "../index.jsonl"}, {"provider": "file", "file": 3},
    {"provider": "file", "file": "a/../../b"}, {"max_definitions": 0},
    {"max_definitions": 1000}, {"max_definitions": True}, "ctags",
])
def test_bad_symbol_settings_are_config_errors(section):
    with pytest.raises(GraphConfigError):
        graph_settings({"graph": {"symbols": section}})


# -- build -----------------------------------------------------------------------

DOC = ("# Frames\n\nCall `render_table()` then `FrameParser.parse_frame`.\n"
       "`init()` runs first. `QdrantClient` is external.\n")


def test_build_writes_symbol_edges_with_the_identifier_as_key(tmp_path):
    root = make_repo(tmp_path, {"docs/frames.md": DOC})
    report = build_graph(root, cfg_for(max_definitions=1))
    sym = report["symbols"]
    assert sym["state"] == "ok" and sym["provider"] == "file"
    assert (sym["resolved"], sym["ambiguous"], sym["unknown"]) == (2, 1, 1)
    edges = symbol_edges(root)
    assert [(e.src, e.dst, e.dst_type, e.line, e.key) for e in edges] == [
        ("docs/frames.md", "src/pkg/frame.py", "file", 3, "FrameParser.parse_frame"),
        ("docs/frames.md", "src/pkg/table.py", "file", 3, "render_table"),
    ]
    first = edges[1]
    assert first.extractor == SYMBOL_EXTRACTOR
    assert first.extractor_version == SYMBOL_EXTRACTOR_VERSION
    assert first.weight == 1.0 and "render_table" in first.evidence
    assert json.loads(first.meta) == {"def_line": 3, "name": "render_table",
                                      "symbol_kind": "function"}
    assert report["by_kind"]["symbol"] == 2
    assert report["edges"] == sum(report["by_kind"].values())


def test_an_ambiguous_name_within_the_limit_is_split_between_its_files(tmp_path):
    root = make_repo(tmp_path, {"docs/frames.md": "`init()` runs first.\n"})
    build_graph(root, cfg_for(max_definitions=2))
    edges = symbol_edges(root)
    assert [(e.dst, e.weight) for e in edges] == [("src/pkg/table.py", 0.5), ("src/wire.c", 0.5)]
    assert all(json.loads(e.meta)["candidates"] == 2 for e in edges)


def test_a_mentioned_path_in_the_same_doc_picks_the_definition(tmp_path):
    root = make_repo(tmp_path, {"docs/wire.md": "See `src/wire.c`: `init()` runs first.\n"})
    build_graph(root, cfg_for(max_definitions=1))
    edges = symbol_edges(root)
    assert [(e.dst, e.weight) for e in edges] == [("src/wire.c", 1.0)]
    assert json.loads(edges[0].meta)["via"] == "mentioned"


def test_rebuild_is_a_no_op_and_a_fresh_store_has_the_same_digest(tmp_path):
    root = make_repo(tmp_path, {"docs/frames.md": DOC})
    cfg = cfg_for()
    first = build_graph(root, cfg)
    again = build_graph(root, cfg)
    assert again["symbols"]["written"] == 0 and again["symbols"]["unchanged"] is True
    assert again["symbols"]["digest"] == first["symbols"]["digest"] != ""
    assert graph_status(root, cfg)["stale"] is False
    os.unlink(store_path(root))
    assert build_graph(root, cfg)["symbols"]["digest"] == first["symbols"]["digest"]
    assert build_graph(root, cfg, force=True)["symbols"]["digest"] == first["symbols"]["digest"]


def test_a_changed_index_or_doc_updates_the_edges(tmp_path):
    root = make_repo(tmp_path, {"docs/frames.md": "`render_table()` here.\n"})
    cfg = cfg_for()
    build_graph(root, cfg)
    assert graph_status(root, cfg)["stale"] is False
    index = root / "index" / "symbols.jsonl"
    index.write_text('{"name": "render_table", "kind": "function", "file": "src/wire.c", '
                     '"line": 9}\n')
    status = graph_status(root, cfg)
    assert status["stale"] and "symbols_changed" in status["stale_reasons"]
    build_graph(root, cfg)
    assert [e.dst for e in symbol_edges(root)] == ["src/wire.c"]
    (root / "docs" / "frames.md").write_text("Nothing named now.\n")
    report = build_graph(root, cfg)
    assert symbol_edges(root) == [] and report["symbols"]["edges"] == 0


def test_definitions_in_docs_excluded_paths_and_unknown_files_are_dropped(tmp_path):
    root = make_repo(tmp_path, {"docs/frames.md": "`render_table()` and `gone_helper()` and "
                                                  "`vendored_fn()` and `Heading_Name`.\n"})
    (root / "node_modules" / "lib").mkdir(parents=True)
    (root / "node_modules" / "lib" / "v.js").write_text("function vendored_fn() {}\n")
    (root / "index" / "symbols.jsonl").write_text(
        '{"name": "render_table", "kind": "function", "file": "src/pkg/table.py", "line": 3}\n'
        '{"name": "gone_helper", "kind": "function", "file": "src/not_here.py", "line": 1}\n'
        '{"name": "vendored_fn", "kind": "function", "file": "node_modules/lib/v.js", "line": 1}\n'
        '{"name": "Heading_Name", "kind": "chapter", "file": "docs/frames.md", "line": 1}\n')
    report = build_graph(root, cfg_for())
    assert [e.dst for e in symbol_edges(root)] == ["src/pkg/table.py"]
    assert report["symbols"]["rows"] == 1 and report["symbols"]["rows_dropped"] == 3


def test_no_provider_means_no_symbol_edges_and_says_so(tmp_path):
    root = make_repo(tmp_path, {"docs/frames.md": DOC})
    build_graph(root, cfg_for())
    assert symbol_edges(root)
    report = build_graph(root, {})
    assert report["symbols"]["state"] == "off" and symbol_edges(root) == []
    status = graph_status(root, {})
    assert status["symbols"]["state"] == "off" and status["stale"] is False
    assert "symbol" not in status["by_kind"]


def test_a_configured_file_that_is_absent_is_unavailable_not_a_failure(tmp_path):
    root = make_repo(tmp_path, {"docs/frames.md": DOC}, symbols_file="")
    cfg = cfg_for()
    report = build_graph(root, cfg)
    assert report["symbols"]["state"] == "unavailable"
    assert "index/symbols.jsonl" in report["symbols"]["detail"]
    assert symbol_edges(root) == [] and graph_status(root, cfg)["stale"] is False
    no_file = build_graph(root, {"graph": {"symbols": {"provider": "file"}}})
    assert no_file["symbols"]["state"] == "unavailable"


def test_a_failed_load_keeps_the_edges_and_leaves_the_store_stale(tmp_path, monkeypatch):
    root = make_repo(tmp_path, {"docs/frames.md": DOC})
    cfg = cfg_for()
    before = build_graph(root, cfg)["symbols"]["digest"]
    kept = symbol_edges(root)
    with open(root / "index" / "symbols.jsonl", "ab") as handle:
        handle.write(b"\n")                         # a different file: it is loaded again
    monkeypatch.setattr(symbols, "MAX_SYMBOL_ROWS", 2)
    report = build_graph(root, cfg)
    assert report["symbols"]["state"] == "failed" and symbol_edges(root) == kept
    assert report["symbols"]["digest"] == before
    status = graph_status(root, cfg)
    assert status["stale"] and "symbols_failed" in status["stale_reasons"]
    monkeypatch.undo()
    assert build_graph(root, cfg)["symbols"]["state"] == "ok"
    assert graph_status(root, cfg)["stale"] is False


@pytest.mark.skipif(sys.platform == "win32", reason="needs symbolic links")
def test_a_symlinked_symbols_file_is_never_followed(tmp_path):
    outside = tmp_path / "outside.jsonl"
    outside.write_bytes(fixture_bytes("symbols.jsonl"))
    root = make_repo(tmp_path / "repo", {"docs/frames.md": DOC}, symbols_file="")
    (root / "index").mkdir()
    os.symlink(outside, root / "index" / "symbols.jsonl")
    report = build_graph(root, cfg_for())
    assert report["symbols"]["state"] == "failed" and symbol_edges(root) == []
    assert "symbolic link" in report["symbols"]["detail"]


# -- the ctags provider, without a ctags binary ------------------------------------

_FAKE_CTAGS = r'''#!{python}
import sys
args = sys.argv[1:]
if args[:1] != ["--options=NONE"]:
    sys.exit(3)
if "--list-features" in args:
    sys.stdout.write("#NAME  DESCRIPTION\njson   supports json format output\n")
    sys.exit(0)
if "--version" in args:
    sys.stdout.write("Universal Ctags 0.0.0(fake)\n")
    sys.exit(0)
listing = args[args.index("-L") + 1]
names = [line for line in open(listing).read().split("\n") if line]
open({log!r}, "w").write("\n".join(names))
sys.stdout.write(open({out!r}).read())
sys.exit({code})
'''

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="needs an executable script")


def fake_ctags(tmp_path: Path, *, code: int = 0, universal: bool = True,
               output: str = "ctags_sample.json") -> Path:
    tool = tmp_path / "tools" / "ctags"
    tool.parent.mkdir(exist_ok=True)
    if universal:
        body = _FAKE_CTAGS.format(python=sys.executable, log=str(tmp_path / "ctags.log"),
                                  out=str(FIXTURES / output), code=code)
    else:       # what a BSD ctags does with a long option
        body = f"#!{sys.executable}\nimport sys\nsys.stderr.write('illegal option\\n')\nsys.exit(1)\n"
    tool.write_text(body)
    tool.chmod(tool.stat().st_mode | stat.S_IXUSR)
    return tool


@posix_only
def test_ctags_is_recognised_by_what_it_can_do_not_by_its_name(tmp_path, monkeypatch):
    monkeypatch.setenv("CARTA_CTAGS", str(fake_ctags(tmp_path, universal=False)))
    assert symbols.find_universal_ctags(tmp_path / "repo") is None
    monkeypatch.setenv("CARTA_CTAGS", str(fake_ctags(tmp_path)))
    found = symbols.find_universal_ctags(tmp_path / "repo")
    assert found is not None and found.version == "Universal Ctags 0.0.0(fake)"


@posix_only
def test_the_ctags_provider_indexes_code_files_only(tmp_path, monkeypatch):
    monkeypatch.setenv("CARTA_CTAGS", str(fake_ctags(tmp_path)))
    root = make_repo(tmp_path / "repo", {"docs/frames.md": DOC, "README.md": "# Title\n"},
                     symbols_file="")
    os.symlink(root / "src" / "wire.c", root / "src" / "alias.c")
    (root / "src" / "odd\nname.py").write_text("x = 1\n")
    cfg = {"graph": {"symbols": {"provider": "ctags"}}}
    report = build_graph(root, cfg)
    assert report["symbols"]["state"] == "ok" and report["symbols"]["provider"] == "ctags"
    assert "0.0.0(fake)" in report["symbols"]["detail"]
    listed = (tmp_path / "ctags.log").read_text().split("\n")
    assert listed == ["./src/pkg/frame.py", "./src/pkg/table.py", "./src/wire.c"]
    # The fake also prints README.md headings: a doc is never a definition site.
    # (`init()` is defined in two files, so by default it makes no edge.)
    assert {e.dst for e in symbol_edges(root)} == {"src/pkg/frame.py", "src/pkg/table.py"}
    again = build_graph(root, cfg)
    assert again["symbols"]["unchanged"] is True
    (root / "src" / "wire.c").write_text("int other;\n")
    status = graph_status(root, cfg)
    assert "symbols_changed" in status["stale_reasons"]


@posix_only
def test_no_universal_ctags_is_unavailable_with_a_reason(tmp_path, monkeypatch):
    monkeypatch.setenv("CARTA_CTAGS", str(fake_ctags(tmp_path, universal=False)))
    root = make_repo(tmp_path / "repo", {"docs/frames.md": DOC}, symbols_file="")
    report = build_graph(root, {"graph": {"symbols": {"provider": "ctags"}}})
    assert report["symbols"]["state"] == "unavailable"
    assert "universal-ctags" in report["symbols"]["detail"]
    assert symbol_edges(root) == []


@posix_only
def test_a_ctags_run_that_fails_is_a_failed_load(tmp_path, monkeypatch):
    monkeypatch.setenv("CARTA_CTAGS", str(fake_ctags(tmp_path, code=2)))
    root = make_repo(tmp_path / "repo", {"docs/frames.md": DOC}, symbols_file="")
    cfg = {"graph": {"symbols": {"provider": "ctags"}}}
    report = build_graph(root, cfg)
    assert report["symbols"]["state"] == "failed"
    assert "symbols_failed" in graph_status(root, cfg)["stale_reasons"]


@posix_only
def test_a_ctags_inside_the_repository_is_never_run(tmp_path, monkeypatch):
    root = make_repo(tmp_path / "repo", {"docs/frames.md": DOC}, symbols_file="")
    planted = fake_ctags(root)
    monkeypatch.setenv("PATH", str(planted.parent) + os.pathsep + os.environ.get("PATH", ""))
    monkeypatch.delenv("CARTA_CTAGS", raising=False)
    found = symbols.find_universal_ctags(root)
    assert found is None or Path(found.path).resolve() != planted.resolve()
    assert not (root / "ctags.log").exists()


def test_real_universal_ctags_agrees_with_the_recorded_fixture(tmp_path, monkeypatch):
    """Runs only where universal-ctags is installed; everything else uses the recording."""
    monkeypatch.delenv("CARTA_CTAGS", raising=False)
    root = make_repo(tmp_path / "repo", {"docs/frames.md": DOC}, symbols_file="")
    found = symbols.find_universal_ctags(root)
    if found is None:
        pytest.skip("universal-ctags is not installed")
    parsed = symbols.run_ctags(root, found, sorted(CODE))
    live = SymbolTable(parsed.rows).rows()
    recorded = table_from("ctags_sample.json", accept=lambda f: not f.endswith(".md")).rows()
    assert [(r.name, r.kind, r.file, r.scope) for r in live] == [
        (r.name, r.kind, r.file, r.scope) for r in recorded]


# -- impact ----------------------------------------------------------------------

def test_impact_reads_symbol_edges_and_labels_them(tmp_path):
    root = make_repo(tmp_path, {"docs/frames.md": DOC, "docs/other.md": "Nothing here.\n"})
    cfg = cfg_for()
    plain = run_impact(root, cfg, paths=["src/pkg/table.py"], kinds=["mention"])
    assert plain["results"] == []
    result = run_impact(root, cfg, paths=["src/pkg/table.py"])
    assert result["query"]["kinds"] == ["mention", "symbol"]
    assert [r["doc"] for r in result["results"]] == ["docs/frames.md"]
    match = result["results"][0]["matches"][0]
    assert match["kind"] == "symbol" and match["target"] == "src/pkg/table.py"
    assert match["evidence"][0]["line"] == 3
    text = format_impact(result)
    assert "symbol" in text and "render_table" in text


def test_impact_without_a_provider_reads_mentions_only(tmp_path):
    root = make_repo(tmp_path, {"docs/frames.md": DOC})
    result = run_impact(root, {}, paths=["src/pkg/table.py"])
    assert result["query"]["kinds"] == ["mention"] and result["results"] == []


# -- review of #131: what the first version got wrong -------------------------------

def _row(name: str, file: str, kind: str = "function", **more) -> str:
    return json.dumps({"name": name, "kind": kind, "file": file, "line": 1, **more}) + "\n"


def test_a_code_file_added_or_deleted_is_noticed_without_touching_the_index(tmp_path):
    root = make_repo(tmp_path, {"docs/a.md": "`render_table()` and `later_thing()`.\n"})
    (root / "index" / "symbols.jsonl").write_text(
        _row("render_table", "src/pkg/table.py") + _row("later_thing", "src/later.py"))
    cfg = cfg_for()
    build_graph(root, cfg)
    assert [e.dst for e in symbol_edges(root)] == ["src/pkg/table.py"]
    assert graph_status(root, cfg)["stale"] is False

    os.unlink(root / "src" / "pkg" / "table.py")
    assert "symbols_changed" in graph_status(root, cfg)["stale_reasons"]
    report = build_graph(root, cfg)
    assert report["symbols"]["unchanged"] is False and symbol_edges(root) == []
    assert graph_status(root, cfg)["stale"] is False

    (root / "src" / "later.py").write_text("def later_thing():\n    pass\n")
    assert "symbols_changed" in graph_status(root, cfg)["stale_reasons"]
    build_graph(root, cfg)
    assert [(e.key, e.dst) for e in symbol_edges(root)] == [("later_thing", "src/later.py")]
    assert run_impact(root, cfg, paths=["src/later.py"])["matched_docs"] == 1
    assert build_graph(root, cfg)["symbols"]["unchanged"] is True


def test_a_key_in_a_data_file_is_not_a_definition():
    data = (_row("repo_root", "report.json", "string", language="JSON")
            + _row("qdrant_url", "conf/app.yaml", "anchor", language="Yaml")
            + _row("render_table", "src/t.py", language="Python")
            + _row("plain_row", "src/u.py"))
    parsed = parse_symbols(data.encode())
    assert [r.name for r in parsed.rows] == ["render_table", "plain_row"]
    assert parsed.skipped == 0          # rightly not rows, like a ctags pseudo-tag
    # ...and ctags is asked for the language of every tag it prints.
    assert "--fields=+nl" in symbols.ctags_command("ctags", "list")


def test_a_byte_order_mark_or_a_comment_does_not_hide_the_format():
    rows = _row("one_fn", "a.py") + _row("two_fn", "b.py")
    bom = b"\xef\xbb\xbf"
    got = parse_symbols(bom + rows.encode())
    assert (got.format, len(got.rows), got.skipped) == ("jsonl", 2, 0)
    got = parse_symbols(b"# made by a tool\n\n" + rows.encode())
    assert (got.format, len(got.rows), got.skipped) == ("jsonl", 2, 0)
    got = parse_symbols(bom + b"one_fn\tfunction\ta.py\t1\ntwo_fn\tfunction\tb.py\t2\n")
    assert (got.format, len(got.rows), got.skipped) == ("tsv", 2, 0)


@pytest.mark.parametrize("garbage", [
    b"\x00\x01\x02" * 100,
    (_row("one_fn", "src/wire.c") * 3).encode("utf-16"),
    b"this is not\nan index at all\n",
], ids=["binary", "utf-16", "prose"])
def test_a_file_with_lines_and_no_row_is_a_failed_load(tmp_path, garbage):
    root = make_repo(tmp_path, {"docs/frames.md": DOC})
    cfg = cfg_for()
    build_graph(root, cfg)
    kept = symbol_edges(root)
    assert kept
    (root / "index" / "symbols.jsonl").write_bytes(garbage)
    report = build_graph(root, cfg)
    assert report["symbols"]["state"] == "failed" and "no row" in report["symbols"]["detail"]
    assert symbol_edges(root) == kept
    assert "symbols_failed" in graph_status(root, cfg)["stale_reasons"]


def test_an_empty_symbols_file_is_an_empty_index(tmp_path):
    root = make_repo(tmp_path, {"docs/frames.md": DOC})
    (root / "index" / "symbols.jsonl").write_bytes(b"\n# nothing yet\n")
    report = build_graph(root, cfg_for())
    assert report["symbols"]["state"] == "ok" and report["symbols"]["rows"] == 0
    assert symbol_edges(root) == []


def test_a_missing_member_falls_back_to_a_class_whatever_its_shape():
    table = SymbolTable([SymbolRow("Parser", "class", "p.py", 1, ""),
                         SymbolRow("os", "variable", "shim.py", 1, ""),
                         SymbolRow("Shape", "class", "a.py", 1, ""),
                         SymbolRow("Shape", "class", "b.py", 1, "")])
    got = resolve_token(table, "Parser.gone_method", Mentioned(), 2)
    assert (got.status, got.via, [r.file for r in got.rows]) == ("resolved", "owner", ["p.py"])
    assert resolve_token(table, "x.y.Parser.zap", Mentioned(), 2).status == "resolved"
    # A plain word that is not a type vouches for nothing ...
    assert resolve_token(table, "os.gone_call", Mentioned(), 2).status == "unknown"
    # ... and neither does a plain type name with two homes.
    assert resolve_token(table, "Shape.gone_method", Mentioned(), 2).status == "unknown"


def test_a_standard_library_call_never_resolves_to_a_local_namesake():
    table = SymbolTable([SymbolRow("check_output", "function", "util/proc.py", 1, ""),
                         SymbolRow("replace_source", "method", "src/db.py", 4, "EdgeStore")])
    assert resolve_token(table, "subprocess.check_output", Mentioned(), 2).status == "unknown"
    assert resolve_token(table, "os.path.check_output", Mentioned(), 2).status == "unknown"
    got = resolve_token(table, "store.replace_source", Mentioned(), 2)
    assert (got.status, got.via) == ("resolved", "leaf")
    assert resolve_token(table, "check_output", Mentioned(), 2).status == "resolved"
    # A local file or scope of that name still wins over the list.
    local = SymbolTable([SymbolRow("check_output", "function", "subprocess/run.py", 1, "")])
    assert resolve_token(local, "subprocess.check_output", Mentioned(), 2).status == "resolved"


_WRAPPER_CTAGS = r'''#!{python}
import subprocess, sys, time
args = sys.argv[1:]
if "--list-features" in args:
    sys.stdout.write("json   supports json format output\n"); sys.exit(0)
if "--version" in args:
    sys.stdout.write("Universal Ctags 0.0.0(fake)\n"); sys.exit(0)
# A wrapper: the real work is a child that keeps this stdout open.
subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
time.sleep(120)
'''


@posix_only
def test_the_ctags_time_limit_holds_for_a_wrapper_that_starts_a_child(tmp_path, monkeypatch):
    import time
    tool = tmp_path / "tools" / "ctags"
    tool.parent.mkdir()
    tool.write_text(_WRAPPER_CTAGS.format(python=sys.executable))
    tool.chmod(tool.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("CARTA_CTAGS", str(tool))
    monkeypatch.setattr(symbols, "CTAGS_TIMEOUT_SECONDS", 1)
    root = make_repo(tmp_path / "repo", {"docs/frames.md": DOC}, symbols_file="")
    found = symbols.find_universal_ctags(root)
    started = time.monotonic()
    with pytest.raises(SymbolsFailed) as err:
        symbols.run_ctags(root, found, sorted(CODE))
    # Without the fix this returns only when the child does, two minutes on.
    assert "did not finish" in str(err.value) and time.monotonic() - started < 60


def test_impact_says_so_when_the_symbol_edges_it_read_may_be_out_of_date(tmp_path, monkeypatch):
    root = make_repo(tmp_path, {"docs/frames.md": DOC})
    cfg = cfg_for()
    fresh = run_impact(root, cfg, paths=["src/pkg/table.py"])
    assert fresh["symbols"] == {"provider": "file", "state": "ok", "stale": False,
                                "detail": "index/symbols.jsonl (jsonl)"}
    assert "out of date" not in format_impact(fresh)
    with open(root / "index" / "symbols.jsonl", "ab") as handle:
        handle.write(b"\n")
    monkeypatch.setattr(symbols, "MAX_SYMBOL_ROWS", 2)
    for build in (True, False):
        result = run_impact(root, cfg, paths=["src/pkg/table.py"], build=build)
        assert [r["doc"] for r in result["results"]] == ["docs/frames.md"]
        assert result["symbols"]["state"] == "failed" and result["symbols"]["stale"] is True
        text = format_impact(result)
        assert "out of date" in text and "could not be loaded" in text
    # An arm that reads no symbol edges has nothing to say about them.
    assert "symbols" not in run_impact(root, cfg, paths=["src/pkg/table.py"], kinds=["mention"])
