"""Session hooks: the Stop/SessionEnd indexer and the SessionStart brief.

Both must never block a session and never print on failure.
"""
import io
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml

from carta.sessions import hooks as hk
from carta.sessions.tests.synth import standard_session

REPO = Path(hk.__file__).resolve().parents[2]
SCRIPT_DIRS = [REPO / "carta" / "hooks", REPO / "hooks" / "scripts"]
DEAD = "http://127.0.0.1:9"      # discard port: connection refused at once


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "widgets"
    (root / ".carta").mkdir(parents=True)
    state = tmp_path / "state"
    cfg = {"project_name": "widgets", "qdrant_url": DEAD,
           "embed": {"ollama_url": DEAD, "embedding_workers": 1},
           "sessions": {"enabled": True, "state_dir": str(state),
                        "transcripts_dir": str(tmp_path / "t")}}
    (root / ".carta" / "config.yaml").write_text(yaml.safe_dump(cfg))
    transcript = standard_session(str(root)).write(tmp_path / "t" / "x" / "sess-alpha.jsonl")
    return {"root": root, "state": state / "widgets", "transcript": transcript,
            "cfg_path": root / ".carta" / "config.yaml"}


def _payload(project, event="Stop", **extra):
    return {"session_id": "sess-alpha", "transcript_path": str(project["transcript"]),
            "cwd": str(project["root"]), "hook_event_name": event, **extra}


class Spy:
    def __init__(self):
        self.calls = []

    def __call__(self, cmd, cwd, log):
        self.calls.append((cmd, cwd, log))


# ---------------------------------------------------------------------------
# handle_stop
# ---------------------------------------------------------------------------

def test_stop_spawns_index_of_the_transcript(project):
    spy = Spy()
    assert hk.handle_stop(_payload(project), spawn=spy) == "spawned"
    (cmd, cwd, log), = spy.calls
    i = cmd.index("-m")
    assert cmd[0] == sys.executable
    assert cmd[i:] == ["-m", "carta.sessions.hooks", "index",
                       str(project["transcript"]), str(project["root"])]
    # The child must not start inside the (untrusted) project: see index_command.
    assert cwd == project["state"]
    assert cwd != project["root"]
    if sys.version_info >= (3, 11):
        assert "-P" in cmd[:i]
    assert log == project["state"] / "hook-index.log"


def test_background_index_runs_under_the_project_lock(project, monkeypatch):
    """Hook-launched runs of one project are serialised: parallel sessions (and a Stop
    run still going when SessionEnd fires) would otherwise each load the shared
    index state, and the last save would drop the others' entries — or an older read
    of the same transcript would overwrite the newer one's points."""
    import fcntl
    monkeypatch.chdir(project["root"])
    seen = {}

    def fake_index(argv):
        with open(project["state"] / hk.LOCK_NAME, "a") as f:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                seen["locked"] = False
            except BlockingIOError:
                seen["locked"] = True
        seen["argv"] = argv
        return 0

    assert hk.index_locked(str(project["transcript"]), run=fake_index) == 0
    assert seen == {"locked": True,
                    "argv": ["sessions", "index", "--session", str(project["transcript"])]}


def test_background_index_gives_up_when_the_lock_stays_held(project, monkeypatch):
    import fcntl
    monkeypatch.chdir(project["root"])
    project["state"].mkdir(parents=True, exist_ok=True)
    calls = []
    with open(project["state"] / hk.LOCK_NAME, "a") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        rc = hk.index_locked(str(project["transcript"]), run=calls.append,
                             wait_s=0.3, poll_s=0.05)
    assert rc == 1 and not calls


def test_state_dir_matches_the_index(project, tmp_path, monkeypatch):
    from carta.config import load_config
    from carta.sessions.index import state_path
    cfg = load_config(project["cfg_path"])
    assert hk._state_dir(cfg) == state_path(cfg).parent
    cfg["sessions"]["state_dir"] = ""
    monkeypatch.setenv("CARTA_HOME", str(tmp_path / "home"))
    assert hk._state_dir(cfg) == state_path(cfg).parent


def test_stop_hook_does_not_import_the_index_stack(project):
    """The Stop hook runs after every turn: it must not pay for Qdrant or PyMuPDF."""
    code = ("import sys, json, io; from carta.sessions import hooks as hk; "
            f"hk.handle_stop(json.loads({json.dumps(json.dumps(_payload(project)))}), "
            "spawn=lambda *a: None); "
            "bad = [m for m in ('qdrant_client', 'fitz', 'carta.sessions.index') "
            "if m in sys.modules]; print(','.join(bad))")
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                       cwd=str(REPO), timeout=60)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == ""


def test_stop_is_throttled_but_session_end_is_not(project):
    spy = Spy()
    now = 1_000_000.0
    assert hk.handle_stop(_payload(project), now=now, spawn=spy) == "spawned"
    assert hk.handle_stop(_payload(project), now=now + 60, spawn=spy) == "throttled"
    assert hk.handle_stop(_payload(project, "SessionEnd"), now=now + 61, spawn=spy) == "spawned"
    assert hk.handle_stop(_payload(project), now=now + 61 + 301, spawn=spy) == "spawned"
    assert len(spy.calls) == 3


def test_stop_throttle_is_configurable(project):
    cfg = yaml.safe_load(project["cfg_path"].read_text())
    cfg["sessions"]["stop_hook_min_interval_s"] = 0
    project["cfg_path"].write_text(yaml.safe_dump(cfg))
    spy = Spy()
    for _ in range(3):
        assert hk.handle_stop(_payload(project), spawn=spy) == "spawned"


def test_stop_throttle_is_per_session(project):
    spy = Spy()
    hk.handle_stop(_payload(project), now=1000.0, spawn=spy)
    other = {**_payload(project), "session_id": "sess-beta"}
    assert hk.handle_stop(other, now=1001.0, spawn=spy) == "spawned"


@pytest.mark.parametrize("payload", [{}, {"transcript_path": ""},
                                     {"transcript_path": "/nonexistent/x.jsonl"},
                                     {"transcript_path": 42}])
def test_stop_without_a_transcript_does_nothing(payload):
    spy = Spy()
    assert hk.handle_stop(payload, spawn=spy) == "no-transcript"
    assert not spy.calls


def test_stop_outside_a_carta_project_does_nothing(project, tmp_path):
    spy = Spy()
    elsewhere = tmp_path / "plain"
    elsewhere.mkdir()
    p = {**_payload(project), "cwd": str(elsewhere)}
    assert hk.handle_stop(p, spawn=spy) == "no-project"
    assert not spy.calls


def test_stop_disabled_module(project):
    cfg = yaml.safe_load(project["cfg_path"].read_text())
    cfg["modules"] = {"session_memory": False}
    project["cfg_path"].write_text(yaml.safe_dump(cfg))
    spy = Spy()
    assert hk.handle_stop(_payload(project), spawn=spy) == "disabled"
    assert not spy.calls


def _opt_out(project):
    cfg = yaml.safe_load(project["cfg_path"].read_text())
    del cfg["sessions"]["enabled"]
    cfg["modules"] = {"session_memory": True}      # what every pre-#137 config carries
    project["cfg_path"].write_text(yaml.safe_dump(cfg))


def test_stop_does_nothing_until_the_project_opts_in(project):
    _opt_out(project)
    spy = Spy()
    assert hk.handle_stop(_payload(project), spawn=spy) == "disabled"
    assert hk.handle_stop(_payload(project, event="SessionEnd"), spawn=spy) == "disabled"
    assert not spy.calls


def test_session_start_prints_nothing_until_the_project_opts_in(project, monkeypatch):
    import carta.sessions.brief as brief
    monkeypatch.setattr(brief, "safe_brief", lambda *a, **k: ["a brief line"])
    assert hk.handle_session_start(_payload(project, event="SessionStart")) == ["a brief line"]
    _opt_out(project)
    assert hk.handle_session_start(_payload(project, event="SessionStart")) == []


def test_session_start_outside_a_project_prints_nothing(tmp_path):
    assert hk.handle_session_start({"cwd": str(tmp_path), "session_id": "s"}) == []


def test_stop_with_broken_config_does_nothing(project):
    project["cfg_path"].write_text("project_name: [unclosed\n")
    spy = Spy()
    assert hk.handle_stop(_payload(project), spawn=spy) == "no-project"


def test_old_stamps_are_pruned(project):
    spy = Spy()
    stamps = project["state"] / "hook-stamps"
    stamps.mkdir(parents=True)
    old = stamps / "ancient"
    old.touch()
    os.utime(old, (1.0, 1.0))
    hk.handle_stop(_payload(project), now=hk.STAMP_MAX_AGE_S + 100.0, spawn=spy)
    assert not old.exists() and (stamps / "sess-alpha").exists()


# ---------------------------------------------------------------------------
# spawn_detached: really detached, really fail-open
# ---------------------------------------------------------------------------

def test_spawn_detached_returns_before_the_child_finishes(tmp_path):
    log = tmp_path / "d" / "log.txt"
    marker = tmp_path / "done"
    code = f"import time, pathlib; time.sleep(1.5); pathlib.Path({str(marker)!r}).touch()"
    t = time.monotonic()
    hk.spawn_detached([sys.executable, "-c", code], tmp_path, log)
    assert time.monotonic() - t < 1.0
    assert not marker.exists()
    for _ in range(100):
        if marker.exists():
            break
        time.sleep(0.05)
    assert marker.exists()


def test_spawn_detached_truncates_a_huge_log(tmp_path):
    log = tmp_path / "log.txt"
    log.write_bytes(b"x" * (hk.LOG_MAX_BYTES + 1))
    hk.spawn_detached([sys.executable, "-c", "pass"], tmp_path, log)
    assert log.stat().st_size < 1000


def test_stop_end_to_end_with_qdrant_down(project):
    """The real background index runs, fails against a dead backend, logs it, and
    leaves no index state, so the transcript is retried next time."""
    t = time.monotonic()
    assert hk.handle_stop(_payload(project, "SessionEnd")) == "spawned"
    assert time.monotonic() - t < 2.0
    log = project["state"] / "hook-index.log"
    for _ in range(600):
        text = log.read_text() if log.exists() else ""
        if "Error" in text or "Indexed" in text:
            break
        time.sleep(0.1)
    assert "Error" in text, text
    state = project["state"] / "index-state.json"
    assert not state.exists() or not json.loads(state.read_text())["transcripts"]


# ---------------------------------------------------------------------------
# run_hook
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("stdin", ["", "not json", "[1, 2]", '{"transcript_path": null}'])
def test_run_hook_bad_input_is_silent(stdin):
    out = io.StringIO()
    assert hk.run_hook("stop", io.StringIO(stdin), out) == 0
    assert hk.run_hook("session-start", io.StringIO(stdin), out) == 0
    assert out.getvalue() == ""


def test_run_hook_swallows_unexpected_errors(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("anything")
    monkeypatch.setattr(hk, "handle_stop", boom)
    monkeypatch.setattr(hk, "handle_session_start", boom)
    out = io.StringIO()
    assert hk.run_hook("stop", io.StringIO("{}"), out) == 0
    assert hk.run_hook("session-start", io.StringIO("{}"), out) == 0
    assert out.getvalue() == ""


def test_session_start_prints_the_brief(project, monkeypatch):
    seen = {}

    def fake_brief(cwd, exclude_session=None, **kw):
        seen.update(cwd=cwd, exclude=exclude_session)
        return ["Past sessions", "This worktree x:", "  2026-03-02  something"]
    monkeypatch.setattr("carta.sessions.brief.safe_brief", fake_brief)
    out = io.StringIO()
    hk.run_hook("session-start", io.StringIO(json.dumps(
        {"session_id": "s-now", "cwd": str(project["root"]), "source": "resume"})), out)
    assert out.getvalue().splitlines()[0] == "Past sessions"
    assert seen == {"cwd": str(project["root"]), "exclude": "s-now"}


def test_session_start_with_qdrant_down_prints_nothing(project):
    out = io.StringIO()
    hk.run_hook("session-start", io.StringIO(json.dumps(
        {"session_id": "s", "cwd": str(project["root"])})), out)
    assert out.getvalue() == ""


# ---------------------------------------------------------------------------
# The shell scripts
# ---------------------------------------------------------------------------

def _fake_carta(bin_dir: Path, body: str) -> None:
    bin_dir.mkdir(parents=True, exist_ok=True)
    f = bin_dir / "carta"
    f.write_text("#!/usr/bin/env bash\n" + body + "\n")
    f.chmod(0o755)


def _run_script(script: Path, path_dirs: list[Path], stdin: str = "{}"):
    env = {"PATH": os.pathsep.join([*map(str, path_dirs), "/usr/bin", "/bin"]),
           "HOME": os.environ.get("HOME", "/tmp")}
    return subprocess.run(["bash", str(script)], input=stdin, capture_output=True,
                          text=True, env=env, timeout=30)


@pytest.mark.parametrize("script_dir", SCRIPT_DIRS)
@pytest.mark.parametrize("name", ["carta-stop-hook.sh", "carta-session-start-hook.sh"])
def test_scripts_exist_and_are_executable(script_dir, name):
    p = script_dir / name
    assert p.is_file() and os.access(p, os.X_OK)


@pytest.mark.parametrize("script_dir", SCRIPT_DIRS)
@pytest.mark.parametrize("name", ["carta-stop-hook.sh", "carta-session-start-hook.sh"])
def test_scripts_without_carta_on_path_are_silent(script_dir, name, tmp_path):
    r = _run_script(script_dir / name, [tmp_path / "empty"])
    assert r.returncode == 0 and r.stdout == "" and r.stderr == ""


@pytest.mark.parametrize("script_dir", SCRIPT_DIRS)
def test_stop_script_hides_a_failing_carta(script_dir, tmp_path):
    _fake_carta(tmp_path / "bin", 'echo "noise"; echo "boom" >&2; exit 3')
    r = _run_script(script_dir / "carta-stop-hook.sh", [tmp_path / "bin"])
    assert r.returncode == 0 and r.stdout == "" and r.stderr == ""


@pytest.mark.parametrize("script_dir", SCRIPT_DIRS)
def test_stop_script_passes_stdin_to_the_handler(script_dir, tmp_path):
    seen = tmp_path / "seen"
    _fake_carta(tmp_path / "bin", f'echo "$@" > {seen}; cat >> {seen}')
    _run_script(script_dir / "carta-stop-hook.sh", [tmp_path / "bin"],
                stdin='{"transcript_path": "/x.jsonl"}')
    assert seen.read_text().splitlines() == ["sessions hook stop",
                                             '{"transcript_path": "/x.jsonl"}']


@pytest.mark.parametrize("script_dir", SCRIPT_DIRS)
def test_session_start_script_forwards_stdout_only(script_dir, tmp_path):
    _fake_carta(tmp_path / "bin", 'echo "Past sessions"; echo "err" >&2; exit 1')
    r = _run_script(script_dir / "carta-session-start-hook.sh", [tmp_path / "bin"])
    assert r.returncode == 0 and r.stdout == "Past sessions\n" and r.stderr == ""


def test_plugin_hooks_json_wires_both_hooks():
    cfg = json.loads((REPO / "hooks" / "hooks.json").read_text())["hooks"]

    def commands(event):
        return [h["command"] for m in cfg.get(event, []) for h in m["hooks"]]
    assert any(c.endswith("/carta-session-start-hook.sh") for c in commands("SessionStart"))
    assert any(c.endswith("/carta-check-deps.sh") for c in commands("SessionStart"))
    assert any(c.endswith("/carta-stop-hook.sh") for c in commands("Stop"))
    assert any(c.endswith("/carta-stop-hook.sh") for c in commands("SessionEnd"))
    assert any(c.endswith("/carta-prompt-hook.sh") for c in commands("UserPromptSubmit"))
    for event in cfg:
        for c in commands(event):
            assert (REPO / c.replace("${CLAUDE_PLUGIN_ROOT}/", "")).is_file(), c


def test_init_copies_both_session_hooks(tmp_path):
    """`carta init` (_register_hooks) installs the new SessionStart script beside the
    updated Stop script, overwriting an old stub in place."""
    from carta.install.bootstrap import _register_hooks
    dest = tmp_path / ".carta" / "hooks"
    dest.mkdir(parents=True)
    (dest / "carta-stop-hook.sh").write_text("#!/usr/bin/env bash\n# old stub\nexit 0\n")
    _register_hooks(tmp_path)
    for name in ("carta-stop-hook.sh", "carta-session-start-hook.sh",
                 "carta-prompt-hook.sh"):
        p = dest / name
        assert p.is_file() and os.access(p, os.X_OK)
    assert "carta sessions hook stop" in (dest / "carta-stop-hook.sh").read_text()


def test_index_child_never_imports_from_the_project(project, tmp_path):
    """A ``carta/`` folder planted in the project must not shadow the installed package."""
    import subprocess
    planted = project["root"] / "carta"
    (planted / "sessions").mkdir(parents=True)
    marker = tmp_path / "planted-code-ran"
    for f in (planted / "__init__.py", planted / "sessions" / "__init__.py",
              planted / "sessions" / "hooks.py"):
        f.write_text(f"open({str(marker)!r}, 'w').close()\n")
    project["state"].mkdir(parents=True, exist_ok=True)
    cmd = hk.index_command(project["transcript"], project["root"])
    probe = ("import os, sys; from carta.sessions import hooks as h; "
             "h._enter_project(sys.argv[1]); import carta.config; "
             "print(os.path.realpath(carta.config.__file__))")
    i = cmd.index("-m")
    out = subprocess.run([*cmd[:i], "-c", probe, str(project["root"])],
                         cwd=str(project["state"]), capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert not marker.exists()
    assert not out.stdout.strip().startswith(os.path.realpath(str(project["root"])))
