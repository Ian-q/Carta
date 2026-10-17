"""The CLI surface of the graph arm: ``carta graph build|status`` and ``carta impact``."""
from __future__ import annotations

import json
import socket
from pathlib import Path

import pytest

from carta import cli
from carta.graph.store import store_path
from carta.graph.tests.conftest import requires_git

CONFIG = ("project_name: demo\n"
          "qdrant_url: http://127.0.0.1:9\n"
          "embed:\n  ollama_url: http://127.0.0.1:9\n")


def write(root: Path, rel: str, text: str = "") -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))
    return path


def fill(root: Path, config: str = CONFIG) -> Path:
    write(root, ".carta/config.yaml", config)
    write(root, "README.md", "Start with `docs/parser.md`.\n")
    write(root, "docs/parser.md",
          "# Parser\n\nThe framing rules live in `src/parser/frame.py`.\n"
          "See [frame.py](../src/parser/frame.py) for the state table.\n")
    write(root, "docs/overview.md", "Everything under `src/parser/` is covered here.\n")
    write(root, "src/parser/frame.py", "x = 1\n")
    write(root, "src/parser/checksum.py", "y = 2\n")
    write(root, "tools/gen_tables.py", "z = 3\n")
    return root


@pytest.fixture
def project(tmp_path, monkeypatch):
    root = fill(tmp_path / "proj")
    monkeypatch.chdir(root)
    return root


def run(monkeypatch, capsys, *argv: str):
    """Run ``carta <argv>`` in-process; returns (exit code, stdout, stderr)."""
    monkeypatch.setattr("sys.argv", ["carta", *argv])
    code = 0
    try:
        cli.main()
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_graph_build_prints_summary_and_json(project, monkeypatch, capsys):
    code, out, err = run(monkeypatch, capsys, "graph", "build")
    assert (code, err) == (0, "")
    assert out == "Graph: 3 docs, 4 edges (3 re-extracted, 0 unchanged)\n"
    code, out, _ = run(monkeypatch, capsys, "graph", "build", "--json")
    report = json.loads(out)
    assert code == 0 and report["docs"] == 3 and report["extracted"] == 0
    assert report["unchanged"] == 3 and report["by_kind"] == {"mention": 4}


def test_graph_build_force(project, monkeypatch, capsys):
    run(monkeypatch, capsys, "graph", "build")
    code, out, _ = run(monkeypatch, capsys, "graph", "build", "--force")
    assert code == 0
    assert out == "Graph: 3 docs, 4 edges (3 re-extracted, 0 unchanged)\n"


def test_graph_build_when_disabled(tmp_path, monkeypatch, capsys):
    root = fill(tmp_path / "proj", CONFIG + "graph:\n  enabled: false\n")
    monkeypatch.chdir(root)
    code, out, _ = run(monkeypatch, capsys, "graph", "build")
    assert code == 0 and "disabled" in out
    assert not store_path(root).exists()


def test_graph_status_before_and_after_build(project, monkeypatch, capsys):
    code, out, _ = run(monkeypatch, capsys, "graph", "status", "--json")
    status = json.loads(out)
    assert code == 0 and status["exists"] is False and status["stale_reasons"] == ["no_store"]
    assert not store_path(project).exists()               # status never creates the store
    code, out, _ = run(monkeypatch, capsys, "graph", "status")
    assert code == 0 and "No edge store yet" in out and "carta graph build" in out

    run(monkeypatch, capsys, "graph", "build")
    code, out, _ = run(monkeypatch, capsys, "graph", "status", "--json")
    status = json.loads(out)
    assert set(status) == {"enabled", "exists", "schema_version", "extractor_version", "docs",
                           "edges", "by_kind", "by_dst_type", "digest", "stale", "stale_reasons"}
    assert status["exists"] and not status["stale"] and status["docs"] == 3
    code, out, _ = run(monkeypatch, capsys, "graph", "status")
    assert out.splitlines()[0] == "Graph: 3 docs, 4 edges, up to date"
    assert "  by kind: mention 4" in out
    assert "  by target: dir 1, doc 1, file 2" in out

    write(project, "docs/new.md", "Also `src/parser/frame.py`.\n")
    code, out, _ = run(monkeypatch, capsys, "graph", "status")
    assert "stale (paths_changed, docs_changed)" in out.splitlines()[0]


def test_graph_without_a_subcommand_prints_usage_and_exits_1(project, monkeypatch, capsys):
    code, out, err = run(monkeypatch, capsys, "graph")
    assert code == 1 and "usage: carta graph" in err and out == ""


def test_impact_paths_human_and_json(project, monkeypatch, capsys):
    code, out, err = run(monkeypatch, capsys, "impact", "src/parser/frame.py")
    assert (code, err) == (0, "")
    assert out.splitlines()[0] == "2 docs mention 1 path (3 docs, 4 edges)"
    assert " 1. docs/parser.md  score 2.347200" in out
    assert "        L3: The framing rules live in `src/parser/frame.py`." in out

    code, out, err = run(monkeypatch, capsys, "impact", "src/parser/frame.py", "--json")
    result = json.loads(out)                               # the object and nothing else
    assert (code, err) == (0, "")
    assert set(result) == {"schema", "query", "store", "matched_docs", "results",
                           "unmatched_paths", "skipped_paths"}
    assert [r["doc"] for r in result["results"]] == ["docs/parser.md", "docs/overview.md"]


def test_impact_with_no_results_exits_zero(project, monkeypatch, capsys):
    code, out, _ = run(monkeypatch, capsys, "impact", "tools/gen_tables.py")
    assert code == 0 and "No doc mentions: tools/gen_tables.py" in out


def test_impact_requires_a_path_or_diff(project, monkeypatch, capsys):
    code, out, err = run(monkeypatch, capsys, "impact")
    assert code == 1 and out == ""
    assert "give at least one path" in err and "usage: carta impact" in err


def test_impact_rejects_paths_together_with_diff(project, monkeypatch, capsys):
    code, out, err = run(monkeypatch, capsys, "impact", "src/parser/frame.py", "--diff")
    assert code == 1 and out == "" and "not both" in err


def test_impact_paths_are_read_from_the_current_directory(project, monkeypatch, capsys):
    monkeypatch.chdir(project / "src" / "parser")
    code, out, _ = run(monkeypatch, capsys, "impact", "frame.py", "--json")
    assert code == 0 and json.loads(out)["query"]["paths"] == ["src/parser/frame.py"]


def test_impact_top_kinds_min_score_flags_reach_the_query(project, monkeypatch, capsys):
    code, out, _ = run(monkeypatch, capsys, "impact", "src/parser/frame.py", "--json",
                       "--top", "1", "--kinds", "mention,symbol", "--min-score", "0.5")
    result = json.loads(out)
    assert code == 0
    assert result["query"] == {"paths": ["src/parser/frame.py"], "diff": None,
                               "kinds": ["mention", "symbol"], "top": 1, "min_score": 0.5}
    assert len(result["results"]) == 1
    code, _, err = run(monkeypatch, capsys, "impact", "src/parser/frame.py", "--kinds", "nope")
    assert code == 1 and "unknown edge kind 'nope'" in err


def test_impact_no_build(project, monkeypatch, capsys):
    code, out, err = run(monkeypatch, capsys, "impact", "src/parser/frame.py", "--no-build")
    assert code == 1 and out == "" and "carta graph build" in err
    run(monkeypatch, capsys, "graph", "build")
    code, out, _ = run(monkeypatch, capsys, "impact", "src/parser/frame.py", "--no-build",
                       "--json")
    assert code == 0 and json.loads(out)["store"]["built"] is False


@requires_git
def test_impact_diff(git_repo, monkeypatch, capsys):
    fill(git_repo.root)
    git_repo.write(".gitignore", ".carta/\n")
    git_repo.commit("first", 0)
    git_repo.git("checkout", "-q", "-b", "feature")
    git_repo.write("src/parser/frame.py", "x = 2\n")
    git_repo.write("docs/overview.md", "Everything under `src/parser/` is covered here. More.\n")
    git_repo.commit("second", 1)
    monkeypatch.chdir(git_repo.root)

    code, out, _ = run(monkeypatch, capsys, "impact", "--diff", "--json")
    result = json.loads(out)
    assert code == 0 and result["query"]["diff"] == "main...HEAD"
    assert result["query"]["paths"] == ["src/parser/frame.py"]
    assert [(r["doc"], r["in_diff"]) for r in result["results"]] == [
        ("docs/parser.md", False), ("docs/overview.md", True)]

    code, out, _ = run(monkeypatch, capsys, "impact", "--diff", "main..HEAD")
    assert code == 0
    assert out.splitlines()[:2] == ["2 docs mention 1 changed path (3 docs, 4 edges)",
                                    "Range: main..HEAD"]
    assert " 2. docs/overview.md  [already in this diff]  score" in out

    code, out, err = run(monkeypatch, capsys, "impact", "--diff", "src/parser/frame.py")
    assert code == 1 and out == ""
    assert "`src/parser/frame.py` is a path, not a range; pass it without --diff" in err


def test_impact_works_with_unreachable_qdrant_and_ollama(project, monkeypatch, capsys):
    def refuse(*args, **kwargs):
        raise AssertionError("the graph arm must not open a network connection")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    for argv in (("graph", "build"), ("graph", "status"),
                 ("impact", "src/parser/frame.py"), ("impact", "src/parser/", "--json")):
        code, out, err = run(monkeypatch, capsys, *argv)
        assert (code, err) == (0, ""), (argv, err)
        assert out


def test_commands_without_config_fail_with_the_standard_message(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    for argv in (("graph", "build"), ("graph", "status"), ("impact", "src/a.py")):
        code, out, err = run(monkeypatch, capsys, *argv)
        assert code == 1 and out == ""
        assert ".carta/config.yaml not found" in err and "carta init" in err


def test_a_bad_graph_config_is_reported_not_raised(tmp_path, monkeypatch, capsys):
    root = fill(tmp_path / "proj",
                CONFIG + "graph:\n  patterns:\n    - name: bad\n      regex: '('\n")
    monkeypatch.chdir(root)
    code, out, err = run(monkeypatch, capsys, "impact", "src/parser/frame.py")
    assert code == 1 and out == "" and "pattern 'bad' has an invalid regex" in err


_FOOTPRINT = r"""
import json, sys
from carta import cli
for argv in (["graph", "build"], ["graph", "status"], ["impact", "src/parser/frame.py"]):
    sys.argv = ["carta", *argv]
    try:
        cli.main()
    except SystemExit as exc:
        assert not exc.code, (argv, exc.code)
heavy = ("qdrant_client", "fastembed", "torch", "requests", "carta.embed", "carta.search",
         "carta.scanner")
bad = sorted(m for m in sys.modules if any(m == h or m.startswith(h + ".") for h in heavy))
print(json.dumps(bad), file=sys.stderr)
"""


def test_graph_commands_never_load_the_vector_stack(project):
    import os
    import subprocess
    import sys
    # The child runs in the project directory, so point it at this checkout
    # explicitly rather than relying on how the package was installed.
    checkout = str(Path(cli.__file__).resolve().parent.parent)
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [checkout, env.get("PYTHONPATH")]))
    proc = subprocess.run([sys.executable, "-c", _FOOTPRINT], cwd=str(project),
                          capture_output=True, text=True, env=env)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stderr.strip().splitlines()[-1]) == []
    assert "2 docs mention 1 path" in proc.stdout


# -- carta graph replay ------------------------------------------------------

def _replay_repo(git_repo):
    git_repo.write("docs/parser.md", "Framing lives in `src/frame.py`.\n")
    git_repo.write("src/frame.py", "X = 1\n")
    git_repo.commit("root", 0)
    for n in (2, 3):
        git_repo.write("src/frame.py", f"X = {n}\n")
        git_repo.write("docs/parser.md", f"Framing lives in `src/frame.py`. v{n}\n")
        git_repo.commit(f"change {n}", n)
    return git_repo.root


@requires_git
def test_cli_graph_replay_json_and_text(git_repo, tmp_path, monkeypatch, capsys):
    root = _replay_repo(git_repo)
    elsewhere = tmp_path / "elsewhere"            # no Carta config anywhere near
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    code, out, err = run(monkeypatch, capsys, "graph", "replay", "--repo", str(root),
                         "--no-cache", "--resamples", "50")
    assert code == 0, err
    assert out.splitlines()[1] == "Population: 3 commits scanned, 2 eligible (66.7%)"
    assert "graph[mention]" in out and "commits/s" in err

    code, out, err = run(monkeypatch, capsys, "graph", "replay", "--repo", str(root),
                         "--no-cache", "--resamples", "50", "--last", "1", "--json")
    assert code == 0, err
    report = json.loads(out)
    assert report["population"]["commits_scanned"] == 1
    assert report["methods"]["graph[mention]"]["any_hit_at"]["1"] == 1.0
    assert not (root / ".carta").exists()

    monkeypatch.chdir(root)                       # --repo defaults to where you are
    code, out, _err = run(monkeypatch, capsys, "graph", "replay", "--no-cache",
                          "--resamples", "50")
    assert code == 0 and out.startswith("Replay  repo=")


@requires_git
def test_cli_graph_replay_help_and_errors(git_repo, tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    code, out, _err = run(monkeypatch, capsys, "graph", "replay", "--help")
    assert code == 0 and "usage: carta graph replay" in out and "--arm" in out
    code, _out, err = run(monkeypatch, capsys, "graph", "replay", "--no-cache")
    assert code == 1 and "not inside a git repository" in err
    code, _out, err = run(monkeypatch, capsys, "graph", "replay", "--no-such-flag")
    assert code == 2 and "usage: carta graph replay" in err
    code, _out, err = run(monkeypatch, capsys, "graph")
    assert code == 1 and "replay" in err


@requires_git
def test_cli_graph_replay_touches_no_vector_stack(git_repo, tmp_path):
    import os
    import subprocess
    import sys
    root = _replay_repo(git_repo)
    code = _FOOTPRINT.replace(
        '(["graph", "build"], ["graph", "status"], ["impact", "src/parser/frame.py"])',
        '(["graph", "replay", "--repo", %r, "--no-cache", "--resamples", "20"],)' % str(root))
    assert "replay" in code
    checkout = str(Path(cli.__file__).resolve().parent.parent)
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [checkout, env.get("PYTHONPATH")]))
    proc = subprocess.run([sys.executable, "-c", code], cwd=str(tmp_path),
                          capture_output=True, text=True, env=env)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stderr.strip().splitlines()[-1]) == []
    assert proc.stdout.startswith("Replay  repo=")
