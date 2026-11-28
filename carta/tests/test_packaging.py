"""The wheel must carry the non-Python files the installed tool copies out."""
import fnmatch
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]


def _package_data() -> list[str]:
    if sys.version_info < (3, 11):
        pytest.skip("tomllib needs Python 3.11")
    import tomllib
    data = tomllib.loads((REPO / "pyproject.toml").read_text())
    return data["tool"]["setuptools"]["package-data"]["carta"]


def test_hook_scripts_are_packaged():
    """`carta init` and `carta doctor --fix` copy these out of the installed package.
    Without them in package-data a wheel install has nothing to copy, and doctor
    reports a dead recall hook as not fixable."""
    patterns = _package_data()
    scripts = sorted((REPO / "carta" / "hooks").glob("*.sh"))
    assert scripts, "no hook scripts found in carta/hooks"
    for s in scripts:
        rel = s.relative_to(REPO / "carta").as_posix()
        assert any(fnmatch.fnmatch(rel, p) for p in patterns), f"{rel} is not in package-data"


def test_skills_are_packaged():
    patterns = _package_data()
    for s in (REPO / "carta" / "skills").glob("*/SKILL.md"):
        rel = s.relative_to(REPO / "carta").as_posix()
        assert any(fnmatch.fnmatch(rel, p) for p in patterns), rel
