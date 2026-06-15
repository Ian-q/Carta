---
id: 2026-06-10-eval-trust-hook-rerank
title: "Eval Trust + Hook Rerank Decoupling (v0.9.1) Implementation Plan"
status: shipped
related:
  - 2026-06-10-eval-trust-hook-rerank-design
date: 2026-06-10
---

# Eval Trust + Hook Rerank Decoupling (v0.9.1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make a silently fail-open reranker impossible to mistake for a result (eval assertion), guarantee the proactive-recall hook never pays rerank latency, and bump CI actions off Node 20 before the 2026-06-16 forced migration — shipped as v0.9.1.

**Architecture:** Both rerankers stamp `rerank_score` on hits only when they actually ran; every fail-open path returns unstamped hits. `run_search` gains an optional `stats` out-param that captures this signal before the key is stripped (zero blast radius on existing callers). `cmd_eval` aggregates per-query stats, prints an applied-count line, and exits 1 when rerank was requested but applied on zero queries. The hook extends its existing colpali-off config override to also force `search.rerank.enabled` off.

**Tech Stack:** Python 3.10+, pytest, unittest.mock. Spec: `docs/superpowers/specs/2026-06-10-eval-trust-hook-rerank-design.md`.

---

### Task 1: `run_search` rerank stats out-param

**Files:**
- Modify: `carta/embed/pipeline.py:1470` (signature) and `:1647-1667` (rerank block + return)
- Test: `carta/tests/test_pipeline.py` (new class after `TestRunSearch`, ~line 624)

- [ ] **Step 1: Write the failing tests**

Append after the `TestRunSearch` class in `carta/tests/test_pipeline.py`:

```python
class TestRunSearchRerankStats:
    """run_search reports via the optional stats out-param whether the reranker
    actually ran. Both backends stamp rerank_score only on success; fail-open
    paths return unstamped hits — that absence is the 0.8.0 silent-failure
    signature this surfaces."""

    def _cfg(self, rerank_enabled=True):
        return {
            "project_name": "test-project",
            "qdrant_url": "http://localhost:6333",
            "embed": {
                "ollama_url": "http://localhost:11434",
                "ollama_model": "nomic-embed-text",
                "colpali_enabled": False,
            },
            "search": {"top_n": 5, "rerank": {"enabled": rerank_enabled, "candidate_pool": 30}},
            "modules": {"doc_search": True},
        }

    def _run(self, cfg, stats, rerank_side_effect=None, points=True):
        from unittest.mock import patch, MagicMock
        from carta.embed.pipeline import run_search

        point = MagicMock()
        point.score = 0.9
        point.payload = {"file_path": "docs/a.md", "text": "alpha"}
        mock_client = MagicMock()
        mock_client.query_points.return_value = MagicMock(points=[point] if points else [])

        patches = [
            patch("carta.embed.pipeline.QdrantClient", return_value=mock_client),
            patch("carta.embed.pipeline.get_embedding", return_value=[0.0] * 768),
            patch("carta.embed.pipeline.collection_is_hybrid", return_value=False),
            patch("carta.search.scoped.get_search_collections", return_value=["test-project_doc"]),
            patch("carta.embed.pipeline.find_config", return_value="/fake/.carta/config.yaml"),
        ]
        if rerank_side_effect is not None:
            patches.append(patch("carta.search.rerank.rerank_dispatch", side_effect=rerank_side_effect))
        from contextlib import ExitStack
        with ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            return run_search("q", cfg, stats=stats)

    def test_stats_reports_applied_when_reranker_stamps_scores(self):
        """Reranker ran: hits come back stamped with rerank_score -> applied True,
        and the transient key is still stripped from the returned hits."""
        def fake_rerank(query, pool, **kwargs):
            for h in pool:
                h["rerank_score"] = 1.0
            return pool

        stats = {}
        results = self._run(self._cfg(rerank_enabled=True), stats, rerank_side_effect=fake_rerank)
        assert stats == {"rerank_requested": True, "rerank_applied": True}
        assert results, "search should return the mocked hit"
        assert all("rerank_score" not in h for h in results), "transient key must be stripped"

    def test_stats_reports_fail_open_when_hits_unstamped(self):
        """Fail-open: reranker returns the pool unchanged (no rerank_score) -> applied False."""
        stats = {}
        self._run(self._cfg(rerank_enabled=True), stats, rerank_side_effect=lambda q, pool, **kw: pool)
        assert stats == {"rerank_requested": True, "rerank_applied": False}

    def test_stats_reports_not_requested_when_rerank_disabled(self):
        stats = {}
        self._run(self._cfg(rerank_enabled=False), stats)
        assert stats == {"rerank_requested": False, "rerank_applied": False}

    def test_stats_requested_but_no_results_is_not_applied(self):
        """Rerank enabled but zero hits: the rerank block is skipped -> applied False."""
        stats = {}
        self._run(self._cfg(rerank_enabled=True), stats, points=False)
        assert stats == {"rerank_requested": True, "rerank_applied": False}

    def test_stats_default_none_is_unchanged_behavior(self):
        """No stats dict passed: run_search works exactly as before."""
        results = self._run(self._cfg(rerank_enabled=False), None)
        assert isinstance(results, list)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest carta/tests/test_pipeline.py::TestRunSearchRerankStats -v`
Expected: FAIL — `TypeError: run_search() got an unexpected keyword argument 'stats'`

- [ ] **Step 3: Implement**

In `carta/embed/pipeline.py`, change the signature (line 1470):

```python
def run_search(query: str, cfg: dict, verbose: bool = False, stats: dict | None = None) -> list[dict]:
```

Add to the docstring Args block: `stats: optional dict; when provided, run_search records
"rerank_requested" and "rerank_applied" (rerank_score observed on hits before stripping).`

Replace the rerank block + return (lines 1647-1667) with:

```python
    # Optional second-stage cross-encoder reranking (opt-in via search.rerank.enabled)
    rerank_applied = False
    if rerank_enabled and all_results:
        from carta.search.rerank import rerank_dispatch
        pool = all_results[:candidate_pool]
        # rerank_hits reads chunk text from key "text"; run_search stores it as "excerpt"
        for h in pool:
            h["text"] = h.get("excerpt", "")
        all_results = rerank_dispatch(
            query,
            pool,
            rr_cfg=rr_cfg,
            ollama_url=cfg.get("embed", {}).get("ollama_url", "http://localhost:11434"),
            top_n=top_n,
        )
        # Both backends stamp rerank_score only when they actually ran; every
        # fail-open path returns unstamped hits. Capture the signal before
        # stripping so callers (eval) can detect a silent fail-open.
        rerank_applied = any("rerank_score" in h for h in all_results)
        # Strip transient keys so returned dicts have a stable shape
        # regardless of whether reranking ran.
        for _h in all_results:
            _h.pop("text", None)
            _h.pop("rerank_score", None)

    if stats is not None:
        stats["rerank_requested"] = rerank_enabled
        stats["rerank_applied"] = rerank_applied

    return all_results[:top_n]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest carta/tests/test_pipeline.py -v`
Expected: all PASS (new class + no regressions in existing pipeline tests)

- [ ] **Step 5: Commit**

```bash
git add carta/embed/pipeline.py carta/tests/test_pipeline.py
git commit -m "feat(search): run_search stats out-param reports whether rerank actually ran"
```

---

### Task 2: Hook forces rerank off

**Files:**
- Modify: `carta/hook/hook.py:76-79`
- Test: `carta/hook/tests/test_hook.py` (after `test_proactive_recall_search_is_text_only`, ~line 271)

- [ ] **Step 1: Write the failing test**

```python
def test_proactive_recall_search_never_reranks():
    """The per-prompt hook must not pay reranker latency.

    Regression: hook.py forced colpali off but passed search.rerank through
    untouched, so enabling search.rerank (e.g. backend=llm, 10s+/call) made
    every prompt submission block on a rerank call. The hook must force
    search.rerank.enabled off in the cfg it passes to run_search.
    """
    cfg = _make_cfg()
    cfg["search"] = {"top_n": 5, "rerank": {"enabled": True, "backend": "llm"}}
    captured = {}

    def fake_search(query, c, *a, **k):
        captured["cfg"] = c
        return []

    with (
        patch("sys.stdin", _stdin("how do I configure the embed pipeline")),
        patch("carta.hook.hook.find_config", return_value=Path("/fake/.carta/config.yaml")),
        patch("carta.hook.hook.load_config", return_value=cfg),
        patch("carta.hook.hook.run_search", side_effect=fake_search),
    ):
        _capture_main()

    rr = captured.get("cfg", {}).get("search", {}).get("rerank", {})
    assert rr.get("enabled") is False, (
        "proactive-recall hook must disable reranking — the hook blocks prompt "
        "submission and must never pay rerank latency"
    )
    # The project cfg itself must not be mutated by the override
    assert cfg["search"]["rerank"]["enabled"] is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest carta/hook/tests/test_hook.py::test_proactive_recall_search_never_reranks -v`
Expected: FAIL — `rr.get("enabled")` is `True`

- [ ] **Step 3: Implement**

Replace hook.py lines 76-79 with:

```python
    # 6. Search — text-only, never reranked. Proactive recall fires on every
    # prompt and blocks submission, so it must never trigger the heavy ColPali
    # visual path (model load ~9s/prompt) nor pay reranker latency (an LLM
    # rerank call can take 10s+). The three-zone judge below already filters
    # for relevance. Force both off for this search regardless of the
    # project's setting.
    search_cfg = {
        **cfg,
        "embed": {**cfg.get("embed", {}), "colpali_enabled": False},
        "search": {
            **cfg.get("search", {}),
            "rerank": {**cfg.get("search", {}).get("rerank", {}), "enabled": False},
        },
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest carta/hook/tests/ -v`
Expected: all PASS (new test + the existing colpali-off regression test)

- [ ] **Step 5: Commit**

```bash
git add carta/hook/hook.py carta/hook/tests/test_hook.py
git commit -m "fix(hook): proactive recall never pays rerank latency (mirror colpali-off)"
```

---

### Task 3: `cmd_eval` rerank reporting + zero-applied hard fail

**Files:**
- Modify: `carta/cli.py:524-556` (`cmd_eval`)
- Test: `carta/tests/test_cli.py` (new class at end of file)

- [ ] **Step 1: Write the failing tests**

Append to `carta/tests/test_cli.py`:

```python
class TestCmdEvalRerankAssertion:
    """cmd_eval reports rerank-applied counts and hard-fails when rerank was
    requested but silently failed open on every query (the 0.8.0 bug class)."""

    def _eval_yaml(self, tmp_path):
        p = tmp_path / "eval.yaml"
        p.write_text(
            'queries:\n'
            '  - q: "alpha"\n'
            '    expect: ["a.md"]\n'
            '  - q: "beta"\n'
            '    expect: ["b.md"]\n'
        )
        return p

    def _run(self, tmp_path, rerank_enabled, applied_per_query):
        """Run cmd_eval with run_search mocked to report the given per-query
        rerank_applied values. Returns the SystemExit code or None."""
        import argparse
        from unittest.mock import patch
        from carta.cli import cmd_eval

        cfg = {
            "project_name": "p",
            "qdrant_url": "http://localhost:6333",
            "embed": {"ollama_url": "http://localhost:11434", "ollama_model": "m"},
            "search": {"top_n": 5, "rerank": {"enabled": rerank_enabled}},
        }
        applied_iter = iter(applied_per_query)

        def fake_run_search(query, c, verbose=False, stats=None):
            if stats is not None:
                stats["rerank_requested"] = rerank_enabled
                stats["rerank_applied"] = next(applied_iter)
            return [{"score": 0.9, "source": "docs/a.md", "excerpt": "x", "type": "text"}]

        args = argparse.Namespace(eval_path=str(self._eval_yaml(tmp_path)), k=5)
        with patch("carta.cli.find_config", return_value=Path("/fake/.carta/config.yaml")), \
             patch("carta.config.load_config", return_value=cfg), \
             patch("carta.embed.pipeline.run_search", side_effect=fake_run_search):
            try:
                cmd_eval(args)
            except SystemExit as e:
                return e.code
        return None

    def test_zero_applied_with_rerank_requested_exits_nonzero(self, tmp_path, capsys):
        code = self._run(tmp_path, rerank_enabled=True, applied_per_query=[False, False])
        captured = capsys.readouterr()
        assert code == 1, "silent fail-open on every query must hard-fail the eval"
        assert "failing open" in captured.err
        assert "applied on 0/2 queries" in captured.out

    def test_partial_applied_reports_count_and_passes(self, tmp_path, capsys):
        code = self._run(tmp_path, rerank_enabled=True, applied_per_query=[True, False])
        captured = capsys.readouterr()
        assert code is None
        assert "rerank: applied on 1/2 queries" in captured.out

    def test_all_applied_reports_count(self, tmp_path, capsys):
        code = self._run(tmp_path, rerank_enabled=True, applied_per_query=[True, True])
        captured = capsys.readouterr()
        assert code is None
        assert "rerank: applied on 2/2 queries" in captured.out

    def test_rerank_not_requested_reports_and_passes(self, tmp_path, capsys):
        code = self._run(tmp_path, rerank_enabled=False, applied_per_query=[False, False])
        captured = capsys.readouterr()
        assert code is None
        assert "rerank: not requested" in captured.out
```

(`Path` and `pytest` are already imported at the top of `test_cli.py`; add them if not.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest carta/tests/test_cli.py::TestCmdEvalRerankAssertion -v`
Expected: FAIL — output lacks the `rerank:` line; zero-applied case returns None instead of 1

- [ ] **Step 3: Implement**

Replace `cmd_eval` body (cli.py lines 524-556) with:

```python
def cmd_eval(args):
    """Score retrieval quality against an eval set (recall@k, MRR).

    Eval is currently repo-scoped only. Scope-aware eval is a follow-up that
    requires run_search to accept a scope parameter.
    """
    import copy
    from carta.config import load_config
    from carta.eval.harness import run_eval
    from carta.embed.pipeline import run_search

    cfg_path = find_config()
    cfg = load_config(cfg_path)
    k = args.k

    # Deep-copy cfg once; the closure mutates top_n per call so each query
    # uses the correct top_k cutoff. run_search reads cfg["search"]["top_n"]
    # internally; results use "source" as the file path key (not "file_path"),
    # so the closure remaps to "file_path".
    eval_cfg = copy.deepcopy(cfg)
    rerank_requested = bool(eval_cfg.get("search", {}).get("rerank", {}).get("enabled", False))
    rerank_applied_count = 0
    query_count = 0

    def _search(query: str, top_k: int) -> list:
        nonlocal rerank_applied_count, query_count
        eval_cfg.setdefault("search", {})["top_n"] = top_k
        stats: dict = {}
        results = run_search(query, eval_cfg, stats=stats) or []
        query_count += 1
        if stats.get("rerank_applied"):
            rerank_applied_count += 1
        # run_search returns {"score", "source", "excerpt", "type"};
        # run_eval expects dicts with "file_path".
        return [{"file_path": r.get("source", ""), **r} for r in results]

    metrics = run_eval(Path(args.eval_path), _search, k=k)
    print(f"queries={metrics['n_queries']}  recall@{k}={metrics['recall_at_k']:.3f}  MRR={metrics['mrr']:.3f}")
    if rerank_requested:
        print(f"rerank: applied on {rerank_applied_count}/{query_count} queries")
    else:
        print("rerank: not requested")
    for row in metrics["per_query"]:
        mark = row["first_hit_rank"] if row["first_hit_rank"] is not None else "MISS"
        print(f"  [{mark}] {row['q']}")

    # A reranker that failed open on EVERY query is indistinguishable from a
    # working one in rank metrics alone — that's how 0.8.0 shipped broken.
    # Make it impossible to mistake for a result.
    if rerank_requested and query_count and rerank_applied_count == 0:
        print(
            "Error: search.rerank.enabled is true but the reranker ran on 0 queries — "
            "it is silently failing open (check the model and search.rerank.* config). "
            "These are NOT reranked numbers.",
            file=sys.stderr,
        )
        sys.exit(1)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest carta/tests/test_cli.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add carta/cli.py carta/tests/test_cli.py
git commit -m "feat(eval): report rerank-applied count; hard-fail on silent total fail-open"
```

---

### Task 4: CI actions Node 24 bump

**Files:**
- Modify: `.github/workflows/test.yml` (2× checkout@v4, 1× setup-python@v5)
- Modify: `.github/workflows/release.yml` (1× checkout@v4, 1× setup-python@v5)

- [ ] **Step 1: Bump versions**

In both files replace every `uses: actions/checkout@v4` with `uses: actions/checkout@v5`
and every `uses: actions/setup-python@v5` with `uses: actions/setup-python@v6`.
(checkout@v5 / setup-python@v6 are the Node 24 releases; GitHub force-migrates Node 20
actions on 2026-06-16. The `with:` blocks are unchanged — both majors are config-compatible.)

- [ ] **Step 2: Verify no stragglers**

Run: `grep -rn "checkout@v4\|setup-python@v5" .github/workflows/`
Expected: no output

- [ ] **Step 3: Commit**

```bash
git add .github/workflows/test.yml .github/workflows/release.yml
git commit -m "ci: bump actions to Node 24 releases (checkout@v5, setup-python@v6)"
```

---

### Task 5: CHANGELOG + README

**Files:**
- Modify: `CHANGELOG.md` (new 0.9.1 section above 0.9.0)
- Modify: `README.md` (rerank section ~line 224; eval/retrieval-quality section ~line 50)

- [ ] **Step 1: Add CHANGELOG entry**

Insert directly under the intro line (above `## [0.9.0]`):

```markdown
## [0.9.1] — 2026-06-10

### Fixed
- **The proactive-recall hook never pays reranker latency.** The hook forced ColPali off but
  passed `search.rerank` through untouched, so enabling the LLM reranker (10s+/call with a strong
  model) made every prompt submission block on a rerank call. The hook now forces
  `search.rerank.enabled` off in its search config (mirroring the colpali-off override) — you can
  enable `search.rerank` for explicit `carta search` without slowing every prompt.
- **`carta eval` can no longer mistake a silently broken reranker for a result.** Both reranker
  backends stamp `rerank_score` only when they actually ran; `run_search` now exposes that signal
  via an optional `stats` out-param, and `carta eval` prints `rerank: applied on N/M queries` and
  **exits 1** when rerank was requested but applied on zero queries. (The 0.8.0 reranker shipped
  fully fail-open and the eval reported its numbers as a win — this class of failure is now a hard
  error.)

### Changed
- CI workflows bumped to Node 24 action releases (`actions/checkout@v5`,
  `actions/setup-python@v6`) ahead of GitHub's 2026-06-16 forced migration.
```

- [ ] **Step 2: README — hook guarantee**

In the "Search reranking" section (after the model-strength paragraph, ~line 233), add:

```markdown
  Reranking applies to explicit searches (`carta search`, the MCP `carta_search` tool, `carta
  eval`). The proactive-recall hook **never reranks** (and never loads ColPali) — it fires on
  every prompt and blocks submission, so it always uses the fast fused order; its gray-zone judge
  handles relevance filtering.
```

- [ ] **Step 3: README — eval output**

In the retrieval-quality/eval section (~line 50-70), add after the eval description:

```markdown
When `search.rerank.enabled` is true, `carta eval` also prints `rerank: applied on N/M queries`
— and **fails (exit 1)** if the reranker ran on zero queries, so a silent fail-open (wrong model
name, Ollama down, reasoning-model misconfig) can never masquerade as a reranked result.
```

- [ ] **Step 4: Commit**

```bash
git add CHANGELOG.md README.md
git commit -m "docs: changelog + README for 0.9.1 (hook rerank guarantee, eval assertion)"
```

---

### Task 6: Eval set expansion + live validation (ET-embed repo — manual, not CI)

**Files:**
- Modify: `~/School/Elementrailer/ET-embed/.carta/eval/et-embed.yaml` (20 → ~60 queries)

- [ ] **Step 1: Enumerate the embedded corpus (ground truth for `expect`)**

From the ET-embed repo, scroll Qdrant for the distinct `file_path` values actually in the
text collections (authoritative — a query expecting a non-embedded doc can never hit):

```bash
cd ~/School/Elementrailer/ET-embed && python3 -c "
from qdrant_client import QdrantClient
c = QdrantClient(url='http://localhost:6333')
paths = set()
for coll in [x.name for x in c.get_collections().collections]:
    if not coll.startswith('et-embed') and not coll.startswith('ET'):
        continue
    if coll.endswith('_visual'):
        continue
    offset = None
    while True:
        pts, offset = c.scroll(coll, limit=500, offset=offset, with_payload=['file_path'])
        for p in pts:
            fp = (p.payload or {}).get('file_path')
            if fp: paths.add(fp)
        if offset is None: break
print('\n'.join(sorted(paths)))
" > /tmp/et-embed-corpus.txt; wc -l /tmp/et-embed-corpus.txt
```

(Adjust the collection-prefix filter to the actual project name from
`~/School/Elementrailer/ET-embed/.carta/config.yaml`.)

- [ ] **Step 2: Draft ~40 new queries grounded in the corpus**

Fan out readers over the corpus docs (group by area: CAN, telemetry, firmware, hardware,
supplier manuals/reference, testing, quirks). For each area produce candidate queries that:
- are phrased as a developer would ask (natural questions, **not** title echoes),
- have `expect` substrings that match a path in `/tmp/et-embed-corpus.txt`,
- target docs not already covered by the existing 20 (dedupe by expect-path),
- include the supplier/manual breadth that the current set lacks.

- [ ] **Step 3: Verify every expect-path and append**

For each new query, programmatically check each `expect` substring (case-insensitive) matches
at least one corpus path; drop or fix non-matching entries. Append to
`.carta/eval/et-embed.yaml` keeping the existing format and comment header. Target ~60 total.

- [ ] **Step 4: Live eval — baseline and reranked (uses the NEW code)**

```bash
cd ~/School/Elementrailer/ET-embed
# Baseline (rerank off in config):
PYTHONPATH=/Users/ian/dev/doc-audit-cc/.claude/worktrees/eval-trust-hook-rerank \
  python3 -m carta eval .carta/eval/et-embed.yaml -k 5
# Reranked (enable search.rerank in .carta/config.yaml, backend llm, qwen3.5:9b):
OMP_NUM_THREADS=1 PYTHONPATH=/Users/ian/dev/doc-audit-cc/.claude/worktrees/eval-trust-hook-rerank \
  python3 -m carta eval .carta/eval/et-embed.yaml -k 5
```

Expected: the reranked run prints `rerank: applied on N/60 queries` with N > 0. Record both
metric lines for the PR/CHANGELOG. Restore the config to its pre-run state afterward.

---

### Task 7: Review, merge, release

- [ ] **Step 1: Full suite** — `python3 -m pytest carta/ -q` → all pass (baseline was 760 passed, 2 skipped)
- [ ] **Step 2: Code review** the branch diff (superpowers:requesting-code-review); fix findings
- [ ] **Step 3: Merge to main, push** — fast-forward/merge `worktree-eval-trust-hook-rerank` into `main` from the main repo root, push, watch test.yml pass (also proves the bumped actions work)
- [ ] **Step 4: Tag and release** — `git tag -a v0.9.1 -m "v0.9.1 — eval rerank assertion, hook never reranks, CI Node 24" && git push origin v0.9.1`; watch release.yml; verify PyPI `carta-cc` 0.9.1 + GitHub Release
