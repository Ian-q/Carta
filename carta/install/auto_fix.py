"""Auto-fix capabilities for Carta pre-flight checks.

Provides automatic installation and setup of missing services
that are required for Carta to function properly.

Usage:
    from carta.install.preflight import PreflightChecker, PreflightResult
    from carta.install.auto_fix import AutoInstaller
    
    checker = PreflightChecker()
    result = checker.run()
    
    installer = AutoInstaller()
    for check in result.fixable_failures:
        if installer.can_fix(check):
            if installer.fix(check):
                print(f"Fixed: {check.name}")
"""

from __future__ import annotations

import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Optional

import requests

if TYPE_CHECKING:
    from carta.install.preflight import PreflightCheck, PreflightResult


class AutoFixError(Exception):
    """Raised when an auto-fix operation fails."""
    pass


class AutoInstaller:
    """Attempts to automatically fix common Carta setup issues."""

    def __init__(self, interactive: bool = True, verbose: bool = False):
        self.interactive = interactive
        self.verbose = verbose
        self.os_type = self._detect_os()
        self._docker_available: Optional[bool] = None

    def _detect_os(self) -> str:
        """Detect operating system type."""
        system = platform.system().lower()
        if system == "darwin":
            return "macos"
        elif system == "linux":
            return "linux"
        elif system == "windows":
            return "windows"
        return "unknown"

    def _prompt_user(self, message: str, default: bool = True) -> bool:
        """Prompt user for Y/n input."""
        if not self.interactive:
            return default

        suffix = " [Y/n]: " if default else " [y/N]: "
        response = input(message + suffix).strip().lower()

        if default:
            return response not in ("n", "no", "false")
        else:
            return response in ("y", "yes", "true")

    def can_fix(self, check: PreflightCheck) -> bool:
        """Check if this specific check can be auto-fixed."""
        if not check.fixable or check.auto_fix_func is None:
            # Try to determine fixability by check name
            return check.name in (
                "qdrant_running",
                "docker_installed",  # Can provide instructions
                "ollama_models",  # Can provide commands
            )
        return True

    def fix(self, check: PreflightCheck) -> bool:
        """Attempt to fix a failed check.
        
        Args:
            check: The failed preflight check to fix
            
        Returns:
            True if fixed successfully, False otherwise
        """
        if check.auto_fix_func:
            try:
                return check.auto_fix_func()
            except Exception as e:
                if self.verbose:
                    print(f"  Auto-fix failed for {check.name}: {e}")
                return False

        # Handle known check types by name
        if check.name == "qdrant_running":
            return self._fix_qdrant()

        return False

    def fix_all(self, result: PreflightResult) -> dict[str, bool]:
        """Attempt to fix all fixable failures.
        
        Args:
            result: The preflight result to fix
            
        Returns:
            Dictionary mapping check names to fix success status
        """
        fixes = {}

        for check in result.fixable_failures:
            if self.can_fix(check):
                print(f"\n🔧 Attempting to fix: {check.name}")
                success = self.fix(check)
                fixes[check.name] = success

                if success:
                    print(f"  ✅ Fixed: {check.name}")
                else:
                    print(f"  ❌ Could not fix: {check.name}")
            else:
                fixes[check.name] = False
                if self.verbose:
                    print(f"  ⏭️  Cannot auto-fix: {check.name}")

        return fixes

    def _fix_qdrant(self) -> bool:
        """Attempt to start Qdrant using Docker."""
        if not self._is_docker_available():
            print("  ❌ Docker not available to start Qdrant")
            return False

        if not self._is_docker_running():
            print("  ❌ Docker daemon not running")
            return False

        # Check if Qdrant container already exists
        if self._qdrant_container_exists():
            if not self._prompt_user("Qdrant container exists but is not running. Start it?"):
                return False
            return self._start_existing_qdrant()

        # Start new Qdrant container
        if not self._prompt_user("Start Qdrant with Docker?"):
            return False

        return self._start_qdrant_container()

    def _is_docker_available(self) -> bool:
        """Check if Docker CLI is available."""
        if self._docker_available is not None:
            return self._docker_available

        self._docker_available = shutil.which("docker") is not None
        return self._docker_available

    def _is_docker_running(self) -> bool:
        """Check if Docker daemon is running."""
        try:
            result = subprocess.run(
                ["docker", "info"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            return result.returncode == 0
        except Exception:
            return False

    def _qdrant_container_exists(self) -> bool:
        """Check if a Qdrant container exists (running or stopped)."""
        try:
            result = subprocess.run(
                ["docker", "ps", "-a", "--filter", "name=qdrant", "--format", "{{.Names}}"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            return "qdrant" in result.stdout
        except Exception:
            return False

    def _start_existing_qdrant(self) -> bool:
        """Start an existing Qdrant container."""
        try:
            print("  🚀 Starting existing Qdrant container...")
            result = subprocess.run(
                ["docker", "start", "qdrant"],
                capture_output=True,
                text=True,
                timeout=10,
            )

            if result.returncode == 0:
                # Wait for Qdrant to be ready
                return self._wait_for_qdrant(timeout=30)
            else:
                print(f"  ❌ Failed to start container: {result.stderr}")
                return False
        except Exception as e:
            print(f"  ❌ Error starting Qdrant: {e}")
            return False

    def _start_qdrant_container(self) -> bool:
        """Start a new Qdrant container."""
        try:
            print("  🐳 Starting Qdrant container...")
            print("     docker run -d -p 6333:6333 --name qdrant qdrant/qdrant:latest")

            result = subprocess.run(
                [
                    "docker", "run", "-d",
                    "-p", "6333:6333",
                    "--name", "qdrant",
                    "qdrant/qdrant:latest",
                ],
                capture_output=True,
                text=True,
                timeout=60,
            )

            if result.returncode == 0:
                container_id = result.stdout.strip()[:12]
                print(f"  📦 Container started: {container_id}")
                # Wait for Qdrant to be ready
                return self._wait_for_qdrant(timeout=30)
            else:
                error = result.stderr.strip()
                if "port is already allocated" in error:
                    print("  ❌ Port 6333 is already in use")
                    print("     Stop the conflicting service or use a different port")
                else:
                    print(f"  ❌ Failed to start container: {error}")
                return False
        except subprocess.TimeoutExpired:
            print("  ❌ Timeout while starting Qdrant container")
            return False
        except Exception as e:
            print(f"  ❌ Error starting Qdrant: {e}")
            return False

    def _wait_for_qdrant(self, timeout: int = 30, interval: float = 1.0) -> bool:
        """Wait for Qdrant to be ready."""
        print(f"  ⏳ Waiting for Qdrant to be ready (timeout: {timeout}s)...")

        start_time = time.time()
        while time.time() - start_time < timeout:
            try:
                response = requests.get("http://localhost:6333/healthz", timeout=2)
                if response.status_code == 200:
                    print("  ✅ Qdrant is ready!")
                    return True
            except requests.ConnectionError:
                pass
            except Exception:
                pass

            time.sleep(interval)

        print("  ⚠️  Qdrant container started but not responding to health checks")
        print("     Check logs with: docker logs qdrant")
        return False

    def get_install_instructions(self, check_name: str) -> str:
        """Get OS-specific installation instructions for a check."""
        instructions = {
            "docker_installed": self._docker_install_instructions(),
            "ollama_installed": self._ollama_install_instructions(),
        }
        return instructions.get(check_name, "No specific instructions available")

    def _docker_install_instructions(self) -> str:
        """Return OS-specific Docker install instructions."""
        if self.os_type == "macos":
            return """Docker Installation (macOS):
  1. Download Docker Desktop: https://docs.docker.com/desktop/install/mac-install/
  2. Install and start Docker Desktop
  3. Wait for "Docker is running" in the menu bar"""

        elif self.os_type == "linux":
            distro = self._detect_linux_distro()
            if distro in ("ubuntu", "debian"):
                return """Docker Installation (Ubuntu/Debian):
  1. curl -fsSL https://get.docker.com | sudo sh
  2. sudo usermod -aG docker $USER
  3. Log out and back in for group changes to take effect
  4. docker run hello-world (to verify)"""
            else:
                return """Docker Installation (Linux):
  1. Visit: https://docs.docker.com/engine/install/
  2. Select your distribution and follow instructions"""

        elif self.os_type == "windows":
            return """Docker Installation (Windows):
  1. Download Docker Desktop: https://docs.docker.com/desktop/install/windows-install/
  2. Requires Windows 10/11 Pro or WSL2 backend
  3. Install and start Docker Desktop"""

        return "Visit https://docs.docker.com/get-docker/ for installation instructions"

    def _ollama_install_instructions(self) -> str:
        """Return OS-specific Ollama install instructions."""
        if self.os_type == "macos":
            return """Ollama Installation (macOS):
  1. Download: https://ollama.ai/download
  2. Install the Ollama app
  3. Start Ollama from Applications folder"""

        elif self.os_type == "linux":
            return """Ollama Installation (Linux):
  1. curl -fsSL https://ollama.com/install.sh | sh
  2. ollama serve (to start the server)"""

        elif self.os_type == "windows":
            return """Ollama Installation (Windows):
  1. Download: https://ollama.ai/download
  2. Requires Windows 10/11 with WSL2
  3. Run the installer and follow prompts"""

        return "Visit https://ollama.ai/download for installation instructions"

    def _detect_linux_distro(self) -> str:
        """Detect Linux distribution."""
        try:
            if Path("/etc/os-release").exists():
                with open("/etc/os-release") as f:
                    content = f.read().lower()
                    if "ubuntu" in content or "debian" in content:
                        return "ubuntu"
                    elif "fedora" in content:
                        return "fedora"
                    elif "arch" in content:
                        return "arch"
                    elif "centos" in content or "rhel" in content:
                        return "rhel"
        except Exception:
            pass
        return "unknown"

    def suggest_model_pulls(self) -> dict[str, str]:
        """Get suggested commands to pull required Ollama models."""
        return {
            "nomic-embed-text": "ollama pull nomic-embed-text",
            "llava": "ollama pull llava",
            "qwen2.5:0.5b": "ollama pull qwen2.5:0.5b",
        }

    def print_setup_guide(self, result: PreflightResult) -> None:
        """Print comprehensive setup guide for unmet requirements."""
        print("\n" + "=" * 60)
        print("📋 Carta Setup Guide")
        print("=" * 60)

        # Critical failures
        critical = result.critical_failures
        if critical:
            print("\n🔴 Critical Issues (must fix before carta init):")
            for check in critical:
                print(f"\n  ❌ {check.name}: {check.message}")
                if check.suggestion:
                    print(f"     → {check.suggestion}")

                # Print detailed instructions
                instructions = self.get_install_instructions(check.name)
                if instructions and "No specific instructions" not in instructions:
                    print(f"\n{instructions}")

        # Warnings
        warnings = result.warnings
        if warnings:
            print("\n⚠️  Warnings (optional but recommended):")
            for check in warnings:
                print(f"\n  ⚠️  {check.name}: {check.message}")
                if check.suggestion:
                    print(f"     → {check.suggestion}")

        # Model suggestions
        if result.is_healthy():
            print("\n📦 Recommended Model Downloads:")
            models = self.suggest_model_pulls()
            for model, command in models.items():
                print(f"     {command}")

        print("\n" + "=" * 60)


def run_auto_fix(result: PreflightResult, interactive: bool = True) -> dict[str, bool]:
    """Convenience function to run auto-fix on a preflight result.
    
    Args:
        result: The preflight result to fix
        interactive: Whether to prompt user for confirmation
        
    Returns:
        Dictionary mapping check names to fix success status
    """
    if not result.fixable_failures:
        return {}

    installer = AutoInstaller(interactive=interactive)
    return installer.fix_all(result)
