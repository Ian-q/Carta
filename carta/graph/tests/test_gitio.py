"""Git plumbing used by the impact query and the scan finding."""
from __future__ import annotations

import pytest

from carta.graph.gitio import (
    BlobReader, GitError, default_branch, deleted_and_renamed, diff_names, first_parent_log,
    ignored_paths, ls_tree, rev_list_first_parent, rev_parse, toplevel,
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


# -- history readers used by the replay eval -------------------------------

def _merge_history(repo):
    """root, c1, then a side branch merged with --no-ff; returns the main-line commit ids."""
    repo.write("src/a.py", "1\n")
    repo.write("docs/a.md", "a\n")
    root = repo.commit("root", 0)
    repo.write("src/a.py", "2\n")
    c1 = repo.commit("c1", 1)
    repo.git("checkout", "-q", "-b", "side")
    repo.write("src/b.py", "1\n")
    repo.commit("side 1", 2)
    repo.write("docs/a.md", "a2\n")
    repo.commit("side 2", 3)
    repo.git("checkout", "-q", "main")
    repo.write("src/c.py", "1\n")
    c2 = repo.commit("c2", 4)
    repo.git("merge", "-q", "--no-ff", "-m", "merge side", "side", date_index=5)
    merge = repo.git("rev-parse", "HEAD").strip()
    return root, c1, c2, merge


def test_toplevel_and_head(git_repo, tmp_path):
    git_repo.write("sub/a.md", "x\n")
    sha = git_repo.commit("first", 0)
    assert toplevel(git_repo.root / "sub").resolve() == git_repo.root.resolve()
    assert toplevel(tmp_path) is None
    assert rev_parse(git_repo.root, "HEAD") == sha
    with pytest.raises(GitError):
        rev_parse(git_repo.root, "no-such-ref")
    with pytest.raises(GitError):
        rev_parse(git_repo.root, "--all")


def test_first_parent_log_order_parents_and_changes(git_repo):
    root, c1, c2, merge = _merge_history(git_repo)
    log = first_parent_log(git_repo.root, "HEAD")
    assert [e.sha for e in log] == [root, c1, c2, merge]          # oldest first, side commits absent
    assert log[0].parents == ()
    assert log[1].parents == (root,)
    assert log[3].parents[0] == c2 and len(log[3].parents) == 2
    assert log[0].changes == (("A", "docs/a.md"), ("A", "src/a.py"))
    assert log[1].changes == (("M", "src/a.py"),)
    # the merge is compared with its first parent: the whole side branch at once
    assert log[3].changes == (("M", "docs/a.md"), ("A", "src/b.py"))
    blob = git_repo.git("rev-parse", "HEAD:src/b.py").strip()
    assert log[3].blobs == {"docs/a.md": git_repo.git("rev-parse", "HEAD:docs/a.md").strip(),
                            "src/b.py": blob}


def test_first_parent_log_rename_is_delete_plus_add_and_odd_names(git_repo):
    git_repo.write("src/old name.py", "x = 1\n" * 20)
    git_repo.write("docs/é.md", "x\n")
    git_repo.commit("first", 0)
    git_repo.git("mv", "src/old name.py", "src/new.py")
    git_repo.rm("docs/é.md")
    git_repo.commit("second", 1)
    log = first_parent_log(git_repo.root, "HEAD")
    assert log[1].changes == (("D", "docs/é.md"), ("A", "src/new.py"), ("D", "src/old name.py"))
    assert set(log[1].blobs) == {"src/new.py"}


def test_first_parent_log_empty_commit_and_empty_repo(git_repo):
    with pytest.raises(GitError):
        first_parent_log(git_repo.root, "HEAD")
    git_repo.write("a.md", "x\n")
    git_repo.commit("first", 0)
    empty = git_repo.commit("nothing", 1)
    log = first_parent_log(git_repo.root, "HEAD")
    assert log[1].sha == empty and log[1].changes == ()


def test_first_parent_log_skips_gitlinks(git_repo):
    git_repo.write("a.md", "x\n")
    sha = git_repo.commit("first", 0)
    git_repo.git("update-index", "--add", "--cacheinfo", f"160000,{sha},vendor/lib")
    git_repo.git("commit", "-q", "-m", "add submodule entry", date_index=1)
    log = first_parent_log(git_repo.root, "HEAD")
    assert log[1].changes == ()
    assert "vendor/lib" not in ls_tree(git_repo.root, "HEAD")


def test_rev_list_first_parent_range(git_repo):
    root, c1, c2, merge = _merge_history(git_repo)
    assert rev_list_first_parent(git_repo.root, f"{c1}..HEAD") == [c2, merge]
    assert rev_list_first_parent(git_repo.root, "HEAD") == [root, c1, c2, merge]
    with pytest.raises(GitError):
        rev_list_first_parent(git_repo.root, "--all")


def test_ls_tree_and_blob_reader_roundtrip(git_repo):
    git_repo.write("docs/a.md", "alpha\n")
    git_repo.write("docs/empty.md", "")
    git_repo.write("bin/blob.dat", "")
    (git_repo.root / "bin/blob.dat").write_bytes(b"\x00\xff\n\x00tail")
    git_repo.write("deep/er/x y.txt", "spaced\n")
    git_repo.commit("first", 0)
    git_repo.write("docs/a.md", "beta\n")          # dirty work tree: must not be seen
    tree = ls_tree(git_repo.root, "HEAD")
    assert sorted(tree) == ["bin/blob.dat", "deep/er/x y.txt", "docs/a.md", "docs/empty.md"]
    with BlobReader(git_repo.root) as reader:
        assert reader.read(tree["docs/a.md"]) == b"alpha\n"
        assert reader.read(tree["docs/empty.md"]) == b""
        assert reader.read(tree["bin/blob.dat"]) == b"\x00\xff\n\x00tail"
        assert reader.read(tree["deep/er/x y.txt"]) == b"spaced\n"
        assert reader.read(tree["docs/a.md"]) == b"alpha\n"
        assert reader.reads == 5


def test_ls_tree_is_whole_tree_from_a_subdirectory(git_repo):
    git_repo.write("docs/a.md", "x\n")
    git_repo.write("src/a.py", "x\n")
    git_repo.commit("first", 0)
    assert sorted(ls_tree(git_repo.root / "docs", "HEAD")) == ["docs/a.md", "src/a.py"]


def test_blob_reader_unknown_id_raises_giterror(git_repo):
    git_repo.write("a.md", "x\n")
    git_repo.commit("first", 0)
    with BlobReader(git_repo.root) as reader:
        with pytest.raises(GitError):
            reader.read("0" * 40)
        with pytest.raises(GitError):
            reader.read("not a sha\nHEAD")
        tree = ls_tree(git_repo.root, "HEAD")
        assert reader.read(tree["a.md"]) == b"x\n"      # still usable after a miss


def test_history_readers_outside_a_repository_raise(tmp_path):
    with pytest.raises(GitError):
        first_parent_log(tmp_path, "HEAD")
    with pytest.raises(GitError):
        ls_tree(tmp_path, "HEAD")
