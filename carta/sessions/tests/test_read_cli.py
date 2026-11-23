"""The read path through its surfaces: `carta recall`, `carta sessions brief|hook`,
`carta eval` on a sessions set, and the `carta_recall` MCP tool."""
import io
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from carta.sessions.tests.corpus import bow_embedding, build_index, make_cfg, utc_tz

EXAMPLE_SET = Path(__file__).resolve().parents[2] / "eval" / "datasets" / \
    "sessions-example.yaml"


@pytest.fixture
def project(tmp_path, monkeypatch):
    with utc_tz(monkeypatch):
        root = tmp_path / "widgets"
        (root / ".carta").mkdir(parents=True)
        cfg = make_cfg(root, tmp_path)
        (root / ".carta" / "config.yaml").write_text(yaml.safe_dump({
            "project_name": "widgets", "qdrant_url": cfg["qdrant_url"],
            "sessions": cfg["sessions"]}))
        client = build_index(cfg, root)
        monkeypatch.chdir(root)
        with patch("carta.sessions.recall.QdrantClient", return_value=client), \
             patch("carta.sessions.brief.QdrantClient", return_value=client), \
             patch("carta.embed.embed.get_embedding", side_effect=bow_embedding):
            yield {"root": root, "client": client, "tmp": tmp_path}


def _main(argv, monkeypatch):
    from carta import cli
    monkeypatch.setattr(sys, "argv", ["carta", *argv])
    cli.main()


# ---------------------------------------------------------------------------
# carta recall
# ---------------------------------------------------------------------------

def test_recall_prints_sessions(project, monkeypatch, capsys):
    _main(["recall", "exporter timing out retries", "--limit", "2"], monkeypatch)
    out = capsys.readouterr().out
    assert out.startswith("[1] 2026-03-04 ")
    assert "session sess-exporter" in out and "refs: #61" in out
    assert "[3]" not in out


def test_recall_json_and_filters(project, monkeypatch, capsys):
    _main(["recall", "anything at all", "--worktree", "theme", "--json"], monkeypatch)
    res = json.loads(capsys.readouterr().out)
    assert [r["session_id"] for r in res] == ["sess-theme"]


def test_recall_worktree_dot_means_current(project, monkeypatch, capsys):
    wt = project["root"] / ".worktrees" / "exporter"
    wt.mkdir(parents=True)
    monkeypatch.chdir(wt)
    _main(["recall", "dark mode", "--worktree", ".", "--json"], monkeypatch)
    assert [r["session_id"] for r in json.loads(capsys.readouterr().out)] == ["sess-exporter"]


def test_recall_since(project, monkeypatch, capsys):
    _main(["recall", "exporter", "--since", "2026-03-05", "--json"], monkeypatch)
    assert {r["session_id"] for r in json.loads(capsys.readouterr().out)} == \
        {"sess-theme", "sess-readme"}


def test_recall_bad_since_exits_2(project, monkeypatch, capsys):
    with pytest.raises(SystemExit) as e:
        _main(["recall", "x", "--since", "fortnight"], monkeypatch)
    assert e.value.code == 2 and "--since" in capsys.readouterr().err


def test_recall_nothing_indexed(project, monkeypatch, capsys):
    from qdrant_client import QdrantClient
    with patch("carta.sessions.recall.QdrantClient", return_value=QdrantClient(":memory:")):
        with pytest.raises(SystemExit) as e:
            _main(["recall", "x"], monkeypatch)
    assert e.value.code == 1
    assert "carta sessions index --backfill" in capsys.readouterr().err


def test_recall_no_match_message(project, monkeypatch, capsys):
    _main(["recall", "x", "--branch", "no-such-branch"], monkeypatch)
    assert capsys.readouterr().out.strip() == "No matching sessions."


# ---------------------------------------------------------------------------
# carta sessions brief / hook
# ---------------------------------------------------------------------------

def test_sessions_brief(project, monkeypatch, capsys):
    _main(["sessions", "brief"], monkeypatch)
    lines = capsys.readouterr().out.splitlines()
    assert 3 <= len(lines) <= 15
    assert lines[1] == "This main checkout:"


def test_sessions_brief_limits(project, monkeypatch, capsys):
    _main(["sessions", "brief", "--worktree-limit", "1", "--repo-limit", "1"], monkeypatch)
    assert len(capsys.readouterr().out.splitlines()) == 5


def test_sessions_brief_silent_when_qdrant_down(project, monkeypatch, capsys):
    with patch("carta.sessions.brief.QdrantClient", side_effect=ConnectionError("down")):
        _main(["sessions", "brief"], monkeypatch)       # no SystemExit: exit status 0
    cap = capsys.readouterr()
    assert cap.out == "" and cap.err == ""


def test_sessions_brief_silent_outside_a_project(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    _main(["sessions", "brief"], monkeypatch)
    cap = capsys.readouterr()
    assert cap.out == "" and cap.err == ""


def test_sessions_hook_session_start(project, monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(
        {"session_id": "sess-readme", "cwd": str(project["root"])})))
    with pytest.raises(SystemExit) as e:
        _main(["sessions", "hook", "session-start"], monkeypatch)
    assert e.value.code == 0
    out = capsys.readouterr().out
    assert "Past sessions" in out and "README" not in out   # the current session is left out


def test_sessions_hook_stop_bad_stdin(project, monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", io.StringIO("garbage"))
    with pytest.raises(SystemExit) as e:
        _main(["sessions", "hook", "stop"], monkeypatch)
    assert e.value.code == 0
    cap = capsys.readouterr()
    assert cap.out == "" and cap.err == ""


# ---------------------------------------------------------------------------
# carta eval on a sessions set
# ---------------------------------------------------------------------------

def test_eval_routes_a_sessions_set_to_recall(project, monkeypatch, capsys):
    with patch("carta.embed.pipeline.run_search", side_effect=AssertionError("doc search")):
        _main(["eval", str(EXAMPLE_SET), "-k", "3"], monkeypatch)
    out = capsys.readouterr().out
    assert out.startswith("target=sessions  queries=5")


def test_eval_sessions_flag(project, monkeypatch, capsys, tmp_path):
    p = tmp_path / "plain.yaml"
    p.write_text("queries:\n  - q: dark mode palette\n    expect: ['session:sess-theme']\n")
    out_json = tmp_path / "m.json"
    _main(["eval", str(p), "--sessions", "--json", str(out_json)], monkeypatch)
    m = json.loads(out_json.read_text())
    assert m["target"] == "sessions" and m["recall_at_k"] == 1.0


# ---------------------------------------------------------------------------
# MCP: carta_recall
# ---------------------------------------------------------------------------

def test_mcp_recall_returns_results(project):
    from carta.mcp import server
    out = server._recall("dark mode palette", limit=2)
    assert out["results"][0]["session_id"] == "sess-theme"
    assert "file_path" not in out["results"][0]
    json.dumps(out)


def test_mcp_recall_filters(project):
    from carta.mcp import server
    out = server._recall("exporter", since="2026-03-05", worktree="theme")
    assert [r["session_id"] for r in out["results"]] == ["sess-theme"]


def test_mcp_recall_invalid_since(project):
    from carta.mcp import server
    assert server._recall("x", since="soon")["error"] == "invalid_request"


def test_mcp_recall_not_indexed(project):
    from qdrant_client import QdrantClient
    from carta.mcp import server
    with patch("carta.sessions.recall.QdrantClient", return_value=QdrantClient(":memory:")):
        assert server._recall("x")["error"] == "not_indexed"


def test_mcp_recall_service_down_never_prints(project, capsys):
    from carta.mcp import server
    with patch("carta.sessions.recall.QdrantClient", side_effect=ConnectionError("down")):
        out = server._recall("x")
    assert out == {"error": "service_unavailable", "detail": "down"}
    assert capsys.readouterr().out == ""


def test_mcp_recall_without_config(tmp_path, monkeypatch):
    from carta.mcp import server
    monkeypatch.chdir(tmp_path)
    assert server._recall("x")["error"] == "service_unavailable"


def test_mcp_recall_is_registered():
    import asyncio
    from carta.mcp import server
    names = {t.name for t in asyncio.run(server.mcp_server.list_tools())}
    assert "carta_recall" in names
