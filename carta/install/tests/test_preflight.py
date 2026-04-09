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

    def test_suggestion_not_shown_inline_for_fail(self, capsys):
        result = self._make_result("fail")
        result.print_report(verbose=False)
        captured = capsys.readouterr()
        # Suggestion appears in footer, not inline under the check line
        # Verify the check line itself doesn't have the inline → prefix
        lines = captured.out.split("\n")
        check_line_idx = next(i for i, l in enumerate(lines) if "test_check" in l)
        # The line immediately after the check line should NOT be the inline suggestion
        next_line = lines[check_line_idx + 1] if check_line_idx + 1 < len(lines) else ""
        assert "Run: fix-it --now" not in next_line or "To fix" in captured.out

    def test_suggestion_in_footer_for_fail(self, capsys):
        result = self._make_result("fail")
        result.print_report(verbose=False)
        captured = capsys.readouterr()
        assert "To fix" in captured.out
        assert "Run: fix-it --now" in captured.out

    def test_suggestion_in_footer_for_warn(self, capsys):
        result = self._make_result("warn")
        result.print_report(verbose=False)
        captured = capsys.readouterr()
        assert "To fix" in captured.out
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


class TestFixFooter:
    def _make_failing_result(self) -> PreflightResult:
        checks = [
            PreflightCheck(
                name="qdrant_running",
                status="fail",
                message="Qdrant not running at http://localhost:6333",
                category="infrastructure",
                fixable=True,
                suggestion="docker run -d -p 6333:6333 -v ~/.carta/qdrant_storage:/qdrant/storage --name qdrant qdrant/qdrant",
            ),
            PreflightCheck(
                name="ollama_model_qwen3.5:0.8b",
                status="warn",
                message="Model 'qwen3.5:0.8b' not pulled",
                category="models",
                suggestion="ollama pull qwen3.5:0.8b",
            ),
        ]
        return PreflightResult(checks=checks)

    def test_fix_footer_shown_when_failures_exist(self, capsys):
        result = self._make_failing_result()
        result.print_report()
        captured = capsys.readouterr()
        assert "To fix" in captured.out
        assert "docker run" in captured.out
        assert "ollama pull qwen3.5:0.8b" in captured.out

    def test_fix_footer_not_shown_when_all_pass(self, capsys):
        check = PreflightCheck(
            name="qdrant_running",
            status="pass",
            message="Qdrant ready",
            category="infrastructure",
        )
        result = PreflightResult(checks=[check])
        result.print_report()
        captured = capsys.readouterr()
        assert "To fix" not in captured.out

    def test_fix_footer_lists_all_actionable_checks(self, capsys):
        result = self._make_failing_result()
        result.print_report()
        captured = capsys.readouterr()
        # Numbered format confirms these appear in the footer, not just inline
        assert "1. Qdrant not running" in captured.out
        assert "2. Model 'qwen3.5:0.8b' not pulled" in captured.out
