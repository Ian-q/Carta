# Visual doc_generation + orphan cleanup — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop ColPali visual points from orphaning on re-embed by stamping `doc_generation`, normalizing to a single stable point-ID key, and sweeping superseded visual points the way the text lane already does.

**Architecture:** Visual point IDs become stable per `(file_path, page_num)` (generation-free), so a re-drained page overwrites in place. A new `_delete_visual_orphans` helper wraps the existing `delete_other_points` (id-set-based, generation-agnostic) and is called after a file's visual pages are written — at the inline call-site and per-file in the drain loop (gated on clean completion). Spec: `docs/superpowers/specs/2026-06-25-visual-doc-generation-cleanup-design.md`.

**Tech Stack:** Python 3.10+, qdrant-client, pytest + unittest.mock. No new dependencies.

## Global Constraints

- Python 3.10+ syntax; 4-space indent; ~100-char lines (verbatim from CLAUDE.md conventions).
- Embed-pipeline changes **must not regress existing sidecar state or Qdrant collections** (CLAUDE.md compatibility constraint).
- Cleanup must stay **best-effort** — `delete_other_points` retries and never raises; never fail an embed that succeeded.
- Scope is issue #78 points **1, 2, 4 only**. Point 3 (`carta doctor` count reconciliation) is a separate fast-follow — do **not** touch doctor/audit accounting here.
- Full test suite must stay green on Python 3.10–3.12.
- Run tests from the worktree root: `python -m pytest <path> -v`.

---

### Task 1: Stamp `doc_generation` + lifecycle fields & single-key stable ID in `upsert_visual_pages`

**Files:**
- Modify: `carta/embed/embed.py:427-435` (the per-page payload build + `id_key` in `upsert_visual_pages`)
- Test: `carta/embed/tests/test_embed.py`

**Interfaces:**
- Consumes: `_visual_point_id(key: str, page_num: int) -> str` (`carta/embed/embed.py:190`), `MINIMAL_CFG` test fixture (already in `test_embed.py`).
- Produces: `upsert_visual_pages` payloads now carry `doc_generation` (int, default `1`), `stale_as_of=None`, `superseded_at=None`, `orphaned_at=None`; point IDs always derive from `file_path` (loud stderr fallback to `slug`). Later tasks rely on the ID being `_visual_point_id(rel_path, page_num)`.

- [ ] **Step 1: Write the failing tests**

Add to `carta/embed/tests/test_embed.py` (mirror the existing `test_upsert_visual_pages` at line 1281):

```python
@patch("carta.embed.embed.QdrantClient")
def test_upsert_visual_pages_stamps_generation_and_lifecycle(mock_qdrant_cls):
    """Visual payloads carry doc_generation + lifecycle fields, mirroring upsert_chunks."""
    from carta.embed.embed import upsert_visual_pages
    import numpy as np

    mock_client = MagicMock()
    mock_client.collection_exists.return_value = True
    mock_qdrant_cls.return_value = mock_client

    pages = [{
        "slug": "datasheet", "file_path": "docs/datasheet.pdf", "page_num": 1,
        "vectors": np.zeros((8, 128), dtype=np.float32),
        "png_path": ".carta/visual_cache/datasheet/page_0001.png",
        "doc_type": "visual_page", "doc_generation": 3,
    }]
    cfg = {**MINIMAL_CFG, "project_name": "test"}

    upsert_visual_pages(pages, cfg, client=mock_client)

    payload = mock_client.upsert.call_args.kwargs["points"][0].payload
    assert payload["doc_generation"] == 3
    assert payload["stale_as_of"] is None
    assert payload["superseded_at"] is None
    assert payload["orphaned_at"] is None


@patch("carta.embed.embed.QdrantClient")
def test_upsert_visual_pages_defaults_generation_to_one(mock_qdrant_cls):
    """A page without doc_generation defaults to generation 1."""
    from carta.embed.embed import upsert_visual_pages
    import numpy as np

    mock_client = MagicMock()
    mock_client.collection_exists.return_value = True
    mock_qdrant_cls.return_value = mock_client

    pages = [{
        "slug": "d", "file_path": "docs/d.pdf", "page_num": 1,
        "vectors": np.zeros((8, 128), dtype=np.float32),
        "png_path": "x.png", "doc_type": "visual_page",
    }]
    upsert_visual_pages(pages, {**MINIMAL_CFG, "project_name": "test"}, client=mock_client)
    assert mock_client.upsert.call_args.kwargs["points"][0].payload["doc_generation"] == 1


@patch("carta.embed.embed.QdrantClient")
def test_upsert_visual_pages_id_uses_file_path(mock_qdrant_cls):
    """Point ID derives from file_path via _visual_point_id."""
    from carta.embed.embed import upsert_visual_pages, _visual_point_id
    import numpy as np

    mock_client = MagicMock()
    mock_client.collection_exists.return_value = True
    mock_qdrant_cls.return_value = mock_client

    pages = [{
        "slug": "d", "file_path": "docs/d.pdf", "page_num": 2,
        "vectors": np.zeros((8, 128), dtype=np.float32),
        "png_path": "x.png", "doc_type": "visual_page",
    }]
    upsert_visual_pages(pages, {**MINIMAL_CFG, "project_name": "test"}, client=mock_client)
    point = mock_client.upsert.call_args.kwargs["points"][0]
    assert point.id == _visual_point_id("docs/d.pdf", 2)


@patch("carta.embed.embed.QdrantClient")
def test_upsert_visual_pages_warns_on_slug_fallback(mock_qdrant_cls, capsys):
    """A page missing file_path warns to stderr and falls back to a slug-keyed ID."""
    from carta.embed.embed import upsert_visual_pages, _visual_point_id
    import numpy as np

    mock_client = MagicMock()
    mock_client.collection_exists.return_value = True
    mock_qdrant_cls.return_value = mock_client

    pages = [{
        "slug": "d", "page_num": 1,  # no file_path
        "vectors": np.zeros((8, 128), dtype=np.float32),
        "png_path": "x.png", "doc_type": "visual_page",
    }]
    upsert_visual_pages(pages, {**MINIMAL_CFG, "project_name": "test"}, client=mock_client)
    point = mock_client.upsert.call_args.kwargs["points"][0]
    assert point.id == _visual_point_id("d", 1)
    assert "no file_path" in capsys.readouterr().err
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest carta/embed/tests/test_embed.py -k "stamps_generation or defaults_generation or id_uses_file_path or warns_on_slug" -v`
Expected: FAIL — `doc_generation`/`stale_as_of` KeyError, and no warning emitted.

- [ ] **Step 3: Implement in `carta/embed/embed.py`**

Replace the payload build + `id_key` lines in `upsert_visual_pages` (currently `embed.py:427-435`) with:

```python
            # Build payload with page metadata
            payload = {
                k: v for k, v in page.items()
                if k not in ("vectors", "png_bytes")
            }
            payload["doc_type"] = page.get("doc_type", "visual_page")
            # Mirror upsert_chunks (embed.py:255-261): stamp generation + lifecycle
            # fields so visual points share the text lane's staleness/cleanup model.
            payload["doc_generation"] = page.get("doc_generation", 1)
            payload["stale_as_of"] = None
            payload["superseded_at"] = None
            payload["orphaned_at"] = None

            id_key = page.get("file_path")
            if not id_key:
                # Slug-keyed IDs collide across same-stem files; make a regression loud.
                print(
                    f"Warning: visual page {page.get('slug', '?')}[p{page.get('page_num', '?')}] "
                    f"has no file_path — falling back to slug-keyed point ID",
                    file=sys.stderr, flush=True,
                )
                id_key = page["slug"]
            point_id = _visual_point_id(id_key, page["page_num"])
```

(`sys` is already imported in `embed.py` — used by the `build_point` warning at line 271.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest carta/embed/tests/test_embed.py -k "visual" -v`
Expected: PASS (new tests + existing `test_upsert_visual_pages*` stay green).

- [ ] **Step 5: Commit**

```bash
git add carta/embed/embed.py carta/embed/tests/test_embed.py
git commit -m "fix(#78): stamp doc_generation + single-key stable ID on visual points"
```

---

### Task 2: Add the `_delete_visual_orphans` helper

**Files:**
- Modify: `carta/embed/pipeline.py` (add `_visual_point_id` to the `carta.embed.embed` import block at lines 24-32; add the helper near the visual functions, e.g. just above `_embed_visual_pages_colpali` at line 757)
- Test: `carta/embed/tests/test_visual_drainer.py`

**Interfaces:**
- Consumes: `delete_other_points(client, collection_name, rel_path, keep_ids)` (already imported, `pipeline.py:35`), `_visual_point_id` (from `carta.embed.embed`).
- Produces: `_delete_visual_orphans(client, cfg: dict, rel_path: str, keep_page_nums: list[int]) -> None` — deletes every `{project}_visual` point for `rel_path` except the stable IDs of `keep_page_nums`. Tasks 3 and 4 call this.

- [ ] **Step 1: Write the failing test**

Add to `carta/embed/tests/test_visual_drainer.py`:

```python
def test_delete_visual_orphans_keeps_only_listed_pages(monkeypatch):
    """_delete_visual_orphans sweeps {project}_visual for rel_path, keeping only
    the stable IDs of the given page numbers."""
    from unittest.mock import MagicMock
    from carta.embed import pipeline
    from carta.embed.embed import _visual_point_id

    calls = []
    monkeypatch.setattr(
        pipeline, "delete_other_points",
        lambda client, collection_name, rel_path, keep_ids: calls.append((collection_name, rel_path, keep_ids)),
    )

    pipeline._delete_visual_orphans(MagicMock(), {"project_name": "proj"}, "docs/x.pdf", [1, 2])

    assert len(calls) == 1
    coll, rel_path, keep_ids = calls[0]
    assert coll == "proj_visual"
    assert rel_path == "docs/x.pdf"
    assert keep_ids == [_visual_point_id("docs/x.pdf", 1), _visual_point_id("docs/x.pdf", 2)]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest carta/embed/tests/test_visual_drainer.py::test_delete_visual_orphans_keeps_only_listed_pages -v`
Expected: FAIL — `AttributeError: module 'carta.embed.pipeline' has no attribute '_delete_visual_orphans'`.

- [ ] **Step 3: Implement in `carta/embed/pipeline.py`**

Add `_visual_point_id` to the import block (lines 24-32), so it reads:

```python
    upsert_chunks,
    get_embedding,
    upsert_visual_pages,
    collection_is_hybrid,
    _point_id_versioned,
    _visual_point_id,
    DENSE_VECTOR_NAME,
    SPARSE_VECTOR_NAME,
    UPSERT_CLIENT_TIMEOUT_S,
)
```

Add the helper just above `def _embed_visual_pages_colpali(` (line 757):

```python
def _delete_visual_orphans(client, cfg: dict, rel_path: str, keep_page_nums: list[int]) -> None:
    """Sweep stale visual points for one file.

    Deletes every ``{project}_visual`` point for ``rel_path`` except the stable
    IDs of ``keep_page_nums``. Mirrors the text lane's post-upsert
    ``delete_other_points`` (pipeline.py:695): id-set-based, so it removes legacy
    slug-keyed points, pre-fix generation-less points, and pages the document no
    longer has — regardless of doc_generation. Best-effort (delete_other_points
    retries and never raises).
    """
    coll = f"{cfg['project_name']}_visual"
    keep_ids = [_visual_point_id(rel_path, p) for p in keep_page_nums]
    delete_other_points(client, coll, rel_path=rel_path, keep_ids=keep_ids)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest carta/embed/tests/test_visual_drainer.py::test_delete_visual_orphans_keeps_only_listed_pages -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add carta/embed/pipeline.py carta/embed/tests/test_visual_drainer.py
git commit -m "fix(#78): add _delete_visual_orphans sweep helper"
```

---

### Task 3: Wire the sweep into the drain path (`run_visual_embed`), gated on clean completion

**Files:**
- Modify: `carta/embed/pipeline.py:1140-1160` (the per-file outer loop in `run_visual_embed`)
- Test: `carta/embed/tests/test_visual_drainer.py`

**Interfaces:**
- Consumes: `_delete_visual_orphans` (Task 2), `VISUAL_PENDING_KEY`/`VISUAL_DONE_KEY` (imported, `pipeline.py:36`).
- Produces: after each file's pages drain with **no failures**, the file's `_visual` orphans are swept using its full post-drain `visual_done` set as `keep_page_nums`.

- [ ] **Step 1: Write the failing tests**

Add to `carta/embed/tests/test_visual_drainer.py` (mirror `test_drainer_checkpoints_each_page` and `test_drainer_leaves_failed_page_pending`):

```python
def test_drainer_sweeps_orphans_after_clean_file(monkeypatch, tmp_path):
    """After a file drains cleanly, its visual orphans are swept with keep = visual_done."""
    sc = {"current_path": "docs/x.pdf", "slug": "x", VISUAL_PENDING_KEY: [1, 2], VISUAL_DONE_KEY: []}
    monkeypatch.setattr(pipeline, "_discover_visual_pending", lambda r: [("sc", sc)], raising=False)
    monkeypatch.setattr(pipeline, "_update_sidecar", lambda *a, **k: None)
    monkeypatch.setattr(pipeline, "_visual_embed_one_page", lambda *a, **k: True, raising=False)
    monkeypatch.setattr(pipeline, "is_colpali_available", lambda: True, raising=False)
    monkeypatch.setattr(pipeline, "QdrantClient", lambda **k: MagicMock())
    _mock_router_embedder(monkeypatch)

    swept = []
    monkeypatch.setattr(pipeline, "_delete_visual_orphans",
                        lambda client, cfg, rel_path, keep: swept.append((rel_path, list(keep))))

    pipeline.run_visual_embed(tmp_path, {"qdrant_url": "x", "embed": {}})

    assert swept == [("docs/x.pdf", [1, 2])]


def test_drainer_skips_sweep_when_a_page_fails(monkeypatch, tmp_path):
    """If any page of a file fails this run, the file's sweep is skipped."""
    sc = {"current_path": "docs/x.pdf", "slug": "x", VISUAL_PENDING_KEY: [1], VISUAL_DONE_KEY: []}
    monkeypatch.setattr(pipeline, "_discover_visual_pending", lambda r: [("sc", sc)], raising=False)
    monkeypatch.setattr(pipeline, "_update_sidecar", lambda *a, **k: None)

    def boom(*a, **k):
        raise RuntimeError("colpali failed")
    monkeypatch.setattr(pipeline, "_visual_embed_one_page", boom, raising=False)
    monkeypatch.setattr(pipeline, "is_colpali_available", lambda: True, raising=False)
    monkeypatch.setattr(pipeline, "QdrantClient", lambda **k: MagicMock())
    _mock_router_embedder(monkeypatch)

    swept = []
    monkeypatch.setattr(pipeline, "_delete_visual_orphans", lambda *a, **k: swept.append(a))

    pipeline.run_visual_embed(tmp_path, {"qdrant_url": "x", "embed": {}})

    assert swept == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest carta/embed/tests/test_visual_drainer.py -k "sweeps_orphans or skips_sweep" -v`
Expected: FAIL — `_delete_visual_orphans` never called (`swept == []` for the clean case).

- [ ] **Step 3: Implement in `carta/embed/pipeline.py`**

Replace the per-file outer loop body in `run_visual_embed` (currently `pipeline.py:1140-1160`) with:

```python
        for sc_path, sc in queued:
            rel_path = sc.get("current_path") or ""
            file_failed = False
            for page in list(sc.get(VISUAL_PENDING_KEY, []) or []):
                idx += 1
                status.file_start(idx, f"page {page} of {rel_path}")
                try:
                    _visual_embed_one_page(sc, page, cfg, client, repo_root, router, embedder, verbose)
                    move_to_done(sc, page)
                    _update_sidecar(sc_path, {
                        VISUAL_PENDING_KEY: sc[VISUAL_PENDING_KEY],
                        VISUAL_DONE_KEY: sc[VISUAL_DONE_KEY],
                    })
                    summary["pages_embedded"] += 1
                    status.file_done(embedded=1)
                except Exception as e:
                    file_failed = True
                    summary["pages_failed"] += 1
                    status.file_done(errors=1)
                    print(
                        f"  visual: page {page} of {sc.get('current_path')} failed: {e} "
                        f"(left pending)",
                        flush=True,
                    )
            # Sweep the file's stale visual points only after a clean drain — never
            # delete a page that's going to be retried (mirrors the text lane's
            # "clean up only after complete success", pipeline.py:687).
            if rel_path and not file_failed and sc.get(VISUAL_DONE_KEY):
                _delete_visual_orphans(client, cfg, rel_path, list(sc[VISUAL_DONE_KEY]))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest carta/embed/tests/test_visual_drainer.py -v`
Expected: PASS (new sweep tests + all existing drainer tests stay green).

- [ ] **Step 5: Commit**

```bash
git add carta/embed/pipeline.py carta/embed/tests/test_visual_drainer.py
git commit -m "fix(#78): sweep visual orphans per file in the drain, gated on clean completion"
```

---

### Task 4: Wire the sweep into the inline path (`_embed_visual_pages_colpali`)

**Files:**
- Modify: `carta/embed/pipeline.py:863-868` (after the inline `upsert_visual_pages` call)
- Test: `carta/embed/tests/test_visual_drainer.py`

**Interfaces:**
- Consumes: `_delete_visual_orphans` (Task 2); `_embed_visual_pages_colpali(file_path, file_info, cfg, client, repo_root, verbose=False)` builds `visual_pages` dicts with `page_num` + `file_path` (`pipeline.py:853-861`).
- Produces: the inline visual path sweeps the file's orphans with `keep_page_nums = [p["page_num"] for p in visual_pages]`.

- [ ] **Step 1: Write the failing test**

Add to `carta/embed/tests/test_visual_drainer.py`:

```python
def test_inline_colpali_sweeps_orphans(monkeypatch, tmp_path):
    """_embed_visual_pages_colpali sweeps the file's orphans after upserting its pages."""
    import numpy as np
    from carta.embed import pipeline

    class _FakeEmbedder:
        def __init__(self, **kw):
            pass
        def embed_pdf_pages(self, file_path, page_nums=None):
            return [{"page_num": 1, "vectors": np.zeros((4, 128), dtype=np.float32), "png_bytes": b"x"}]
        def save_page_cache(self, file_path, page_num, png_bytes):
            return tmp_path / ".carta" / "visual_cache" / "x" / "page_0001.png"

    monkeypatch.setattr("carta.embed.colpali.is_colpali_available", lambda: True, raising=False)
    monkeypatch.setattr("carta.embed.colpali.ColPaliEmbedder", lambda **kw: _FakeEmbedder(), raising=False)
    monkeypatch.setattr(pipeline, "upsert_visual_pages", lambda pages, cfg, client=None: len(pages))

    swept = []
    monkeypatch.setattr(pipeline, "_delete_visual_orphans",
                        lambda client, cfg, rel_path, keep: swept.append((rel_path, list(keep))))

    pdf = tmp_path / "docs" / "x.pdf"
    pdf.parent.mkdir(parents=True)
    pdf.write_bytes(b"%PDF-1.4")

    pipeline._embed_visual_pages_colpali(
        pdf, {"slug": "x"}, {"project_name": "test", "embed": {}}, MagicMock(), tmp_path,
    )

    assert swept == [("docs/x.pdf", [1])]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest carta/embed/tests/test_visual_drainer.py::test_inline_colpali_sweeps_orphans -v`
Expected: FAIL — `swept == []` (no sweep wired into the inline path).

- [ ] **Step 3: Implement in `carta/embed/pipeline.py`**

Replace the inline upsert block (currently `pipeline.py:863-868`) with:

```python
        # Upsert to visual collection
        if visual_pages:
            upserted = upsert_visual_pages(visual_pages, cfg, client=client)
            rel_path = str(file_path.relative_to(repo_root))
            _delete_visual_orphans(client, cfg, rel_path, [p["page_num"] for p in visual_pages])
            if verbose:
                print(f"    ColPali: embedded {upserted} visual page(s)", flush=True)
            return upserted
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest carta/embed/tests/test_visual_drainer.py::test_inline_colpali_sweeps_orphans -v`
Expected: PASS.

- [ ] **Step 5: Run the full embed suite + commit**

```bash
python -m pytest carta/embed/tests/ -q
```
Expected: all pass (no regressions).

```bash
git add carta/embed/pipeline.py carta/embed/tests/test_visual_drainer.py
git commit -m "fix(#78): sweep visual orphans on the inline ColPali path"
```

---

### Task 5: Full-suite verification + file the point-3 fast-follow

**Files:** none (verification + issue admin)

- [ ] **Step 1: Run the full test suite on the worktree**

Run: `python -m pytest -q`
Expected: all green (matches the ~1035-test baseline; no regressions).

- [ ] **Step 2: File the point-3 fast-follow issue**

```bash
gh issue create --title "carta doctor: reconcile sidecar visual_done count with _visual collection" \
  --body "Fast-follow to #78 (storage fix landed). The sidecar visual_done accounting and the _visual collection point count diverge, so carta doctor's visual count check is not meaningful. Reconcile them now that re-drains replace rather than orphan. See docs/superpowers/specs/2026-06-25-visual-doc-generation-cleanup-design.md (Fast-follow section)."
```

- [ ] **Step 3: Update the GitHub Project + finish the branch**

Move #78 to Done on Project #4, then follow `superpowers:finishing-a-development-branch` to merge `fix/issue-78-visual-doc-generation` and remove the worktree.

## Self-Review

**Spec coverage:**
- Goal 1 (stamp `doc_generation` + lifecycle) → Task 1. ✓
- Goal 2 (replace, don't orphan; `delete_other_points`) → Tasks 2 (helper), 3 (drain), 4 (inline). ✓
- Goal 3 (single-key stable IDs) → Task 1 (`id_key = file_path`, stable `_visual_point_id`). ✓
- Goal 4 (no new failure modes; best-effort; clean-completion gate) → Task 2 (reuses `delete_other_points`), Task 3 (`file_failed` gate). ✓
- Non-goal: point 3 explicitly deferred → Task 5 Step 2 files it; no doctor/audit code touched. ✓

**Placeholder scan:** No TBD/TODO; every code + test step shows full code and exact run commands. ✓

**Type consistency:** `_delete_visual_orphans(client, cfg, rel_path, keep_page_nums)` defined in Task 2 and called with that exact signature in Tasks 3 and 4. `_visual_point_id(key, page_num)` used consistently. `keep_page_nums` is always a `list[int]` of page numbers (drain: `sc[VISUAL_DONE_KEY]`; inline: `[p["page_num"] for p in visual_pages]`). ✓
