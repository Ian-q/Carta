from unittest.mock import patch

import requests

from carta.install.auto_fix import AutoInstaller


def test_suggest_model_pulls_uses_current_vision_default():
    """The `carta doctor --fix` pull map must offer the current vision default
    (qwen3-vl:8b) so it matches what preflight checks — not the retired
    qwen2.5vl:7b (#45)."""
    pulls = AutoInstaller(interactive=False).suggest_model_pulls()
    assert pulls.get("qwen3-vl:8b") == "ollama pull qwen3-vl:8b"
    assert "qwen2.5vl:7b" not in pulls


def test_suggest_model_pulls_judge_matches_preflight():
    """The pull map's judge entry must match what preflight checks
    (qwen3.5:0.8b) — not the retired qwen2.5:0.5b judge default (#56)."""
    pulls = AutoInstaller(interactive=False).suggest_model_pulls()
    assert pulls.get("qwen3.5:0.8b") == "ollama pull qwen3.5:0.8b"
    assert "qwen2.5:0.5b" not in pulls


class TestWaitForQdrantDetectsCrashedContainer:
    """A Qdrant container that panics at boot (e.g. corrupt WAL) exits within a
    second. Polling only the HTTP health endpoint cannot tell "still starting"
    from "already dead", so `doctor --fix` burned the full timeout and then
    reported the misleading "started but not responding to health checks"."""

    def _installer(self) -> AutoInstaller:
        return AutoInstaller(interactive=False)

    def test_returns_false_immediately_when_container_exited(self, capsys):
        installer = self._installer()

        def fake_inspect(cmd, **kwargs):
            assert cmd[:2] == ["docker", "inspect"]
            return type("R", (), {"returncode": 0, "stdout": "false 101\n", "stderr": ""})()

        with patch("carta.install.auto_fix.requests.get", side_effect=requests.ConnectionError()), \
             patch("carta.install.auto_fix.subprocess.run", side_effect=fake_inspect), \
             patch("carta.install.auto_fix.time.sleep") as mock_sleep:
            ok = installer._wait_for_qdrant(timeout=30)

        assert ok is False
        # Must bail out on the crash, not grind through the whole timeout.
        assert mock_sleep.call_count <= 1

        out = capsys.readouterr().out
        assert "exited" in out.lower()
        assert "101" in out
        assert "docker logs qdrant" in out

    def test_returns_true_when_healthy(self):
        installer = self._installer()

        with patch("carta.install.auto_fix.requests.get") as mock_get:
            mock_get.return_value = type("R", (), {"status_code": 200})()
            assert installer._wait_for_qdrant(timeout=5) is True

    def test_keeps_waiting_while_container_still_running(self, capsys):
        installer = self._installer()
        calls = {"n": 0}

        def fake_get(url, **kwargs):
            calls["n"] += 1
            if calls["n"] < 3:
                raise requests.ConnectionError()
            return type("R", (), {"status_code": 200})()

        def fake_inspect(cmd, **kwargs):
            return type("R", (), {"returncode": 0, "stdout": "true 0\n", "stderr": ""})()

        with patch("carta.install.auto_fix.requests.get", side_effect=fake_get), \
             patch("carta.install.auto_fix.subprocess.run", side_effect=fake_inspect), \
             patch("carta.install.auto_fix.time.sleep"):
            assert installer._wait_for_qdrant(timeout=30) is True


class TestStartQdrantContainerPersistence:
    """The fresh-container path passed no -v at all, so a doctor-created Qdrant
    kept its storage in the container's writable layer — every `docker rm` (or a
    recreate) silently destroyed every collection. It must mount a named volume,
    which is also the fix for the recurring WAL corruption: a Docker Desktop host
    bind mount does not honor fsync, a named volume lives on the VM's ext4."""

    def _run_and_capture_argv(self) -> list[list[str]]:
        installer = AutoInstaller(interactive=False)
        argv: list[list[str]] = []

        def fake_run(cmd, **kwargs):
            argv.append(cmd)
            return type("R", (), {"returncode": 0, "stdout": "abc123def456", "stderr": ""})()

        with patch("carta.install.auto_fix.subprocess.run", side_effect=fake_run), \
             patch.object(AutoInstaller, "_wait_for_qdrant", return_value=True):
            assert installer._start_qdrant_container() is True
        return argv

    def test_mounts_named_volume_for_storage(self):
        docker_run = [c for c in self._run_and_capture_argv() if c[:2] == ["docker", "run"]][0]
        assert "-v" in docker_run
        mount = docker_run[docker_run.index("-v") + 1]
        assert mount == "qdrant_storage:/qdrant/storage"

    def test_does_not_use_a_host_bind_mount(self):
        docker_run = [c for c in self._run_and_capture_argv() if c[:2] == ["docker", "run"]][0]
        mount = docker_run[docker_run.index("-v") + 1]
        # A leading path (~ or /) means a host bind mount, which loses fsync durability.
        assert not mount.startswith(("~", "/"))

    def test_creates_the_volume_before_running(self):
        argv = self._run_and_capture_argv()
        cmds = [" ".join(c) for c in argv]
        assert any(c.startswith("docker volume create qdrant_storage") for c in cmds), cmds
        assert cmds.index(next(c for c in cmds if "volume create" in c)) < \
               cmds.index(next(c for c in cmds if c.startswith("docker run")))

    def test_sets_restart_policy_so_it_survives_reboot(self):
        docker_run = [c for c in self._run_and_capture_argv() if c[:2] == ["docker", "run"]][0]
        assert "--restart" in docker_run
        assert docker_run[docker_run.index("--restart") + 1] == "unless-stopped"
