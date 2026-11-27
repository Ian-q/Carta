"""#138: the recall hook must not be able to die silently.

Covers: judge status vs verdict, the judge-unavailable fallback, the debug line,
the trace's judge_status, `decide()`, the health checks behind `carta doctor` /
`carta hook explain`, and the prompt-hook wrapper's enabled gate.
"""
import io
import json
import os
import shutil
import stat
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from carta.hook import health


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _cfg(**pr):
    base = {
        "high_threshold": 0.85, "low_threshold": 0.60, "max_results": 5,
        "judge_timeout_s": 3, "ollama_model": "small-judge:1b", "agree_rank": 1,
        "judge_backend": "ollama", "trace": False,
    }
    base.update(pr)
    return {
        "project_name": "demo",
        "qdrant_url": "http://qdrant.invalid:6333",
        "modules": {"proactive_recall": True},
        "proactive_recall": base,
        "embed": {"ollama_url": "http://ollama.invalid:11434"},
        "hooks": {"stale_scan": {"enabled": True, "ollama_model": "big-judge:9b",
                                 "judge_timeout_s": 20}},
        "search": {"top_n": 5},
    }


def _hybrid_hit(dense, ranks=None, source="docs/a.md"):
    return {"score": 0.5, "source": source, "excerpt": "Invented excerpt text.",
            "dense_score": dense, "lane_ranks": ranks or {"dense": 2, "sparse": 0}}


def _resp(body, code=200):
    m = MagicMock()
    m.status_code = code
    m.json.return_value = body
    return m


_MISSING_MODEL = {"error": 'model "small-judge:1b" not found, try pulling it first'}


def _decide(hits, cfg, post=None, side_effect=None):
    from carta.hook.hook import decide
    kw = {"return_value": post} if post is not None else {"side_effect": side_effect}
    with (
        patch("carta.hook.hook.run_search", return_value=hits),
        patch("requests.post", **kw),
    ):
        return decide("an invented prompt about widgets", cfg)


# ---------------------------------------------------------------------------
# Judge: error is not "no"
# ---------------------------------------------------------------------------

def test_ollama_yesno_reports_missing_model_reason():
    from carta.hook.judge import ollama_yesno
    err = {}
    with patch("requests.post", return_value=_resp(_MISSING_MODEL, 404)):
        assert ollama_yesno("http://x", "m", "s", "u", timeout_s=1, err=err) is None
    assert "not found" in err["error"]


def test_ollama_yesno_reports_connection_error():
    from carta.hook.judge import ollama_yesno
    err = {}
    with patch("requests.post", side_effect=ConnectionError("refused")):
        assert ollama_yesno("http://x", "m", "s", "u", timeout_s=1, err=err) is None
    assert "refused" in err["error"]


def test_missing_judge_model_is_status_error_not_no():
    d = _decide([_hybrid_hit(0.70)], _cfg(), post=_resp(_MISSING_MODEL, 404))
    assert d.zone == "judge"
    assert d.judge_status == "error"
    assert "not found" in d.judge_error


def test_real_no_is_status_no():
    d = _decide([_hybrid_hit(0.70)], _cfg(), post=_resp({"message": {"content": "no"}}))
    assert d.judge_status == "no"
    assert d.judge_error is None
    assert d.inject is False


def test_judge_with_timeout_reports_timeout_status():
    import time as _t
    from carta.hook.hook import _judge_with_timeout

    def slow(*a, **k):
        _t.sleep(1.5)
        return True

    out = {}
    with patch("carta.hook.hook._call_ollama_judge", side_effect=slow):
        assert _judge_with_timeout("p", [], _cfg(), 1, out=out) is False
    assert out["status"] == "timeout"
    assert out["elapsed_ms"] >= 900


# ---------------------------------------------------------------------------
# Judge unavailable: high-confidence still injects, grey zone stays silent
# ---------------------------------------------------------------------------

def test_judge_unavailable_injects_above_high_threshold():
    d = _decide([_hybrid_hit(0.91)], _cfg(), post=_resp(_MISSING_MODEL, 404))
    assert d.judge_status == "error"
    assert d.judge_fallback == "inject_high_confidence"
    assert d.inject is True


def test_judge_unavailable_grey_zone_stays_silent():
    d = _decide([_hybrid_hit(0.70)], _cfg(), post=_resp(_MISSING_MODEL, 404))
    assert d.judge_fallback == "silent_grey_zone"
    assert d.inject is False


def test_judge_timeout_above_high_threshold_injects():
    import time as _t

    def slow(*a, **k):
        _t.sleep(1.5)
        return False

    from carta.hook.hook import decide
    with (
        patch("carta.hook.hook.run_search", return_value=[_hybrid_hit(0.90)]),
        patch("carta.hook.hook._call_ollama_judge", side_effect=slow),
    ):
        d = decide("p", _cfg(judge_timeout_s=1))
    assert d.judge_status == "timeout"
    assert d.inject is True


def test_judge_no_is_respected_even_above_high_threshold():
    """The fallback is for an UNAVAILABLE judge; a real "no" still wins."""
    d = _decide([_hybrid_hit(0.95)], _cfg(), post=_resp({"message": {"content": "no"}}))
    assert d.judge_status == "no"
    assert d.inject is False


def test_sparse_only_hit_never_clears_high_without_judge():
    hit = {"score": 0.5, "source": "x", "excerpt": "e",
           "lane_ranks": {"dense": None, "sparse": 0}}
    d = _decide([hit], _cfg(), post=_resp(_MISSING_MODEL, 404))
    assert d.inject is False


def test_crossenc_exception_is_status_error_with_fallback():
    from carta.hook.hook import decide
    with (
        patch("carta.hook.hook.run_search", return_value=[_hybrid_hit(0.90)]),
        patch("carta.hook.hook._resolve_judge_backend", return_value="crossenc"),
        patch("carta.hook.hook._call_crossenc_judge", side_effect=ImportError("no fastembed")),
    ):
        d = decide("p", _cfg(judge_backend="crossenc"))
    assert d.judge_status == "error"
    assert "fastembed" in d.judge_error
    assert d.inject is True


def test_search_error_is_reported_not_raised():
    from carta.hook.hook import decide
    with patch("carta.hook.hook.run_search", side_effect=RuntimeError("qdrant down")):
        d = decide("p", _cfg())
    assert "qdrant down" in d.search_error
    assert d.inject is False


# ---------------------------------------------------------------------------
# main(): stderr line, debug line, trace status
# ---------------------------------------------------------------------------

def _run_main(cfg, hits, post, env=None, tmp_path=None):
    from carta.hook.hook import main
    out, err = io.StringIO(), io.StringIO()
    env = env or {}
    with (
        patch.dict(os.environ, env),
        patch("sys.stdin", io.StringIO(json.dumps({"prompt": "invented prompt"}))),
        patch("carta.hook.hook.find_config", return_value=Path("/nonexistent/.carta/config.yaml")),
        patch("carta.hook.hook.load_config", return_value=cfg),
        patch("carta.hook.hook.run_search", return_value=hits),
        patch("requests.post", return_value=post),
        patch("sys.stdout", out), patch("sys.__stdout__", out), patch("sys.stderr", err),
    ):
        with pytest.raises(SystemExit) as e:
            main()
    assert e.value.code in (0, None)
    return out.getvalue(), err.getvalue()


def test_main_says_on_stderr_when_the_judge_cannot_run():
    out, err = _run_main(_cfg(), [_hybrid_hit(0.70)], _resp(_MISSING_MODEL, 404))
    assert out == ""
    assert "judge error" in err and "not found" in err


def test_debug_env_prints_one_decision_line():
    out, err = _run_main(_cfg(), [_hybrid_hit(0.70)],
                         _resp({"message": {"content": "yes"}}),
                         env={"CARTA_HOOK_DEBUG": "1"})
    assert json.loads(out)["context"]
    line = [ln for ln in err.splitlines() if ln.startswith("carta-hook: INJECT")]
    assert line, err
    assert "zone=judge" in line[0] and "judge=ollama:yes" in line[0]
    assert "low=0.6" in line[0] and "ms=" in line[0]


def test_no_debug_line_by_default():
    _, err = _run_main(_cfg(), [_hybrid_hit(0.70)], _resp({"message": {"content": "no"}}),
                       env={"CARTA_HOOK_DEBUG": ""})
    assert "zone=" not in err


def test_trace_records_judge_status(tmp_path, monkeypatch):
    monkeypatch.setenv("CARTA_HOME", str(tmp_path / "home"))
    cfg = _cfg(trace=True)
    _run_main(cfg, [_hybrid_hit(0.70)], _resp(_MISSING_MODEL, 404))
    files = list((tmp_path / "home" / "traces" / "demo").glob("hook-*.jsonl"))
    rec = json.loads(files[0].read_text().splitlines()[-1])
    assert rec["judge_status"] == "error"
    assert rec["injected"] is False


# ---------------------------------------------------------------------------
# Health checks
# ---------------------------------------------------------------------------

def _install_wrapper(repo: Path, text: str) -> Path:
    p = repo / ".carta" / "hooks" / health.WRAPPER_NAME
    p.parent.mkdir(parents=True)
    p.write_text(text)
    return p


_LEGACY = ('ENABLED=$(python3 -c "import yaml" 2>/dev/null || echo "False")\n'
           'exec carta-hook\n')


def test_packaged_wrappers_do_not_carry_the_legacy_gate():
    root = Path(__file__).resolve().parents[3]
    for p in (health.packaged_wrapper(), root / "hooks" / "scripts" / health.WRAPPER_NAME):
        if p.exists():
            assert health._LEGACY_GATE_MARKER not in p.read_text(), p


def test_legacy_wrapper_without_yaml_is_an_error_and_fixable(tmp_path):
    w = _install_wrapper(tmp_path, _LEGACY)
    item = health.check_wrapper(tmp_path, python3_has_yaml=lambda: False)
    assert item.status == "fail"
    assert "never runs carta-hook" in item.message
    assert "carta doctor --fix" in item.suggestion
    assert item.fix() is True
    assert w.read_text() == health.packaged_wrapper().read_text()
    assert w.stat().st_mode & stat.S_IXUSR


def test_legacy_wrapper_with_yaml_is_a_warning(tmp_path):
    _install_wrapper(tmp_path, _LEGACY)
    assert health.check_wrapper(tmp_path, python3_has_yaml=lambda: True).status == "warn"


def test_current_wrapper_passes(tmp_path):
    _install_wrapper(tmp_path, health.packaged_wrapper().read_text())
    assert health.check_wrapper(tmp_path, python3_has_yaml=lambda: False).status == "pass"


def _tags(*names):
    return _resp({"models": [{"name": n} for n in names]})


def test_missing_ollama_judge_model_is_an_error_with_pull_command(tmp_path):
    with (
        patch("requests.get", return_value=_tags("other:2b")),
        patch.object(health, "check_wrapper", return_value=health.HealthItem("w", "pass", "")),
    ):
        items = health.check_recall_hook(_cfg(), tmp_path, probe=False)
    judge = next(i for i in items if i.name == "recall_judge_model")
    assert judge.status == "fail"
    assert judge.suggestion == "ollama pull small-judge:1b"


def test_unreachable_ollama_for_judge_is_an_error(tmp_path):
    import requests as rq
    with (
        patch("requests.get", side_effect=rq.ConnectionError("down")),
        patch.object(health, "check_wrapper", return_value=health.HealthItem("w", "pass", "")),
    ):
        items = health.check_recall_hook(_cfg(), tmp_path, probe=False)
    assert next(i for i in items if i.name == "recall_judge_model").status == "fail"
    assert next(i for i in items if i.name == "recall_hook_qdrant").status == "fail"


def test_model_tag_latest_matches_bare_name():
    assert health.model_present("foo", ["foo:latest"])
    assert not health.model_present("foo:1b", ["foo:2b"])


def test_disabled_hook_is_skipped(tmp_path):
    cfg = _cfg()
    cfg["modules"]["proactive_recall"] = False
    items = health.check_recall_hook(cfg, tmp_path, probe=False)
    assert [i.status for i in items] == ["skip"]


def test_crossenc_without_fastembed_is_an_error():
    with patch("importlib.util.find_spec", return_value=None):
        items = health._check_crossenc_judge({}, 3, probe=False)
    assert items[0].status == "fail"


def test_crossenc_model_not_cached_is_a_fixable_error(tmp_path):
    from carta.search import rerank
    with (
        patch("importlib.util.find_spec", return_value=object()),
        patch.object(rerank, "model_cache_dir", return_value=tmp_path),
    ):
        items = health._check_crossenc_judge({"judge_model": "org/tiny"}, 3, probe=False)
    assert items[0].status == "fail" and items[0].fix is not None


@pytest.mark.parametrize("cold,warm,expect", [
    (0.5, None, "pass"),
    (9.0, 0.4, "warn"),     # cold start only
    (9.0, 8.0, "fail"),     # every call times out (#83)
    (9.0, None, "fail"),
])
def test_timeout_verdict(cold, warm, expect):
    item = health._timeout_verdict("x", "judge", "k.judge_timeout_s", 3, cold, warm)
    assert item.status == expect
    if expect == "fail":
        assert "k.judge_timeout_s" in item.suggestion


def test_probe_measures_slow_ollama_judge_as_failure(tmp_path):
    clock = iter([0.0, 10.0, 20.0, 30.0])
    with (
        patch("carta.hook.health.time.monotonic", side_effect=lambda: next(clock)),
        patch("carta.hook.judge.ollama_yesno", return_value=True),
    ):
        item = health._probe_ollama_recall_judge("http://x", "m", 3)
    assert item.status == "fail"
    assert "times out" in item.message


def test_stale_scan_missing_model_severity_follows_git_hook(tmp_path):
    with patch("requests.get", return_value=_tags("small-judge:1b")):
        warn = health.check_stale_scan_judge(_cfg(), tmp_path, probe=False)
        hooks = tmp_path / ".git" / "hooks"
        hooks.mkdir(parents=True)
        from carta.hook.git_hook import SENTINEL_START
        (hooks / "pre-push").write_text(f"#!/bin/sh\n{SENTINEL_START}\n")
        fail = health.check_stale_scan_judge(_cfg(), tmp_path, probe=False)
    assert warn[0].status == "warn"
    assert fail[0].status == "fail"
    assert fail[0].suggestion == "ollama pull big-judge:9b"


def test_stale_scan_probe_timeout_is_a_failure(tmp_path):
    with (
        patch("requests.get", return_value=_tags("big-judge:9b")),
        patch("carta.hook.judge.ollama_json", return_value=None),
    ):
        items = health.check_stale_scan_judge(_cfg(), tmp_path, probe=True)
    lat = next(i for i in items if i.name == "stale_scan_judge_latency")
    assert lat.status == "fail"
    assert "hooks.stale_scan.judge_timeout_s" in lat.message


# ---------------------------------------------------------------------------
# Doctor integration
# ---------------------------------------------------------------------------

def _project(tmp_path, cfg):
    import yaml
    (tmp_path / ".carta").mkdir()
    (tmp_path / ".carta" / "config.yaml").write_text(yaml.safe_dump(cfg))
    return tmp_path


def test_doctor_phase5_makes_missing_judge_a_critical_failure(tmp_path):
    from carta.install.preflight import PreflightChecker
    root = _project(tmp_path, _cfg())
    checker = PreflightChecker(interactive=False, project_root=root, include_hooks=True,
                               probe_judges=False)
    with patch("requests.get", return_value=_tags("other:2b")):
        checker._phase5_hooks()
    from carta.install.preflight import PreflightResult
    res = PreflightResult(checker.checks)
    judge = next(c for c in res.checks if c.name == "recall_judge_model")
    assert judge.status == "fail" and judge.category == "hooks"
    assert judge in res.critical_failures
    assert not res.can_proceed()


def test_init_preflight_does_not_run_phase5(tmp_path):
    from carta.install.preflight import PreflightChecker
    root = _project(tmp_path, _cfg())
    checker = PreflightChecker(interactive=False, project_root=root)
    with patch.object(PreflightChecker, "_phase5_hooks") as p5, \
         patch.object(PreflightChecker, "_phase1_environment"), \
         patch.object(PreflightChecker, "_phase2_infrastructure"), \
         patch.object(PreflightChecker, "_phase3_models"), \
         patch.object(PreflightChecker, "_phase4_resources"):
        checker.run()
    p5.assert_not_called()


# ---------------------------------------------------------------------------
# explain
# ---------------------------------------------------------------------------

def test_explain_prints_zone_judge_and_timings(tmp_path, capsys):
    from carta.hook.explain import explain
    cfg = _cfg()
    with (
        patch.object(health, "check_recall_hook",
                     return_value=[health.HealthItem("recall_judge_model", "fail",
                                                     "judge model missing",
                                                     suggestion="ollama pull small-judge:1b")]),
        patch("carta.hook.hook.run_search", return_value=[_hybrid_hit(0.70)]),
        patch("requests.post", return_value=_resp(_MISSING_MODEL, 404)),
    ):
        code = explain("invented prompt", cfg, tmp_path)
    out = capsys.readouterr().out
    assert code == 1
    assert "ollama pull small-judge:1b" in out
    assert "zone: judge" in out
    assert "judge (ollama): error" in out
    assert "grey-zone hits not injected" in out
    assert "timings:" in out


def test_explain_json_would_inject_false_when_wrapper_blocks(tmp_path, capsys):
    from carta.hook.explain import explain
    with (
        patch.object(health, "check_recall_hook",
                     return_value=[health.HealthItem("recall_hook_wrapper", "fail", "x")]),
        patch("carta.hook.hook.run_search",
              return_value=[_hybrid_hit(0.9, {"dense": 0, "sparse": 0})]),
    ):
        explain("p", _cfg(), tmp_path, as_json=True)
    doc = json.loads(capsys.readouterr().out)
    assert doc["decision"]["inject"] is True
    assert doc["would_inject"] is False


# ---------------------------------------------------------------------------
# The wrapper script itself (#138 root cause)
# ---------------------------------------------------------------------------

def _shim_env(tmp_path, python3_body):
    """A PATH with a fake python3 and a fake carta-hook that records it ran."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "python3").write_text(f"#!/bin/sh\n{python3_body}\n")
    (bindir / "carta-hook").write_text(f"#!/bin/sh\necho ran > {tmp_path}/ran\n")
    for f in bindir.iterdir():
        f.chmod(0o755)
    git_dir = shutil.which("git")
    env = {"PATH": f"{bindir}:{Path(git_dir).parent}:/usr/bin:/bin", "HOME": str(tmp_path)}
    return env


@pytest.mark.skipif(shutil.which("bash") is None or shutil.which("git") is None,
                    reason="needs bash and git")
@pytest.mark.parametrize("python3_body,should_run", [
    ('echo "No module named yaml" >&2; exit 1', True),   # the silent-death case
    ("echo True", True),
    ("echo False", False),                                 # explicit opt-out
])
def test_wrapper_gate(tmp_path, python3_body, should_run):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / ".carta").mkdir()
    (repo / ".carta" / "config.yaml").write_text("modules: {proactive_recall: true}\n")
    env = _shim_env(tmp_path, python3_body)
    root = Path(__file__).resolve().parents[3]
    for script in (health.packaged_wrapper(), root / "hooks" / "scripts" / health.WRAPPER_NAME):
        if not script.exists():
            continue
        (tmp_path / "ran").unlink(missing_ok=True)
        r = subprocess.run(["bash", str(script)], cwd=repo, input='{"prompt": "x"}',
                           text=True, capture_output=True, env=env)
        assert r.returncode == 0, r.stderr
        assert (tmp_path / "ran").exists() is should_run, (script, r.stdout, r.stderr)


@pytest.mark.skipif(shutil.which("bash") is None or shutil.which("git") is None,
                    reason="needs bash and git")
def test_wrapper_without_carta_hook_exits_zero(tmp_path):
    """python3 without PyYAML now falls through to carta-hook; if carta-hook is not
    installed either, the wrapper must not turn that into a hook error per prompt."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / ".carta").mkdir()
    (repo / ".carta" / "config.yaml").write_text("modules: {proactive_recall: true}\n")
    env = _shim_env(tmp_path, 'echo "No module named yaml" >&2; exit 1')
    (tmp_path / "bin" / "carta-hook").unlink()
    r = subprocess.run(["bash", str(health.packaged_wrapper())], cwd=repo,
                       input='{"prompt": "x"}', text=True, capture_output=True, env=env)
    assert r.returncode == 0, r.stderr
    assert r.stdout == ""
    assert "carta-hook not on PATH" in r.stderr
