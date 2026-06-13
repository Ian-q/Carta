# Visual-cap Fusion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop the `_visual` collection from claiming ~half of every query's fused candidate pool, so text questions keep their retrieval depth — recovering the three dilution misses on the ET-embed eval without regressing the visual eval.

**Architecture:** One localized change in `_rrf_merge_collections` (`carta/embed/pipeline.py`): after the existing RRF sort, cap how many fused-pool slots may be visual (`type == "visual"`), preserving RRF order among admitted hits and backfilling vacated slots with deeper text. A new config knob `search.fusion.visual_max_ratio` controls the cap; `run_search` reads it and passes it through. The merge feeds both the hybrid-alone return and the rerank pool, so this fixes both paths at once. No-op for pure-text corpora and when the ratio is `>= 1.0`.

**Tech Stack:** Python 3.10+, pytest, qdrant-client. No new dependencies.

**Spec:** [docs/superpowers/specs/2026-06-12-visual-pool-dilution-design.md](../specs/2026-06-12-visual-pool-dilution-design.md) · **Issue:** [#36](https://github.com/Ian-q/Carta/issues/36)

**Before starting:** This is feature work; an isolated worktree/branch (e.g. `visual-pool-dilution`) should exist via the `superpowers:using-git-worktrees` skill. Per repo convention, version bump / tag / PyPI release are a **separate post-merge chore** and are NOT part of this plan (see Closeout).

---

## File Structure

- **Modify** `carta/embed/pipeline.py`
  - `_rrf_merge_collections` (currently lines 1509–1535): add `visual_max_ratio` param + cap logic.
  - `run_search` (the merge call, currently line 1738): read `search.fusion.visual_max_ratio` and pass it.
- **Modify** `carta/config.py`
  - `DEFAULTS["search"]`: add a `"fusion": {"visual_max_ratio": 0.34}` block (provisional default; finalized by the sweep in Task 4).
- **Test** `carta/embed/tests/test_visual_search_merge.py` — new cap unit tests (extends the existing file).
- **Test** `carta/tests/test_config.py` — new `fusion` default test.
- **Docs** `CHANGELOG.md`, project memory `MEMORY.md`/`et-embed-eval-workflow`, and the ET-embed `RESULTS.md` (sweep table).

---

## Task 1: Cap the visual lane in `_rrf_merge_collections`

**Files:**
- Modify: `carta/embed/pipeline.py:1509-1535`
- Test: `carta/embed/tests/test_visual_search_merge.py`

- [ ] **Step 1: Write the failing tests**

Append to `carta/embed/tests/test_visual_search_merge.py` (the `_text`/`_visual` helpers already exist at the top of the file):

```python
def test_cap_is_noop_without_visual_lane():
    # A capping ratio must not change output when there are no visual hits.
    text = [_text(f"t{i}", 0.5 - i * 0.01) for i in range(6)]
    capped = _rrf_merge_collections([text], top_n=5, visual_max_ratio=0.2)
    uncapped = _rrf_merge_collections([text], top_n=5, visual_max_ratio=1.0)
    assert [m["source"] for m in capped] == [m["source"] for m in uncapped]
    assert [m["source"] for m in capped] == ["t0", "t1", "t2", "t3", "t4"]


def test_visual_cap_limits_visual_share():
    # cap = round(0.2 * 5) = 1 -> exactly one visual survives, text keeps the rest.
    text = [_text(f"t{i}", 0.5 - i * 0.01) for i in range(5)]
    visual = [_visual(f"v{i}", 30 - i) for i in range(5)]
    merged = _rrf_merge_collections([text, visual], top_n=5, visual_max_ratio=0.2)
    types = [m["type"] for m in merged]
    assert types.count("visual") == 1
    assert types.count("text") == 4
    # RRF order preserved among admitted hits (text-first tie at rank 0).
    assert [m["source"] for m in merged] == ["t0", "v0", "t1", "t2", "t3"]


def test_overflow_backfills_when_text_exhausted():
    # Few text hits, many visual, cap = 1: pool must still fill to top_n from the
    # diverted (overflow) visual hits, in RRF order.
    text = [_text("t0", 0.5), _text("t1", 0.49)]
    visual = [_visual(f"v{i}", 30 - i) for i in range(6)]
    merged = _rrf_merge_collections([text, visual], top_n=5, visual_max_ratio=0.2)
    assert len(merged) == 5
    assert [m["source"] for m in merged] == ["t0", "v0", "t1", "v1", "v2"]


def test_admitted_hits_keep_rrf_order():
    # cap = round(0.25 * 6) = 2: two visual admitted, interleave order preserved, no backfill.
    text = [_text(f"t{i}", 0.5 - i * 0.01) for i in range(4)]
    visual = [_visual(f"v{i}", 30 - i) for i in range(4)]
    merged = _rrf_merge_collections([text, visual], top_n=6, visual_max_ratio=0.25)
    assert [m["source"] for m in merged] == ["t0", "v0", "t1", "v1", "t2", "t3"]


def test_ratio_one_disables_cap():
    # visual_max_ratio = 1.0 (the function default) -> full RRF interleave, unchanged.
    text = [_text(f"t{i}", 0.5 - i * 0.01) for i in range(3)]
    visual = [_visual(f"v{i}", 30 - i) for i in range(3)]
    merged = _rrf_merge_collections([text, visual], top_n=6, visual_max_ratio=1.0)
    assert [m["source"] for m in merged] == ["t0", "v0", "t1", "v1", "t2", "v2"]


def test_ratio_zero_excludes_visual_when_text_fills_pool():
    # cap = round(0.0 * 5) = 0 -> no visual admitted while text can fill the pool.
    text = [_text(f"t{i}", 0.5 - i * 0.01) for i in range(5)]
    visual = [_visual(f"v{i}", 30 - i) for i in range(3)]
    merged = _rrf_merge_collections([text, visual], top_n=5, visual_max_ratio=0.0)
    assert all(m["type"] == "text" for m in merged)
    assert [m["source"] for m in merged] == ["t0", "t1", "t2", "t3", "t4"]
```

- [ ] **Step 2: Run the new tests to verify they fail**

Run: `python -m pytest carta/embed/tests/test_visual_search_merge.py -v`
Expected: the 6 new tests FAIL with `TypeError: _rrf_merge_collections() got an unexpected keyword argument 'visual_max_ratio'`. The 4 existing tests still PASS.

- [ ] **Step 3: Implement the cap**

Replace the whole function body at `carta/embed/pipeline.py:1509-1535` with:

```python
def _rrf_merge_collections(
    per_collection: list[list[dict]],
    top_n: int,
    k: int = 60,
    visual_max_ratio: float = 1.0,
) -> list[dict]:
    """Fuse ranked hit lists from multiple collections with Reciprocal Rank Fusion.

    Each collection's native scores live on incomparable scales — text uses cosine
    or RRF (~0-1) while the visual collection uses ColPali MaxSim (a sum over query
    tokens, ~10-40).  Merging by raw score lets visual hits crowd out every text
    hit.  RRF discards score magnitude and fuses by rank instead, so a rank-0 text
    hit and a rank-0 visual hit compete fairly regardless of scale.

    RRF alone, however, interleaves text and visual ~1:1 by rank, so once a `_visual`
    collection has hits ~half of every fused pool is visual — even for pure-text
    questions — halving effective text depth.  `visual_max_ratio` caps the visual
    lane's share of the returned pool: visual hits beyond the cap are dropped and the
    freed slots are backfilled with deeper text (or, if text is exhausted, restored
    from the diverted visual).  RRF order is preserved among everything admitted.

    Args:
        per_collection: one list per collection, each already ordered best-first.
        top_n: number of fused results to return.
        k: RRF damping constant (Qdrant's fusion default is 60).
        visual_max_ratio: ceiling on the visual lane's share of the pool, as a
            fraction of `top_n` (cap = round(visual_max_ratio * top_n)). 1.0 (default)
            disables the cap; a corpus with no visual hits is unaffected either way.

    Returns:
        Flat list of the original hit dicts, best-first by RRF, length <= top_n.
        Ties (same rank across collections) break toward earlier collections, so
        callers should pass the text collection before the visual one.
    """
    scored = []
    for coll_index, hits in enumerate(per_collection):
        for rank, hit in enumerate(hits):
            rrf = 1.0 / (k + rank + 1)
            scored.append((rrf, coll_index, rank, hit))
    # -rrf: higher fused score first. coll_index/rank: deterministic, text-first ties.
    scored.sort(key=lambda t: (-t[0], t[1], t[2]))

    # Cap the visual lane's share of the pool. No-op when no hit is visual or when
    # visual_cap >= top_n (i.e. visual_max_ratio >= 1.0): the visual branch never
    # diverts, so the walk reproduces scored[:top_n] exactly.
    visual_cap = round(visual_max_ratio * top_n)
    result: list[dict] = []
    overflow: list[dict] = []
    visual_admitted = 0
    for _, _, _, hit in scored:
        if len(result) >= top_n:
            break
        if hit.get("type") == "visual":
            if visual_admitted < visual_cap:
                result.append(hit)
                visual_admitted += 1
            else:
                overflow.append(hit)
        else:
            result.append(hit)
    # Text too shallow to fill the pool: restore diverted visual, still RRF order.
    if len(result) < top_n and overflow:
        result.extend(overflow[: top_n - len(result)])
    return result
```

- [ ] **Step 4: Run the merge tests to verify they pass**

Run: `python -m pytest carta/embed/tests/test_visual_search_merge.py -v`
Expected: all 10 tests PASS (4 original + 6 new).

- [ ] **Step 5: Commit**

```bash
git add carta/embed/pipeline.py carta/embed/tests/test_visual_search_merge.py
git commit -m "feat(search): cap visual lane's share of the fused pool (#36)"
```

---

## Task 2: Add the `search.fusion.visual_max_ratio` config default

**Files:**
- Modify: `carta/config.py:45-55` (inside `DEFAULTS["search"]`, after the `graph` block)
- Test: `carta/tests/test_config.py`

- [ ] **Step 1: Write the failing test**

Append to `carta/tests/test_config.py`:

```python
def test_fusion_defaults_present():
    from carta.config import DEFAULTS
    fusion = DEFAULTS["search"]["fusion"]
    # Provisional cap; finalized by the ratio sweep (see RESULTS.md 2026-06-12).
    assert fusion["visual_max_ratio"] == 0.34
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python -m pytest carta/tests/test_config.py::test_fusion_defaults_present -v`
Expected: FAIL with `KeyError: 'fusion'`.

- [ ] **Step 3: Add the default block**

In `carta/config.py`, inside `DEFAULTS["search"]`, immediately after the closing `}` of the `"graph": { ... }` block (currently line 54) and before the closing `}` of `"search"`, add:

```python
        "fusion": {
            # Ceiling on the visual (_visual/ColPali) collection's share of the fused
            # candidate pool, as a fraction of pool size (cap = round(ratio * pool)).
            # RRF interleaves text and visual ~1:1 by rank, which halves text depth on
            # every query once a _visual collection exists; this bounds visual so text
            # questions keep their depth. 1.0 disables the cap (legacy behaviour). No
            # effect on pure-text corpora. Eval-swept optimum — see RESULTS.md 2026-06-12.
            "visual_max_ratio": 0.34,
        },
```

- [ ] **Step 4: Run it to verify it passes**

Run: `python -m pytest carta/tests/test_config.py::test_fusion_defaults_present -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add carta/config.py carta/tests/test_config.py
git commit -m "feat(config): add search.fusion.visual_max_ratio default (#36)"
```

---

## Task 3: Wire `run_search` to read and pass the ratio

**Files:**
- Modify: `carta/embed/pipeline.py` (the `_rrf_merge_collections` call, currently line 1738)
- Test: `carta/embed/tests/test_visual_search_merge.py`

- [ ] **Step 1: Write the failing test**

Append to `carta/embed/tests/test_visual_search_merge.py`. Add `from unittest.mock import MagicMock` to the imports at the top of the file if not already present:

```python
def test_run_search_forwards_configured_visual_max_ratio(monkeypatch, tmp_path):
    # The configured search.fusion.visual_max_ratio must reach _rrf_merge_collections.
    import carta.embed.pipeline as pipeline

    captured = {}

    def fake_merge(per_collection, top_n, k=60, visual_max_ratio=1.0):
        captured["ratio"] = visual_max_ratio
        return []

    monkeypatch.setattr(pipeline, "_rrf_merge_collections", fake_merge)
    monkeypatch.setattr(pipeline, "QdrantClient", lambda *a, **kw: MagicMock())
    monkeypatch.setattr(pipeline, "find_config", lambda: str(tmp_path / ".carta" / "config.yaml"))
    # No collections -> the per-collection loop is skipped and the merge is still called.
    monkeypatch.setattr("carta.search.scoped.get_search_collections", lambda cfg, scope: [])

    cfg = {
        "project_name": "proj",
        "qdrant_url": "http://localhost:6333",
        "embed": {"ollama_url": "http://localhost:11434", "ollama_model": "nomic-embed-text"},
        "search": {"top_n": 5, "fusion": {"visual_max_ratio": 0.34}},
    }
    pipeline.run_search("query", cfg)
    assert captured["ratio"] == 0.34
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python -m pytest carta/embed/tests/test_visual_search_merge.py::test_run_search_forwards_configured_visual_max_ratio -v`
Expected: FAIL — `captured["ratio"]` is `1.0` (the function default), not `0.34`, because `run_search` does not yet pass the config value. Assertion error `assert 1.0 == 0.34`.

- [ ] **Step 3: Read the config value and pass it**

In `carta/embed/pipeline.py`, find the merge call (currently line 1738):

```python
    all_results = _rrf_merge_collections(per_collection, fetch_limit)
```

Replace it with:

```python
    fusion_cfg = cfg.get("search", {}).get("fusion", {})
    visual_max_ratio = fusion_cfg.get("visual_max_ratio", 1.0)
    all_results = _rrf_merge_collections(
        per_collection, fetch_limit, visual_max_ratio=visual_max_ratio
    )
```

- [ ] **Step 4: Run it to verify it passes**

Run: `python -m pytest carta/embed/tests/test_visual_search_merge.py -v`
Expected: all tests PASS (including the new wiring test).

- [ ] **Step 5: Commit**

```bash
git add carta/embed/pipeline.py carta/embed/tests/test_visual_search_merge.py
git commit -m "feat(search): run_search passes visual_max_ratio to fusion (#36)"
```

---

## Task 4: Full suite + ratio sweep (finalize the default)

**Files:**
- Possibly modify: `carta/config.py` (set the swept-best ratio if it differs from 0.34)
- Modify: `~/School/Elementrailer/ET-embed/RESULTS.md` (append a dated sweep table)

- [ ] **Step 1: Run the full carta test suite**

Run: `python -m pytest -q`
Expected: all tests PASS (851 prior + 7 new = 858), no failures. If anything else broke, stop and fix before sweeping.

- [ ] **Step 2: Sweep the ratio on both eval sets**

The sweep uses the unreleased checkout against the live ET-embed corpus. From the ET-embed root
(`~/School/Elementrailer/ET-embed`), for each `R` in `1.0, 0.5, 0.34, 0.2`, temporarily set
`search.fusion.visual_max_ratio: R` in `.carta/config.yaml`, then run:

```bash
# 62-query text eval, hybrid-alone (default config = rerank off)
PYTHONPATH=/Users/ian/dev/doc-audit-cc ~/.local/pipx/venvs/carta-cc/bin/python \
  -m carta eval .carta/eval/et-embed.yaml -k 5

# 62-query text eval, reranked (temporarily enable the rerank block documented in config.yaml)
OMP_NUM_THREADS=1 PYTHONPATH=/Users/ian/dev/doc-audit-cc ~/.local/pipx/venvs/carta-cc/bin/python \
  -m carta eval .carta/eval/et-embed.yaml -k 5

# 14-query visual eval, auto (rerank off) — the regression guard
PYTHONPATH=/Users/ian/dev/doc-audit-cc ~/.local/pipx/venvs/carta-cc/bin/python \
  -m carta eval .carta/eval/et-embed-datasheets.yaml -k 5
```

Record recall@5 / MRR for each (ratio × eval-set × mode) cell. Baselines to beat / hold:
hybrid-alone 0.839/0.674, reranked 0.903/0.819 (these are the `R=1.0` rows by construction),
and the visual eval's current recall@5 (capture the `R=1.0` value first as the no-regression bar).

- [ ] **Step 3: Pick the default and set it**

Choose the lowest-dilution ratio that **maximizes 62-query text recall@5/MRR (hybrid-alone and
reranked) while holding the 14-query visual eval flat**. If that ratio is not `0.34`, update
`carta/config.py` `DEFAULTS["search"]["fusion"]["visual_max_ratio"]` to the chosen value and
update `carta/tests/test_config.py::test_fusion_defaults_present` to match. Confirm the three
dilution misses (SAFETY-MCU-MESSAGES, TIMING_ARCHITECTURE, connector-map) now land in top-5.

- [ ] **Step 4: Restore the ET-embed config**

Set `~/School/Elementrailer/ET-embed/.carta/config.yaml` back to its normal state (rerank off,
no temporary `visual_max_ratio` override — the new default is inherited from carta). Verify with
`git -C ~/School/Elementrailer/ET-embed diff .carta/config.yaml` (should show no leftover sweep edits).

- [ ] **Step 5: Re-run the suite if the default changed, then commit**

If Step 3 changed the default:

```bash
python -m pytest carta/tests/test_config.py::test_fusion_defaults_present -v   # PASS
git add carta/config.py carta/tests/test_config.py
git commit -m "perf(search): set visual_max_ratio default to swept optimum (#36)"
```

Append the sweep table to the ET-embed `RESULTS.md` under a `## 2026-06-12 — visual-cap fusion (#36)`
heading (this file lives in the ET-embed repo, committed there separately, not in carta).

---

## Task 5: Closeout — CHANGELOG + memory

**Files:**
- Modify: `CHANGELOG.md`
- Modify: `MEMORY.md` index + `et-embed-eval-workflow` memory (carta-cc session memory dir)

- [ ] **Step 1: Add the CHANGELOG entry**

In `CHANGELOG.md`, add a new section above the `## [0.11.0]` heading. Use the chosen ratio
from Task 4 in the prose:

```markdown
## [Unreleased]

### Fixed
- **Visual pool dilution (#36).** Cross-collection RRF fusion interleaved text and visual
  hits ~1:1 by rank, so once a `_visual` collection had content ~half of every query's
  candidate pool was visual — including pure-text questions — halving effective text depth.
  A new `search.fusion.visual_max_ratio` knob caps the visual lane's share of the fused pool
  (default `<chosen>`; `1.0` restores the old behaviour); freed slots backfill with deeper
  text. No effect on pure-text corpora. Recovers the SAFETY-MCU-MESSAGES, TIMING_ARCHITECTURE,
  and connector-map misses on the ET-embed eval.
```

- [ ] **Step 2: Commit the CHANGELOG**

```bash
git add CHANGELOG.md
git commit -m "docs: changelog for visual-cap fusion (#36)"
```

- [ ] **Step 3: Update project memory**

Update the `et-embed-eval-workflow` memory (in the carta-cc session memory dir,
`/Users/ian/.claude/projects/-Users-ian-dev-doc-audit-cc/memory/project_et-embed-eval-workflow.md`)
with the new post-#36 baseline numbers and the chosen `visual_max_ratio`. This is session memory,
not a repo file — no git commit.

- [ ] **Step 4: Final verification before PR**

Run: `python -m pytest -q`
Expected: all PASS. The branch is ready for `requesting-code-review` → PR. Version bump
(`carta/__init__.py` → `0.12.0`), tag, and PyPI release are a **separate post-merge chore**
per repo convention; decide at PR time whether to ship #36 alone as 0.12.0 or bundle it with
#37 (reranker demotion, phase 2 of this cycle).

---

## Self-Review Notes

- **Spec coverage:** mechanism → Task 1; config knob → Task 2; `run_search` wiring → Task 3;
  no-op/ratio-1.0/backfill/order/cap-0 test cases → Task 1 (all six from the spec's testing
  section); ratio sweep on both eval sets + swept default → Task 4; CHANGELOG + memory → Task 5.
- **Non-goals respected:** no score-gating, no fetch-widening, no visual drain, no #37 work.
- **Type/name consistency:** `visual_max_ratio` (float), `search.fusion.visual_max_ratio`, and
  `_rrf_merge_collections(..., visual_max_ratio=...)` are spelled identically in every task.
- **Provisional value:** `0.34` appears in Tasks 2 and the Task 4 sweep grid; Task 4 Step 3 is
  the single place it is finalized, with the test updated in lockstep.
