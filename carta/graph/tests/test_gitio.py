"""Git plumbing used by the impact query and the scan finding."""
from __future__ import annotations

import pytest

from carta.graph.gitio import (
    GitError, default_branch, deleted_and_renamed, diff_names, ignored_paths,
)
from carta.graph.tests.conftest import requires_git

pytestmark = requires_git


def test_default_branch_falls_back_to_main_without_a_remote(git_repo):
    git_repo.write("a.md", "x\n")
    git_repo.commit("first", 0)
    assert default_branch(git_repo.root) == "main"


def test_default_branch_reads_origin_head(git_repo):
    git_repo.write("a.md", "x\n")
    sha = git_repo.commit("first", 0)
    git_repo.git("update-ref", "refs/remotes/origin/trunk", sha)
    git_repo.git("symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/trunk")
    assert default_branch(git_repo.root) == "trunk"


def test_diff_names_lists_added_modified_and_deleted_sorted(git_repo):
    git_repo.write("src/a.py", "1\n")
    git_repo.write("src/gone.py", "1\n")
    git_repo.write("docs/a.md", "x\n")
    git_repo.commit("first", 0)
    git_repo.write("src/a.py", "2\n")
    git_repo.write("src/new file.py", "1\n")
    git_repo.rm("src/gone.py")
    git_repo.commit("second", 1)
    assert diff_names(git_repo.root, "HEAD~1..HEAD") == [
        "src/a.py", "src/gone.py", "src/new file.py"]


def test_diff_names_reports_a_rename_as_both_paths(git_repo):
    git_repo.write("src/old.py", "print('a long enough body to be detected as a rename')\n")
    git_repo.commit("first", 0)
    git_repo.git("mv", "src/old.py", "src/new.py")
    git_repo.commit("second", 1)
    assert diff_names(git_repo.root, "HEAD~1..HEAD") == ["src/new.py", "src/old.py"]


def test_diff_names_is_relative_to_a_subdirectory_root(git_repo):
    git_repo.write("pkg/src/a.py", "1\n")
    git_repo.write("other/b.py", "1\n")
    git_repo.commit("first", 0)
    git_repo.write("pkg/src/a.py", "2\n")
    git_repo.write("other/b.py", "2\n")
    git_repo.commit("second", 1)
    assert diff_names(git_repo.root / "pkg", "HEAD~1..HEAD") == ["src/a.py"]


def test_diff_names_bad_range_raises_giterror(git_repo):
    git_repo.write("a.md", "x\n")
    git_repo.commit("first", 0)
    with pytest.raises(GitError):
        diff_names(git_repo.root, "no-such-ref...HEAD")
    with pytest.raises(GitError):
        diff_names(git_repo.root, "--output=/tmp/x")


def test_deleted_and_renamed(git_repo):
    body = "def parse():\n    return 'a body long enough for rename detection'\n"
    git_repo.write("src/old_frame.py", body)
    git_repo.write("src/legacy.py", "x = 1\n")
    git_repo.write("tools/gen/a.py", "a = 1\n")
    git_repo.write("keep.py", "k = 1\n")
    git_repo.commit("first", 0)
    git_repo.git("mv", "src/old_frame.py", "src/frame.py")
    git_repo.rm("src/legacy.py")
    git_repo.rm("tools/gen/a.py")
    git_repo.commit("second", 1)
    deleted, renamed = deleted_and_renamed(git_repo.root, 100)
    assert deleted == frozenset({"src/legacy.py", "tools/gen/a.py"})
    assert renamed == {"src/old_frame.py": "src/frame.py"}


def test_deleted_and_renamed_newest_record_wins_and_limit_applies(git_repo):
    body = "def parse():\n    return 'a body long enough for rename detection'\n"
    git_repo.write("a.py", body)
    git_repo.commit("c0", 0)
    git_repo.git("mv", "a.py", "b.py")
    git_repo.commit("c1", 1)
    git_repo.write("a.py", "unrelated = 1\n")
    git_repo.commit("c2", 2)
    git_repo.rm("a.py")
    git_repo.commit("c3", 3)
    deleted, renamed = deleted_and_renamed(git_repo.root, 100)
    assert "a.py" in deleted and "a.py" not in renamed      # the deletion is newer
    deleted, renamed = deleted_and_renamed(git_repo.root, 1)
    assert deleted == frozenset({"a.py"}) and renamed == {}


def test_deleted_and_renamed_relative_to_a_subdirectory_root(git_repo):
    git_repo.write("pkg/src/gone.py", "x = 1\n")
    git_repo.write("other/gone.py", "y = 1\n")
    git_repo.commit("first", 0)
    git_repo.rm("pkg/src/gone.py")
    git_repo.rm("other/gone.py")
    git_repo.commit("second", 1)
    deleted, _ = deleted_and_renamed(git_repo.root / "pkg", 100)
    assert deleted == frozenset({"src/gone.py"})


def test_deleted_and_renamed_outside_a_repository_raises(tmp_path):
    with pytest.raises(GitError):
        deleted_and_renamed(tmp_path, 10)


def test_ignored_paths(git_repo):
    git_repo.write(".gitignore", "htmlcov/\n*.log\n")
    git_repo.write("a.md", "x\n")
    git_repo.commit("first", 0)
    assert ignored_paths(git_repo.root, ["htmlcov/index.html", "docs/a.md", "run.log"]) == \
        frozenset({"htmlcov/index.html", "run.log"})
    assert ignored_paths(git_repo.root, ["docs/a.md"]) == frozenset()
    assert ignored_paths(git_repo.root, []) == frozenset()


def test_ignored_paths_outside_a_repository_is_empty(tmp_path):
    assert ignored_paths(tmp_path, ["a/b.py"]) == frozenset()
