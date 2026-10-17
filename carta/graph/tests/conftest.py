"""Shared fixtures for the graph-arm tests: a reproducible throwaway git repository."""
from __future__ import annotations

import os
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

requires_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not on PATH")

_EPOCH = datetime(2020, 1, 1, tzinfo=timezone.utc)


class GitRepo:
    """A git repository in a temp dir with a fixed identity and fixed dates.

    Two repositories built with the same writes and commits have the same
    commit ids, so tests can be exact about history.
    """

    def __init__(self, root: Path, empty_config: Path) -> None:
        self.root = root
        self._env = dict(os.environ)
        self._env.update({
            "GIT_AUTHOR_NAME": "Test Author",
            "GIT_AUTHOR_EMAIL": "author@example.invalid",
            "GIT_COMMITTER_NAME": "Test Author",
            "GIT_COMMITTER_EMAIL": "author@example.invalid",
            "GIT_CONFIG_GLOBAL": str(empty_config),
            "GIT_CONFIG_SYSTEM": str(empty_config),
            "GIT_CONFIG_NOSYSTEM": "1",
            "LC_ALL": "C",
        })
        for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
            self._env.pop(name, None)
        root.mkdir(parents=True, exist_ok=True)
        self.git("init", "-q", "-b", "main")

    def git(self, *args: str, date_index: int | None = None) -> str:
        env = dict(self._env)
        if date_index is not None:
            stamp = (_EPOCH + timedelta(days=date_index)).strftime("%Y-%m-%dT%H:%M:%S+0000")
            env["GIT_AUTHOR_DATE"] = stamp
            env["GIT_COMMITTER_DATE"] = stamp
        proc = subprocess.run(
            ["git", "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null",
             "-c", "core.autocrlf=false", *args],
            cwd=str(self.root), env=env, capture_output=True, text=True,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr}")
        return proc.stdout

    def write(self, rel: str, text: str = "") -> Path:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))
        return path

    def rm(self, rel: str) -> None:
        (self.root / rel).unlink()

    def commit(self, message: str, date_index: int = 0) -> str:
        self.git("add", "-A")
        self.git("commit", "-q", "--allow-empty", "-m", message, date_index=date_index)
        return self.git("rev-parse", "HEAD").strip()


@pytest.fixture
def git_repo(tmp_path):
    """Factory-free form: one repository at ``tmp_path / 'repo'``."""
    empty = tmp_path / "empty.gitconfig"
    empty.write_text("")
    return GitRepo(tmp_path / "repo", empty)


@pytest.fixture
def make_git_repo(tmp_path):
    """Factory: ``make_git_repo('name')`` for tests that need two repositories."""
    empty = tmp_path / "empty.gitconfig"
    empty.write_text("")

    def _make(name: str) -> GitRepo:
        return GitRepo(tmp_path / name, empty)

    return _make
