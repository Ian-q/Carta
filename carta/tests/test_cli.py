import subprocess
import sys
from pathlib import Path
import os
import shutil

from carta.install.bootstrap import CARTA_RUNTIME_SRC

def run_carta(args: list[str], cwd: Path = None) -> subprocess.CompletedProcess:
    repo_root = Path(__file__).resolve().parents[2]
    env = os.environ.copy()
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = str(repo_root) if not existing else f"{repo_root}{os.pathsep}{existing}"
    return subprocess.run(
        [sys.executable, "-m", "carta.cli"] + args,
        capture_output=True,
        text=True,
        cwd=str(cwd) if cwd else None,
        env=env,
    )

def test_version():
    result = run_carta(["--version"])
    assert result.returncode == 0
    from carta import __version__
    assert __version__ in result.stdout

def test_unknown_command_exits_nonzero():
    result = run_carta(["notacommand"])
    assert result.returncode != 0

def test_scan_requires_config(tmp_path):
    result = run_carta(["scan"], cwd=tmp_path)
    assert result.returncode != 0
    assert "config" in result.stderr.lower() or "config" in result.stdout.lower()


def test_runtime_cli_direct_execution(tmp_path):
    # Simulate what `carta init` does: copy the runtime into `.carta/carta`.
    # Then run the runtime's CLI via `python .carta/carta/cli.py ...`.
    dest = tmp_path / ".carta" / "carta"
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(CARTA_RUNTIME_SRC, dest)

    env = os.environ.copy()
    env.pop("PYTHONPATH", None)

    result = subprocess.run(
        [sys.executable, str(dest / "cli.py"), "--version"],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
        env=env,
    )
    assert result.returncode == 0, result.stderr


class TestCmdDoctorInteractiveFix:
    """carta doctor prompts to fix Qdrant without --fix flag."""

    def test_calls_fix_all_when_fixable_failures_exist_without_fix_flag(self):
        """When fixable failures exist, fix_all is called even without --fix."""
        import argparse
        from unittest.mock import patch, MagicMock, call
        from carta.cli import cmd_doctor

        args = argparse.Namespace(fix=False, yes=False, verbose=False, json=False)

        mock_result = MagicMock()
        mock_result.fixable_failures = [MagicMock()]
        mock_result.critical_failures = []
        mock_result.can_proceed.return_value = True
        mock_result.is_healthy.return_value = True
        mock_result.warnings = []

        with patch("carta.install.preflight.PreflightChecker") as MockChecker, \
             patch("carta.install.auto_fix.AutoInstaller") as MockInstaller:
            mock_checker_instance = MagicMock()
            mock_checker_instance.run.return_value = mock_result
            MockChecker.return_value = mock_checker_instance

            mock_installer_instance = MagicMock()
            mock_installer_instance.fix_all.return_value = {"qdrant_running": True}
            MockInstaller.return_value = mock_installer_instance

            try:
                cmd_doctor(args)
            except SystemExit:
                pass

            mock_installer_instance.fix_all.assert_called_once_with(mock_result)

    def test_does_not_call_fix_all_when_no_fixable_failures(self):
        """When no fixable failures, fix_all is not called."""
        import argparse
        from unittest.mock import patch, MagicMock
        from carta.cli import cmd_doctor

        args = argparse.Namespace(fix=False, yes=False, verbose=False, json=False)

        mock_result = MagicMock()
        mock_result.fixable_failures = []
        mock_result.critical_failures = []
        mock_result.can_proceed.return_value = True
        mock_result.is_healthy.return_value = True
        mock_result.warnings = []

        with patch("carta.install.preflight.PreflightChecker") as MockChecker, \
             patch("carta.install.auto_fix.AutoInstaller") as MockInstaller:
            mock_checker_instance = MagicMock()
            mock_checker_instance.run.return_value = mock_result
            MockChecker.return_value = mock_checker_instance

            mock_installer_instance = MagicMock()
            MockInstaller.return_value = mock_installer_instance

            try:
                cmd_doctor(args)
            except SystemExit:
                pass

            mock_installer_instance.fix_all.assert_not_called()
