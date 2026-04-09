"""Pre-flight checks for Carta initialization.

Provides comprehensive 4-phase validation of the environment before Carta
can be initialized. Each phase checks a different category:

1. Environment: Python version, pip, network
2. Infrastructure: Docker, Qdrant, Ollama, ports
3. Models: Ollama models, ColPali cache
4. Resources: Disk space, memory, GPU

Usage:
    checker = PreflightChecker(interactive=True)
    result = checker.run()
    result.print_report()
    
    if not result.can_proceed():
        # Handle blocking issues
        pass
"""

from __future__ import annotations

import json
import platform
import shutil
import socket
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Literal, Optional
from urllib.parse import urlparse

import requests


@dataclass
class PreflightCheck:
    """Result of an individual preflight check."""

    name: str
    status: Literal["pass", "fail", "warn", "skip"]
    message: str
    category: Literal["environment", "infrastructure", "models", "resources"]
    fixable: bool = False
    auto_fix_func: Optional[Callable[[], bool]] = None
    suggestion: Optional[str] = None
    details: Optional[dict] = field(default_factory=dict)

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return {
            "name": self.name,
            "status": self.status,
            "message": self.message,
            "category": self.category,
            "fixable": self.fixable,
            "suggestion": self.suggestion,
            "details": self.details,
        }


@dataclass
class PreflightResult:
    """Complete result of all preflight checks."""

    checks: list[PreflightCheck] = field(default_factory=list)

    @property
    def passed(self) -> list[PreflightCheck]:
        return [c for c in self.checks if c.status == "pass"]

    @property
    def failed(self) -> list[PreflightCheck]:
        return [c for c in self.checks if c.status == "fail"]

    @property
    def warnings(self) -> list[PreflightCheck]:
        return [c for c in self.checks if c.status == "warn"]

    @property
    def skipped(self) -> list[PreflightCheck]:
        return [c for c in self.checks if c.status == "skip"]

    @property
    def fixable_failures(self) -> list[PreflightCheck]:
        return [c for c in self.checks if c.status == "fail" and c.fixable]

    @property
    def critical_failures(self) -> list[PreflightCheck]:
        return [c for c in self.checks if c.status == "fail" and not c.fixable]

    def is_healthy(self) -> bool:
        """Return True if no critical failures exist."""
        return len(self.critical_failures) == 0

    def can_proceed(self) -> bool:
        """Check if carta init should be allowed to proceed."""
        return len(self.critical_failures) == 0 and len(self.fixable_failures) == 0

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return {
            "status": "healthy" if self.is_healthy() else "unhealthy",
            "can_proceed": self.can_proceed(),
            "summary": {
                "total": len(self.checks),
                "passed": len(self.passed),
                "failed": len(self.failed),
                "warnings": len(self.warnings),
                "skipped": len(self.skipped),
                "fixable": len(self.fixable_failures),
            },
            "checks": [c.to_dict() for c in self.checks],
        }

    def to_json(self, indent: Optional[int] = 2) -> str:
        """Serialize to JSON string."""
        return json.dumps(self.to_dict(), indent=indent)

    def print_report(self, verbose: bool = False) -> None:
        """Print human-readable report to stdout."""
        print("\n🔍 Carta Pre-flight Check")
        print("━" * 55)

        current_category = None
        for check in self.checks:
            if check.category != current_category:
                current_category = check.category
                print(f"\n{self._category_header(current_category)}")

            self._print_check(check, verbose)

        print("\n" + "━" * 55)
        self._print_summary()

    def _category_header(self, category: str) -> str:
        titles = {
            "environment": "Phase 1: Environment",
            "infrastructure": "Phase 2: Infrastructure",
            "models": "Phase 3: Models",
            "resources": "Phase 4: Resources",
        }
        return titles.get(category, f"Phase: {category.title()}")

    def _print_check(self, check: PreflightCheck, verbose: bool) -> None:
        icons = {
            "pass": "✅",
            "fail": "❌",
            "warn": "⚠️ ",
            "skip": "⏭️ ",
        }
        icon = icons.get(check.status, "❓")
        print(f"  {icon} {check.name}: {check.message}")

        if verbose and check.details:
            for key, value in check.details.items():
                print(f"     • {key}: {value}")

    def _print_summary(self) -> None:
        total = len(self.checks)
        passed = len(self.passed)
        failed = len(self.failed)
        warnings = len(self.warnings)
        fixable = len(self.fixable_failures)

        print(f"\n📊 Summary: {passed}/{total} passed", end="")
        if failed > 0:
            print(f", {failed} failed ({fixable} fixable)", end="")
        if warnings > 0:
            print(f", {warnings} warnings", end="")
        print()

        if self.can_proceed():
            print("\n✅ All checks passed. Ready to initialize Carta.")
        elif self.fixable_failures and not self.critical_failures:
            print(f"\n🔧 {fixable} issue(s) can be auto-fixed.")
        else:
            critical = len(self.critical_failures)
            print(f"\n🔴 {critical} critical issue(s) must be resolved manually.")

        actionable = [
            c for c in self.checks
            if c.status in ("fail", "warn") and c.suggestion
        ]
        if actionable:
            print(f"\n{'━' * 55}")
            count = len(actionable)
            print(f"\n🔧 To fix ({count} issue{'s' if count > 1 else ''}):\n")
            for i, check in enumerate(actionable, 1):
                print(f"  {i}. {check.message}")
                print(f"     → {check.suggestion}\n")


class PreflightChecker:
    """Orchestrates all preflight checks across 4 phases."""

    def __init__(self, interactive: bool = True, verbose: bool = False):
        self.interactive = interactive
        self.verbose = verbose
        self.checks: list[PreflightCheck] = []
        self.os_type = self._detect_os()

    def run(self) -> PreflightResult:
        """Run all 4 phases and return results."""
        self.checks = []

        self._phase1_environment()
        self._phase2_infrastructure()
        self._phase3_models()
        self._phase4_resources()

        return PreflightResult(self.checks)

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

    def _phase1_environment(self) -> None:
        """Check Python version, pip, network, virtual environment."""
        self.checks.append(self._check_python_version())
        self.checks.append(self._check_pip())
        self.checks.append(self._check_virtual_environment())
        self.checks.append(self._check_network_connectivity())

    def _phase2_infrastructure(self) -> None:
        """Check Docker, Qdrant, Ollama, ports."""
        self.checks.append(self._check_docker_installed())
        self.checks.append(self._check_docker_running())
        self.checks.append(self._check_qdrant_running())
        self.checks.append(self._check_ollama_installed())
        self.checks.append(self._check_ollama_running())
        self.checks.append(self._check_ports_available())

    def _phase3_models(self) -> None:
        """Check Ollama models, ColPali cache."""
        # Only check models if Ollama is running
        ollama_check = next(
            (c for c in self.checks if c.name == "ollama_running"), None
        )
        if ollama_check and ollama_check.status == "pass":
            self.checks.append(self._check_ollama_model("nomic-embed-text"))
            self.checks.append(self._check_ollama_model("llava"))
            self.checks.append(self._check_ollama_model("qwen3.5:0.8b"))
        else:
            self.checks.append(
                PreflightCheck(
                    name="ollama_models",
                    status="skip",
                    message="Skipped (Ollama not running)",
                    category="models",
                )
            )

        # Check ColPali availability separately
        self.checks.append(self._check_colpali_available())

    def _phase4_resources(self) -> None:
        """Check disk space, memory, GPU."""
        self.checks.append(self._check_disk_space())
        self.checks.append(self._check_memory())
        self.checks.append(self._check_gpu_available())

    # ═════════════════════════════════════════════════════════════════
    # Phase 1: Environment Checks
    # ═════════════════════════════════════════════════════════════════

    def _check_python_version(self) -> PreflightCheck:
        """Check Python version is 3.10+."""
        version = sys.version_info
        min_version = (3, 10)

        if version >= min_version:
            return PreflightCheck(
                name="python_version",
                status="pass",
                message=f"Python {version.major}.{version.minor}.{version.micro} (supported)",
                category="environment",
                details={"min_required": "3.10", "current": f"{version.major}.{version.minor}.{version.micro}"},
            )
        else:
            return PreflightCheck(
                name="python_version",
                status="fail",
                message=f"Python {version.major}.{version.minor} (requires 3.10+)",
                category="environment",
                fixable=False,
                suggestion="Install Python 3.10 or newer from python.org or your package manager",
            )

    def _check_pip(self) -> PreflightCheck:
        """Check pip or pipx is available."""
        pip_path = shutil.which("pip") or shutil.which("pip3")
        pipx_path = shutil.which("pipx")

        if pipx_path:
            return PreflightCheck(
                name="pip_availability",
                status="pass",
                message="pipx available (recommended)",
                category="environment",
                details={"pipx_path": pipx_path},
            )
        elif pip_path:
            try:
                result = subprocess.run(
                    [pip_path, "--version"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                version = result.stdout.strip().split()[1] if result.returncode == 0 else "unknown"
                return PreflightCheck(
                    name="pip_availability",
                    status="pass",
                    message=f"pip {version} available",
                    category="environment",
                    details={"pip_path": pip_path, "version": version},
                )
            except Exception:
                pass

        return PreflightCheck(
            name="pip_availability",
            status="fail",
            message="pip/pipx not found",
            category="environment",
            fixable=False,
            suggestion="Install pip: https://pip.pypa.io/en/stable/installation/",
        )

    def _check_virtual_environment(self) -> PreflightCheck:
        """Warn if running in system Python (not virtual env)."""
        in_venv = (
            hasattr(sys, "real_prefix")
            or (hasattr(sys, "base_prefix") and sys.base_prefix != sys.prefix)
        )

        if in_venv:
            return PreflightCheck(
                name="virtual_environment",
                status="pass",
                message="Running in virtual environment",
                category="environment",
                details={"prefix": sys.prefix},
            )
        else:
            return PreflightCheck(
                name="virtual_environment",
                status="warn",
                message="Not in a virtual environment (recommended)",
                category="environment",
                fixable=False,
                suggestion="Create a virtualenv: python3 -m venv ~/.venv/carta && source ~/.venv/carta/bin/activate",
            )

    def _check_network_connectivity(self) -> PreflightCheck:
        """Check network connectivity to key services."""
        test_urls = [
            ("pypi.org", "Package installation"),
            ("github.com", "Repository access"),
        ]

        reachable = []
        unreachable = []

        for host, purpose in test_urls:
            try:
                socket.create_connection((host, 443), timeout=3)
                reachable.append((host, purpose))
            except OSError:
                unreachable.append((host, purpose))

        if not unreachable:
            return PreflightCheck(
                name="network_connectivity",
                status="pass",
                message="Network connectivity OK",
                category="environment",
                details={"reachable": [h for h, _ in reachable]},
            )
        elif len(unreachable) == len(test_urls):
            return PreflightCheck(
                name="network_connectivity",
                status="fail",
                message="No network connectivity",
                category="environment",
                fixable=False,
                suggestion="Check your internet connection and try again",
            )
        else:
            return PreflightCheck(
                name="network_connectivity",
                status="warn",
                message=f"Limited connectivity ({len(unreachable)} of {len(test_urls)} hosts unreachable)",
                category="environment",
                details={
                    "reachable": [h for h, _ in reachable],
                    "unreachable": [h for h, _ in unreachable],
                },
            )

    # ═════════════════════════════════════════════════════════════════
    # Phase 2: Infrastructure Checks
    # ═════════════════════════════════════════════════════════════════

    def _check_docker_installed(self) -> PreflightCheck:
        """Check Docker is installed."""
        docker_path = shutil.which("docker")

        if docker_path:
            try:
                result = subprocess.run(
                    ["docker", "--version"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                version = result.stdout.strip() if result.returncode == 0 else "installed"
                return PreflightCheck(
                    name="docker_installed",
                    status="pass",
                    message="Docker installed",
                    category="infrastructure",
                    details={"path": docker_path, "version": version},
                )
            except Exception:
                pass

        return PreflightCheck(
            name="docker_installed",
            status="warn",
            message="Docker not installed (optional but recommended)",
            category="infrastructure",
            fixable=False,
            suggestion=self._docker_install_instructions(),
        )

    def _check_docker_running(self) -> PreflightCheck:
        """Check Docker daemon is running."""
        try:
            result = subprocess.run(
                ["docker", "info"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0:
                return PreflightCheck(
                    name="docker_running",
                    status="pass",
                    message="Docker daemon running",
                    category="infrastructure",
                )
            else:
                return PreflightCheck(
                    name="docker_running",
                    status="warn",
                    message="Docker installed but daemon not running",
                    category="infrastructure",
                    fixable=False,
                    suggestion=self._docker_running_instructions(),
                )
        except Exception as e:
            return PreflightCheck(
                name="docker_running",
                status="skip",
                message=f"Could not check Docker status: {e}",
                category="infrastructure",
            )

    def _docker_running_instructions(self) -> str:
        """Return OS-specific instructions for starting Docker daemon."""
        if self.os_type == "macos":
            return (
                "Open the Docker Desktop app first "
                "(look for the whale icon in your menu bar). "
                "On macOS, Docker requires the Desktop app to be running."
            )
        elif self.os_type == "linux":
            return "Run: sudo systemctl start docker"
        elif self.os_type == "windows":
            return "Start Docker Desktop from the Start menu"
        return "Start Docker Desktop (macOS/Windows) or run 'sudo systemctl start docker' (Linux)"

    def _check_qdrant_running(self, url: str = "http://localhost:6333") -> PreflightCheck:
        """Check Qdrant is running."""
        try:
            response = requests.get(f"{url}/healthz", timeout=3)
            if response.status_code == 200:
                return PreflightCheck(
                    name="qdrant_running",
                    status="pass",
                    message=f"Qdrant ready at {url}",
                    category="infrastructure",
                    details={"url": url, "status": response.status_code},
                )
            else:
                return PreflightCheck(
                    name="qdrant_running",
                    status="fail",
                    message=f"Qdrant not running at {url}",
                    category="infrastructure",
                    fixable=True,
                    suggestion="Start with: docker run -d -p 6333:6333 -v ~/.carta/qdrant_storage:/qdrant/storage --name qdrant qdrant/qdrant",
                )
        except requests.ConnectionError:
            return PreflightCheck(
                name="qdrant_running",
                status="fail",
                message=f"Qdrant not running at {url}",
                category="infrastructure",
                fixable=True,
                suggestion="Start with: docker run -d -p 6333:6333 -v ~/.carta/qdrant_storage:/qdrant/storage --name qdrant qdrant/qdrant",
            )
        except Exception as e:
            return PreflightCheck(
                name="qdrant_running",
                status="fail",
                message=f"Cannot reach Qdrant at {url}: {e}",
                category="infrastructure",
                fixable=True,
                suggestion="Start with: docker run -d -p 6333:6333 -v ~/.carta/qdrant_storage:/qdrant/storage --name qdrant qdrant/qdrant",
            )

    def _check_ollama_installed(self) -> PreflightCheck:
        """Check Ollama is installed."""
        ollama_path = shutil.which("ollama")

        if ollama_path:
            return PreflightCheck(
                name="ollama_installed",
                status="pass",
                message="Ollama installed",
                category="infrastructure",
                details={"path": ollama_path},
            )

        return PreflightCheck(
            name="ollama_installed",
            status="warn",
            message="Ollama not found (optional)",
            category="infrastructure",
            fixable=False,
            suggestion="Install from: https://ollama.ai/download",
        )

    def _check_ollama_running(self, url: str = "http://localhost:11434") -> PreflightCheck:
        """Check Ollama server is running."""
        try:
            response = requests.get(url, timeout=3)
            if response.status_code == 200:
                return PreflightCheck(
                    name="ollama_running",
                    status="pass",
                    message="Ollama server running",
                    category="infrastructure",
                    details={"url": url},
                )
            else:
                return PreflightCheck(
                    name="ollama_running",
                    status="warn",
                    message="Ollama installed but server not running",
                    category="infrastructure",
                    fixable=False,
                    suggestion="Start Ollama application or run 'ollama serve'",
                )
        except requests.ConnectionError:
            return PreflightCheck(
                name="ollama_running",
                status="warn",
                message="Ollama server not running",
                category="infrastructure",
                fixable=False,
                suggestion="Start Ollama application or run 'ollama serve'",
            )
        except Exception:
            return PreflightCheck(
                name="ollama_running",
                status="warn",
                message="Cannot check Ollama status",
                category="infrastructure",
            )

    def _check_ports_available(self) -> PreflightCheck:
        """Check if required ports are available."""
        ports = [
            (6333, "Qdrant"),
            (11434, "Ollama"),
        ]

        conflicts = []
        for port, service in ports:
            if self._is_port_in_use(port):
                # Check if it's the expected service
                if service == "Qdrant":
                    try:
                        response = requests.get(f"http://localhost:{port}/healthz", timeout=1)
                        if response.status_code == 200:
                            continue  # Qdrant is running on this port, that's fine
                    except:
                        pass
                conflicts.append((port, service))

        if not conflicts:
            return PreflightCheck(
                name="ports_available",
                status="pass",
                message="Required ports available (6333, 11434)",
                category="infrastructure",
            )
        else:
            conflict_str = ", ".join([f"{port} ({service})" for port, service in conflicts])
            return PreflightCheck(
                name="ports_available",
                status="warn",
                message=f"Port conflicts: {conflict_str}",
                category="infrastructure",
                details={"conflicts": conflicts},
                suggestion="Stop conflicting services or change port mappings",
            )

    def _is_port_in_use(self, port: int) -> bool:
        """Check if a port is in use."""
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("localhost", port))
                return False
            except OSError:
                return True

    def _docker_install_instructions(self) -> str:
        """Return OS-specific Docker install instructions."""
        if self.os_type == "macos":
            return "Install Docker Desktop: https://docs.docker.com/desktop/install/mac-install/"
        elif self.os_type == "linux":
            return "Install Docker: https://docs.docker.com/engine/install/"
        elif self.os_type == "windows":
            return "Install Docker Desktop: https://docs.docker.com/desktop/install/windows-install/"
        return "Install Docker: https://docs.docker.com/get-docker/"

    # ═════════════════════════════════════════════════════════════════
    # Phase 3: Model Checks
    # ═════════════════════════════════════════════════════════════════

    def _check_ollama_model(self, model_name: str) -> PreflightCheck:
        """Check if an Ollama model is pulled."""
        try:
            result = subprocess.run(
                ["ollama", "list"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0 and model_name in result.stdout:
                return PreflightCheck(
                    name=f"ollama_model_{model_name}",
                    status="pass",
                    message=f"Model '{model_name}' available",
                    category="models",
                )
            else:
                return PreflightCheck(
                    name=f"ollama_model_{model_name}",
                    status="warn",
                    message=f"Model '{model_name}' not pulled",
                    category="models",
                    fixable=False,
                    suggestion=f"Pull with: ollama pull {model_name}",
                )
        except Exception as e:
            return PreflightCheck(
                name=f"ollama_model_{model_name}",
                status="skip",
                message=f"Could not check model status: {e}",
                category="models",
            )

    def _check_colpali_available(self) -> PreflightCheck:
        """Check if ColPali dependencies are available."""
        try:
            import torch
            from transformers import ColPaliForRetrieval

            return PreflightCheck(
                name="colpali_available",
                status="pass",
                message="ColPali dependencies available ([visual] extra installed)",
                category="models",
            )
        except ImportError:
            return PreflightCheck(
                name="colpali_available",
                status="skip",
                message="ColPali not installed (optional - install with: pip install carta-cc[visual])",
                category="models",
            )

    # ═════════════════════════════════════════════════════════════════
    # Phase 4: Resource Checks
    # ═════════════════════════════════════════════════════════════════

    def _check_disk_space(self, min_gb: float = 2.0) -> PreflightCheck:
        """Check available disk space."""
        try:
            stat = shutil.disk_usage(Path.home())
            available_gb = stat.free / (1024 ** 3)

            if available_gb >= min_gb * 2:  # Plenty of space
                return PreflightCheck(
                    name="disk_space",
                    status="pass",
                    message=f"{available_gb:.1f}GB disk space available",
                    category="resources",
                    details={"available_gb": round(available_gb, 2), "recommended_gb": min_gb * 2},
                )
            elif available_gb >= min_gb:
                return PreflightCheck(
                    name="disk_space",
                    status="warn",
                    message=f"{available_gb:.1f}GB disk space available (low)",
                    category="resources",
                    details={"available_gb": round(available_gb, 2), "recommended_gb": min_gb * 2},
                    suggestion=f"Free up space. {min_gb * 2}GB recommended for visual embedding",
                )
            else:
                return PreflightCheck(
                    name="disk_space",
                    status="fail",
                    message=f"{available_gb:.1f}GB disk space available (insufficient)",
                    category="resources",
                    fixable=False,
                    details={"available_gb": round(available_gb, 2), "required_gb": min_gb},
                    suggestion=f"Free up at least {min_gb}GB of disk space",
                )
        except Exception as e:
            return PreflightCheck(
                name="disk_space",
                status="skip",
                message=f"Could not check disk space: {e}",
                category="resources",
            )

    def _check_memory(self, min_gb: float = 4.0) -> PreflightCheck:
        """Check available memory."""
        try:
            import psutil
            mem = psutil.virtual_memory()
            total_gb = mem.total / (1024 ** 3)
            available_gb = mem.available / (1024 ** 3)

            if total_gb >= min_gb:
                return PreflightCheck(
                    name="memory",
                    status="pass",
                    message=f"{total_gb:.1f}GB RAM available",
                    category="resources",
                    details={
                        "total_gb": round(total_gb, 2),
                        "available_gb": round(available_gb, 2),
                        "percent_used": mem.percent,
                    },
                )
            else:
                return PreflightCheck(
                    name="memory",
                    status="warn",
                    message=f"{total_gb:.1f}GB RAM (low for visual embedding)",
                    category="resources",
                    details={"total_gb": round(total_gb, 2), "recommended_gb": min_gb},
                    suggestion=f"Visual embedding performs better with {min_gb}GB+ RAM",
                )
        except ImportError:
            # psutil not available, skip with info
            return PreflightCheck(
                name="memory",
                status="skip",
                message="Memory check skipped (psutil not installed)",
                category="resources",
            )

    def _check_gpu_available(self) -> PreflightCheck:
        """Check if GPU is available for inference."""
        try:
            import torch
            if torch.cuda.is_available():
                gpu_count = torch.cuda.device_count()
                gpu_name = torch.cuda.get_device_name(0)
                return PreflightCheck(
                    name="gpu_available",
                    status="pass",
                    message=f"GPU detected: {gpu_name}",
                    category="resources",
                    details={"gpu_count": gpu_count, "gpu_name": gpu_name},
                )
            elif hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
                return PreflightCheck(
                    name="gpu_available",
                    status="pass",
                    message="Apple Silicon GPU (MPS) detected",
                    category="resources",
                )
            else:
                return PreflightCheck(
                    name="gpu_available",
                    status="pass",
                    message="No GPU detected (CPU mode)",
                    category="resources",
                    suggestion="CPU mode works but is slower. GPU recommended for visual embedding",
                )
        except ImportError:
            return PreflightCheck(
                name="gpu_available",
                status="skip",
                message="GPU check skipped (torch not installed)",
                category="resources",
            )


def run_preflight_checks(interactive: bool = True, verbose: bool = False) -> PreflightResult:
    """Convenience function to run all preflight checks.
    
    Args:
        interactive: Whether to prompt user for auto-fix decisions
        verbose: Whether to include detailed output
        
    Returns:
        PreflightResult with all check results
    """
    checker = PreflightChecker(interactive=interactive, verbose=verbose)
    return checker.run()
