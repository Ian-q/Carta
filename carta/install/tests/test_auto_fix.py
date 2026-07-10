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
