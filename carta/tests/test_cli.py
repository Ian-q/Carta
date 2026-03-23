import subprocess
import sys
from pathlib import Path

def run_carta(args: list[str], cwd: Path = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "carta.cli"] + args,
        capture_output=True, text=True, cwd=str(cwd) if cwd else None
    )

def test_version():
    result = run_carta(["--version"])
    assert result.returncode == 0
    assert "0.1.0" in result.stdout

def test_unknown_command_exits_nonzero():
    result = run_carta(["notacommand"])
    assert result.returncode != 0

def test_scan_requires_config(tmp_path):
    result = run_carta(["scan"], cwd=tmp_path)
    assert result.returncode != 0
    assert "config" in result.stderr.lower() or "config" in result.stdout.lower()
