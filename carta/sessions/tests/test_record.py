"""Session-record builder: noise filtering, sidechains, extraction, caps."""

import pytest

from carta.sessions.record import build_record, render_record, RecordLimits
from carta.sessions.transcript import read_transcript
from carta.sessions.tests.synth import Transcript, standard_session


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "widgets"
    root.mkdir()
    return root


def _build(tmp_path, tr, repo, **kw):
    path = tr.write(tmp_path / "t" / f"{tr.session_id}.jsonl")
    return build_record(read_transcript(path), repo_root=repo, **kw)


def test_typed_prompts_verbatim_and_noise_dropped(tmp_path, repo):
    rec = _build(tmp_path, standard_session(str(repo)), repo)
    assert rec.prompts == [
        "Please make the widget counter thread-safe, see issue #42",
        "also add a test for it",
        "/review carefully",
        "open a PR when done",
    ]


def test_sidechain_lines_never_count(tmp_path, repo):
    rec = _build(tmp_path, standard_session(str(repo)), repo)
    assert all("Subagent" not in p for p in rec.prompts)
    assert "src/sidechain_only.py" not in rec.files_edited
    assert rec.final_text == "Done: the counter is locked and PR #57 is open."


def test_metadata(tmp_path, repo):
    rec = _build(tmp_path, standard_session(str(repo)), repo)
    assert rec.session_id == "sess-alpha"
    assert rec.repo == "widgets"
    assert rec.worktree == "widgets"
    assert rec.worktree_path == str(repo)
    assert rec.branch == "feat/widgets"
    assert rec.started_at.startswith("2026-03-02T09:01")
    assert rec.ended_at > rec.started_at
    assert rec.end_ts >= rec.start_ts
    assert rec.title == "Thread-safe widget counter"
    assert rec.summary is None


def test_files_issues_prs_commits(tmp_path, repo):
    rec = _build(tmp_path, standard_session(str(repo)), repo)
    assert rec.files_edited == ["src/counter.py", "tests/test_counter.py"]
    assert rec.commits == ["Make counter thread-safe"]
    assert rec.prs == [57]
    assert rec.issues == [42]


def test_custom_title_beats_ai_title(tmp_path, repo):
    tr = Transcript("s1", str(repo)).prompt("hello there")
    tr.title("machine title").title("my own name", kind="custom-title").title("later machine")
    assert _build(tmp_path, tr, repo).title == "my own name"


def test_legacy_prompt_without_origin_fields_is_kept(tmp_path, repo):
    tr = Transcript("s1", str(repo)).prompt("an older transcript line", with_origin=False)
    assert _build(tmp_path, tr, repo).prompts == ["an older transcript line"]


def test_image_prompt_keeps_text_only(tmp_path, repo):
    tr = Transcript("s1", str(repo)).prompt_blocks("what is in this picture")
    assert _build(tmp_path, tr, repo).prompts == ["what is in this picture"]


def test_no_prompts_returns_none(tmp_path, repo):
    tr = Transcript("s1", str(repo)).meta("only meta").notification("x").say("hi")
    assert _build(tmp_path, tr, repo) is None


def test_garbage_and_partial_lines_are_tolerated(tmp_path, repo):
    tr = Transcript("s1", str(repo)).prompt("first")
    path = tr.write(tmp_path / "s1.jsonl", garbage_line=True)
    rec = build_record(read_transcript(path), repo_root=repo)
    assert rec.prompts == ["first"]


def test_worktree_name_from_dot_worktrees(tmp_path, repo):
    wt = repo / ".worktrees" / "feature-x"
    tr = Transcript("s1", str(wt / "src"), branch="feat/x").prompt("in a worktree subdir")
    rec = _build(tmp_path, tr, repo)
    assert rec.worktree == "feature-x"
    assert rec.worktree_path == str(wt)


def test_worktree_name_from_live_worktree_outside_root(tmp_path, repo):
    outside = tmp_path / "elsewhere" / "wt-b"
    tr = Transcript("s1", str(outside)).prompt("outside")
    rec = _build(tmp_path, tr, repo, worktrees=[outside])
    assert rec.worktree == "wt-b"
    assert rec.repo == "widgets"


def test_caps(tmp_path, repo):
    tr = Transcript("s1", str(repo))
    for i in range(30):
        tr.prompt(f"prompt number {i} " + "word " * 100)
    tr.say("x" * 10_000)
    limits = RecordLimits(max_prompts=10, max_prompt_chars=50, max_final_chars=200)
    rec = _build(tmp_path, tr, repo, limits=limits)
    assert len(rec.prompts) == 10
    # first half and last half are kept, so the start and the end of the work survive
    assert rec.prompts[0].startswith("prompt number 0 ")
    assert rec.prompts[-1].startswith("prompt number 29 ")
    assert rec.prompts_total == 30
    assert all(len(p) <= 51 for p in rec.prompts)  # 50 + ellipsis
    assert len(rec.final_text) <= 201


def test_issue_refs_ignore_hex_colors_and_entities(tmp_path, repo):
    tr = Transcript("s1", str(repo)).prompt(
        "colour #ffcc00 and &#123; are not issues but #7 and issues/9 are; see pull/11")
    rec = _build(tmp_path, tr, repo)
    assert rec.issues == [7, 9]
    assert rec.prs == [11]


def test_render_is_deterministic_and_contains_the_parts(tmp_path, repo):
    rec = _build(tmp_path, standard_session(str(repo)), repo)
    text = render_record(rec)
    assert text == render_record(rec)
    for part in ("thread-safe", "src/counter.py", "Make counter thread-safe",
                 "#42", "#57", "feat/widgets", "Done: the counter"):
        assert part in text


def test_render_includes_summary_when_present(tmp_path, repo):
    rec = _build(tmp_path, standard_session(str(repo)), repo)
    rec.summary = "A three line summary."
    assert "A three line summary." in render_record(rec)


def test_to_payload_round_trip(tmp_path, repo):
    rec = _build(tmp_path, standard_session(str(repo)), repo)
    d = rec.to_dict()
    assert d["session_id"] == "sess-alpha"
    assert d["prompts"][0].startswith("Please make")
    assert isinstance(d["start_ts"], int)


def test_sdk_sessions_are_skipped_unless_included(tmp_path, repo):
    tr = Transcript("s1", str(repo))
    line = tr._base("user", message={"role": "user", "content": "scripted task text"},
                    promptSource="sdk", entrypoint="sdk-py")
    tr.raw(line).say("did it")
    path = tr.write(tmp_path / "s1.jsonl")
    assert build_record(read_transcript(path), repo_root=repo) is None
    rec = build_record(read_transcript(path), repo_root=repo, include_sdk=True)
    assert rec.prompts == ["scripted task text"]
    assert rec.entrypoint == "sdk-py"


@pytest.mark.parametrize("command,subject", [
    ("git commit -q -m 'Fix the frobnicator'", "Fix the frobnicator"),
    ('git -C /w/x commit -qam "Two words\n\nbody text"', "Two words"),
    ("git commit -q -m \"$(cat <<'EOF'\nHeredoc subject line\n\nMore body.\nEOF\n)\"",
     "Heredoc subject line"),
    ("git commit --message='Equals form'", "Equals form"),
    ("git commit -F msg.txt", None),
    ("git status", None),
])
def test_commit_subject_from_command(command, subject):
    from carta.sessions.record import commit_subject
    assert commit_subject(command) == subject


def test_quiet_commit_counts_only_when_it_succeeded(tmp_path, repo):
    tr = Transcript("s1", str(repo)).prompt("commit it")
    tr.tool("Bash", {"command": "git commit -q -m 'Landed change'"}, "c1").tool_result("c1", "")
    tr.tool("Bash", {"command": "git commit -q -m 'Rejected change'"}, "c2")
    tr.raw(tr._base("user", message={"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "c2", "is_error": True,
         "content": "pre-commit hook failed"}]}))
    assert _build(tmp_path, tr, repo).commits == ["Landed change"]
