import subprocess
import requests
from unittest.mock import patch
import pytest

from carta.install.preflight import PreflightCheck, PreflightChecker, PreflightResult


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


class TestPrintCheckSuggestions:
    def _make_result(self, status: str) -> PreflightResult:
        check = PreflightCheck(
            name="test_check",
            status=status,
            message="Something went wrong",
            category="infrastructure",
            suggestion="Run: fix-it --now",
        )
        return PreflightResult(checks=[check])

    def test_suggestion_shown_for_fail_without_verbose(self, capsys):
        result = self._make_result("fail")
        result.print_report(verbose=False)
        captured = capsys.readouterr()
        assert "Run: fix-it --now" in captured.out

    def test_suggestion_shown_for_warn_without_verbose(self, capsys):
        result = self._make_result("warn")
        result.print_report(verbose=False)
        captured = capsys.readouterr()
        assert "Run: fix-it --now" in captured.out

    def test_suggestion_not_shown_for_pass(self, capsys):
        check = PreflightCheck(
            name="ok_check",
            status="pass",
            message="All good",
            category="infrastructure",
            suggestion="You shouldn't see this",
        )
        result = PreflightResult(checks=[check])
        result.print_report(verbose=False)
        captured = capsys.readouterr()
        assert "You shouldn't see this" not in captured.out


class TestJudgeModelCheck:
    def test_judge_model_checked_when_ollama_running(self):
        checker = PreflightChecker(interactive=False)
        checker.checks = [
            PreflightCheck(
                name="ollama_running",
                status="pass",
                message="Ollama server running",
                category="infrastructure",
            )
        ]
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = type("R", (), {
                "returncode": 0,
                "stdout": "nomic-embed-text:latest\nllava:latest\n",
                "stderr": "",
            })()
            checker._phase3_models()

        check_names = [c.name for c in checker.checks]
        assert "ollama_model_qwen3.5:0.8b" in check_names

    def test_judge_model_warn_when_not_pulled(self):
        checker = PreflightChecker(interactive=False)
        checker.checks = [
            PreflightCheck(
                name="ollama_running",
                status="pass",
                message="Ollama server running",
                category="infrastructure",
            )
        ]
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = type("R", (), {
                "returncode": 0,
                "stdout": "nomic-embed-text:latest\n",
                "stderr": "",
            })()
            checker._phase3_models()

        judge_check = next(
            c for c in checker.checks if c.name == "ollama_model_qwen3.5:0.8b"
        )
        assert judge_check.status == "warn"
        assert "qwen3.5:0.8b" in judge_check.suggestion
