"""The replay's own bounds: none can be lifted by the repository being replayed."""
from __future__ import annotations

import pytest

from carta.graph import limits
from carta.graph.eval.replay import format_replay, main as replay_main, run_replay
from carta.graph.settings import GraphConfigError, graph_settings
from carta.graph.tests.conftest import requires_git

ARGS = ["--no-cache", "--resamples", "20"]


@pytest.mark.parametrize("value", [limits.MAX_REPLAY_COMMIT_FILES + 1, 10 ** 12, 0, -1])
def test_config_cannot_lift_or_void_the_bulk_rule(value):
    with pytest.raises(GraphConfigError, match="graph.replay.max_files"):
        graph_settings({"graph": {"replay": {"max_files": value}}})
    assert graph_settings({"graph": {"replay": {"max_files": limits.MAX_REPLAY_COMMIT_FILES}}}
                          ).replay_max_files == limits.MAX_REPLAY_COMMIT_FILES


@requires_git
def test_a_repository_config_cannot_lift_the_bulk_rule(git_repo, capsys):
    git_repo.write("README.md", "See `src/app.py`.\n")
    git_repo.write(".carta/config.yaml", "graph:\n  replay:\n    max_files: 1000000000\n")
    git_repo.commit("first", 0)
    assert replay_main(["--repo", str(git_repo.root), *ARGS]) == 1
    assert "graph.replay.max_files must be between" in capsys.readouterr().err


def _docs_history(repo, n_docs: int) -> None:
    for i in range(n_docs):
        repo.write(f"docs/d{i}.md", f"About `src/m{i}.py`. " + "filler " * 40 + "\n")
        repo.write(f"src/m{i}.py", "x = 1\n")
    repo.commit("first", 0)
    for step in (1, 2):
        repo.write("src/m0.py", f"x = {step + 1}\n")
        repo.write("docs/d0.md", f"About `src/m0.py`, revision {step}.\n")
        repo.commit(f"change {step}", step)


@requires_git
def test_doc_text_held_at_once_is_bounded_in_total(git_repo, monkeypatch):
    """One doc is bounded by MAX_DOC_BYTES; the first sync reads every doc of the tree."""
    _docs_history(git_repo, 12)
    free: dict = {}
    plain = run_replay(git_repo.root, use_disk_cache=False, resamples=20, stats=free)
    assert free["text_held_peak"] > 2000                    # all twelve docs at once
    monkeypatch.setattr("carta.graph.eval.replay.MAX_REPLAY_TEXT_BYTES", 700)
    held: dict = {}
    bounded = run_replay(git_repo.root, use_disk_cache=False, resamples=20, stats=held)
    assert 0 < held["text_held_peak"] <= 700
    assert bounded == plain                                 # what is held is only a saving


@requires_git
def test_the_replay_report_writes_out_control_characters(git_repo):
    hub = "docs/\x1b[2J\x1b]0;owned\x07hub.md"
    git_repo.write(hub, "About `src/app.py`.\n")
    git_repo.write("src/app.py", "x = 0\n")
    git_repo.commit("first", 0)
    for step in (1, 2, 3):
        git_repo.write("src/app.py", f"x = {step}\n")
        git_repo.write(hub, f"About `src/app.py`, revision {step}.\n")
        git_repo.commit(f"change {step}", step)
    settings = graph_settings({"excluded_paths": ["vendor\x1b[31m/"]})
    report = run_replay(git_repo.root, use_disk_cache=False, resamples=20, settings=settings)
    assert [h["doc"] for h in report["no_hub"]["hub_docs"]] == [hub]
    text = format_replay(report)
    assert "\x1b" not in text and "\x07" not in text
    assert "\\x1b[2J" in text and "vendor\\x1b[31m/" in text
