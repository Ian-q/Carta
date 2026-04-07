# Embed Vision Progress Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Show real-time per-page vision progress in the embed spinner and a per-strategy page-range summary after each PDF file completes.

**Architecture:** Thread a 5-arg progress callback from `pipeline._embed_one_file()` through `extract_image_descriptions_intelligent()` to `SmartRouter.extract_pdf()`. Callback fires after each page is routed (not before), reporting page class, model, and char count. Events accumulate in a closure list, passed via a temp `_vision_events` key in `sidecar_updates` to `run_embed()`, which calls a new `Progress.vision_done()` method to render the summary block.

**Tech Stack:** Python 3.10+, unittest.mock, pytest, carta.vision.router, carta.embed.pipeline, carta.ui.progress

---

## Files Modified

- `carta/vision/router.py` — Move callback call post-routing; expand to 5-arg signature
- `carta/ui/progress.py` — Add `_format_page_ranges()` helper + `Progress.vision_done()` method
- `carta/embed/pipeline.py` — Add `_vision_callback` closure in `_embed_one_file()`; pop `_vision_events` and call `vision_done()` in `run_embed()`
- `carta/vision/tests/test_router.py` — New tests for 5-arg callback behaviour
- `carta/tests/test_progress.py` — New tests for `vision_done()` and `_format_page_ranges()`
- `carta/tests/test_pipeline.py` — New tests for pipeline wiring

---

## Task 1: Router callback — 5-arg, post-routing

**Files:**
- Modify: `carta/vision/router.py:87-98`
- Test: `carta/vision/tests/test_router.py`

- [ ] **Step 1: Write the failing tests**

Add to `carta/vision/tests/test_router.py` (after the existing `TestRouteFlattened` class):

```python
class TestExtractPdfProgressCallback:
    """Verify extract_pdf fires callback AFTER routing with 5-arg signature."""

    def _make_router(self):
        cfg = {"embed": {"ollama_url": "http://localhost:11434"}}
        return SmartRouter(cfg)

    def test_callback_not_fired_before_routing(self):
        """Callback must fire after _route(), so page_class is known."""
        router = self._make_router()
        call_order = []

        def cb(page_num, total_pages, page_class, model_used, char_count):
            call_order.append(("cb", page_num))

        page = MagicMock()
        profile = _profile(PageClass.PURE_TEXT)
        with patch.object(router, "analyzer") as mock_analyzer, \
             patch.object(router, "_route") as mock_route:
            mock_analyzer.analyze.side_effect = lambda p: (call_order.append(("analyze",)) or profile)
            mock_route.side_effect = lambda *a, **kw: (call_order.append(("route",)) or [])
            with patch("fitz.open") as mock_open:
                doc = MagicMock()
                doc.__iter__ = MagicMock(return_value=iter([page]))
                doc.__len__ = MagicMock(return_value=1)
                mock_open.return_value = doc
                router.extract_pdf(MagicMock(), progress_callback=cb)

        # callback must come after route
        route_idx = call_order.index(("route",))
        cb_idx = call_order.index(("cb", 1))
        assert cb_idx > route_idx

    def test_callback_pure_text_args(self):
        """PURE_TEXT page: model_used='skip', char_count=0."""
        router = self._make_router()
        received = []

        def cb(page_num, total_pages, page_class, model_used, char_count):
            received.append((page_num, total_pages, page_class, model_used, char_count))

        page = MagicMock()
        profile = _profile(PageClass.PURE_TEXT)
        with patch.object(router, "analyzer") as mock_analyzer, \
             patch.object(router, "_route", return_value=[]):
            mock_analyzer.analyze.return_value = profile
            with patch("fitz.open") as mock_open:
                doc = MagicMock()
                doc.__iter__ = MagicMock(return_value=iter([page]))
                doc.__len__ = MagicMock(return_value=3)
                mock_open.return_value = doc
                router.extract_pdf(MagicMock(), progress_callback=cb)

        assert len(received) == 1
        page_num, total_pages, page_class, model_used, char_count = received[0]
        assert page_num == 1
        assert total_pages == 3
        assert page_class == "pure_text"
        assert model_used == "skip"
        assert char_count == 0

    def test_callback_structured_text_args(self):
        """STRUCTURED_TEXT page: model_used='glm-ocr', char_count=len of extracted text."""
        router = self._make_router()
        received = []

        def cb(page_num, total_pages, page_class, model_used, char_count):
            received.append((page_num, total_pages, page_class, model_used, char_count))

        chunk = {
            "doc_type": "image_description",
            "page_num": 1,
            "image_index": 0,
            "text": "extracted text here",
            "model_used": "glm-ocr",
            "page_class": "structured_text",
            "content_type": "structured_text",
        }
        page = MagicMock()
        profile = _profile(PageClass.STRUCTURED_TEXT)
        with patch.object(router, "analyzer") as mock_analyzer, \
             patch.object(router, "_route", return_value=[chunk]):
            mock_analyzer.analyze.return_value = profile
            with patch("fitz.open") as mock_open:
                doc = MagicMock()
                doc.__iter__ = MagicMock(return_value=iter([page]))
                doc.__len__ = MagicMock(return_value=1)
                mock_open.return_value = doc
                router.extract_pdf(MagicMock(), progress_callback=cb)

        assert len(received) == 1
        _, _, page_class, model_used, char_count = received[0]
        assert page_class == "structured_text"
        assert model_used == "glm-ocr"
        assert char_count == len("extracted text here")

    def test_callback_exception_does_not_abort_extraction(self):
        """Exception inside callback must not propagate or stop processing."""
        router = self._make_router()

        def bad_cb(*args):
            raise ValueError("oops")

        page1, page2 = MagicMock(), MagicMock()
        profile = _profile(PageClass.PURE_TEXT)
        with patch.object(router, "analyzer") as mock_analyzer, \
             patch.object(router, "_route", return_value=[]):
            mock_analyzer.analyze.return_value = profile
            with patch("fitz.open") as mock_open:
                doc = MagicMock()
                doc.__iter__ = MagicMock(return_value=iter([page1, page2]))
                doc.__len__ = MagicMock(return_value=2)
                mock_open.return_value = doc
                # Must not raise
                result = router.extract_pdf(MagicMock(), progress_callback=bad_cb)
        assert result == []
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /Users/ian/dev/doc-audit-cc
python -m pytest carta/vision/tests/test_router.py::TestExtractPdfProgressCallback -v
```

Expected: 4 failures mentioning callback signature or ordering.

- [ ] **Step 3: Update `SmartRouter.extract_pdf()` in `carta/vision/router.py`**

Replace lines 87-98 (the `for page_num, page in enumerate` loop) with:

```python
        results = []
        total_pages = len(doc)
        for page_num, page in enumerate(doc, start=1):
            profile = self.analyzer.analyze(page)
            chunks = self._route(page, page_num, profile, doc)
            results.extend(chunks)
            if progress_callback:
                try:
                    char_count = sum(len(c.get("text", "")) for c in chunks)
                    model_used = chunks[0]["model_used"] if chunks else "skip"
                    page_class = profile.page_class.name.lower()
                    progress_callback(page_num, total_pages, page_class, model_used, char_count)
                except Exception:
                    pass
        doc.close()
        return results
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
python -m pytest carta/vision/tests/test_router.py -v
```

Expected: all pass including the 4 new tests.

- [ ] **Step 5: Commit**

```bash
git add carta/vision/router.py carta/vision/tests/test_router.py
git commit -m "feat: expand vision callback to 5-arg post-routing signature"
```

---

## Task 2: `Progress.vision_done()` + `_format_page_ranges()` helper

**Files:**
- Modify: `carta/ui/progress.py`
- Test: `carta/tests/test_progress.py`

- [ ] **Step 1: Write the failing tests**

Add to `carta/tests/test_progress.py` (after `TestTTYMode`):

```python
from carta.ui.progress import _format_page_ranges


class TestFormatPageRanges:
    def test_empty_list_returns_empty_string(self):
        assert _format_page_ranges([]) == ""

    def test_single_page(self):
        assert _format_page_ranges([5]) == "5"

    def test_two_consecutive(self):
        assert _format_page_ranges([3, 4]) == "3-4"

    def test_non_consecutive(self):
        assert _format_page_ranges([1, 3, 5]) == "1, 3, 5"

    def test_mixed_ranges(self):
        # pages 1-3, gap, 5-6, gap, 8
        assert _format_page_ranges([1, 2, 3, 5, 6, 8]) == "1-3, 5-6, 8"

    def test_unsorted_input_is_sorted(self):
        assert _format_page_ranges([10, 1, 2]) == "1-2, 10"


class TestVisionDonePlainMode:
    def _make_plain(self):
        p = Progress(total=3)
        p._tty = False
        return p

    def test_empty_events_prints_nothing(self, capsys):
        p = self._make_plain()
        p.vision_done([])
        captured = capsys.readouterr()
        assert captured.out == ""

    def test_pure_text_pages_label(self, capsys):
        p = self._make_plain()
        events = [
            {"page": 1, "page_class": "pure_text", "model_used": "skip", "char_count": 0},
            {"page": 2, "page_class": "pure_text", "model_used": "skip", "char_count": 0},
        ]
        p.vision_done(events)
        captured = capsys.readouterr()
        assert "pure-text" in captured.out
        assert "2 pages" in captured.out
        assert "1-2" in captured.out

    def test_glm_ocr_label_and_suffix(self, capsys):
        p = self._make_plain()
        events = [
            {"page": 5, "page_class": "structured_text", "model_used": "glm-ocr", "char_count": 400},
        ]
        p.vision_done(events)
        captured = capsys.readouterr()
        assert "structured" in captured.out
        assert "1 page" in captured.out
        assert "5" in captured.out
        assert "GLM-OCR" in captured.out

    def test_llava_label_and_suffix(self, capsys):
        p = self._make_plain()
        events = [
            {"page": 3, "page_class": "text_with_images", "model_used": "llava", "char_count": 250},
        ]
        p.vision_done(events)
        captured = capsys.readouterr()
        assert "image" in captured.out
        assert "LLaVA" in captured.out

    def test_mixed_strategies_all_present(self, capsys):
        p = self._make_plain()
        events = [
            {"page": 1, "page_class": "pure_text", "model_used": "skip", "char_count": 0},
            {"page": 2, "page_class": "structured_text", "model_used": "glm-ocr", "char_count": 300},
            {"page": 3, "page_class": "text_with_images", "model_used": "llava", "char_count": 200},
        ]
        p.vision_done(events)
        captured = capsys.readouterr()
        assert "pure-text" in captured.out
        assert "structured" in captured.out
        assert "image" in captured.out

    def test_display_order_skip_before_glm_before_llava(self, capsys):
        p = self._make_plain()
        events = [
            {"page": 3, "page_class": "text_with_images", "model_used": "llava", "char_count": 200},
            {"page": 1, "page_class": "pure_text", "model_used": "skip", "char_count": 0},
            {"page": 2, "page_class": "structured_text", "model_used": "glm-ocr", "char_count": 300},
        ]
        p.vision_done(events)
        out = capsys.readouterr().out
        skip_pos = out.index("pure-text")
        glm_pos = out.index("structured")
        llava_pos = out.index("image")
        assert skip_pos < glm_pos < llava_pos
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python -m pytest carta/tests/test_progress.py::TestFormatPageRanges carta/tests/test_progress.py::TestVisionDonePlainMode -v
```

Expected: failures on `ImportError: cannot import name '_format_page_ranges'` and `AttributeError: 'Progress' object has no attribute 'vision_done'`.

- [ ] **Step 3: Add `_format_page_ranges` and `Progress.vision_done()` to `carta/ui/progress.py`**

Add this module-level helper immediately after the `_TICK_INTERVAL` constant (before the `Progress` class):

```python
def _format_page_ranges(pages: list[int]) -> str:
    """Format a sorted list of page numbers as compact ranges.

    Examples:
        [1, 2, 3, 5, 6, 8] → "1-3, 5-6, 8"
        [5]                 → "5"
        []                  → ""
    """
    if not pages:
        return ""
    pages = sorted(pages)
    ranges = []
    start = end = pages[0]
    for p in pages[1:]:
        if p == end + 1:
            end = p
        else:
            ranges.append(f"{start}-{end}" if end > start else str(start))
            start = end = p
    ranges.append(f"{start}-{end}" if end > start else str(start))
    return ", ".join(ranges)
```

Add this method to the `Progress` class, after the `summary()` method and before `scan_step()`:

```python
    def vision_done(self, events: list[dict]) -> None:
        """Print per-strategy page summary after a PDF file completes.

        Args:
            events: list of dicts with keys: page (int), page_class (str),
                    model_used (str), char_count (int).
        """
        if not events:
            return

        from collections import defaultdict
        groups: dict[str, list[int]] = defaultdict(list)
        for e in events:
            groups[e["model_used"]].append(e["page"])

        _LABELS = {
            "skip":    ("pure-text",   ""),
            "glm-ocr": ("structured",  "  (GLM-OCR)"),
            "llava":   ("image",       "  (LLaVA)"),
        }

        lines = []
        for model_used in ["skip", "glm-ocr", "llava"]:
            if model_used not in groups:
                continue
            pages = groups[model_used]
            label, suffix = _LABELS[model_used]
            count = len(pages)
            noun = "page" if count == 1 else "pages"
            ranges = _format_page_ranges(pages)
            lines.append(f"  {label}: {count} {noun} — {ranges}{suffix}")

        for model_used, pages in groups.items():
            if model_used in _LABELS:
                continue
            count = len(pages)
            noun = "page" if count == 1 else "pages"
            ranges = _format_page_ranges(pages)
            lines.append(f"  {model_used}: {count} {noun} — {ranges}")

        output = "\n".join(lines) + "\n"
        sys.stdout.write(self._c(_DIM, output))
        sys.stdout.flush()
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
python -m pytest carta/tests/test_progress.py -v
```

Expected: all pass including the 12 new tests.

- [ ] **Step 5: Commit**

```bash
git add carta/ui/progress.py carta/tests/test_progress.py
git commit -m "feat: add Progress.vision_done() with page-range summary"
```

---

## Task 3: Wire callback and events through pipeline

**Files:**
- Modify: `carta/embed/pipeline.py:105-273` (`_embed_one_file`) and `:639-704` (`run_embed`)
- Test: `carta/tests/test_pipeline.py`

- [ ] **Step 1: Write the failing tests**

Add to `carta/tests/test_pipeline.py` (at the end of the file):

```python
class TestVisionProgressWiring:
    """Verify _vision_callback is passed and _vision_events are handled correctly."""

    def _make_pdf(self, tmp_path: Path) -> Path:
        """Create a minimal PDF-like file (just needs .pdf extension for pipeline routing)."""
        p = tmp_path / "docs" / "test.pdf"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"%PDF-1.4 fake")
        return p

    def test_vision_events_not_written_to_sidecar(self, tmp_path):
        """_vision_events must be popped from sidecar_updates before _update_sidecar."""
        from carta.embed.pipeline import run_embed
        from unittest.mock import patch, Mock, call

        repo_root = tmp_path
        carta_dir = repo_root / ".carta"
        carta_dir.mkdir()
        cfg = {
            "project_name": "test-proj",
            "docs_root": "docs",
            "qdrant_url": "http://localhost:6333",
            "embed": {
                "ollama_url": "http://localhost:11434",
                "ollama_model": "nomic-embed-text",
                "chunking": {"max_tokens": 400, "overlap_fraction": 0.15},
                "stale_alert_threshold": 0.30,
                "max_generations": 2,
            },
        }

        pdf = self._make_pdf(tmp_path)
        sidecar_path = pdf.parent / (pdf.stem + ".embed-meta.yaml")

        import yaml
        sidecar_data = {
            "slug": "test",
            "doc_type": "guide",
            "status": "pending",
            "file_type": "pdf",
            "current_path": "docs/test.pdf",
        }
        with open(sidecar_path, "w") as f:
            yaml.dump(sidecar_data, f)

        written_data = {}

        def fake_update_sidecar(path, updates):
            written_data.update(updates)

        mock_client = Mock()
        mock_client.get_collections.return_value = Mock()

        with patch("carta.embed.pipeline.QdrantClient", return_value=mock_client), \
             patch("carta.embed.pipeline.ensure_collection"), \
             patch("carta.embed.pipeline._heal_sidecar_current_paths"), \
             patch("carta.embed.pipeline._update_sidecar", side_effect=fake_update_sidecar), \
             patch("carta.embed.pipeline._embed_one_file", return_value=(
                 5,
                 {"status": "embedded", "chunk_count": 5, "_vision_events": [
                     {"page": 1, "page_class": "pure_text", "model_used": "skip", "char_count": 0}
                 ]},
             )):
            run_embed(repo_root, cfg, progress=None)

        assert "_vision_events" not in written_data

    def test_vision_done_called_with_events(self, tmp_path):
        """progress.vision_done() is called when _vision_events is non-empty."""
        from carta.embed.pipeline import run_embed
        from unittest.mock import patch, Mock, MagicMock

        repo_root = tmp_path
        carta_dir = repo_root / ".carta"
        carta_dir.mkdir()
        cfg = {
            "project_name": "test-proj",
            "docs_root": "docs",
            "qdrant_url": "http://localhost:6333",
            "embed": {
                "ollama_url": "http://localhost:11434",
                "ollama_model": "nomic-embed-text",
                "chunking": {"max_tokens": 400, "overlap_fraction": 0.15},
                "stale_alert_threshold": 0.30,
                "max_generations": 2,
            },
        }

        pdf = self._make_pdf(tmp_path)
        sidecar_path = pdf.parent / (pdf.stem + ".embed-meta.yaml")

        import yaml
        with open(sidecar_path, "w") as f:
            yaml.dump({"slug": "test", "doc_type": "guide", "status": "pending",
                       "file_type": "pdf", "current_path": "docs/test.pdf"}, f)

        vision_events = [
            {"page": 1, "page_class": "pure_text", "model_used": "skip", "char_count": 0},
            {"page": 2, "page_class": "structured_text", "model_used": "glm-ocr", "char_count": 300},
        ]

        mock_progress = MagicMock()
        mock_client = Mock()
        mock_client.get_collections.return_value = Mock()

        with patch("carta.embed.pipeline.QdrantClient", return_value=mock_client), \
             patch("carta.embed.pipeline.ensure_collection"), \
             patch("carta.embed.pipeline._heal_sidecar_current_paths"), \
             patch("carta.embed.pipeline._update_sidecar"), \
             patch("carta.embed.pipeline._embed_one_file", return_value=(
                 5,
                 {"status": "embedded", "chunk_count": 5, "_vision_events": vision_events},
             )):
            run_embed(repo_root, cfg, progress=mock_progress)

        mock_progress.vision_done.assert_called_once_with(vision_events)

    def test_vision_done_not_called_when_events_empty(self, tmp_path):
        """progress.vision_done() is NOT called for non-PDF files (empty _vision_events)."""
        from carta.embed.pipeline import run_embed
        from unittest.mock import patch, Mock, MagicMock

        repo_root = tmp_path
        carta_dir = repo_root / ".carta"
        carta_dir.mkdir()
        cfg = {
            "project_name": "test-proj",
            "docs_root": "docs",
            "qdrant_url": "http://localhost:6333",
            "embed": {
                "ollama_url": "http://localhost:11434",
                "ollama_model": "nomic-embed-text",
                "chunking": {"max_tokens": 400, "overlap_fraction": 0.15},
                "stale_alert_threshold": 0.30,
                "max_generations": 2,
            },
        }

        md = repo_root / "docs" / "test.md"
        md.parent.mkdir(parents=True, exist_ok=True)
        md.write_text("# Hello\n\nworld")
        sidecar_path = md.parent / (md.stem + ".embed-meta.yaml")

        import yaml
        with open(sidecar_path, "w") as f:
            yaml.dump({"slug": "test", "doc_type": "guide", "status": "pending",
                       "file_type": "markdown", "current_path": "docs/test.md"}, f)

        mock_progress = MagicMock()
        mock_client = Mock()
        mock_client.get_collections.return_value = Mock()

        with patch("carta.embed.pipeline.QdrantClient", return_value=mock_client), \
             patch("carta.embed.pipeline.ensure_collection"), \
             patch("carta.embed.pipeline._heal_sidecar_current_paths"), \
             patch("carta.embed.pipeline._update_sidecar"), \
             patch("carta.embed.pipeline._embed_one_file", return_value=(
                 3,
                 {"status": "embedded", "chunk_count": 3},  # no _vision_events key
             )):
            run_embed(repo_root, cfg, progress=mock_progress)

        mock_progress.vision_done.assert_not_called()
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python -m pytest carta/tests/test_pipeline.py::TestVisionProgressWiring -v
```

Expected: 3 failures — `vision_done` not called, or `_vision_events` present in sidecar.

- [ ] **Step 3: Update `_embed_one_file()` in `carta/embed/pipeline.py`**

In `_embed_one_file()`, add the event list and closure immediately before the vision extraction block (before the `if file_path.suffix == ".pdf":` check, around line 162). Insert:

```python
    # Vision progress tracking
    _page_events: list[dict] = []

    def _vision_callback(page_num: int, total_pages: int, page_class: str, model_used: str, char_count: int) -> None:
        _page_events.append({
            "page": page_num,
            "page_class": page_class,
            "model_used": model_used,
            "char_count": char_count,
        })
        if progress:
            if model_used == "skip":
                msg = f"vision: page {page_num}/{total_pages} → pure-text (skip)"
            else:
                msg = f"vision: page {page_num}/{total_pages} → {model_used} → {char_count} chars"
            progress.step(msg)
```

Then find the call to `extract_image_descriptions_intelligent` (around line 193) and update it from:

```python
            img_descs = extract_image_descriptions_intelligent(file_path, cfg)
```

to:

```python
            img_descs = extract_image_descriptions_intelligent(
                file_path, cfg, progress_callback=_vision_callback
            )
```

Then find `sidecar_updates = {` near the end of `_embed_one_file` (around line 252) and add `_vision_events` after the block is built, immediately before the `return` statement:

```python
    sidecar_updates["_vision_events"] = _page_events
    return count + image_chunk_count, sidecar_updates
```

- [ ] **Step 4: Update `run_embed()` in `carta/embed/pipeline.py`**

Find the block inside `run_embed()` that handles `future.result()` (around line 666):

```python
            try:
                count, sidecar_updates = future.result(timeout=FILE_TIMEOUT_S)
                _update_sidecar(sidecar_path, sidecar_updates)
                elapsed = time.monotonic() - t0
                if progress:
                    progress.done(chunks=count, elapsed=elapsed)
```

Replace with:

```python
            try:
                count, sidecar_updates = future.result(timeout=FILE_TIMEOUT_S)
                vision_events = sidecar_updates.pop("_vision_events", [])
                _update_sidecar(sidecar_path, sidecar_updates)
                elapsed = time.monotonic() - t0
                if progress:
                    progress.done(chunks=count, elapsed=elapsed)
                    if vision_events:
                        progress.vision_done(vision_events)
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
python -m pytest carta/tests/test_pipeline.py -v
```

Expected: all pass including the 3 new tests.

- [ ] **Step 6: Run full test suite**

```bash
python -m pytest --tb=short -q
```

Expected: all pass, no regressions.

- [ ] **Step 7: Commit**

```bash
git add carta/embed/pipeline.py carta/tests/test_pipeline.py
git commit -m "feat: wire per-page vision callback and vision_done summary into embed pipeline"
```
