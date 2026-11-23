"""`carta status` must not under-report projects (#117).

The registry only knows projects some carta command ran in. These tests cover
the three gaps: git worktrees carrying their own `.carta/`, configs that were
never registered (`--scan`), and a per-row endpoint plus reachability flag so a
stale `qdrant_url` is visible in one command.
"""

import argparse
import json
import shutil
import subprocess

import pytest

from carta import status
from carta.cli import cmd_status
from carta.registry import register_project


def _project(root, name, url="http://qdrant.example:6333"):
    (root / ".carta" / "sidecars").mkdir(parents=True, exist_ok=True)
    (root / ".carta" / "config.yaml").write_text(f"project_name: {name}\nqdrant_url: {url}\n")
    return root


def _args(**kw):
    base = {"check": False, "json": True, "scan": None}
    base.update(kw)
    return argparse.Namespace(**base)


def _run_json(capsys, **kw):
    cmd_status(_args(**kw))
    return json.loads(capsys.readouterr().out)


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("CARTA_HOME", str(tmp_path / "carta-home"))
    nowhere = tmp_path / "nowhere"
    nowhere.mkdir()
    monkeypatch.chdir(nowhere)
    return tmp_path


# ---------------------------------------------------------------------------
# Discovery helpers
# ---------------------------------------------------------------------------

def test_scan_finds_nested_and_hidden_configs(tmp_path):
    a = _project(tmp_path / "work" / "alpha", "alpha")
    b = _project(tmp_path / "work" / "alpha" / ".worktrees" / "feat", "alpha")
    (tmp_path / "work" / "node_modules" / "pkg" / ".carta").mkdir(parents=True)
    (tmp_path / "work" / "node_modules" / "pkg" / ".carta" / "config.yaml").write_text("x: 1\n")
    found = {p.resolve() for p in status.scan_for_projects(tmp_path / "work")}
    assert found == {a.resolve(), b.resolve()}


def test_scan_respects_max_depth(tmp_path):
    deep = _project(tmp_path / "a" / "b" / "c" / "d", "deep")
    assert status.scan_for_projects(tmp_path, max_depth=2) == []
    assert [p.resolve() for p in status.scan_for_projects(tmp_path, max_depth=5)] == [deep.resolve()]


def test_scan_missing_root_is_empty(tmp_path):
    assert status.scan_for_projects(tmp_path / "absent") == []


def test_read_project_config_tolerates_garbage(tmp_path):
    root = tmp_path / "p"
    (root / ".carta").mkdir(parents=True)
    (root / ".carta" / "config.yaml").write_text("project_name: [unclosed\n")
    assert status.read_project_config(root) is None
    _project(root, "good", "http://q.example:6333")
    assert status.read_project_config(root) == ("good", "http://q.example:6333")


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_list_worktrees_returns_worktrees_with_carta_config(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com"]
    subprocess.run(git + ["init", "-q"], check=True)
    subprocess.run(git + ["commit", "-q", "--allow-empty", "-m", "init"], check=True)
    with_cfg = tmp_path / "wt-a"
    without_cfg = tmp_path / "wt-b"
    subprocess.run(git + ["worktree", "add", "-q", str(with_cfg)], check=True)
    subprocess.run(git + ["worktree", "add", "-q", str(without_cfg)], check=True)
    _project(with_cfg, "repo")
    found = [p.resolve() for p in status.list_worktrees(repo)]
    assert found == [with_cfg.resolve()]


def test_list_worktrees_non_git_dir_is_empty(tmp_path):
    assert status.list_worktrees(tmp_path) == []


def test_probe_qdrant_reachable_and_unreachable(monkeypatch):
    import requests

    class _Resp:
        status_code = 401  # answered, even if auth is wrong: the endpoint is alive

    calls = []

    def fake_get(url, timeout):
        calls.append(url)
        if "dead" in url:
            raise requests.ConnectionError("refused")
        return _Resp()

    monkeypatch.setattr(requests, "get", fake_get)
    assert status.probe_qdrant("http://live.example:6333/") is True
    assert status.probe_qdrant("http://dead.example:6333") is False
    assert calls[0] == "http://live.example:6333/collections"


# ---------------------------------------------------------------------------
# cmd_status integration
# ---------------------------------------------------------------------------

def test_registry_row_uses_live_config_url_not_stale_registry(home, capsys):
    proj = _project(home / "alpha", "alpha", url="http://new.example:6333")
    register_project(proj, "alpha", "http://old.example:6333")
    doc = _run_json(capsys)
    (row,) = doc["others"]
    assert row["qdrant_url"] == "http://new.example:6333"
    assert row["source"] == "registered"


def test_scan_reports_unregistered_projects(home, capsys):
    registered = _project(home / "src" / "alpha", "alpha")
    register_project(registered, "alpha", "http://qdrant.example:6333")
    _project(home / "src" / "beta", "beta")
    doc = _run_json(capsys, scan=str(home / "src"))
    by_name = {r["name"]: r for r in doc["others"]}
    assert set(by_name) == {"alpha", "beta"}
    assert by_name["alpha"]["source"] == "registered"
    assert by_name["beta"]["source"] == "unregistered"


def test_without_scan_unregistered_projects_are_not_walked(home, capsys):
    _project(home / "src" / "beta", "beta")
    doc = _run_json(capsys)
    assert doc["others"] == []


def test_worktrees_of_registered_projects_are_listed(home, capsys, monkeypatch):
    main = _project(home / "alpha", "alpha")
    wt = _project(home / "alpha-wt", "alpha", url="http://stale.example:6333")
    register_project(main, "alpha", "http://qdrant.example:6333")
    monkeypatch.setattr(
        status, "list_worktrees",
        lambda root: [wt] if root.resolve() == main.resolve() else [],
    )
    doc = _run_json(capsys)
    rows = {r["path"]: r for r in doc["others"]}
    assert rows[str(wt.resolve())]["source"] == "worktree"
    assert rows[str(wt.resolve())]["qdrant_url"] == "http://stale.example:6333"
    assert len(doc["others"]) == 2  # no duplicates


def test_check_flags_unreachable_endpoint_per_row(home, capsys, monkeypatch):
    live = _project(home / "live", "live", url="http://up.example:6333")
    dead = _project(home / "dead", "dead", url="http://down.example:6333")
    twin = _project(home / "twin", "twin", url="http://down.example:6333")
    for p, n in ((live, "live"), (dead, "dead"), (twin, "twin")):
        register_project(p, n, "ignored")
    probed = []

    def fake_probe(url):
        probed.append(url)
        return "up" in url

    monkeypatch.setattr(status, "probe_qdrant", fake_probe)
    doc = _run_json(capsys, check=True)
    reach = {r["name"]: r["endpoint_reachable"] for r in doc["others"]}
    assert reach == {"live": True, "dead": False, "twin": False}
    assert sorted(probed) == ["http://down.example:6333", "http://up.example:6333"]  # once per URL


def test_text_output_shows_endpoint_and_tags(home, capsys, monkeypatch):
    _project(home / "src" / "beta", "beta", url="http://down.example:6333")
    monkeypatch.setattr(status, "probe_qdrant", lambda url: False)
    cmd_status(_args(json=False, check=True, scan=str(home / "src")))
    out = capsys.readouterr().out
    assert "http://down.example:6333" in out
    assert "unregistered" in out
    assert "unreachable" in out


def test_scan_flag_in_help():
    from carta.tests.test_cli import run_carta
    result = run_carta(["status", "--help"])
    assert result.returncode == 0
    assert "--scan" in result.stdout
