"""#106: the recall hook fails open quickly when a remote backend is unreachable.

Unreachable is simulated by a connect that never returns (a black-holed host or a
hung resolver behave the same way from the caller's side). Hostnames are invented.
"""
import io
import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from carta.hook import reach

REMOTE_Q = "http://vectors.example:6333"
REMOTE_O = "http://gpu-box.example:11434"


def _cfg(qdrant=REMOTE_Q, ollama=REMOTE_O, **pr):
    base = {"high_threshold": 0.85, "low_threshold": 0.60, "max_results": 5,
            "judge_timeout_s": 3, "search_timeout_s": 3, "connect_timeout_s": 0.3,
            "ollama_model": "small-judge:1b", "judge_backend": "ollama", "trace": False}
    base.update(pr)
    return {"project_name": "demo", "qdrant_url": qdrant,
            "modules": {"proactive_recall": True}, "proactive_recall": base,
            "embed": {"ollama_url": ollama}, "search": {"top_n": 5}}


def _hang(host, port, timeout_s, out):
    time.sleep(30)  # never answers within any budget under test


# ---------------------------------------------------------------------------
# reach.probe
# ---------------------------------------------------------------------------

@pytest.mark.real_reach
def test_probe_skips_loopback_without_connecting():
    with patch.object(reach, "_connect", side_effect=AssertionError("connected")):
        assert reach.probe({"Qdrant": "http://localhost:6333",
                            "Ollama": "http://127.0.0.1:11434"}, 0.2) == []


@pytest.mark.real_reach
def test_probe_is_bounded_when_connect_hangs():
    with patch.object(reach, "_connect", side_effect=_hang):
        t0 = time.monotonic()
        down = reach.probe({"Qdrant": REMOTE_Q, "Ollama (query)": REMOTE_O}, 0.3)
        elapsed = time.monotonic() - t0
    # Parallel, so two hanging hosts cost one timeout, not two.
    assert elapsed < 0.3 + 0.4, elapsed
    assert [u.name for u in down] == ["Qdrant", "Ollama (query)"]
    assert "no answer within 0.3s" in str(down[0])
    assert REMOTE_Q in str(down[0])


@pytest.mark.real_reach
def test_probe_real_socket_open_and_refused():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    open_port = srv.getsockname()[1]
    closed = socket.socket()
    closed.bind(("127.0.0.1", 0))
    closed_port = closed.getsockname()[1]
    closed.close()
    try:
        down = reach.probe({"up": f"http://127.0.0.1:{open_port}",
                            "down": f"http://127.0.0.1:{closed_port}"}, 1.0,
                           include_loopback=True)
    finally:
        srv.close()
    assert [u.name for u in down] == ["down"]
    assert "refused" in down[0].reason.lower()


def test_host_port_defaults():
    assert reach.host_port("http://h.example") == ("h.example", 80)
    assert reach.host_port("https://h.example") == ("h.example", 443)
    assert reach.host_port("http://h.example:6333/x") == ("h.example", 6333)


# ---------------------------------------------------------------------------
# decide(): unreachable remote skips everything else, inside the connect budget
# ---------------------------------------------------------------------------

@pytest.mark.real_reach
def test_decide_unreachable_remote_returns_fast_without_searching():
    from carta.hook.hook import decide
    with patch.object(reach, "_connect", side_effect=_hang), \
         patch("carta.hook.hook.run_search", side_effect=AssertionError("searched")), \
         patch("requests.post", side_effect=AssertionError("called ollama")):
        t0 = time.monotonic()
        d = decide("an invented prompt " * 50, _cfg())   # >500 chars: would call the extractor
        elapsed = time.monotonic() - t0
    assert elapsed < 0.3 + 0.4, elapsed
    assert not d.inject
    assert len(d.backends_down) == 2
    assert d.zone_reason == "backend unreachable"
    assert "connect" in d.timings_ms


@pytest.mark.real_reach
def test_decide_local_backends_are_not_probed():
    from carta.hook.hook import decide
    with patch.object(reach, "_connect", side_effect=AssertionError("probed")), \
         patch("carta.hook.hook.run_search", return_value=[]):
        d = decide("q", _cfg(qdrant="http://localhost:6333", ollama="http://localhost:11434"))
    assert d.backends_down == [] and d.search_error is None


@pytest.mark.real_reach
def test_connect_timeout_zero_disables_probe():
    from carta.hook.hook import decide
    with patch.object(reach, "_connect", side_effect=AssertionError("probed")), \
         patch("carta.hook.hook.run_search", return_value=[]):
        d = decide("q", _cfg(connect_timeout_s=0))
    assert d.backends_down == []


@pytest.mark.real_reach
def test_split_host_probes_query_ollama_not_ingest():
    """The hook never uses the ingest host, so a GPU box that is off must not
    silence recall (#116)."""
    from carta.hook.hook import decide
    probed = []

    def connect(host, port, timeout_s, out):
        probed.append(host)
        out["ok"] = True

    cfg = _cfg(ollama="http://localhost:11434")
    cfg["embed"]["ingest_ollama_url"] = "http://gpu-box.example:11434"
    with patch.object(reach, "_connect", side_effect=connect), \
         patch("carta.hook.hook.run_search", return_value=[]):
        decide("q", cfg)
    assert probed == ["vectors.example"]


# ---------------------------------------------------------------------------
# main(): one stderr line, exit 0, no traceback, within budget
# ---------------------------------------------------------------------------

def _run_main(cfg, tmp_path, monkeypatch, prompt="an invented prompt"):
    from carta.hook import hook
    (tmp_path / ".carta").mkdir(exist_ok=True)
    (tmp_path / ".carta" / "config.yaml").write_text(yaml.safe_dump(cfg))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"prompt": prompt})))
    t0 = time.monotonic()
    with pytest.raises(SystemExit) as exc:
        hook.main()
    return exc.value.code, time.monotonic() - t0


@pytest.mark.real_reach
def test_main_unreachable_remote_fails_open_with_one_line(tmp_path, monkeypatch, capsys):
    with patch.object(reach, "_connect", side_effect=_hang), \
         patch("carta.hook.hook.run_search", side_effect=AssertionError("searched")):
        code, elapsed = _run_main(_cfg(), tmp_path, monkeypatch)
    out, err = capsys.readouterr()
    assert code == 0
    assert out == ""
    assert elapsed < 0.3 + 0.5, elapsed
    lines = [ln for ln in err.splitlines() if ln.strip()]
    assert len(lines) == 1, err
    assert "unreachable" in lines[0] and "fail-open" in lines[0]
    assert "Traceback" not in err


def test_main_hard_ceiling_exits_when_a_step_ignores_its_timeout(tmp_path, monkeypatch, capsys):
    from carta.hook import hook
    release = threading.Event()

    def stuck(prompt, cfg):
        release.wait(30)

    def fake_exit(code):
        raise SystemExit(code)

    monkeypatch.setattr(hook.os, "_exit", fake_exit)
    try:
        with patch.object(hook, "decide", side_effect=stuck):
            code, elapsed = _run_main(_cfg(hook_timeout_s=0.3), tmp_path, monkeypatch)
    finally:
        release.set()
    err = capsys.readouterr().err
    assert code == 0
    assert elapsed < 0.3 + 0.5, elapsed
    assert "no answer within 0.3s" in err and "Traceback" not in err


def test_search_error_is_one_line(tmp_path, monkeypatch, capsys):
    long = ("Could not embed the query — is Ollama running?\n(Detail: HTTPConnectionPool"
            "(host='x', port=1): Max retries exceeded " + "y" * 500 + ")")
    with patch("carta.hook.hook.run_search", side_effect=RuntimeError(long)):
        code, _ = _run_main(_cfg(), tmp_path, monkeypatch)
    err = capsys.readouterr().err
    assert code == 0
    assert len(err.strip().splitlines()) == 1
    assert "Detail" not in err and len(err) < 400


def test_hard_timeout_default_is_sum_of_budgets():
    from carta.hook.hook import _hard_timeout_s
    cfg = _cfg(connect_timeout_s=1.0, search_timeout_s=3, judge_timeout_s=3)
    assert _hard_timeout_s("short", cfg) == pytest.approx(8.0)
    assert _hard_timeout_s("x" * 501, cfg) == pytest.approx(12.0)
    assert _hard_timeout_s("short", _cfg(hook_timeout_s=2)) == 2.0


def test_explain_shows_unreachable_backends(tmp_path, capsys):
    from carta.hook import explain as explain_mod
    from carta.hook.hook import Decision
    d = Decision(backends_down=[f"Qdrant at {REMOTE_Q} unreachable (connect timed out after 1s)"],
                 search_error="backend unreachable: ...", zone_reason="backend unreachable")
    d.thresholds = {"low": 0.6, "high": 0.85, "agree_rank": 1, "max_results": 5}
    with patch("carta.hook.hook.decide", return_value=d), \
         patch("carta.hook.health.check_recall_hook", return_value=[]):
        explain_mod.explain("p", _cfg(), tmp_path)
    out = capsys.readouterr().out
    assert "BACKEND UNREACHABLE" in out and REMOTE_Q in out


# ---------------------------------------------------------------------------
# Real process, real network stack: a non-routable remote (RFC 5737 TEST-NET-1)
# ---------------------------------------------------------------------------

def test_real_hook_process_against_unroutable_remote_exits_0_quickly(tmp_path):
    cfg = _cfg(qdrant="http://192.0.2.1:6333", ollama="http://192.0.2.1:11434",
               connect_timeout_s=0.5)
    (tmp_path / ".carta").mkdir()
    (tmp_path / ".carta" / "config.yaml").write_text(yaml.safe_dump(cfg))
    env = {**os.environ, "CARTA_HOME": str(tmp_path / "home")}
    env.pop("CARTA_HOOK_DEBUG", None)
    t0 = time.monotonic()
    r = subprocess.run(
        [sys.executable, "-c", "from carta.hook.hook import main; main()"],
        input=json.dumps({"prompt": "an invented prompt"}), capture_output=True, text=True,
        cwd=tmp_path, env=env, timeout=60)
    elapsed = time.monotonic() - t0
    assert r.returncode == 0
    assert r.stdout == ""
    assert "Traceback" not in r.stderr
    assert "unreachable" in r.stderr, r.stderr
    # Interpreter start-up and imports dominate; the probe itself is 0.5 s.
    assert elapsed < 15, elapsed
