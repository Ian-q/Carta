import subprocess
import requests
from unittest.mock import patch
import pytest

from carta.install.preflight import PreflightChecker


class TestDockerRunningTip:
    def _make_checker(self, os_type: str) -> PreflightChecker:
        checker = PreflightChecker(interactive=False)
        checker.os_type = os_type
        return checker

    def test_macos_tip_mentions_docker_desktop_app(self):
        checker = self._make_checker("macos")
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = type("R", (), {"returncode": 1, "stdout": "", "stderr": ""})()
            result = checker._check_docker_running()
        assert result.status == "warn"
        assert "Docker Desktop" in result.suggestion or "menu bar" in result.suggestion.lower()

    def test_linux_tip_mentions_systemctl(self):
        checker = self._make_checker("linux")
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = type("R", (), {"returncode": 1, "stdout": "", "stderr": ""})()
            result = checker._check_docker_running()
        assert result.status == "warn"
        assert "systemctl" in result.suggestion

    def test_pass_when_docker_running(self):
        checker = self._make_checker("macos")
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = type("R", (), {"returncode": 0, "stdout": "ok", "stderr": ""})()
            result = checker._check_docker_running()
        assert result.status == "pass"


class TestQdrantSuggestion:
    def test_suggestion_includes_volume_flag(self):
        checker = PreflightChecker(interactive=False)
        with patch("carta.install.preflight.requests.get", side_effect=requests.ConnectionError()):
            result = checker._check_qdrant_running()
        assert result.status == "fail"
        assert "-v ~/.carta/qdrant_storage:/qdrant/storage" in result.suggestion

    def test_suggestion_includes_detached_flag(self):
        checker = PreflightChecker(interactive=False)
        with patch("carta.install.preflight.requests.get", side_effect=requests.ConnectionError()):
            result = checker._check_qdrant_running()
        assert "-d" in result.suggestion
