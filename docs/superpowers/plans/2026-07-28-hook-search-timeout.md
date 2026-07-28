# Hook Search Timeout Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give `carta-hook` a bounded wall-clock search budget so an unreachable remote backend costs ~3 s per prompt instead of ~80 s.

**Architecture:** Thread an optional timeout from the hook down through `run_search` into both network calls on its path (`get_embedding` and `QdrantClient`). A single wall-clock deadline, not a per-call timeout. `timeout_s=None` preserves today's behaviour exactly, so no non-hook caller changes.

**Tech Stack:** Python 3.10+, pytest, `unittest.mock`, `monkeypatch`.

## Global Constraints

- **`timeout_s=None` must be byte-identical to current behaviour**: 60 s embed, 10 s Qdrant, no deadline checks. CLI, MCP and eval pass nothing. Test 1 in Task 3 pins this.
- **Baseline is `1205 passed, 3 skipped`.** Any drop is a regression, not an acceptable trade.
- **Never wrap in `ThreadPoolExecutor` as the fix.** `__exit__` waits for the abandoned thread, so an outer timeout does not free a blocked inner call — see `_call_ollama_judge`'s docstring in `carta/hook/hook.py`.
- **Fail-open is non-negotiable.** Every hook path exits 0; the prompt always proceeds.
- **Budget exhaustion returns partial results, never raises.** Raising would print to stderr on every prompt during an outage.
- Do **not** commit `.carta/config.yaml` or `docs/superpowers/plans/2026-06-17-search-result-dedup.md` — both are pre-existing uncommitted work. Stage explicit paths only, never `git add -A`.
- `import time` and `import requests` already exist in the modules being edited. Do not re-add.

## File Structure

| File | Responsibility |
|---|---|
| `carta/embed/embed.py` | `get_embedding` gains `timeout` param (innermost network call) |
| `carta/embed/pipeline.py` | `run_search` + `_embed_query_or_raise` gain the budget; deadline logic |
| `carta/config.py` | `search_timeout_s` default |
| `carta/hook/hook.py` | Reads the config key, passes it to `run_search` |
| `carta/embed/tests/test_search_timeout.py` | **new** — Tasks 1–3 |
| `carta/hook/tests/test_hook.py` | append — Task 4 |

---

### Task 1: `get_embedding` accepts a timeout

**Files:**
- Modify: `carta/embed/embed.py:84-96`
- Test: `carta/embed/tests/test_search_timeout.py` (create)

**Interfaces:**
- Produces: `get_embedding(text, ollama_url=..., model=..., prefix=..., timeout=60)`. Task 2 passes `timeout` from the deadline.

- [ ] **Step 1: Write the failing tests**

```python
"""Tests for the hook's bounded search budget (issue #106)."""

from unittest.mock import MagicMock

import carta.embed.embed as embed_mod


def _ok_response():
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"embedding": [0.0] * 768}
    return resp


def test_get_embedding_default_timeout_is_60(monkeypatch):
    """Unchanged default: every existing caller keeps the 60s ceiling."""
    captured = {}

    def fake_post(url, **kwargs):
        captured["timeout"] = kwargs.get("timeout")
        return _ok_response()

    monkeypatch.setattr(embed_mod.requests, "post", fake_post)
    embed_mod.get_embedding("hello")
    assert captured["timeout"] == 60


def test_get_embedding_honours_explicit_timeout(monkeypatch):
    """An explicit timeout reaches requests.post."""
    captured = {}

    def fake_post(url, **kwargs):
        captured["timeout"] = kwargs.get("timeout")
        return _ok_response()

    monkeypatch.setattr(embed_mod.requests, "post", fake_post)
    embed_mod.get_embedding("hello", timeout=2.5)
    assert captured["timeout"] == 2.5
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m pytest carta/embed/tests/test_search_timeout.py -v`
Expected: `test_get_embedding_honours_explicit_timeout` FAILS with `TypeError: get_embedding() got an unexpected keyword argument 'timeout'`. The default test PASSES already.

- [ ] **Step 3: Add the parameter**

In `carta/embed/embed.py`, add `timeout: float = 60,` to the signature after `prefix`, and change the `requests.post` call's `timeout=60` to `timeout=timeout`.

- [ ] **Step 4: Run to verify pass**

Run: `python3 -m pytest carta/embed/tests/test_search_timeout.py -v`
Expected: both PASS.

- [ ] **Step 5: Full suite**

Run: `python3 -m pytest -q`
Expected: `1207 passed, 3 skipped` (1205 + 2 new).

- [ ] **Step 6: Commit**

```bash
git add carta/embed/embed.py carta/embed/tests/test_search_timeout.py
git commit -m "feat(embed): get_embedding accepts an explicit timeout

Default stays 60s so every existing caller is unchanged. The hook's
bounded search budget needs to clamp this call."
```

---

### Task 2: `_embed_query_or_raise` forwards the timeout

**Files:**
- Modify: `carta/embed/pipeline.py:1930-1949`
- Test: `carta/embed/tests/test_search_timeout.py` (append)

**Interfaces:**
- Consumes: `get_embedding(..., timeout=...)` from Task 1.
- Produces: `_embed_query_or_raise(query, cfg, collections, timeout=None)`. Task 3 passes the remaining budget.

- [ ] **Step 1: Write the failing test**

```python
import carta.embed.pipeline as pipeline


def test_embed_query_or_raise_forwards_timeout(monkeypatch):
    """The budget must reach get_embedding, not stop at the wrapper."""
    captured = {}

    def fake_get_embedding(text, **kwargs):
        captured["timeout"] = kwargs.get("timeout")
        return [0.0] * 768

    monkeypatch.setattr(pipeline, "get_embedding", fake_get_embedding)
    cfg = {"embed": {"ollama_url": "http://x", "ollama_model": "m"}}
    pipeline._embed_query_or_raise("q", cfg, ["proj_doc"], timeout=1.5)
    assert captured["timeout"] == 1.5


def test_embed_query_or_raise_default_timeout_is_none(monkeypatch):
    """Without a budget, no timeout kwarg is forced on get_embedding."""
    captured = {}

    def fake_get_embedding(text, **kwargs):
        captured["timeout"] = kwargs.get("timeout", "ABSENT")
        return [0.0] * 768

    monkeypatch.setattr(pipeline, "get_embedding", fake_get_embedding)
    cfg = {"embed": {"ollama_url": "http://x", "ollama_model": "m"}}
    pipeline._embed_query_or_raise("q", cfg, ["proj_doc"])
    assert captured["timeout"] == "ABSENT"
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m pytest carta/embed/tests/test_search_timeout.py -v -k embed_query`
Expected: `test_embed_query_or_raise_forwards_timeout` FAILS with `TypeError: _embed_query_or_raise() got an unexpected keyword argument 'timeout'`.

- [ ] **Step 3: Implement**

Change the signature to:

```python
def _embed_query_or_raise(query: str, cfg: dict, collections: list[str],
                          timeout: float | None = None) -> list[float] | None:
```

and the `get_embedding` call to pass the timeout only when one was given, so the
no-budget path keeps `get_embedding`'s own default:

```python
    kwargs = {"ollama_url": ollama_url, "model": model, "prefix": "search_query: "}
    if timeout is not None:
        kwargs["timeout"] = timeout
    try:
        return get_embedding(query, **kwargs)
```

- [ ] **Step 4: Run to verify pass**

Run: `python3 -m pytest carta/embed/tests/test_search_timeout.py -v -k embed_query`
Expected: both PASS.

- [ ] **Step 5: Commit**

```bash
git add carta/embed/pipeline.py carta/embed/tests/test_search_timeout.py
git commit -m "feat(search): _embed_query_or_raise forwards an optional timeout"
```

---

### Task 3: `run_search` deadline

The core of the fix. Adds the budget, clamps both network calls to remaining time, and stops querying further collections once exhausted.

**Files:**
- Modify: `carta/embed/pipeline.py:2171-2245`
- Test: `carta/embed/tests/test_search_timeout.py` (append)

**Interfaces:**
- Consumes: `_embed_query_or_raise(..., timeout=...)` from Task 2.
- Produces: `run_search(query, cfg, verbose=False, stats=None, timeout_s=None)`. Task 4's hook passes `timeout_s`.

- [ ] **Step 1: Write the failing tests**

```python
def _make_timeout_cfg():
    return {
        "project_name": "test_proj",
        "qdrant_url": "http://localhost:6333",
        "embed": {"ollama_url": "http://x", "ollama_model": "m"},
        "search": {"top_n": 5, "dedupe_results": False},
    }


def _patch_search_deps(monkeypatch, *, collections, client_timeouts,
                       embed_timeouts, queried, clock=None):
    """Patch run_search's deps, recording the timeouts each dep receives."""
    monkeypatch.setattr(pipeline, "find_config", lambda: "/fake/.carta/config.yaml")

    import carta.search.scoped as scoped_mod
    monkeypatch.setattr(scoped_mod, "get_search_collections",
                        lambda cfg, scope: list(collections))

    def fake_embed(query, cfg, colls, timeout=None):
        embed_timeouts.append(timeout)
        return [0.0] * 768

    monkeypatch.setattr(pipeline, "_embed_query_or_raise", fake_embed)
    monkeypatch.setattr(pipeline, "collection_is_hybrid", lambda c, n: False)

    resp = MagicMock()
    resp.points = []

    class FakeQdrantClient:
        def __init__(self, *a, **k):
            client_timeouts.append(k.get("timeout"))

        def query_points(self, **kwargs):
            queried.append(kwargs.get("collection_name"))
            return resp

    monkeypatch.setattr(pipeline, "QdrantClient", FakeQdrantClient)

    if clock is not None:
        # Patch pipeline's `time` reference, NOT the real time module. Doing
        # `setattr(pipeline.time, "monotonic", clock)` would mutate the stdlib
        # module process-wide for the duration, handing the fake clock to pytest
        # internals and anything else running. The shim delegates everything
        # except monotonic.
        import time as _real_time

        class _FakeTime:
            monotonic = staticmethod(clock)

            def __getattr__(self, name):
                return getattr(_real_time, name)

        monkeypatch.setattr(pipeline, "time", _FakeTime())


def test_run_search_without_budget_keeps_legacy_timeouts(monkeypatch):
    """REGRESSION GUARD: no timeout_s means 10s Qdrant and no embed clamp."""
    client_timeouts, embed_timeouts, queried = [], [], []
    _patch_search_deps(monkeypatch, collections=["test_proj_doc"],
                       client_timeouts=client_timeouts,
                       embed_timeouts=embed_timeouts, queried=queried)

    pipeline.run_search("q", _make_timeout_cfg())

    assert client_timeouts == [10], "non-hook callers must keep the 10s client"
    assert embed_timeouts == [None], "non-hook callers must not clamp the embed"


def test_run_search_budget_clamps_both_calls(monkeypatch):
    """With a budget, both the embed and the Qdrant client are bounded by it."""
    client_timeouts, embed_timeouts, queried = [], [], []
    _patch_search_deps(monkeypatch, collections=["test_proj_doc"],
                       client_timeouts=client_timeouts,
                       embed_timeouts=embed_timeouts, queried=queried)

    pipeline.run_search("q", _make_timeout_cfg(), timeout_s=3)

    assert embed_timeouts[0] is not None and embed_timeouts[0] <= 3
    assert client_timeouts[0] is not None and client_timeouts[0] <= 3


def test_run_search_budget_exhausted_skips_later_collections(monkeypatch):
    """A budget spent during the first collection stops the loop; no raise."""
    client_timeouts, embed_timeouts, queried = [], [], []
    # First call stamps the deadline at t=0 (so deadline=3); every later call
    # reports t=100, i.e. long past it. Deliberately NOT a fixed tick list —
    # that would silently break if the implementation calls monotonic() a
    # different number of times.
    calls = {"n": 0}

    def clock():
        calls["n"] += 1
        return 0.0 if calls["n"] == 1 else 100.0

    _patch_search_deps(monkeypatch, collections=["a_doc", "b_doc", "c_doc"],
                       client_timeouts=client_timeouts,
                       embed_timeouts=embed_timeouts, queried=queried,
                       clock=clock)

    results = pipeline.run_search("q", _make_timeout_cfg(), timeout_s=3)

    assert results == [], "exhausted budget returns partial results, not an error"
    assert len(queried) < 3, f"expected the loop to stop early, queried {queried}"
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m pytest carta/embed/tests/test_search_timeout.py -v -k run_search`
Expected: the legacy-guard test PASSES; the two budget tests FAIL with `TypeError: run_search() got an unexpected keyword argument 'timeout_s'`.

- [ ] **Step 3: Implement**

In `run_search`, add `timeout_s: float | None = None` to the signature. Immediately after the docstring's imports block, add:

```python
    # Wall-clock budget (issue #106). A per-call timeout is not a bound: the same
    # 3s across one embed and N collections is 3s x (1+N). The hook blocks prompt
    # submission, so it needs a limit it can actually reason about.
    deadline = (time.monotonic() + timeout_s) if timeout_s else None

    def _remaining() -> float | None:
        """Seconds left in the budget, floored so we never pass 0/negative."""
        if deadline is None:
            return None
        return max(0.1, deadline - time.monotonic())
```

Delete the existing client construction at lines 2227-2230 and move it to **after**
`_embed_query_or_raise`, so it can be built with the time actually left:

```python
    text_query_vec = _embed_query_or_raise(query, cfg, collections, timeout=_remaining())

    # Constructed AFTER the embed so a budget reflects time already spent.
    # QdrantClient does not connect on construction, so ordering is safe.
    try:
        client = QdrantClient(url=cfg["qdrant_url"], timeout=_remaining() or 10)
    except Exception as e:
        raise RuntimeError(f"Cannot connect to Qdrant: {e}") from e
```

Then inside `for coll_name in collections:`, as the very first statement:

```python
        if deadline is not None and time.monotonic() >= deadline:
            # Budget spent. Return what we have rather than raising — the hook's
            # noise gate exits silently on an empty set, so an outage degrades to
            # silence instead of a stderr line on every prompt.
            break
```

- [ ] **Step 4: Run to verify pass**

Run: `python3 -m pytest carta/embed/tests/test_search_timeout.py -v`
Expected: all PASS, including the legacy guard.

- [ ] **Step 5: Full suite — the real check**

Run: `python3 -m pytest -q`
Expected: `1212 passed, 3 skipped`. Any pre-existing test that now fails means the `None` path was not preserved — fix that rather than updating the test.

- [ ] **Step 6: Commit**

```bash
git add carta/embed/pipeline.py carta/embed/tests/test_search_timeout.py
git commit -m "feat(search): optional wall-clock budget for run_search

A single deadline rather than a per-call timeout, since 3s across one
embed and N collections is not a bound. Clamps the query embed and the
Qdrant client to the time remaining, and stops querying further
collections once spent, returning partial results rather than raising.

timeout_s=None preserves today's 60s/10s behaviour exactly, so CLI, MCP
and eval are untouched."
```

---

### Task 4: Config default and hook wiring

**Files:**
- Modify: `carta/config.py` (`proactive_recall` DEFAULTS block, ~line 134)
- Modify: `carta/hook/hook.py:69-93`
- Test: `carta/hook/tests/test_hook.py` (append)

**Interfaces:**
- Consumes: `run_search(..., timeout_s=...)` from Task 3.
- Produces: the wired hook. Nothing downstream consumes this.

- [ ] **Step 1: Write the failing tests**

Append to `carta/hook/tests/test_hook.py`. Note `_make_cfg()` already exists in that file — these tests mutate the dict it returns rather than changing its signature.

```python
# ---------------------------------------------------------------------------
# Bounded search budget (issue #106)
# ---------------------------------------------------------------------------

def test_hook_passes_search_timeout_to_run_search():
    """The configured budget must reach run_search."""
    captured = {}

    def fake_run_search(query, cfg, **kwargs):
        captured["timeout_s"] = kwargs.get("timeout_s")
        return [_make_hit(0.90)]

    cfg = _make_cfg()
    cfg["proactive_recall"]["search_timeout_s"] = 7
    with (
        patch("sys.stdin", _stdin("query")),
        patch("carta.hook.hook.find_config", return_value=Path("/fake/.carta/config.yaml")),
        patch("carta.hook.hook.load_config", return_value=cfg),
        patch("carta.hook.hook.run_search", side_effect=fake_run_search),
    ):
        _capture_main()

    assert captured["timeout_s"] == 7


def test_hook_search_timeout_defaults_to_3():
    """Absent config key falls back to 3s, matching judge_timeout_s."""
    captured = {}

    def fake_run_search(query, cfg, **kwargs):
        captured["timeout_s"] = kwargs.get("timeout_s")
        return [_make_hit(0.90)]

    cfg = _make_cfg()
    cfg["proactive_recall"].pop("search_timeout_s", None)
    with (
        patch("sys.stdin", _stdin("query")),
        patch("carta.hook.hook.find_config", return_value=Path("/fake/.carta/config.yaml")),
        patch("carta.hook.hook.load_config", return_value=cfg),
        patch("carta.hook.hook.run_search", side_effect=fake_run_search),
    ):
        _capture_main()

    assert captured["timeout_s"] == 3


def test_hook_still_fails_open_when_search_raises():
    """Fail-open is non-negotiable: a search error must still exit 0, silently."""
    cfg = _make_cfg()
    with (
        patch("sys.stdin", _stdin("query")),
        patch("carta.hook.hook.find_config", return_value=Path("/fake/.carta/config.yaml")),
        patch("carta.hook.hook.load_config", return_value=cfg),
        patch("carta.hook.hook.run_search", side_effect=TimeoutError("backend down")),
    ):
        out = _capture_main()

    assert out.strip() == "", "must not inject when the search failed"


def test_search_timeout_default_registered_in_config():
    """The key must exist in DEFAULTS so `carta init` writes it."""
    from carta.config import DEFAULTS
    assert DEFAULTS["proactive_recall"]["search_timeout_s"] == 3
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m pytest carta/hook/tests/test_hook.py -v -k "search_timeout or fails_open_when_search"`
Expected: the two `timeout_s` tests FAIL (`assert None == 7`), `test_search_timeout_default_registered_in_config` FAILS with `KeyError`. The fail-open test PASSES already.

- [ ] **Step 3: Implement**

In `carta/config.py`, add to the `proactive_recall` DEFAULTS block:

```python
        "search_timeout_s": 3,
```

In `carta/hook/hook.py`, read it alongside the other thresholds (after `judge_timeout_s`):

```python
    search_timeout_s = pr.get("search_timeout_s", 3)
```

and pass it at the call site:

```python
        hits = run_search(query, search_cfg, timeout_s=search_timeout_s)
```

Then extend the existing comment block above `search_cfg` with a third sentence:

```python
    # ... and it runs under a wall-clock budget (search_timeout_s) so an
    # unreachable REMOTE backend cannot stall submission. A dead localhost
    # backend refuses instantly; a dead tailnet peer drops packets silently, so
    # without a budget the 60s embed timeout becomes a 60s stall per prompt.
```

- [ ] **Step 4: Run to verify pass**

Run: `python3 -m pytest carta/hook/tests/test_hook.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add carta/config.py carta/hook/hook.py carta/hook/tests/test_hook.py
git commit -m "feat(hook): bound proactive-recall search with search_timeout_s

Default 3s, matching judge_timeout_s. Only the hook opts in; every other
run_search caller keeps the unbounded-in-practice defaults."
```

---

### Task 5: End-to-end proof and docs

Proves the bound holds against a backend that genuinely blocks, rather than a mock that returns instantly.

**Files:**
- Test: `carta/hook/tests/test_hook.py` (append)
- Modify: `README.md` (proactive-recall section)

**Interfaces:**
- Consumes: everything above. Produces nothing.

- [ ] **Step 1: Write the failing test**

```python
def test_hook_returns_within_budget_against_a_blocking_backend():
    """The whole point: a backend that hangs must not hang the prompt.

    Without the budget this blocks for the full 60s embed timeout. `run_search`
    is patched to block, so this exercises the hook's contract end to end rather
    than the deadline arithmetic already covered in test_search_timeout.py.
    """
    cfg = _make_cfg()
    cfg["proactive_recall"]["search_timeout_s"] = 1

    def blocking_run_search(query, search_cfg, **kwargs):
        budget = kwargs.get("timeout_s")
        assert budget == 1, f"hook must pass its budget down, got {budget}"
        time.sleep(budget)
        raise TimeoutError("backend unreachable")

    start = time.monotonic()
    with (
        patch("sys.stdin", _stdin("query")),
        patch("carta.hook.hook.find_config", return_value=Path("/fake/.carta/config.yaml")),
        patch("carta.hook.hook.load_config", return_value=cfg),
        patch("carta.hook.hook.run_search", side_effect=blocking_run_search),
    ):
        out = _capture_main()
    elapsed = time.monotonic() - start

    assert out.strip() == "", "must not inject when the backend is unreachable"
    assert elapsed < 5, f"hook took {elapsed:.1f}s; budget was 1s"
```

`time` is already imported at the top of `test_hook.py`.

- [ ] **Step 2: Run to verify it passes**

Run: `python3 -m pytest carta/hook/tests/test_hook.py -v -k blocking_backend`
Expected: PASS (Task 4 already wired it; this test locks the contract in).

- [ ] **Step 3: Document it**

Add to the proactive-recall section of `README.md`:

```markdown
**Search budget.** The hook runs under a wall-clock budget —
`proactive_recall.search_timeout_s`, default 3 s — covering the query embed and
the Qdrant queries together. If it expires the hook stays silent and the prompt
proceeds immediately.

This matters most with a **remote** Qdrant. A dead localhost backend refuses
instantly, so the underlying 60 s embed timeout never binds; a dead remote peer
drops packets with no RST, so without a budget every prompt would stall for the
full timeout. Only the hook is bounded — `carta search`, MCP and `carta eval`
keep their generous timeouts, since a slow search there is fine and a slow prompt
is not.
```

- [ ] **Step 4: Full suite**

Run: `python3 -m pytest -q`
Expected: `1217 passed, 3 skipped` (1205 baseline + 12 new).

- [ ] **Step 5: Commit and push**

```bash
git add carta/hook/tests/test_hook.py README.md
git commit -m "test(hook): prove the budget bounds a blocking backend; document it"
git push -u origin fix/hook-search-timeout
```

---

## Done when

- `python3 -m pytest -q` reports `1217 passed, 3 skipped` with no pre-existing test modified
- `run_search` with no `timeout_s` still constructs a 10 s client and does not clamp the embed
- The hook passes its configured budget and returns promptly against a blocking backend
- Fail-open is intact: no injection, exit 0, on any search error

## Not in this plan

- `carta doctor` remote reachability checks and documented remote-endpoint config — both #106, independent
- Repointing the Mac at homelab — follows a release
- Any global search timeout — explicitly rejected in the spec
