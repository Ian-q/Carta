import re
import shutil
import subprocess
import sys
from typing import Optional

from carta.update.checker import _fetch_latest, _installed_version, _version_tuple


def _detect_install_method() -> tuple[str, Optional[str]]:
    """Return (method, pipx_version) where method is 'pipx' or 'pip'.

    pipx_version is the actual version installed in the pipx venv, or None
    if not installed via pipx or version cannot be determined.
    """
    if shutil.which("pipx") is None:
        return "pip", None
    try:
        result = subprocess.run(
            ["pipx", "list", "--short"],
            capture_output=True, text=True, timeout=5,
        )
        for line in result.stdout.splitlines():
            if line.strip().startswith("carta-cc"):
                parts = line.split()
                pipx_version = parts[1] if len(parts) >= 2 else None
                return "pipx", pipx_version
    except Exception:
        pass
    return "pip", None


def _parse_pipx_upgraded_version(output: str) -> Optional[str]:
    """Parse the version pipx actually installed from its upgrade output.

    Matches lines like: "upgraded package carta-cc from 0.3.9 to 0.3.10 (location: ...)"
    Returns None if the line is not found (e.g. already up to date).
    """
    m = re.search(r"upgraded package carta-cc from \S+ to (\S+)", output)
    return m.group(1) if m else None


def run_update(yes: bool = False) -> int:
    """Upgrade carta-cc to the latest version. Returns exit code.

    Note: the runtime copy at .carta/carta/ is updated on the next `carta init`
    run in each project (bootstrap copy is idempotent).
    """
    method, pipx_version = _detect_install_method()

    # Prefer the actual pipx-installed version over __version__ (which reflects the
    # running process and may be a dev checkout or old runtime copy).
    installed = pipx_version if pipx_version else _installed_version()
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

    if method == "pipx":
        cmd = ["pipx", "upgrade", "carta-cc"]
    else:
        cmd = [sys.executable, "-m", "pip", "install", "--upgrade", "carta-cc"]

    print(f"Running: {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)

    # Always surface the tool's own output
    if result.stdout:
        print(result.stdout, end="")
    if result.stderr:
        print(result.stderr, end="", file=sys.stderr)

    if result.returncode != 0:
        print("Upgrade failed.", file=sys.stderr)
        return result.returncode

    # Confirm using the version pipx actually installed, not the pre-fetched PyPI value.
    # These can differ when a release is very freshly published and CDN propagation is
    # incomplete — the fetch and the upgrade may hit different edge nodes.
    actual = _parse_pipx_upgraded_version(result.stdout) if method == "pipx" else None
    confirmed = actual or latest

    print(f"\ncarta updated to {confirmed}")
    if actual and _version_tuple(actual) < _version_tuple(latest):
        print(
            f"Note: PyPI reported {latest} but {actual} was installed "
            f"(CDN propagation lag). Run `carta update` again in a moment to get {latest}."
        )
    print("Run `carta init` in your projects to update the local runtime copy.")
    return 0


def print_check() -> None:
    """Print current vs latest version for --check flag."""
    _, pipx_version = _detect_install_method()
    installed = pipx_version if pipx_version else _installed_version()
    latest = _fetch_latest()
    if latest is None:
        print("Could not reach PyPI.")
        return
    if _version_tuple(latest) > _version_tuple(installed):
        print(f"carta {installed} installed  →  {latest} available")
        print("Run `carta update` to upgrade.")
    else:
        print(f"carta {installed} — up to date")
