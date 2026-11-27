"""`carta sessions brief`: payload-only, capped, scoped, and silent on failure."""
from unittest.mock import patch

import pytest
from qdrant_client import QdrantClient

from carta.sessions import brief as br
from carta.sessions.tests.corpus import DAY, build_index, make_cfg, sessions, utc_tz
from carta.sessions.tests.synth import Transcript


@pytest.fixture
def env(tmp_path, monkeypatch):
    with utc_tz(monkeypatch):
        root = tmp_path / "widgets"
        root.mkdir()
        cfg = make_cfg(root, tmp_path)
        yield {"root": root, "cfg": cfg, "tmp": tmp_path}


@pytest.fixture
def indexed(env):
    env["client"] = build_index(env["cfg"], env["root"])
    return env


def _brief(env, cwd=None, **kw):
    return br.build_brief(env["cfg"], cwd or env["root"], repo_root=env["root"],
                          client=env["client"], **kw)


def test_brief_main_checkout_then_repo(indexed):
    lines = _brief(indexed)
    assert lines[0].startswith("Past sessions")
    assert lines[1] == "This main checkout:"
    assert "Update the README install section" in lines[2]
    assert lines[2].startswith("  2026-03-07  ")
    # sess-counter ran in the main checkout too (same cwd), older
    assert "widget counter thread-safe" in lines[3] and "(#42 PR #57)" in lines[3]
    assert lines[4] == "Elsewhere in widgets:"
    # newest first, with the worktree named
    assert lines[5].startswith("  2026-03-05  [theme] Colours on the settings page")
    assert "(PR #70)" in lines[5]
    assert lines[6].startswith("  2026-03-04  [exporter] The CSV exporter")
    assert len(lines) == 7


def test_brief_in_a_worktree_lists_it_first(indexed):
    lines = _brief(indexed, cwd=indexed["root"] / ".worktrees" / "exporter" / "src")
    assert lines[1] == "This worktree exporter:"
    assert "[exporter]" not in lines[2] and "The CSV exporter" in lines[2]
    assert lines[3] == "Elsewhere in widgets:"
    assert sum("exporter" in ln and "CSV" in ln for ln in lines) == 1


def test_brief_excludes_the_current_session(indexed):
    lines = _brief(indexed, exclude_session="sess-readme")
    assert not any("README" in ln for ln in lines)


def test_brief_makes_no_model_call(indexed):
    with patch("carta.embed.embed.get_embedding", side_effect=AssertionError("model")), \
         patch("requests.post", side_effect=AssertionError("network")):
        assert _brief(indexed)


def test_brief_hard_cap_15_lines(env):
    many = []
    for i in range(30):
        wt = "busy" if i % 2 else "other"
        tr = Transcript(f"sess-{i:02d}", f"{env['root']}/.worktrees/{wt}",
                        start_minutes=i * DAY / 4)
        tr.prompt(f"task number {i} with issue #{100 + i}")
        many.append(tr)
    env["client"] = build_index(env["cfg"], env["root"], transcripts=many)
    lines = _brief(env, cwd=env["root"] / ".worktrees" / "busy",
                   per_worktree=20, per_repo=20, max_lines=99)
    assert len(lines) == br.HARD_MAX_LINES
    assert lines[1] == "This worktree busy:"
    assert "Elsewhere in widgets:" in lines      # the repo section keeps a place
    # newest first
    assert "task number 29" in lines[2]


def test_brief_small_cap_keeps_worktree_section(indexed):
    lines = _brief(indexed, max_lines=3)
    assert lines == lines[:3] and len(lines) == 3
    assert lines[1] == "This main checkout:"


def test_brief_trims_long_prompts(env):
    tr = Transcript("sess-long", str(env["root"]))
    tr.prompt("word " * 500)
    env["client"] = build_index(env["cfg"], env["root"], transcripts=[tr])
    lines = _brief(env, width=80)
    assert all(len(ln) <= 80 for ln in lines[1:])
    assert lines[2].endswith("…")


def test_brief_prefers_summary(env):
    from carta.sessions import index as sidx
    from carta.sessions.tests.corpus import bow_embedding, write_all
    write_all(env["cfg"], sessions(env["root"])[3:])
    client = QdrantClient(":memory:")
    with patch("carta.embed.embed.get_embedding", side_effect=bow_embedding):
        sidx.run_index(env["cfg"], env["root"], client=client, backfill=True, verbose=False,
                       summarize=lambda rec: "Refreshed the install guide.")
    env["client"] = client
    assert "Refreshed the install guide." in _brief(env)[2]


def test_brief_empty_when_no_collection(env):
    env["client"] = QdrantClient(":memory:")
    assert _brief(env) == []


def test_brief_empty_when_disabled(indexed):
    indexed["cfg"]["modules"]["session_memory"] = False
    assert _brief(indexed) == []


def test_brief_raises_but_safe_brief_is_silent_when_qdrant_down(indexed, monkeypatch):
    class Down:
        def __init__(self, *a, **k):
            pass

        def collection_exists(self, *a, **k):
            raise ConnectionError("refused")

    with pytest.raises(ConnectionError):
        br.build_brief(indexed["cfg"], indexed["root"], repo_root=indexed["root"],
                       client=Down())
    carta = indexed["root"] / ".carta"
    carta.mkdir()
    (carta / "config.yaml").write_text(
        "project_name: widgets\nqdrant_url: http://127.0.0.1:9\n")
    monkeypatch.setattr(br, "QdrantClient", Down)
    assert br.safe_brief(indexed["root"]) == []


def test_safe_brief_without_config(tmp_path):
    assert br.safe_brief(tmp_path) == []


def test_safe_brief_unreachable_real_client(tmp_path):
    """A real client against a closed port: nothing printed, no exception."""
    carta = tmp_path / ".carta"
    carta.mkdir()
    (carta / "config.yaml").write_text(
        "project_name: widgets\nqdrant_url: http://127.0.0.1:9\n")
    assert br.safe_brief(tmp_path, timeout=1) == []
