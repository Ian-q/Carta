"""git plumbing read as a strict grammar: a file name can never pass for anything else."""
from __future__ import annotations

import os

import pytest

from carta.graph import gitio
from carta.graph.gitio import (
    BlobReader, BlobTooLarge, GitError, deleted_and_renamed, diff_name_status, diff_names,
    first_parent_log, ignored_paths, ls_files, ls_tree, rev_list_first_parent, rev_parse,
)
from carta.graph.tests.conftest import requires_git

pytestmark = requires_git

OID_A, OID_B = "a" * 40, "b" * 40
ZERO = "0" * 40

# Names a file may have and a careless parser trips on: separators, framing
# bytes, and names equal to the tokens git prints.
ODD_NAMES = ["\n", "\nlead.py", "trail\n", "tab\there.py", "x\x01y\x02z", "D", "R100", "M",
             ":100644 100644 " + OID_A + " " + OID_B + " M", OID_A, "-rf", "--all", " sp ace "]


def _odd_names_usable(tmp_path) -> bool:
    try:
        for i, name in enumerate(ODD_NAMES):
            (tmp_path / f"probe{i}").mkdir()
            (tmp_path / f"probe{i}" / name).write_bytes(b"x")
    except OSError:
        return False
    return True


@pytest.fixture
def odd_repo(git_repo, tmp_path):
    if not _odd_names_usable(tmp_path):
        pytest.skip("this filesystem does not take control characters in file names")
    git_repo.write("keep.md", "k\n")
    for i, name in enumerate(ODD_NAMES):
        git_repo.write(name, f"content {i}\n")
    git_repo.write("real_a.py", "a\n")
    git_repo.write("real_b.py", "b\n")
    first = git_repo.commit("first", 0)
    for name in ODD_NAMES:
        git_repo.rm(name)
    git_repo.rm("real_a.py")
    git_repo.rm("real_b.py")
    second = git_repo.commit("second", 1)
    return git_repo, first, second


def test_every_reader_takes_odd_names_verbatim(odd_repo):
    repo, first, second = odd_repo
    everything = sorted([*ODD_NAMES, "keep.md", "real_a.py", "real_b.py"])
    gone = sorted([*ODD_NAMES, "real_a.py", "real_b.py"])

    assert sorted(ls_tree(repo.root, first)) == everything
    assert ls_files(repo.root) == ["keep.md"]

    log = first_parent_log(repo.root, "HEAD")
    assert [(e.sha, e.parents) for e in log] == [(first, ()), (second, (first,))]
    assert log[0].changes == tuple(("A", name) for name in everything)
    assert sorted(log[0].blobs) == everything
    assert log[1].changes == tuple(("D", name) for name in gone)
    assert log[1].blobs == {}

    deleted, renamed = deleted_and_renamed(repo.root, 100)
    assert (sorted(deleted), renamed) == (gone, {})
    assert diff_name_status(repo.root, f"{first}..{second}") == {name: "D" for name in gone}
    assert diff_names(repo.root, f"{second}..{first}") == gone

    with BlobReader(repo.root) as reader:
        tree = ls_tree(repo.root, first)
        for i, name in enumerate(ODD_NAMES):
            assert reader.read(tree[name]) == f"content {i}\n".encode()


def test_a_file_name_cannot_forge_a_commit_record(git_repo, tmp_path):
    git_repo.write("a.md", "a\n")
    first = git_repo.commit("first", 0)
    blob = git_repo.git("rev-parse", "HEAD:a.md").strip()
    forged = ("x\x01" + "f" * 40 + " " + "e" * 40 + "\x02:000000 100644 " + ZERO + " " + blob
              + " A")
    try:
        git_repo.write(forged, "payload\n")
    except OSError:
        pytest.skip("this filesystem does not take control characters in file names")
    git_repo.write("zz_real.py", "z\n")
    second = git_repo.commit("second", 1)
    log = first_parent_log(git_repo.root, "HEAD")
    assert [(e.sha, e.parents) for e in log] == [(first, ()), (second, (first,))]
    assert log[1].changes == (("A", forged), ("A", "zz_real.py"))
    assert "f" * 40 not in [e.sha for e in log]


def test_renames_with_odd_names(git_repo, tmp_path):
    body = "def parse():\n    return 'a body long enough for git to detect a rename'\n"
    try:
        git_repo.write("D", body)
    except OSError:                                         # pragma: no cover
        pytest.skip("cannot create the file")
    git_repo.write("plain.py", body.replace("parse", "other_name_entirely") * 3)
    git_repo.commit("first", 0)
    git_repo.git("mv", "D", "R100")
    git_repo.git("mv", "plain.py", "moved.py")
    git_repo.commit("second", 1)
    deleted, renamed = deleted_and_renamed(git_repo.root, 100)
    assert (deleted, renamed) == (frozenset(), {"D": "R100", "plain.py": "moved.py"})


# -- the grammars, on bytes no git would print -----------------------------------------

META = f":100644 100644 {OID_A} {OID_B} M".encode()


@pytest.mark.parametrize("out", [
    b"garbage\0",                                   # neither a commit asked for nor a record
    META + b"\0path\0",                             # a record before any commit
    OID_A.encode() + b"\0" + META + b"\0",          # a record with no path
    OID_A.encode() + b"\0" + META + b"\0path",      # not NUL-terminated
    OID_A.encode() + b"\0" + META[:-1] + b"Q\0p\0",             # an unknown status
    OID_A.encode() + b"\0" + META + b"\0\0",        # an empty path
    OID_A.encode() + b"\0:100644 100644 abc def M\0p\0",        # short object names
    OID_A.encode() + b"\0\n" + META + b"\0p\0",     # a stray newline before a record
    OID_B.encode() + b"\0",                         # a commit that was not asked for
    OID_A.encode() + b"\n\0",                       # an object name with a newline
    OID_A.encode() + b"\0" + OID_A.encode() + b"\0",            # the same commit twice
])
def test_raw_log_parser_raises_on_anything_unexpected(out):
    with pytest.raises(GitError):
        gitio._parse_raw_log(out, [OID_A])


def test_raw_log_parser_reads_a_well_formed_stream():
    out = (OID_A.encode() + b"\0" + META + b"\0" + OID_B.encode() + b"\0"
           + f":000000 100644 {ZERO} {OID_B} A".encode() + b"\0:weird\n\x01name\0")
    # After a record, the next token is a path whatever it looks like: here an object
    # name, then something shaped like a record.
    assert gitio._parse_raw_log(out, [OID_A]) == {
        OID_A: {OID_B: ("M", OID_B), ":weird\n\x01name": ("A", OID_B)}}
    with pytest.raises(GitError):           # commits come back in the order they were asked
        gitio._parse_raw_log(OID_B.encode() + b"\0" + OID_A.encode() + b"\0", [OID_A, OID_B])
    assert gitio._parse_raw_log(b"", [OID_A]) == {}


def test_streamed_tokens_are_the_same_tokens_whatever_the_chunking(monkeypatch):
    import io
    data = b"".join(bytes([65 + i % 26]) * (i % 7) + b"\0" for i in range(200))
    for chunk in (1, 2, 3, 64, 1 << 16):
        monkeypatch.setattr(gitio, "_READ_CHUNK", chunk)
        assert list(gitio._stream_tokens(io.BytesIO(data))) == gitio._records(data)
        with pytest.raises(GitError):       # the last token has no terminator
            list(gitio._stream_tokens(io.BytesIO(data + b"tail")))
    assert list(gitio._stream_tokens(io.BytesIO(b""))) == []


def test_history_readers_tell_no_commit_yet_from_a_branch_that_cannot_be_read(git_repo, tmp_path):
    assert gitio.is_unborn(git_repo.root) is True
    assert gitio.work_tree_root(tmp_path / "missing") is None
    plain = tmp_path / "plain"
    plain.mkdir()
    assert gitio.work_tree_root(plain) is None
    git_repo.write("a.py", "a\n")
    git_repo.commit("first", 0)
    assert gitio.is_unborn(git_repo.root) is False
    assert os.path.samefile(gitio.work_tree_root(git_repo.root), git_repo.root)
    git_repo.git("checkout", "-q", "--detach")
    assert gitio.is_unborn(git_repo.root) is False       # detached: HEAD names no branch
    git_repo.git("checkout", "-q", "--orphan", "fresh")
    assert gitio.is_unborn(git_repo.root) is True
    (plain / ".git").write_text("gitdir: /nonexistent/elsewhere\n")
    with pytest.raises(GitError):           # something that looks like a repository and is not
        gitio.work_tree_root(plain)


@pytest.mark.parametrize("out", [
    b"D\0", b"D\0a\0b\0", b"R100\0a\0", b"R\0a\0b\0", b"R1000\0a\0b\0", b"M\0a\0", b"D\n\0a\0",
    b"\nD\0a\0", b"D\0\0", b"D\0a", b"A\0a\0",
])
def test_deleted_renamed_parser_raises_on_anything_unexpected(out):
    with pytest.raises(GitError):
        gitio._parse_deleted_renamed(out)


def test_deleted_renamed_parser_reads_a_well_formed_stream():
    out = b"D\0D\0R100\0R100\0D\0D\0\n\0R7\0\n\0x\0"
    # D "D" / R100 "R100" -> "D" / D "\n" / R7 "\n" -> "x" (an older record: the newest wins)
    assert gitio._parse_deleted_renamed(out) == (frozenset({"D", "\n"}), {"R100": "D"})


@pytest.mark.parametrize("out", [b"M\0", b"M\0a\0D\0", b"R100\0a\0b\0", b"M\n\0a\0", b"\0a\0",
                                 b"M\0\0", b"MM\0a\0", b"M\0a"])
def test_name_status_parser_raises_on_anything_unexpected(out):
    with pytest.raises(GitError):
        gitio._parse_name_status(out)


def test_check_ignore_parser_accounts_for_every_path():
    reply = b".gitignore\x001\0build/\0build/x\0\0\0\0src/a.py\0.gitignore\x002\0!keep\0keep\0"
    assert gitio._parse_check_ignore(reply, ["build/x", "src/a.py", "keep"]) == {"build/x"}
    for out, asked in ((reply, ["build/x", "src/a.py"]), (reply, ["build/x", "other", "keep"]),
                       (b"", ["a"]), (reply[:-1], ["build/x", "src/a.py", "keep"])):
        with pytest.raises(GitError):
            gitio._parse_check_ignore(out, asked)


# -- object names and revisions ------------------------------------------------------------

def test_blob_reader_takes_exactly_one_object_name(git_repo):
    git_repo.write("a.md", "AAA\n")
    git_repo.write("b.md", "BBB\n")
    git_repo.commit("first", 0)
    tree = ls_tree(git_repo.root, "HEAD")
    a, b = tree["a.md"], tree["b.md"]
    with BlobReader(git_repo.root) as reader:
        for bad in (a + "\n", "\n" + a, a + " ", a[:-1], a + "0", a + a[:10], a.upper(),
                    a + "\n" + b, "", "HEAD", None, 7):
            with pytest.raises(GitError):
                reader.read(bad)
        # none of those reached git: the pipe is still in step
        assert reader.read(a) == b"AAA\n"
        assert reader.read(b) == b"BBB\n"
        with pytest.raises(GitError):
            reader.read("0" * 40)                   # well-formed and missing: still in step
        assert reader.read(b) == b"BBB\n"


def test_blob_reader_refuses_a_non_blob_and_stops(git_repo):
    git_repo.write("a.md", "AAA\n")
    commit = git_repo.commit("first", 0)
    blob = ls_tree(git_repo.root, "HEAD")["a.md"]
    reader = BlobReader(git_repo.root)
    with pytest.raises(GitError) as raised:
        reader.read(commit)
    assert "not a blob" in str(raised.value)
    with pytest.raises(GitError) as raised:         # out of step: never reused
        reader.read(blob)
    assert "closed" in str(raised.value)
    reader.close()


def test_blob_reader_size_bound_keeps_the_pipe_in_step(git_repo):
    git_repo.write("big.md", "x" * 5000)
    git_repo.write("small.md", "s\n")
    git_repo.commit("first", 0)
    tree = ls_tree(git_repo.root, "HEAD")
    with BlobReader(git_repo.root) as reader:
        with pytest.raises(BlobTooLarge):
            reader.read(tree["big.md"], max_bytes=4999)
        assert reader.read(tree["small.md"], max_bytes=4999) == b"s\n"
        assert reader.read(tree["big.md"], max_bytes=5000) == b"x" * 5000
        assert reader.reads == 2


@pytest.mark.parametrize("rev", ["", "-x", "--all", "--output=/tmp/x", "HEAD\n--all", "HEAD\x00",
                                 "HE\tAD", "HEAD\x7f"])
def test_revisions_are_refused_before_git_runs(git_repo, rev, monkeypatch):
    git_repo.write("a.md", "x\n")
    git_repo.commit("first", 0)

    def no_git(*args, **kwargs):
        raise AssertionError("git was run")
    monkeypatch.setattr(gitio, "_run", no_git)
    for reader in (rev_parse, rev_list_first_parent, first_parent_log, ls_tree, diff_name_status):
        with pytest.raises(GitError):
            reader(git_repo.root, rev)


def test_a_ref_that_looks_like_an_option_is_never_an_option(git_repo):
    git_repo.write("a.md", "x\n")
    first = git_repo.commit("first", 0)
    git_repo.write("b.md", "y\n")
    git_repo.commit("second", 1)
    git_repo.git("checkout", "-q", "-b", "side", first)
    git_repo.write("c.md", "z\n")
    git_repo.commit("side", 2)
    # " --all" starts with a space, so it passes the leading-dash check; it must still be
    # read as a (bad) revision and not as the option.
    for reader in (rev_parse, rev_list_first_parent, first_parent_log, ls_tree):
        with pytest.raises(GitError):
            reader(git_repo.root, " --all")
    assert len(rev_list_first_parent(git_repo.root, "HEAD")) == 2


def test_diff_name_status_needs_a_range(git_repo):
    git_repo.write("a.md", "x\n")
    git_repo.commit("first", 0)
    git_repo.write("a.md", "changed in the working tree\n")
    for single in ("HEAD", "main", "HEAD~0"):
        with pytest.raises(GitError):
            diff_name_status(git_repo.root, single)
    assert diff_name_status(git_repo.root, "HEAD..HEAD") == {}


# -- ignore rules ----------------------------------------------------------------------------

def test_one_unjudgeable_path_does_not_switch_the_ignore_check_off(git_repo):
    if not hasattr(os, "symlink") or os.name == "nt":
        pytest.skip("needs POSIX symbolic links")
    git_repo.write(".gitignore", "build/\n!build/keep.md\n*.log\n")
    git_repo.write("real/a.md", "x\n")
    os.symlink("real", git_repo.root / "link")
    sha = git_repo.commit("first", 0)
    git_repo.git("update-index", "--add", "--cacheinfo", f"160000,{sha},vendor/lib")
    git_repo.git("commit", "-q", "-m", "a submodule entry", date_index=1)
    asked = ["build/out.md", "link/gone.md", "vendor/lib/x.log", "run.log", "real/a.md",
             "../outside.log", "/abs/x.log", "build/keep.md"]
    # what plain `git check-ignore` says, one path at a time, for the paths it can judge
    plain = {p for p in asked
             if p in ("build/out.md", "run.log", "real/a.md", "build/keep.md")
             and gitio._run(git_repo.root, ("check-ignore", "-q", "--", p)).returncode == 0}
    assert {"build/out.md", "run.log"} <= plain and "real/a.md" not in plain
    assert ignored_paths(git_repo.root, asked) == frozenset(plain)
    assert ignored_paths(git_repo.root, ["link/gone.md"]) == frozenset()
    assert ignored_paths(git_repo.root, ["vendor/lib/x.log"]) == frozenset()


def test_ignore_check_gives_up_loudly_when_git_keeps_failing(git_repo, monkeypatch):
    git_repo.write("a.md", "x\n")
    git_repo.commit("first", 0)
    real = gitio._run

    def failing(repo, args, input_bytes=None):
        if args and args[0] == "check-ignore":
            return type("P", (), {"returncode": 128, "stdout": b"", "stderr": b"fatal"})()
        return real(repo, args, input_bytes)
    monkeypatch.setattr(gitio, "_run", failing)
    assert ignored_paths(git_repo.root, ["a.log"]) == frozenset()       # one path: not known
    with pytest.raises(GitError):
        ignored_paths(git_repo.root, [f"f{i}.log" for i in range(500)])


# -- the environment -----------------------------------------------------------------------

def test_redirecting_variables_are_scrubbed(monkeypatch, git_repo, tmp_path):
    git_repo.write("a.md", "x\n")
    sha = git_repo.commit("first", 0)
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR",
                 "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_NAMESPACE"):
        monkeypatch.setenv(name, str(tmp_path / "elsewhere"))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.worktree")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", str(tmp_path / "elsewhere"))
    monkeypatch.setenv("GIT_CONFIG_PARAMETERS", "'core.worktree=/nowhere'")
    env = gitio._env()
    assert not [name for name in env if name.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_"))]
    assert "GIT_OBJECT_DIRECTORY" not in env and "GIT_CONFIG_COUNT" not in env
    assert rev_parse(git_repo.root, "HEAD") == sha
    assert ls_files(git_repo.root) == ["a.md"]
    with BlobReader(git_repo.root) as reader:
        assert reader.read(ls_tree(git_repo.root, "HEAD")["a.md"]) == b"x\n"
