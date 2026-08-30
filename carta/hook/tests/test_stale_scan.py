"""Tests for carta.hook.stale_scan — scan core, collectors, and config defaults."""
from pathlib import Path

from carta.config import DEFAULTS

from carta.hook.stale_scan import ChangedDoc, StaleFinding, StaleScanResult, _in_doc_scope, _search_cfg


def test_stale_scan_defaults_present():
    sc = DEFAULTS["hooks"]["stale_scan"]
    assert sc["enabled"] is True
    assert sc["block_on_stale"] is False
    assert sc["candidate_threshold"] == 0.65
    assert sc["judge_timeout_s"] == 60
    assert sc["ollama_model"] == "qwen3.5:9b"
    assert sc["max_judge_calls"] == 30


def test_dataclasses_construct():
    cd = ChangedDoc(path="docs/a.md", text="hi")
    assert cd.path == "docs/a.md"
    f = StaleFinding(file="docs/a.md", section="## X", snippet="...", candidate_path="docs/b.md", candidate_score=0.9)
    assert f.candidate_score == 0.9
    r = StaleScanResult()
    assert r.findings == [] and r.scanned == 0 and r.judge_calls == 0 and r.skipped_overflow == 0


def test_search_cfg_forces_rerank_and_colpali_off():
    cfg = {"embed": {"colpali_enabled": True}, "search": {"rerank": {"enabled": True}}}
    out = _search_cfg(cfg)
    assert out["embed"]["colpali_enabled"] is False
    assert out["search"]["rerank"]["enabled"] is False


def test_search_cfg_searches_deeper_than_the_display_top_n():
    """The scan must not inherit `search.top_n` (5).

    A superseding doc fused at rank 6-29 is retrieved, embedded and ranked, then
    truncated away before any threshold can see it — so the relevance floor would
    be tuned against a ceiling. Judge cost is unchanged: one call per chunk."""
    cfg = {"search": {"top_n": 5}}
    assert _search_cfg(cfg)["search"]["top_n"] == 30


def test_search_cfg_depth_is_configurable():
    cfg = {"search": {"top_n": 5}, "hooks": {"stale_scan": {"candidate_depth": 12}}}
    assert _search_cfg(cfg)["search"]["top_n"] == 12


def test_in_doc_scope(tmp_path):
    cfg = {"docs_root": "docs/", "excluded_paths": []}
    assert _in_doc_scope("docs/guide.md", cfg, tmp_path) is True
    assert _in_doc_scope("src/main.py", cfg, tmp_path) is False      # not .md
    assert _in_doc_scope("notes/x.md", cfg, tmp_path) is False        # outside docs_root


# ---------------------------------------------------------------------------
# Task 5: run_stale_scan core
# ---------------------------------------------------------------------------

from carta.hook.stale_scan import run_stale_scan

_CFG = {"embed": {"chunking": {"max_tokens": 400}}, "hooks": {"stale_scan": {"candidate_threshold": 0.65, "max_judge_calls": 30}}}


def _doc(path="docs/a.md"):
    return ChangedDoc(path=path, text="## micro-ROS UART\nThe UART transport uses micro-ROS.\n")


def _fused_hits(dense_specs, sparse_ids):
    """Build lane point lists, fuse them for real, and shape real hit dicts.

    Real fusion, not hand-written scores: the defect under test IS a scale error,
    so a fabricated `score` could hide it.

    dense_specs: [(id, cosine), ...] in dense-lane rank order.
    sparse_ids:  [id, ...] in sparse-lane rank order.
    """
    from unittest.mock import MagicMock
    from carta.embed import pipeline

    def _pt(pid, score):
        p = MagicMock()
        p.id = pid
        p.score = score
        p.payload = {"file_path": f"docs/{pid}.md", "text": pid}
        return p

    dense = [_pt(pid, cos) for pid, cos in dense_specs]
    sparse = [_pt(pid, 10.0) for pid in sparse_ids]
    fused = pipeline._fuse_lanes(dense, sparse, top_n=5, k=2)
    return [pipeline._text_hit(e["point"].payload, e["score"], e["ranks"], e["dense_score"])
            for e in fused]


def _scan_with(hits, cfg=None):
    judged = []

    def judge_fn(section_text, candidate):
        judged.append(candidate)
        return True

    result = run_stale_scan(Path("/repo"), cfg or _CFG, [_doc()],
                            search_fn=lambda q: hits, judge_fn=judge_fn)
    return result, judged


def test_relevant_single_lane_candidate_reaches_the_judge():
    """A doc rank 0 in the dense lane with a strong cosine, absent from sparse,
    fuses to exactly 1/(2+0) = 0.5. That is below the 0.65 `candidate_threshold`,
    so today it is dropped and the supersession is never reported — a MISSED
    WARNING, which is the fail-open direction that matters for this feature."""
    hits = _fused_hits([("b", 0.82)], [])
    assert hits[0]["score"] == 0.5           # pins the RRF scale before behaviour
    assert hits[0]["dense_score"] == 0.82    # genuinely relevant

    result, judged = _scan_with(hits)
    assert judged, "relevant single-lane candidate never reached the judge"
    assert len(result.findings) == 1


def test_bm25_only_candidate_is_never_dropped():
    """A sparse-only hit has no dense cosine to measure, so it cannot be
    MEASURABLY irrelevant. Dropping it is the recall bug being replaced —
    supersession often turns on a shared identifier the dense lane misses."""
    hits = _fused_hits([], ["b"])
    assert hits[0]["dense_score"] is None

    _result, judged = _scan_with(hits)
    assert judged, "BM25-only candidate was silenced instead of judged"


def test_measurably_irrelevant_candidate_is_still_skipped():
    """Proves this is a re-scaling, not a deletion of the gate: a hit ranked 0 in
    BOTH lanes fuses to 1.0 — the maximum — yet a 0.18 cosine says it is not
    about the same thing. A high fused score must not buy past a low cosine."""
    hits = _fused_hits([("b", 0.18)], ["b"])
    assert hits[0]["score"] == 1.0

    _result, judged = _scan_with(hits)
    assert not judged, "measurably irrelevant candidate still cost a judge call"


def test_gate_does_not_read_the_cross_collection_ordering_field():
    """`score` is INTRA-collection while ordering comes from `fused_score`, so
    hits[0] is not necessarily the best candidate. Gating on hits[0] alone
    discards better candidates sitting behind it."""
    from carta.embed import pipeline

    weak = _fused_hits([("weak", 0.20)], ["weak"])       # score 1.0, cosine 0.20
    strong = _fused_hits([("strong", 0.88)], [])          # score 0.5, cosine 0.88
    merged = pipeline._rrf_merge_collections([weak, strong], top_n=5)
    assert merged[0]["dense_score"] == 0.20               # weak leads the list

    _result, judged = _scan_with(merged)
    assert judged, "a strong candidate behind hits[0] was never considered"
    assert judged[0]["source"] == "docs/strong.md"


def test_default_search_fn_actually_applies_search_cfg(monkeypatch):
    """The call site must apply `_search_cfg`, not just possess it.

    Testing `_search_cfg` as a pure function proves nothing about the one place it
    is used. Mutating `run_search(q, _search_cfg(cfg), ...)` to pass `cfg` raw —
    dropping the depth override, the ColPali kill and the rerank kill in one edit
    — previously failed no test in the suite.
    """
    import carta.embed.pipeline as pipeline

    seen = {}

    def fake_run_search(q, cfg, *a, trace_stages=None, **kw):
        seen.update(cfg)
        return []

    monkeypatch.setattr(pipeline, "run_search", fake_run_search)
    run_stale_scan(Path("/repo"), _CFG, [_doc()], judge_fn=lambda s, c: False)

    search = seen.get("search", {})
    assert search.get("top_n") == 30, f"depth override not applied at the call site: {seen}"
    assert seen.get("embed", {}).get("colpali_enabled") is False
    assert search.get("rerank", {}).get("enabled") is False


def test_scan_counts_candidates_truncated_before_the_gate(monkeypatch):
    """Candidates cut by search depth before the gate are counted, not silent.

    The scan now searches to `candidate_depth`, so this should normally be 0 — a
    non-zero value means even that depth is too shallow for the corpus, which is
    otherwise invisible: a missed warning looks identical to no warning.
    """
    import carta.embed.pipeline as pipeline

    fused = [{"source": f"docs/c{i}.md", "score": 0.9 - i * 0.01, "excerpt": "x"}
             for i in range(12)]

    def fake_run_search(q, cfg, *a, trace_stages=None, **kw):
        if trace_stages is not None:
            trace_stages["retrieved"] = list(fused)
            trace_stages["fused"] = list(fused)
            trace_stages["final"] = fused[:5]
        return fused[:5]

    monkeypatch.setattr(pipeline, "run_search", fake_run_search)

    result = run_stale_scan(Path("/repo"), _CFG, [_doc()],
                            judge_fn=lambda section_text, candidate: False)

    assert result.candidates_truncated == 7, (
        f"expected the 7 fused candidates cut by top_n to be counted, "
        f"got {result.candidates_truncated}"
    )


def test_injected_search_fn_leaves_the_truncation_counter_at_zero():
    """An injected search_fn carries no stage data; the counter must stay 0
    rather than fabricate a measurement."""
    search = lambda q: [{"source": "docs/cobs.md", "score": 0.91, "excerpt": "e"}]
    result = run_stale_scan(Path("/repo"), _CFG, [_doc()], search_fn=search,
                            judge_fn=lambda s, c: False)
    assert result.candidates_truncated == 0


def test_stale_section_with_yes_judge_yields_finding():
    search = lambda q: [{"source": "docs/cobs.md", "score": 0.91, "excerpt": "COBS+JSON replaced micro-ROS"}]
    judge = lambda section_text, candidate: True
    result = run_stale_scan(Path("/repo"), _CFG, [_doc()], search_fn=search, judge_fn=judge)
    assert result.scanned == 1
    assert len(result.findings) == 1
    f = result.findings[0]
    assert f.file == "docs/a.md"
    assert f.candidate_path == "docs/cobs.md"
    assert f.candidate_score == 0.91


def test_related_section_with_no_judge_yields_nothing():
    search = lambda q: [{"source": "docs/cobs.md", "score": 0.91, "excerpt": "related but not superseding"}]
    judge = lambda section_text, candidate: False
    result = run_stale_scan(Path("/repo"), _CFG, [_doc()], search_fn=search, judge_fn=judge)
    assert result.findings == []
    assert result.judge_calls == 1


def test_below_threshold_skips_judge():
    calls = []
    search = lambda q: [{"source": "docs/cobs.md", "score": 0.40, "excerpt": "weak match"}]
    judge = lambda section_text, candidate: calls.append(1) or True
    result = run_stale_scan(Path("/repo"), _CFG, [_doc()], search_fn=search, judge_fn=judge)
    assert result.findings == []
    assert result.judge_calls == 0
    assert calls == []


def test_judge_none_fails_open():
    search = lambda q: [{"source": "docs/cobs.md", "score": 0.91, "excerpt": "x"}]
    judge = lambda section_text, candidate: None
    result = run_stale_scan(Path("/repo"), _CFG, [_doc()], search_fn=search, judge_fn=judge)
    assert result.findings == []


def test_self_hits_filtered():
    # Only hit is the doc itself -> no external candidate -> no judge, no finding
    search = lambda q: [{"source": "docs/a.md", "score": 0.99, "excerpt": "itself"}]
    judge = lambda section_text, candidate: True
    result = run_stale_scan(Path("/repo"), _CFG, [_doc("docs/a.md")], search_fn=search, judge_fn=judge)
    assert result.judge_calls == 0
    assert result.findings == []


def test_search_error_fails_open_per_section():
    def boom(q):
        raise RuntimeError("qdrant down")
    result = run_stale_scan(Path("/repo"), _CFG, [_doc()], search_fn=boom, judge_fn=lambda s, c: True)
    assert result.findings == []


def test_max_judge_calls_cap_reports_overflow():
    cfg = {"embed": {"chunking": {"max_tokens": 400}}, "hooks": {"stale_scan": {"candidate_threshold": 0.65, "max_judge_calls": 1}}}
    docs = [
        ChangedDoc(path="docs/a.md", text="## A\nalpha body text\n"),
        ChangedDoc(path="docs/b.md", text="## B\nbeta body text\n"),
    ]
    search = lambda q: [{"source": "docs/other.md", "score": 0.9, "excerpt": "x"}]
    judge = lambda section_text, candidate: True
    result = run_stale_scan(Path("/repo"), cfg, docs, search_fn=search, judge_fn=judge)
    assert result.judge_calls == 1
    assert result.skipped_overflow == 1
    assert len(result.findings) == 1


# ---------------------------------------------------------------------------
# Task 6: _stale_judge — default judge prompt over ollama_yesno
# ---------------------------------------------------------------------------

from unittest.mock import patch
from carta.hook.stale_scan import _stale_judge

_JCFG = {"embed": {"ollama_url": "http://x"}, "hooks": {"stale_scan": {"ollama_model": "m", "judge_timeout_s": 5}}}


def test_stale_judge_calls_ollama_with_both_excerpts():
    cand = {"source": "docs/cobs.md", "excerpt": "COBS+JSON replaced micro-ROS"}
    with patch("carta.hook.stale_scan.ollama_json",
               return_value={"section_claim": "micro-ROS", "doc_clause": "COBS+JSON replaced micro-ROS", "conflict": True}) as oj:
        out = _stale_judge("micro-ROS UART section", cand, _JCFG)
    assert out is True
    args, kwargs = oj.call_args
    # positional: (ollama_url, model, system, user)
    assert args[0] == "http://x"
    assert args[1] == "m"
    assert "micro-ROS UART section" in args[3]
    assert "COBS+JSON replaced micro-ROS" in args[3]
    assert kwargs["timeout_s"] == 5


# ---------------------------------------------------------------------------
# Task 7: git collectors — collect_staged, collect_pushed
# ---------------------------------------------------------------------------

import subprocess
import pytest
from carta.hook.stale_scan import collect_staged, collect_pushed


def _git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.email", "t@t.t")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "docs").mkdir()
    return tmp_path


def test_collect_staged_returns_scoped_docs(repo):
    (repo / "docs" / "a.md").write_text("## A\nbody\n")
    (repo / "src.py").write_text("print()\n")
    _git(repo, "add", "docs/a.md", "src.py")
    cfg = {"docs_root": "docs/", "excluded_paths": []}
    docs = collect_staged(repo, cfg)
    paths = [d.path for d in docs]
    assert paths == ["docs/a.md"]              # .py excluded by scope
    assert "## A" in docs[0].text


def test_collect_pushed_uses_range_and_pushed_tip(repo):
    (repo / "docs" / "a.md").write_text("v1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "c1")
    base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True).stdout.strip()
    (repo / "docs" / "a.md").write_text("## A\nv2 changed\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "c2")
    tip = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True).stdout.strip()
    cfg = {"docs_root": "docs/", "excluded_paths": []}
    stdin_lines = [f"refs/heads/main {tip} refs/heads/main {base}"]
    docs = collect_pushed(repo, cfg, stdin_lines)
    assert [d.path for d in docs] == ["docs/a.md"]
    assert "v2 changed" in docs[0].text


def test_collect_pushed_new_branch_zero_oid(repo):
    # remote_sha is the zero OID -> fall back to default-branch merge-base range
    (repo / "docs" / "a.md").write_text("## A\nnew branch content\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "c1")
    tip = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True).stdout.strip()
    cfg = {"docs_root": "docs/", "excluded_paths": []}
    zero = "0" * 40
    stdin_lines = [f"refs/heads/feature {tip} refs/heads/feature {zero}"]
    docs = collect_pushed(repo, cfg, stdin_lines)
    assert [d.path for d in docs] == ["docs/a.md"]


# ---------------------------------------------------------------------------
# Task 1 (slice 2): _range_tip + collect_range + _collect_from_ranges refactor
# ---------------------------------------------------------------------------

from carta.hook.stale_scan import _range_tip, collect_range


def test_range_tip_parses_two_and_three_dot():
    assert _range_tip("a..b") == "b"
    assert _range_tip("a...b") == "b"
    assert _range_tip("main...HEAD") == "HEAD"
    assert _range_tip("main...") == "HEAD"      # trailing-empty right side
    assert _range_tip("origin/main..feature") == "feature"


def test_collect_range_returns_scoped_docs_at_tip(repo):
    (repo / "docs" / "a.md").write_text("v1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "c1")
    base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True).stdout.strip()
    (repo / "docs" / "a.md").write_text("## A\nv2 changed\n")
    (repo / "docs" / "b.md").write_text("## B\nbrand new\n")
    (repo / "src.py").write_text("print()\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "c2")
    cfg = {"docs_root": "docs/", "excluded_paths": []}
    docs = collect_range(repo, cfg, f"{base}...HEAD")
    paths = sorted(d.path for d in docs)
    assert paths == ["docs/a.md", "docs/b.md"]     # src.py excluded by scope
    by_path = {d.path: d.text for d in docs}
    assert "v2 changed" in by_path["docs/a.md"]     # tip content, not v1
    assert "brand new" in by_path["docs/b.md"]


def test_collect_range_two_dot_range(repo):
    (repo / "docs" / "a.md").write_text("v1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "c1")
    base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True).stdout.strip()
    (repo / "docs" / "a.md").write_text("## A\nv2\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "c2")
    cfg = {"docs_root": "docs/", "excluded_paths": []}
    docs = collect_range(repo, cfg, f"{base}..HEAD")
    assert [d.path for d in docs] == ["docs/a.md"]
    assert "v2" in docs[0].text


def test_finding_carries_candidate_excerpt():
    from carta.hook.stale_scan import run_stale_scan, ChangedDoc

    doc = ChangedDoc(path="docs/a.md", text="## Title\n\nOld approach uses polling.")
    search_fn = lambda q: [{
        "source": "docs/b.md",
        "score": 0.9,
        "excerpt": "The polling approach was replaced by push events.",
    }]
    judge_fn = lambda section_text, candidate: True

    result = run_stale_scan(
        repo_root=None, cfg={}, changed_docs=[doc],
        search_fn=search_fn, judge_fn=judge_fn,
    )
    assert len(result.findings) == 1
    assert result.findings[0].candidate_excerpt == "The polling approach was replaced by push events."


def test_judge_none_counts_as_error_not_finding():
    """A judge that returns None (timeout/error) must be counted, not silently
    treated as 'not stale' — otherwise a non-responding judge looks like a clean bill."""
    from carta.hook.stale_scan import run_stale_scan, ChangedDoc

    doc = ChangedDoc(path="docs/a.md", text="## Title\n\nOld approach uses polling.")
    search_fn = lambda q: [{"source": "docs/b.md", "score": 0.9, "excerpt": "replaced by push."}]
    judge_fn = lambda section_text, candidate: None  # simulate timeout

    result = run_stale_scan(
        repo_root=None, cfg={}, changed_docs=[doc],
        search_fn=search_fn, judge_fn=judge_fn,
    )
    assert result.findings == []
    assert result.judge_calls == 1
    assert result.judge_errors == 1


def test_stale_judge_true_only_on_conflict(monkeypatch):
    from carta.hook import stale_scan

    cfg = {"embed": {"ollama_url": "http://x"}, "hooks": {"stale_scan": {}}}
    cand = {"source": "docs/a.md", "excerpt": "X was removed and replaced by Y."}

    monkeypatch.setattr(stale_scan, "ollama_json",
                        lambda *a, **k: {"section_claim": "uses X", "doc_clause": "X removed", "conflict": True})
    assert stale_scan._stale_judge("we use X", cand, cfg) is True

    monkeypatch.setattr(stale_scan, "ollama_json",
                        lambda *a, **k: {"section_claim": "uses X", "doc_clause": "X discussed", "conflict": False})
    assert stale_scan._stale_judge("we use X", cand, cfg) is False


def test_stale_judge_none_on_bad_or_missing_output(monkeypatch):
    from carta.hook import stale_scan

    cfg = {"embed": {"ollama_url": "http://x"}, "hooks": {"stale_scan": {}}}
    cand = {"source": "docs/a.md", "excerpt": "..."}

    monkeypatch.setattr(stale_scan, "ollama_json", lambda *a, **k: None)
    assert stale_scan._stale_judge("s", cand, cfg) is None

    monkeypatch.setattr(stale_scan, "ollama_json", lambda *a, **k: {"section_claim": "x"})  # no 'conflict'
    assert stale_scan._stale_judge("s", cand, cfg) is None


def test_hit_with_none_score_does_not_crash_the_scan():
    """A hit with no usable relevance signal must be skipped, not raise TypeError
    and unwind the whole fail-open scan.

    This is the LEGACY (non-hybrid) shape: no `lane_ranks`, no `dense_score`, and
    `score` explicitly None, so there is nothing to measure and nothing to compare.
    Contrast `test_bm25_only_candidate_is_never_dropped`, where `lane_ranks` IS
    present — a hybrid sparse-only hit has no cosine *by construction* and must
    reach the judge rather than be silenced."""
    search = lambda q: [{"source": "docs/cobs.md", "score": None, "excerpt": "x"}]
    judge = lambda section_text, candidate: True
    result = run_stale_scan(Path("/repo"), _CFG, [_doc()], search_fn=search, judge_fn=judge)
    assert result.scanned == 1
    assert result.findings == []  # no measurable signal → no judge, no finding
