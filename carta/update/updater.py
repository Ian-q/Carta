import shutil
import subprocess
import sys

from carta.update.checker import _fetch_latest, _installed_version, _version_tuple


def _detect_install_method() -> str:
    """Return 'pipx' if carta-cc is installed via pipx, otherwise 'pip'."""
    if shutil.which("pipx") is None:
        return "pip"
    try:
        result = subprocess.run(
            ["pipx", "list", "--short"],
            capture_output=True, text=True, timeout=5,
        )
        if "carta-cc" in result.stdout:
            return "pipx"
    except Exception:
        pass
    return "pip"


def run_update(yes: bool = False) -> int:
    """Upgrade carta-cc to the latest version. Returns exit code.

    Note: the runtime copy at .carta/carta/ is updated on the next `carta init`
    run in each project (bootstrap copy is idempotent).
    """
    installed = _installed_version()
    print(f"Checking for updates (installed: {installed})...")

    latest = _fetch_latest()
    if latest is None:
        print("Could not reach PyPI. Check your network connection.", file=sys.stderr)
        return 1

    if _version_tuple(latest) <= _version_tuple(installed):
        print(f"carta {installed} — up to date")
        return 0

    if not yes:
        try:
            answer = input(f"Upgrade carta {installed} → {latest}? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\nAborted.")
            return 0
        if answer not in ("y", "yes"):
            print("Aborted.")
            return 0

    method = _detect_install_method()
    if method == "pipx":
        cmd = ["pipx", "upgrade", "carta-cc"]
    else:
        cmd = [sys.executable, "-m", "pip", "install", "--upgrade", "carta-cc"]

    print(f"Running: {' '.join(cmd)}")
    result = subprocess.run(cmd, check=False)
    if result.returncode != 0:
        print("Upgrade failed.", file=sys.stderr)
        return result.returncode

    print(f"\ncarta updated to {latest}")
    print("Run `carta init` in your projects to update the local runtime copy.")
    return 0


def print_check() -> None:
    """Print current vs latest version for --check flag."""
    installed = _installed_version()
    latest = _fetch_latest()
    if latest is None:
        print("Could not reach PyPI.")
        return
    if _version_tuple(latest) > _version_tuple(installed):
        print(f"carta {installed} installed  →  {latest} available")
        print("Run `carta update` to upgrade.")
    else:
        print(f"carta {installed} — up to date")
