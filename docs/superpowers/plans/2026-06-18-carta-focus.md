# Carta Focus (file-scoped retrieval) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `carta focus` (CLI) + `carta_focus` (MCP) capability that goes deep in one already-located file — page-anchored passages, an outline mode, and table/figure pages returned as images — plus surface `page`/`section_heading` on every search result.

**Architecture:** A new `run_focus` engine in `carta/embed/pipeline.py` reuses the existing per-collection query helpers (`_hybrid_query_collection`, `_rrf_merge_collections`, `_visual_collection_ready`) but scopes every Qdrant call with a `file_path` payload filter, forces dedup off, disables the visual cap, and attaches a rendered page PNG to visual hits. Two surfaces wrap it: a `carta_focus` MCP tool (base64 images inline) and a `carta focus` CLI subcommand (writes PNGs to cache, prints paths). A small shared change surfaces `page`/`section_heading`, already in the Qdrant payload, on all search results.

**Tech Stack:** Python 3.10+, qdrant-client (`query_points`/`scroll`/`create_payload_index`, `models.Filter`), PyMuPDF (`fitz`) for page rendering, pytest + unittest.mock for tests.

**Spec:** `docs/superpowers/specs/2026-06-18-carta-focus-design.md`

---

## File Structure

- **Modify** `carta/embed/pipeline.py` — add `page`/`section_heading` to `run_search` result builders; add `query_filter` param to `_hybrid_query_collection`; add new helpers `_normalize_source`, `_file_filter`, `_ensure_file_path_index`, `render_page_png`, `_attach_page_images`, `_focus_outline`, `_focus_deep`, and the public `run_focus`. Module constant `_FOCUS_DEFAULT_LIMIT = 15`.
- **Modify** `carta/mcp/server.py` — import `run_focus`; add `carta_focus` tool.
- **Modify** `carta/cli.py` — add `cmd_focus`, the `focus` subparser, and the dispatch entry.
- **Modify** `carta/tests/test_pipeline.py` — unit/integration tests for all new pipeline functions.
- **Modify** `carta/tests/test_mcp_server.py` — test for `carta_focus`.
- **Modify** `carta/tests/test_cli.py` — test for `cmd_focus`.
- **Modify** `CLAUDE.md`, `README.md`, `AGENTS.md` — document the new surface.

> Conventions verified in-repo: text chunks store `page` + `section_heading` in the Qdrant payload (`carta/embed/parse.py:204` → `carta/embed/embed.py:256`). `run_search` currently drops them (`pipeline.py:1930-1938`). The MCP `carta_search` bypasses `run_search` with its own logic — **`carta_focus` deliberately calls the real `run_focus` instead.** qdrant-client filter param names differ by call: `query_points(..., query_filter=...)`, `scroll(..., scroll_filter=...)`, `Prefetch(..., filter=...)`.

---

## Phase 1 — Anchor surfacing (shared foundation)

### Task 1: Surface `page` + `section_heading` on every search result

**Files:**
- Modify: `carta/embed/pipeline.py` (text builder ~1930-1938; visual builder ~1881-1889)
- Test: `carta/tests/test_pipeline.py`

- [ ] **Step 1: Write the failing test**

Add to `carta/tests/test_pipeline.py`, in the `TestRunSearch` class:

```python
    def test_results_carry_page_and_section_anchors(self):
        """run_search surfaces page + section_heading from the payload on each hit."""
        from unittest.mock import patch, MagicMock
        from carta.embed.pipeline import run_search

        cfg = {
            "project_name": "test-project",
            "qdrant_url": "http://localhost:6333",
            "embed": {"ollama_url": "http://localhost:11434",
                      "ollama_model": "nomic-embed-text", "colpali_enabled": False},
            "search": {"top_n": 5},
            "modules": {"doc_search": True},
        }

        point = MagicMock()
        point.score = 0.91
        point.payload = {"file_path": "docs/imu.pdf", "text": "sensitivity register",
                         "page": 47, "section_heading": "6.3 Gyro Config", "doc_type": ""}
        resp = MagicMock(); resp.points = [point]
        mock_client = MagicMock()
        mock_client.query_points.return_value = resp

        with patch("carta.embed.pipeline.QdrantClient", return_value=mock_client), \
             patch("carta.embed.pipeline.get_embedding", return_value=[0.0] * 768), \
             patch("carta.embed.pipeline.collection_is_hybrid", return_value=False), \
             patch("carta.search.scoped.get_search_collections", return_value=["test-project_doc"]), \
             patch("carta.embed.pipeline.find_config", return_value="/fake/.carta/config.yaml"):
            results = run_search("imu sensitivity", cfg)

        assert results, "expected at least one hit"
        assert results[0]["page"] == 47
        assert results[0]["section_heading"] == "6.3 Gyro Config"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest carta/tests/test_pipeline.py::TestRunSearch::test_results_carry_page_and_section_anchors -v`
Expected: FAIL — `KeyError: 'page'` (run_search does not yet surface it).

- [ ] **Step 3: Add the keys to both result builders**

In `carta/embed/pipeline.py`, the **text** builder (~1932-1938) becomes:

```python
                for r in response.points:
                    payload = r.payload or {}
                    coll_results.append({
                        "score": r.score,
                        "source": payload.get("file_path", payload.get("slug", "")),
                        "excerpt": payload.get("text", ""),
                        "type": "text",
                        "doc_type": payload.get("doc_type", ""),
                        "page": payload.get("page"),
                        "section_heading": payload.get("section_heading", ""),
                    })
```

The **visual** builder (~1883-1889) becomes:

```python
                    for r in response.points:
                        payload = r.payload or {}
                        coll_results.append({
                            "score": r.score,
                            "source": f"{payload.get('file_path', payload.get('slug', ''))} (page {payload.get('page_num', '?')})",
                            "excerpt": f"[Visual result] Page {payload.get('page_num', '?')} - {payload.get('file_path', '')}",
                            "type": "visual",
                            "doc_type": payload.get("doc_type", ""),
                            "page": payload.get("page_num"),
                            "section_heading": "",
                        })
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest carta/tests/test_pipeline.py::TestRunSearch -v`
Expected: PASS (new test green, existing `TestRunSearch` tests still green).

- [ ] **Step 5: Verify no consumer breaks on the new keys**

Run: `python -m pytest carta/tests/test_pipeline.py carta/tests/test_cli.py carta/tests/test_mcp_server.py -q`
Expected: PASS. (Consumers read results by key; added keys are inert. If any test asserts exact dict equality on a result, update it to include the new keys.)

- [ ] **Step 6: Commit**

```bash
git add carta/embed/pipeline.py carta/tests/test_pipeline.py
git commit -m "feat(search): surface page + section_heading on all search results"
```

---

## Phase 2 — Focus engine

### Task 2: Add a `query_filter` parameter to `_hybrid_query_collection`

**Files:**
- Modify: `carta/embed/pipeline.py:1610-1636`
- Test: `carta/tests/test_pipeline.py`

- [ ] **Step 1: Write the failing test**

Add a new class to `carta/tests/test_pipeline.py`:

```python
class TestHybridQueryFilter:
    """_hybrid_query_collection threads an optional Qdrant filter into each prefetch lane."""

    def test_query_filter_applied_to_prefetch(self):
        from unittest.mock import MagicMock, patch
        from carta.embed.pipeline import _hybrid_query_collection
        from qdrant_client.models import Filter, FieldCondition, MatchValue

        client = MagicMock()
        client.query_points.return_value = MagicMock(points=[])
        ff = Filter(must=[FieldCondition(key="file_path", match=MatchValue(value="a.pdf"))])

        with patch("carta.embed.pipeline.embed_sparse_query",
                   return_value=MagicMock(indices=[1], values=[1.0])):
            _hybrid_query_collection(client, "c", "q", [0.0] * 768, 10,
                                     prefetch_limit=40, bm25_model="Qdrant/bm25",
                                     query_filter=ff)

        kwargs = client.query_points.call_args.kwargs
        prefetches = kwargs["prefetch"]
        assert all(p.filter is ff for p in prefetches), "filter must reach every prefetch lane"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest carta/tests/test_pipeline.py::TestHybridQueryFilter -v`
Expected: FAIL — `TypeError: _hybrid_query_collection() got an unexpected keyword argument 'query_filter'`.

- [ ] **Step 3: Add the parameter**

Replace `_hybrid_query_collection` (`pipeline.py:1610-1636`) with:

```python
def _hybrid_query_collection(client, coll_name, query, dense_vec, top_n,
                              prefetch_limit, bm25_model, query_filter=None):
    """Run a hybrid BM25+dense query with Qdrant RRF fusion.

    Fetches `prefetch_limit` candidates from each of the dense and sparse
    indexes, then fuses them with Reciprocal Rank Fusion and returns the
    top `top_n` results.

    `top_n` controls the Qdrant fusion `limit` (i.e. how many fused results
    to return).  When reranking is enabled, callers should pass `fetch_limit`
    (= candidate_pool) here so that the reranker has a wide enough pool to
    promote lower-ranked relevant documents.

    `query_filter` (optional) is applied to BOTH prefetch lanes so the filter
    takes effect before fusion (used by run_focus to scope to one file).
    """
    sv = embed_sparse_query(query, model_name=bm25_model)
    return client.query_points(
        collection_name=coll_name,
        prefetch=[
            qmodels.Prefetch(query=dense_vec, using=DENSE_VECTOR_NAME,
                             limit=prefetch_limit, filter=query_filter),
            qmodels.Prefetch(
                query=qmodels.SparseVector(indices=sv.indices, values=sv.values),
                using=SPARSE_VECTOR_NAME, limit=prefetch_limit, filter=query_filter,
            ),
        ],
        query=qmodels.FusionQuery(fusion=qmodels.Fusion.RRF),
        limit=top_n,
        with_payload=True,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest carta/tests/test_pipeline.py::TestHybridQueryFilter -v`
Expected: PASS. (Default `query_filter=None` keeps every existing caller unchanged.)

- [ ] **Step 5: Commit**

```bash
git add carta/embed/pipeline.py carta/tests/test_pipeline.py
git commit -m "feat(search): thread optional query_filter through _hybrid_query_collection"
```

---

### Task 3: Source helpers — `_normalize_source`, `_file_filter`, `_ensure_file_path_index`

**Files:**
- Modify: `carta/embed/pipeline.py` (add near the other search helpers, after `_apply_visual_cap`)
- Test: `carta/tests/test_pipeline.py`

- [ ] **Step 1: Write the failing test**

```python
class TestFocusSourceHelpers:
    def test_normalize_strips_visual_page_suffix(self):
        from carta.embed.pipeline import _normalize_source
        assert _normalize_source("docs/imu.pdf (page 12)") == "docs/imu.pdf"
        assert _normalize_source("docs/imu.pdf") == "docs/imu.pdf"
        assert _normalize_source("  a/b.md  ") == "a/b.md"

    def test_file_filter_matches_file_path(self):
        from carta.embed.pipeline import _file_filter
        ff = _file_filter("docs/imu.pdf")
        cond = ff.must[0]
        assert cond.key == "file_path"
        assert cond.match.value == "docs/imu.pdf"

    def test_ensure_index_swallows_errors(self):
        from unittest.mock import MagicMock
        from carta.embed.pipeline import _ensure_file_path_index
        client = MagicMock()
        client.create_payload_index.side_effect = Exception("already exists")
        # Must not raise.
        _ensure_file_path_index(client, "test-project_doc")
        client.create_payload_index.assert_called_once()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest carta/tests/test_pipeline.py::TestFocusSourceHelpers -v`
Expected: FAIL — `ImportError: cannot import name '_normalize_source'`.

- [ ] **Step 3: Implement the helpers**

Add to `carta/embed/pipeline.py` (after `_apply_visual_cap`, ~line 1681). Note `re`, `Filter`, and `qmodels` are already imported at the top of the module:

```python
_FOCUS_DEFAULT_LIMIT = 15  # passages returned by a deep focus query


def _normalize_source(source: str) -> str:
    """Strip a trailing ' (page N)' suffix (the visual-hit source form) to the bare file_path."""
    return re.sub(r"\s*\(page\s+\S+\)\s*$", "", source).strip()


def _file_filter(source: str) -> Filter:
    """Qdrant filter matching points whose file_path payload equals `source`."""
    return Filter(must=[qmodels.FieldCondition(
        key="file_path", match=qmodels.MatchValue(value=source))])


def _ensure_file_path_index(client, coll_name: str) -> None:
    """Idempotently create a keyword payload index on file_path to speed the focus filter.

    Fail-open: an existing index, a missing collection, or an older server all just mean
    the filter runs unindexed (correct, slower) — never an error to the caller.
    """
    try:
        client.create_payload_index(
            collection_name=coll_name,
            field_name="file_path",
            field_schema=qmodels.PayloadSchemaType.KEYWORD,
        )
    except Exception:
        pass
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest carta/tests/test_pipeline.py::TestFocusSourceHelpers -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add carta/embed/pipeline.py carta/tests/test_pipeline.py
git commit -m "feat(search): add focus source helpers (normalize, file filter, payload index)"
```

---

### Task 4: Page rendering — `render_page_png` + `_attach_page_images`

**Files:**
- Modify: `carta/embed/pipeline.py` (add after the Task 3 helpers)
- Test: `carta/tests/test_pipeline.py`

- [ ] **Step 1: Write the failing test**

```python
class TestRenderPageImages:
    def test_non_pdf_returns_none(self, tmp_path):
        from carta.embed.pipeline import render_page_png
        md = tmp_path / "a.md"; md.write_text("hello")
        assert render_page_png(md, 1, tmp_path) is None

    def test_cache_hit_returns_cached_bytes(self, tmp_path):
        from carta.embed.pipeline import render_page_png
        pdf = tmp_path / "imu.pdf"; pdf.write_bytes(b"%PDF-1.4 fake")
        cache = tmp_path / ".carta" / "visual_cache" / "imu"
        cache.mkdir(parents=True)
        (cache / "page_0007.png").write_bytes(b"CACHEDPNG")
        assert render_page_png(pdf, 7, tmp_path) == b"CACHEDPNG"

    def test_real_render_and_out_of_range(self, tmp_path):
        import fitz
        from carta.embed.pipeline import render_page_png
        pdf = tmp_path / "doc.pdf"
        doc = fitz.open()
        doc.new_page(); doc.new_page()
        doc.save(str(pdf)); doc.close()
        png = render_page_png(pdf, 1, tmp_path)
        assert png is not None and png[:8] == b"\x89PNG\r\n\x1a\n"
        assert render_page_png(pdf, 99, tmp_path) is None  # out of range

    def test_attach_images_only_to_visual_hits(self, tmp_path):
        from unittest.mock import patch
        from carta.embed.pipeline import _attach_page_images
        hits = [
            {"type": "text", "page": 3},
            {"type": "visual", "page": 7},
            {"type": "visual", "page": None},
        ]
        with patch("carta.embed.pipeline.render_page_png", return_value=b"PNGBYTES"):
            out = _attach_page_images(hits, tmp_path / "imu.pdf", tmp_path)
        assert "image_b64" not in out[0]            # text untouched
        assert out[1]["image_b64"]                  # visual w/ page rendered
        assert "image_b64" not in out[2]            # visual w/o page skipped
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest carta/tests/test_pipeline.py::TestRenderPageImages -v`
Expected: FAIL — `ImportError: cannot import name 'render_page_png'`.

- [ ] **Step 3: Implement the helpers**

Add to `carta/embed/pipeline.py` after the Task 3 helpers:

```python
_FOCUS_RENDER_DPI = 150  # page-image resolution for focus visual hits


def render_page_png(abs_file_path: Path, page: int, repo_root: Path) -> bytes | None:
    """Return PNG bytes for a 1-indexed PDF page, or None if it can't be produced.

    Fast path: a ColPali cache PNG at .carta/visual_cache/<stem>/page_NNNN.png.
    Fallback: render on demand with PyMuPDF. Non-PDF, out-of-range page, or any
    failure returns None so the caller degrades to anchors-only for that hit.
    """
    try:
        if abs_file_path.suffix.lower() != ".pdf":
            return None
        cached = (repo_root / ".carta" / "visual_cache" /
                  abs_file_path.stem / f"page_{page:04d}.png")
        if cached.is_file():
            return cached.read_bytes()
        import fitz  # PyMuPDF, imported lazily
        with fitz.open(str(abs_file_path)) as doc:
            if page < 1 or page > len(doc):
                return None
            pix = doc[page - 1].get_pixmap(dpi=_FOCUS_RENDER_DPI)
            return pix.tobytes("png")
    except Exception:
        return None


def _attach_page_images(hits: list[dict], abs_source_path: Path, repo_root: Path) -> list[dict]:
    """Attach a base64 page PNG to each visual hit that has a page number. Mutates + returns hits."""
    import base64
    for hit in hits:
        if hit.get("type") == "visual" and hit.get("page"):
            png = render_page_png(abs_source_path, hit["page"], repo_root)
            if png is not None:
                hit["image_b64"] = base64.b64encode(png).decode("ascii")
    return hits
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest carta/tests/test_pipeline.py::TestRenderPageImages -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add carta/embed/pipeline.py carta/tests/test_pipeline.py
git commit -m "feat(search): add render_page_png + _attach_page_images helpers"
```

---

### Task 5: Outline mode — `_focus_outline`

**Files:**
- Modify: `carta/embed/pipeline.py` (add after Task 4 helpers)
- Test: `carta/tests/test_pipeline.py`

- [ ] **Step 1: Write the failing test**

```python
class TestFocusOutline:
    def test_outline_returns_distinct_sections_in_page_order(self):
        from unittest.mock import MagicMock
        from carta.embed.pipeline import _focus_outline, _file_filter

        def mk(page, heading):
            p = MagicMock(); p.payload = {"page": page, "section_heading": heading}; return p

        client = MagicMock()
        # Unordered, with a duplicate (3,"Intro") that must collapse.
        client.scroll.return_value = (
            [mk(3, "Intro"), mk(1, "Cover"), mk(3, "Intro"), mk(2, "Setup")], None)

        rows = _focus_outline(client, ["test-project_doc"], _file_filter("imu.pdf"), "imu.pdf")

        assert [(r["page"], r["section_heading"]) for r in rows] == [
            (1, "Cover"), (2, "Setup"), (3, "Intro")]
        assert all(r["type"] == "outline" and r["source"] == "imu.pdf" for r in rows)
        # Outline must not embed anything — it scrolls payloads only.
        client.scroll.assert_called_once()

    def test_outline_skips_visual_collections(self):
        from unittest.mock import MagicMock
        from carta.embed.pipeline import _focus_outline, _file_filter
        client = MagicMock(); client.scroll.return_value = ([], None)
        _focus_outline(client, ["test-project_visual"], _file_filter("imu.pdf"), "imu.pdf")
        client.scroll.assert_not_called()  # visual collections are skipped
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest carta/tests/test_pipeline.py::TestFocusOutline -v`
Expected: FAIL — `ImportError: cannot import name '_focus_outline'`.

- [ ] **Step 3: Implement `_focus_outline`**

Add to `carta/embed/pipeline.py`:

```python
def _focus_outline(client, collections: list[str], ff: Filter, source: str) -> list[dict]:
    """Return the file's distinct (section_heading, page) rows in page order — a synthetic TOC.

    Scrolls text-collection payloads only (no embedding); pages with no number sort last.
    """
    seen: set = set()
    rows: list[tuple] = []
    for coll in collections:
        if coll.endswith("_visual"):
            continue
        try:
            points, _ = client.scroll(
                collection_name=coll, scroll_filter=ff,
                with_payload=True, limit=10_000,
            )
        except Exception:
            continue
        for p in points:
            payload = p.payload or {}
            page = payload.get("page")
            heading = payload.get("section_heading", "")
            key = (page, heading)
            if key in seen:
                continue
            seen.add(key)
            sort_page = page if isinstance(page, int) else 1_000_000
            rows.append((sort_page, page, heading))
    rows.sort(key=lambda r: r[0])
    return [{"score": 0.0, "source": source, "page": page,
             "section_heading": heading, "excerpt": "", "type": "outline",
             "doc_type": ""} for _sort, page, heading in rows]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest carta/tests/test_pipeline.py::TestFocusOutline -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add carta/embed/pipeline.py carta/tests/test_pipeline.py
git commit -m "feat(search): add _focus_outline (file section/page map)"
```

---

### Task 6: Deep mode + orchestrator — `_focus_deep` and `run_focus`

**Files:**
- Modify: `carta/embed/pipeline.py` (add after Task 5)
- Test: `carta/tests/test_pipeline.py`

- [ ] **Step 1: Write the failing tests**

```python
class TestRunFocus:
    BASE_CFG = {
        "project_name": "test-project",
        "qdrant_url": "http://localhost:6333",
        "embed": {"ollama_url": "http://localhost:11434",
                  "ollama_model": "nomic-embed-text", "colpali_enabled": False},
        "search": {"top_n": 5},
        "modules": {"doc_search": True},
    }

    def test_deep_filters_to_file_and_keeps_all_chunks(self):
        """Deep mode applies the file filter and does NOT dedup (multiple chunks, one source)."""
        from unittest.mock import patch, MagicMock
        from carta.embed.pipeline import run_focus

        def mk(page):
            p = MagicMock(); p.score = 1.0 / page
            p.payload = {"file_path": "docs/imu.pdf", "text": f"chunk p{page}",
                         "page": page, "section_heading": f"S{page}", "doc_type": ""}
            return p
        resp = MagicMock(); resp.points = [mk(10), mk(11), mk(12)]
        client = MagicMock(); client.query_points.return_value = resp

        with patch("carta.embed.pipeline.QdrantClient", return_value=client), \
             patch("carta.embed.pipeline.get_embedding", return_value=[0.0] * 768), \
             patch("carta.embed.pipeline.collection_is_hybrid", return_value=False), \
             patch("carta.embed.pipeline._ensure_file_path_index"), \
             patch("carta.search.scoped.get_search_collections", return_value=["test-project_doc"]), \
             patch("carta.embed.pipeline.find_config", return_value="/fake/.carta/config.yaml"):
            results = run_focus("docs/imu.pdf (page 10)", self.BASE_CFG, query="sensitivity")

        # All three chunks of the one file survive (dedup is off in focus).
        assert len(results) == 3
        assert {r["page"] for r in results} == {10, 11, 12}
        assert all(r["source"] == "docs/imu.pdf" for r in results)
        # Filter reached Qdrant (normalized — no '(page 10)' suffix).
        ff = client.query_points.call_args.kwargs["query_filter"]
        assert ff.must[0].match.value == "docs/imu.pdf"

    def test_empty_query_returns_outline(self):
        from unittest.mock import patch, MagicMock
        from carta.embed.pipeline import run_focus

        def mk(page, heading):
            p = MagicMock(); p.payload = {"page": page, "section_heading": heading}; return p
        client = MagicMock(); client.scroll.return_value = ([mk(1, "Cover"), mk(2, "Setup")], None)

        with patch("carta.embed.pipeline.QdrantClient", return_value=client), \
             patch("carta.embed.pipeline._ensure_file_path_index"), \
             patch("carta.embed.pipeline.get_embedding") as mock_embed, \
             patch("carta.search.scoped.get_search_collections", return_value=["test-project_doc"]), \
             patch("carta.embed.pipeline.find_config", return_value="/fake/.carta/config.yaml"):
            results = run_focus("docs/imu.pdf", self.BASE_CFG)  # no query

        assert [r["type"] for r in results] == ["outline", "outline"]
        assert [r["page"] for r in results] == [1, 2]
        mock_embed.assert_not_called()  # outline does no embedding
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest carta/tests/test_pipeline.py::TestRunFocus -v`
Expected: FAIL — `ImportError: cannot import name 'run_focus'`.

- [ ] **Step 3: Implement `_focus_deep` and `run_focus`**

Add to `carta/embed/pipeline.py`:

```python
def _focus_deep(client, collections: list[str], ff: Filter, query: str,
                cfg: dict, repo_root: Path, source: str, limit: int) -> list[dict]:
    """File-scoped deep retrieval: filtered per-collection queries, RRF fused, NO dedup,
    NO visual cap, NO graph expansion; visual hits get a rendered page image."""
    per_collection: list[list[dict]] = []
    for coll_name in collections:
        coll_results: list[dict] = []
        try:
            if coll_name.endswith("_visual"):
                embed_cfg = cfg.get("embed", {})
                if embed_cfg.get("colpali_enabled", None) is False:
                    continue
                from carta.embed.colpali import is_colpali_available, ColPaliEmbedder
                if not is_colpali_available():
                    continue
                if not _visual_collection_ready(client, coll_name):
                    continue
                embedder = ColPaliEmbedder(
                    model_name=embed_cfg.get("colpali_model", "vidore/colqwen2-v1.0-hf"),
                    device=embed_cfg.get("colpali_device", "cpu"), batch_size=1)
                qv = embedder.embed_query(query)
                qv = qv.tolist() if hasattr(qv, "tolist") else list(qv)
                response = client.query_points(
                    collection_name=coll_name, query=qv, using="colpali",
                    limit=limit, with_payload=True, query_filter=ff)
                for r in response.points:
                    payload = r.payload or {}
                    coll_results.append({
                        "score": r.score,
                        "source": f"{payload.get('file_path', payload.get('slug', ''))} (page {payload.get('page_num', '?')})",
                        "excerpt": f"[Visual result] Page {payload.get('page_num', '?')} - {payload.get('file_path', '')}",
                        "type": "visual", "doc_type": payload.get("doc_type", ""),
                        "page": payload.get("page_num"), "section_heading": ""})
            else:
                ollama_url = cfg["embed"]["ollama_url"]
                model = cfg["embed"]["ollama_model"]
                query_vec = get_embedding(query, ollama_url=ollama_url, model=model,
                                          prefix="search_query: ")
                hybrid_cfg = cfg.get("search", {}).get("hybrid", {})
                is_hybrid = collection_is_hybrid(client, coll_name)
                if hybrid_cfg.get("enabled", False) and is_hybrid:
                    response = _hybrid_query_collection(
                        client, coll_name, query, query_vec, limit,
                        prefetch_limit=hybrid_cfg.get("prefetch_limit", 40),
                        bm25_model=hybrid_cfg.get("bm25_model", "Qdrant/bm25"),
                        query_filter=ff)
                elif is_hybrid:
                    response = client.query_points(
                        collection_name=coll_name, query=query_vec, using=DENSE_VECTOR_NAME,
                        limit=limit, with_payload=True, query_filter=ff)
                else:
                    response = client.query_points(
                        collection_name=coll_name, query=query_vec,
                        limit=limit, with_payload=True, query_filter=ff)
                for r in response.points:
                    payload = r.payload or {}
                    coll_results.append({
                        "score": r.score,
                        "source": payload.get("file_path", payload.get("slug", "")),
                        "excerpt": payload.get("text", ""), "type": "text",
                        "doc_type": payload.get("doc_type", ""),
                        "page": payload.get("page"),
                        "section_heading": payload.get("section_heading", "")})
            per_collection.append(coll_results)
        except Exception as e:
            err_str = str(e).lower()
            if "404" in err_str or "not found" in err_str or "doesn't exist" in err_str:
                continue
            if any(kw in err_str for kw in ("connection refused", "connection error",
                                            "network", "timeout", "unreachable")):
                raise RuntimeError(
                    f"Cannot reach Qdrant — is it running? "
                    f"Start it with: carta doctor --fix\n(Detail: {e})") from e
            continue

    # RRF fuse across lanes; visual_max_ratio=1.0 disables the cap (we WANT the file's pages).
    fused = _rrf_merge_collections(per_collection, limit, visual_max_ratio=1.0)
    fused = _attach_page_images(fused, repo_root / source, repo_root)
    return fused[:limit]


def run_focus(source: str, cfg: dict, *, query: str = "",
              limit: int = _FOCUS_DEFAULT_LIMIT) -> list[dict]:
    """Deep, file-scoped retrieval over a single source file.

    Modes:
      - query == "" : outline — the file's distinct (section_heading, page) rows in page order.
      - query set   : deep — up to `limit` page-anchored passages from the file (dedup off,
                      no graph expansion, visual cap off); visual hits carry image_b64.

    Returns list of dicts: {score, source, page, section_heading, excerpt, type, doc_type, image_b64?}.
    Fail-open: an unknown/never-embedded file yields []. Raises RuntimeError only on Qdrant transport failure.
    """
    from carta.search.scoped import get_search_collections

    source = _normalize_source(source)
    repo_root = Path(find_config()).parent.parent
    try:
        client = QdrantClient(url=cfg["qdrant_url"], timeout=10)
    except Exception as e:
        raise RuntimeError(f"Cannot connect to Qdrant: {e}") from e

    try:
        collections = get_search_collections(cfg, "repo")
    except ValueError:
        collections = [collection_name(cfg, "doc")]
        if cfg.get("embed", {}).get("colpali_enabled", None) is not False:
            collections.append(f"{cfg['project_name']}_visual")

    ff = _file_filter(source)
    for coll in collections:
        _ensure_file_path_index(client, coll)

    if not query:
        return _focus_outline(client, collections, ff, source)
    return _focus_deep(client, collections, ff, query, cfg, repo_root, source, limit)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest carta/tests/test_pipeline.py::TestRunFocus -v`
Expected: PASS (both deep and outline modes).

- [ ] **Step 5: Run the whole pipeline test module**

Run: `python -m pytest carta/tests/test_pipeline.py -q`
Expected: PASS (all Phase 1–2 tests green).

- [ ] **Step 6: Commit**

```bash
git add carta/embed/pipeline.py carta/tests/test_pipeline.py
git commit -m "feat(search): add run_focus engine (deep file-scoped retrieval + outline)"
```

---

## Phase 3 — Surfaces & docs

### Task 7: MCP tool — `carta_focus`

**Files:**
- Modify: `carta/mcp/server.py` (import at line 19; add tool after `carta_search`, ~line 138)
- Test: `carta/tests/test_mcp_server.py`

- [ ] **Step 1: Write the failing test**

Add to `carta/tests/test_mcp_server.py`:

```python
class TestCartaFocus:
    def test_formats_results_and_passes_through_image(self):
        from unittest.mock import patch
        import carta.mcp.server as server

        fake = [
            {"score": 0.876543, "source": "docs/imu.pdf", "page": 47,
             "section_heading": "6.3 Gyro", "excerpt": "x" * 400, "type": "text"},
            {"score": 0.5, "source": "docs/imu.pdf (page 47)", "page": 47,
             "section_heading": "", "excerpt": "[Visual]", "type": "visual",
             "image_b64": "QkFTRTY0"},
        ]
        with patch.object(server, "_load_cfg", return_value={"x": 1}), \
             patch.object(server, "run_focus", return_value=fake) as mock_focus:
            out = server.carta_focus(source="docs/imu.pdf (page 47)", query="sensitivity", top_k=15)

        mock_focus.assert_called_once_with("docs/imu.pdf (page 47)", {"x": 1},
                                           query="sensitivity", limit=15)
        assert out[0]["score"] == 0.8765          # rounded to 4dp
        assert out[0]["page"] == 47
        assert len(out[0]["excerpt"]) == 300       # truncated
        assert out[1]["image_b64"] == "QkFTRTY0"   # passed through on visual hits

    def test_returns_error_dict_on_config_failure(self):
        from unittest.mock import patch
        import carta.mcp.server as server
        from carta.config import ConfigError
        with patch.object(server, "_load_cfg", side_effect=ConfigError("no config")):
            out = server.carta_focus(source="x.pdf")
        assert out["error"] == "service_unavailable"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest carta/tests/test_mcp_server.py::TestCartaFocus -v`
Expected: FAIL — `AttributeError: module 'carta.mcp.server' has no attribute 'carta_focus'`.

- [ ] **Step 3: Add the import and the tool**

In `carta/mcp/server.py`, extend the pipeline import (line 19) to include `run_focus`:

```python
from carta.embed.pipeline import run_search, run_focus, run_embed_file, discover_stale_files, run_embed, FILE_TIMEOUT_S
```

Add this tool immediately after `carta_search` returns (after ~line 138):

```python
@mcp_server.tool()
def carta_focus(source: str, query: str = "", top_k: int = 15) -> list[dict] | dict:
    """Go deep in ONE already-located file: page-anchored passages, an outline, and
    table/figure pages returned as images.

    Use AFTER carta_search has identified the relevant file — pass that result's `source`
    string here. With an EMPTY query, returns the file's section/page outline (a synthetic
    table of contents) so you can choose where to read.

    Args:
        source: Repo-relative file path (the `source` from a carta_search result; a
                trailing " (page N)" from a visual result is fine — it is stripped).
        query: Natural-language query. Empty string => outline mode.
        top_k: Maximum passages to return (default 15).

    Returns:
        List of dicts: {score, source, page, section_heading, excerpt, type, image_b64?}.
        `image_b64` is present on visual (table/figure) hits. On failure:
        {"error": "<type>", "detail": "<message>"}.
    """
    try:
        cfg = _load_cfg()
    except (ConfigError, FileNotFoundError) as e:
        return {"error": "service_unavailable", "detail": str(e)}

    try:
        results = run_focus(source, cfg, query=query, limit=top_k)
    except RuntimeError as e:
        return {"error": "service_unavailable", "detail": str(e)}
    except Exception as e:
        _logger.warning("carta_focus unexpected error: %s", e)
        return {"error": "service_unavailable", "detail": str(e)}

    formatted = []
    for r in results:
        item = {
            "score": round(r.get("score", 0.0), 4),
            "source": r["source"],
            "page": r.get("page"),
            "section_heading": r.get("section_heading", ""),
            "excerpt": (r.get("excerpt") or "")[:300],
            "type": r.get("type", "text"),
        }
        if r.get("image_b64"):
            item["image_b64"] = r["image_b64"]
        formatted.append(item)
    return formatted
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest carta/tests/test_mcp_server.py::TestCartaFocus -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add carta/mcp/server.py carta/tests/test_mcp_server.py
git commit -m "feat(mcp): add carta_focus tool (file-scoped deep retrieval)"
```

---

### Task 8: CLI subcommand — `carta focus`

**Files:**
- Modify: `carta/cli.py` (add `cmd_focus` after `cmd_search` ~line 330; subparser after `search_p` ~line 947; dispatch entry ~line 1054)
- Test: `carta/tests/test_cli.py`

- [ ] **Step 1: Write the failing test**

Add to `carta/tests/test_cli.py`:

```python
class TestCmdFocus:
    def _cfg(self, tmp_path):
        return {"modules": {"doc_search": True}, "project_name": "p",
                "qdrant_url": "http://localhost:6333", "embed": {}}

    def test_outline_mode_prints_sections(self, tmp_path, capsys):
        import argparse
        from unittest.mock import patch
        from carta import cli
        outline = [{"score": 0.0, "source": "imu.pdf", "page": 1,
                    "section_heading": "Cover", "excerpt": "", "type": "outline"}]
        args = argparse.Namespace(source="imu.pdf", query=[], limit=15)
        with patch("carta.cli.find_config", return_value=tmp_path / ".carta" / "config.yaml"), \
             patch("carta.config.load_config", return_value=self._cfg(tmp_path)), \
             patch("carta.embed.pipeline.run_focus", return_value=outline):
            cli.cmd_focus(args)
        out = capsys.readouterr().out
        assert "Outline of imu.pdf" in out and "Cover" in out

    def test_deep_mode_writes_image_and_prints_path(self, tmp_path, capsys):
        import argparse, base64
        from unittest.mock import patch
        from carta import cli
        (tmp_path / ".carta").mkdir()
        hits = [{"score": 0.9, "source": "imu.pdf", "page": 47,
                 "section_heading": "Gyro", "excerpt": "regs",
                 "type": "visual", "image_b64": base64.b64encode(b"PNG").decode()}]
        args = argparse.Namespace(source="imu.pdf", query=["gyro", "regs"], limit=15)
        with patch("carta.cli.find_config", return_value=tmp_path / ".carta" / "config.yaml"), \
             patch("carta.config.load_config", return_value=self._cfg(tmp_path)), \
             patch("carta.embed.pipeline.run_focus", return_value=hits):
            cli.cmd_focus(args)
        out = capsys.readouterr().out
        assert "p.47" in out and "Gyro" in out
        img = tmp_path / ".carta" / "cache" / "focus" / "imu-p47.png"
        assert img.is_file() and img.read_bytes() == b"PNG"
        assert str(img) in out
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest carta/tests/test_cli.py::TestCmdFocus -v`
Expected: FAIL — `AttributeError: module 'carta.cli' has no attribute 'cmd_focus'`.

- [ ] **Step 3: Implement `cmd_focus`, the subparser, and dispatch**

Add `cmd_focus` after `cmd_search` in `carta/cli.py` (`Path` is already imported at the top of the module):

```python
def cmd_focus(args):
    from carta.config import load_config
    cfg_path = find_config()
    cfg = load_config(cfg_path)
    if not cfg["modules"].get("doc_search"):
        print("doc_search module is disabled in config.", file=sys.stderr)
        sys.exit(1)
    from carta.embed.pipeline import run_focus
    repo_root = cfg_path.parent.parent
    query = " ".join(args.query) if args.query else ""
    try:
        results = run_focus(args.source, cfg, query=query, limit=args.limit)
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    if not results:
        print(f"No focus results for {args.source!r}. Is the file embedded? "
              f"Use `carta search` to find the exact source path.")
        return

    if not query:
        print(f"Outline of {args.source} ({len(results)} sections):")
        for r in results:
            page = r.get("page")
            page_s = f"p.{page}" if page is not None else "p.?"
            heading = r.get("section_heading") or "(no heading)"
            print(f"  {page_s:>6}  {heading}")
        return

    cache_dir = repo_root / ".carta" / "cache" / "focus"
    stem = Path(args.source).stem
    for r in results:
        page = r.get("page")
        page_s = f"p.{page}" if page is not None else "p.?"
        heading = r.get("section_heading") or ""
        head_s = f" §{heading}" if heading else ""
        print(f"[{r['score']:.2f}] {r['source']} {page_s}{head_s} — {r['excerpt']}")
        if r.get("image_b64"):
            import base64
            cache_dir.mkdir(parents=True, exist_ok=True)
            img_path = cache_dir / f"{stem}-p{page}.png"
            img_path.write_bytes(base64.b64decode(r["image_b64"]))
            print(f"        ↳ page image: {img_path}")
```

Add the subparser after the `search_p` block (~line 947):

```python
    focus_p = sub.add_parser(
        "focus",
        help="Go deep in one file: page-anchored passages, or an outline (omit the query)")
    focus_p.add_argument("query", nargs="*",
                         help="Query to search within the file; omit for a section/page outline")
    focus_p.add_argument("--source", required=True, metavar="PATH",
                         help="Repo-relative file path (the 'source' from a carta search result)")
    focus_p.add_argument("--limit", type=int, default=15,
                         help="Max passages to return (default 15)")
```

Add to the `dispatch` dict (~line 1044):

```python
        "focus": cmd_focus,
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest carta/tests/test_cli.py::TestCmdFocus -v`
Expected: PASS.

- [ ] **Step 5: Smoke-test argparse wiring**

Run: `python -m carta focus --help`
Expected: prints focus usage with `--source` and `--limit`; exit 0.

- [ ] **Step 6: Commit**

```bash
git add carta/cli.py carta/tests/test_cli.py
git commit -m "feat(cli): add carta focus subcommand (deep file search + outline)"
```

---

### Task 9: Documentation

**Files:**
- Modify: `CLAUDE.md` (Carta surface CLI table + MCP tools line), `README.md`, `AGENTS.md`

- [ ] **Step 1: Update the CLAUDE.md surface table**

In `CLAUDE.md`, add a row to the CLI table (after the `search` row):

```markdown
| `focus` | Deep retrieval scoped to **one file**: page-anchored passages, an outline (omit query), and table/figure pages as images. Two-step partner to `search` (locate → go deep) |
```

Update the MCP tools line to list the new tool:

```markdown
Claude-initiated tools: `carta_search`, `carta_focus`, `carta_embed`, `carta_scan`, `carta_remember`.
```

- [ ] **Step 2: Update README.md and AGENTS.md**

Mirror the same one-line description of `carta focus` / `carta_focus` wherever `carta search` / `carta_search` is documented in `README.md` and `AGENTS.md` (search each file for "carta search" and add the focus partner alongside, noting: file-scoped, outline via empty query, images for table/figure pages).

- [ ] **Step 3: Verify references**

Run: `grep -rn "carta_focus\|carta focus" CLAUDE.md README.md AGENTS.md`
Expected: each file shows the new entries.

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md README.md AGENTS.md
git commit -m "docs: document carta focus (CLI) and carta_focus (MCP)"
```

---

### Task 10: Acceptance — full suite + broad-eval no-regression

**Files:** none (verification only)

- [ ] **Step 1: Run the full test suite**

Run: `python -m pytest carta -q`
Expected: PASS (all green, including the new focus tests). If any pre-existing test asserted an exact result-dict shape, fix it to include `page`/`section_heading` (Task 1) and re-run.

- [ ] **Step 2: Broad-eval no-regression gate**

Confirm the eval set path, then run the default (rerank-off) eval:

Run: `carta eval .carta/eval/et-embed.yaml -k 5`
Expected: recall@5 **≥ 0.984** (the v0.12.4 baseline). Anchor surfacing is pure metadata and `run_focus` is new surface area, so broad recall must be unchanged. If the local eval set lives elsewhere, use that path; if it is unavailable in this environment, record that the gate was skipped and why.

- [ ] **Step 3: Manual end-to-end smoke (if a populated project is available)**

```bash
carta focus --source <a-real-embedded-pdf>            # outline mode
carta focus --source <a-real-embedded-pdf> register sensitivity   # deep mode
```
Expected: outline lists sections with page numbers; deep mode prints page-anchored passages and, for any visual hit, writes a PNG under `.carta/cache/focus/` and prints its path.

- [ ] **Step 4: Final commit (if Step 1 required test fixes)**

```bash
git add -A
git commit -m "test: align result-shape assertions with page/section anchors"
```

---

## Self-Review notes (for the executor)

- **Spec coverage:** Piece 1 → Task 1. Piece 2 (filter, dedup-off, visual-cap-off, outline, deep) → Tasks 2–6. Piece 3 (render helper) → Task 4. Surfaces → Tasks 7–8. Docs + acceptance → Tasks 9–10. Mode C is intentionally **out of scope** (no `pages` param) per the spec.
- **Type consistency:** every hit dict uses the keys `{score, source, page, section_heading, excerpt, type, doc_type, image_b64?}`. `run_focus(source, cfg, *, query="", limit=...)` is called identically from the MCP tool (`limit=top_k`) and CLI (`limit=args.limit`). Filter param names are deliberately different per qdrant-client API: `query_filter` (query_points), `scroll_filter` (scroll), `filter` (Prefetch).
- **No silent caps:** outline scroll uses `limit=10_000`; if a single file ever exceeds that, raise it — do not let it silently truncate the TOC.
