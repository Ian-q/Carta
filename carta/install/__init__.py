"""Carta installation and bootstrap utilities."""

from carta.install.auto_fix import AutoFixError, AutoInstaller, run_auto_fix
from carta.install.bootstrap import run_bootstrap
from carta.install.preflight import (
    PreflightCheck,
    PreflightChecker,
    PreflightResult,
    run_preflight_checks,
)

__all__ = [
    # Bootstrap
    "run_bootstrap",
    # Preflight checks
    "PreflightCheck",
    "PreflightChecker",
    "PreflightResult",
    "run_preflight_checks",
    # Auto-fix
    "AutoFixError",
    "AutoInstaller",
    "run_auto_fix",
]
