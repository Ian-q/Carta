---
id: 2026-06-07-two-pass-visual-embedding
title: "Two-Pass Visual Embedding — Implementation Plan"
status: shipped
related:
  - 2026-06-07-two-pass-visual-embedding-design
date: 2026-06-07
---

# Two-Pass Visual Embedding — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `carta embed` extract text fast and *queue* image-heavy PDF pages, then let `carta embed --visual` slowly, resumably drain that queue doing glm-ocr text + ColPali visual embedding per page.

**Architecture:** Pass-1 classifies each PDF page (existing `PageAnalyzer`); image-heavy pages (`TEXT_WITH_IMAGES`/`FLATTENED`) are recorded in the sidecar's `visual_pending` list instead of being embedded inline. Pass-2 (`carta embed --visual`) scans sidecars for `visual_pending`, processes one page at a time with a generous timeout, and checkpoints each page `visual_pending → visual_done` so it's interrupt-safe and resumable.

**Tech Stack:** Python 3.10+, existing Carta pipeline (`carta/embed/pipeline.py`, `carta/embed/induct.py`, `carta/vision/classifier.py`, `carta/embed/colpali.py`), Qdrant, Ollama (glm-ocr), fastembed, optional `[visual]` extra (torch+transformers for ColPali). pytest with Framework Python 3.12: `/Library/Frameworks/Python.framework/Versions/3.12/bin/python3.12 -m pytest`.

Spec: `docs/superpowers/specs/2026-06-07-two-pass-visual-embedding-design.md`.

---

## Current-state anchors (verified)
- Sidecar I/O: `read_sidecar(sidecar_path)->dict|None`, `write_sidecar(file_path, stub, repo_root)`, `generate_sidecar_stub(...)`, `sidecar_path(file_path, repo_root)` in `carta/embed/induct.py`; `_update_sidecar(sidecar_path, updates)` in `carta/embed/pipeline.py:124`.
- Discovery pattern: `discover_pending_files(repo_root)` (`pipeline.py:132`) scans `.carta/sidecars/**/*.embed-meta.yaml`.
- Classifier: `carta/vision/classifier.py` `PageClass` = {PURE_TEXT, STRUCTURED_TEXT, TEXT_WITH_IMAGES, FLATTENED}.
- Per-file embed: `_embed_one_file(...)` (the inner embed used by both `run_embed` and `run_embed_file`); the per-page vision/OCR work happens here.
- ColPali: `_embed_visual_pages_colpali(file_path, file_info, cfg, client, repo_root, verbose)` (`pipeline.py:509`); `ColPaliEmbedder.embed_pdf_pages(pdf_path, page_nums=[1-indexed], dpi)` (`colpali.py:275`); `is_colpali_available()` (`colpali.py`).
- CLI: `cmd_embed(args)` (`cli.py:179`), `embed_p` subparser (`cli.py:554`).
- Config DEFAULTS: `carta/config.py` `DEFAULTS["embed"]`.

---

## File structure

| File | Create/Modify | Responsibility |
|------|---------------|----------------|
| `carta/embed/visual_queue.py` | Create | Pure helpers: mark pages pending, move pending→done, discover queued pages across sidecars, count summary |
| `carta/embed/tests/test_visual_queue.py` | Create | Unit tests for the queue helpers (no I/O beyond tmp sidecars) |
| `carta/config.py` | Modify | Add `embed.two_pass_visual` (default true) + `embed.visual_timeout_s` defaults |
| `carta/embed/pipeline.py` | Modify | Pass-1: mark image-heavy pages instead of inline vision; `run_visual_embed()` drainer; end-of-pass summary |
| `carta/embed/tests/test_pass1_marking.py` | Create | Pass-1 marks image-heavy pages (mocked classifier) |
| `carta/embed/tests/test_visual_drainer.py` | Create | Drainer: per-page checkpoint, resume, preflight gating (mocked models) |
| `carta/cli.py` | Modify | `--visual` flag + dispatch; print end-of-pass summary |
| `README.md` / `AGENTS.md` | Modify | Document the two-pass workflow |

---

## Task 1: Visual-queue helpers (pure functions)

**Files:**
- Create: `carta/embed/visual_queue.py`
- Test: `carta/embed/tests/test_visual_queue.py`

- [ ] **Step 1: Write the failing test** — `carta/embed/tests/test_visual_queue.py`
```python
from carta.embed.visual_queue import (
    add_pending_pages, move_to_done, queue_summary, VISUAL_PENDING_KEY, VISUAL_DONE_KEY,
)


def test_add_pending_pages_dedupes_and_sorts():
    sc = {}
    add_pending_pages(sc, [3, 1, 1])
    assert sc[VISUAL_PENDING_KEY] == [1, 3]
    add_pending_pages(sc, [2, 3])
    assert sc[VISUAL_PENDING_KEY] == [1, 2, 3]


def test_move_to_done_transfers_page():
    sc = {VISUAL_PENDING_KEY: [1, 2, 3], VISUAL_DONE_KEY: []}
    move_to_done(sc, 2)
    assert sc[VISUAL_PENDING_KEY] == [1, 3]
    assert sc[VISUAL_DONE_KEY] == [2]
    # idempotent: moving an already-done page is a no-op
    move_to_done(sc, 2)
    assert sc[VISUAL_PENDING_KEY] == [1, 3]
    assert sc[VISUAL_DONE_KEY] == [2]


def test_queue_summary_counts_files_and_pages():
    sidecars = [
        {VISUAL_PENDING_KEY: [1, 2]},
        {VISUAL_PENDING_KEY: [5]},
        {VISUAL_PENDING_KEY: []},
        {},
    ]
    s = queue_summary(sidecars)
    assert s == {"files": 2, "pages": 3}
```

- [ ] **Step 2: Run → fail**
Run: `/Library/Frameworks/Python.framework/Versions/3.12/bin/python3.12 -m pytest carta/embed/tests/test_visual_queue.py -v`
Expected: FAIL (ModuleNotFoundError: carta.embed.visual_queue).

- [ ] **Step 3: Implement** — `carta/embed/visual_queue.py`
```python
"""Helpers for the deferred visual-embedding queue stored in sidecar state.

A sidecar gains two lists of 1-indexed page numbers:
  visual_pending: pages awaiting glm-ocr text + ColPali visual embedding
  visual_done:    pages already visual-embedded
Per-page transitions make the visual pass resumable/interrupt-safe.
"""
from __future__ import annotations

VISUAL_PENDING_KEY = "visual_pending"
VISUAL_DONE_KEY = "visual_done"


def add_pending_pages(sidecar: dict, pages: list[int]) -> None:
    """Add page numbers to visual_pending (deduped, sorted), excluding already-done."""
    done = set(sidecar.get(VISUAL_DONE_KEY, []) or [])
    cur = set(sidecar.get(VISUAL_PENDING_KEY, []) or [])
    cur.update(p for p in pages if p not in done)
    sidecar[VISUAL_PENDING_KEY] = sorted(cur)


def move_to_done(sidecar: dict, page: int) -> None:
    """Move one page from visual_pending to visual_done (idempotent)."""
    pending = [p for p in sidecar.get(VISUAL_PENDING_KEY, []) or [] if p != page]
    sidecar[VISUAL_PENDING_KEY] = pending
    done = sidecar.get(VISUAL_DONE_KEY, []) or []
    if page not in done:
        done = sorted(done + [page])
    sidecar[VISUAL_DONE_KEY] = done


def queue_summary(sidecars: list[dict]) -> dict:
    """Count files with >=1 pending page and total pending pages."""
    files = 0
    pages = 0
    for sc in sidecars:
        p = sc.get(VISUAL_PENDING_KEY, []) or []
        if p:
            files += 1
            pages += len(p)
    return {"files": files, "pages": pages}
```

- [ ] **Step 4: Run → pass**
Run: `/Library/Frameworks/Python.framework/Versions/3.12/bin/python3.12 -m pytest carta/embed/tests/test_visual_queue.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**
```bash
cd /Users/ian/dev/doc-audit-cc
git add carta/embed/visual_queue.py carta/embed/tests/test_visual_queue.py
git commit -m "feat(embed): visual-queue sidecar helpers (pending/done page transitions)"
```

---

## Task 2: Config defaults

**Files:**
- Modify: `carta/config.py` (`DEFAULTS["embed"]`)
- Test: `carta/tests/test_config.py` (append)

- [ ] **Step 1: Write failing test** — append to `carta/tests/test_config.py`
```python
def test_two_pass_visual_defaults():
    from carta.config import DEFAULTS
    e = DEFAULTS["embed"]
    assert e["two_pass_visual"] is True
    assert e["visual_timeout_s"] == 3600
```

- [ ] **Step 2: Run → fail**
Run: `/Library/Frameworks/Python.framework/Versions/3.12/bin/python3.12 -m pytest carta/tests/test_config.py::test_two_pass_visual_defaults -v`
Expected: FAIL (KeyError: 'two_pass_visual').

- [ ] **Step 3: Implement** — in `carta/config.py`, add to the `embed` block of `DEFAULTS`:
```python
        "two_pass_visual": True,    # pass-1 marks image-heavy pages; pass-2 (--visual) drains them
        "visual_timeout_s": 3600,   # generous per-file timeout for the slow visual pass (0 = unbounded)
```

- [ ] **Step 4: Run → pass**
Run: `/Library/Frameworks/Python.framework/Versions/3.12/bin/python3.12 -m pytest carta/tests/test_config.py::test_two_pass_visual_defaults -v`
Expected: PASS.

- [ ] **Step 5: Commit**
```bash
cd /Users/ian/dev/doc-audit-cc
git add carta/config.py carta/tests/test_config.py
git commit -m "feat(config): two_pass_visual + visual_timeout_s defaults"
```

---

## Task 3: Pass-1 marks image-heavy pages instead of embedding them inline

**Files:**
- Modify: `carta/embed/pipeline.py` (`_embed_one_file` per-page routing)
- Test: `carta/embed/tests/test_pass1_marking.py`

**Context to read first:** open `_embed_one_file` in `carta/embed/pipeline.py` and find the per-page loop where `PageClass` is decided and vision/OCR/ColPali is invoked. The change: when `two_pass_visual` is enabled and a page is `TEXT_WITH_IMAGES` or `FLATTENED`, do NOT run the heavy VLM/ColPali for that page; instead collect its page number and (after the loop) call `add_pending_pages` on the sidecar updates dict. Still extract any quick PyMuPDF text for the page (keep existing text extraction). Return the pending page list as part of the sidecar updates so the caller persists it.

- [ ] **Step 1: Write failing test** — `carta/embed/tests/test_pass1_marking.py`
```python
from unittest.mock import patch
from carta.embed import pipeline
from carta.embed.visual_queue import VISUAL_PENDING_KEY


def test_pass1_marks_image_heavy_pages(tmp_path, monkeypatch):
    """When two_pass_visual is on, image-heavy pages are queued, not vision-embedded."""
    # Build a fake per-page classification: page 1 pure text, page 2 image-heavy.
    from carta.vision.classifier import PageClass
    monkeypatch.setattr(pipeline, "_classify_pages",
                        lambda *a, **k: [PageClass.PURE_TEXT, PageClass.TEXT_WITH_IMAGES],
                        raising=False)
    # Guard: the heavy colpali path must NOT be called during pass-1.
    called = {"colpali": 0}
    monkeypatch.setattr(pipeline, "_embed_visual_pages_colpali",
                        lambda *a, **k: called.__setitem__("colpali", called["colpali"] + 1))
    cfg = {"embed": {"two_pass_visual": True, "chunking": {}}}
    updates = pipeline._mark_or_collect_visual_pages(
        page_classes=[PageClass.PURE_TEXT, PageClass.TEXT_WITH_IMAGES], cfg=cfg)
    assert updates[VISUAL_PENDING_KEY] == [2]
    assert called["colpali"] == 0
```

> NOTE: this test pins a small, testable helper `_mark_or_collect_visual_pages(page_classes, cfg) -> dict`. Factor the "which pages are image-heavy → visual_pending" decision into that helper so it's unit-testable without a real PDF. The integration into `_embed_one_file`'s loop (skipping inline vision for those pages) is verified by the full suite + a manual run; keep the helper as the tested seam.

- [ ] **Step 2: Run → fail**
Run: `/Library/Frameworks/Python.framework/Versions/3.12/bin/python3.12 -m pytest carta/embed/tests/test_pass1_marking.py -v`
Expected: FAIL (AttributeError: `_mark_or_collect_visual_pages`).

- [ ] **Step 3: Implement**
Add the helper near the top of `pipeline.py`:
```python
from carta.embed.visual_queue import add_pending_pages, VISUAL_PENDING_KEY
from carta.vision.classifier import PageClass

_IMAGE_HEAVY = {PageClass.TEXT_WITH_IMAGES, PageClass.FLATTENED}


def _mark_or_collect_visual_pages(page_classes, cfg) -> dict:
    """Return sidecar updates queuing 1-indexed image-heavy pages when two_pass_visual is on."""
    if not cfg.get("embed", {}).get("two_pass_visual", True):
        return {}
    pending = [i + 1 for i, pc in enumerate(page_classes) if pc in _IMAGE_HEAVY]
    updates: dict = {}
    if pending:
        add_pending_pages(updates, pending)  # writes updates[VISUAL_PENDING_KEY]
    return updates
```
Then, in `_embed_one_file`'s per-page handling: when `two_pass_visual` is enabled, SKIP the inline VLM/ColPali embedding for `TEXT_WITH_IMAGES`/`FLATTENED` pages (still do PyMuPDF text extraction), and merge `_mark_or_collect_visual_pages(...)` into the returned `sidecar_updates`. Adapt to the real loop variable names; the invariant: image-heavy pages get queued, not inline-vision-embedded, when `two_pass_visual` is true. (When `two_pass_visual` is false, behavior is unchanged — inline vision as today.)

- [ ] **Step 4: Run → pass + full suite**
Run: `/Library/Frameworks/Python.framework/Versions/3.12/bin/python3.12 -m pytest carta/embed/tests/test_pass1_marking.py -v && /Library/Frameworks/Python.framework/Versions/3.12/bin/python3.12 -m pytest -q`
Expected: new test PASS; full suite green (existing inline-vision tests still pass because `two_pass_visual` only changes behavior when enabled — if any existing test asserts inline vision with default config, update it to set `two_pass_visual: False` for that scenario, noting why).

- [ ] **Step 5: Commit**
```bash
cd /Users/ian/dev/doc-audit-cc
git add carta/embed/pipeline.py carta/embed/tests/test_pass1_marking.py
git commit -m "feat(embed): pass-1 queues image-heavy pages (visual_pending) instead of inline vision"
```

---

## Task 4: End-of-pass summary nudge

**Files:**
- Modify: `carta/embed/pipeline.py` (`run_embed` end) and/or `carta/cli.py` (`cmd_embed`)
- Test: covered via `queue_summary` (Task 1) + a CLI smoke check

- [ ] **Step 1: Write failing test** — append to `carta/embed/tests/test_visual_queue.py`
```python
from carta.embed.visual_queue import format_summary_line


def test_format_summary_line():
    assert format_summary_line({"files": 18, "pages": 42}) == (
        "Visual queue: 42 page(s) across 18 file(s) await visual embedding. "
        "Run `carta embed --visual` to process them."
    )
    assert format_summary_line({"files": 0, "pages": 0}) == ""
```

- [ ] **Step 2: Run → fail**
Run: `/Library/Frameworks/Python.framework/Versions/3.12/bin/python3.12 -m pytest carta/embed/tests/test_visual_queue.py::test_format_summary_line -v`
Expected: FAIL (ImportError: format_summary_line).

- [ ] **Step 3: Implement** — add to `carta/embed/visual_queue.py`:
```python
def format_summary_line(summary: dict) -> str:
    """Human nudge printed at the end of pass-1; empty string when nothing queued."""
    if not summary.get("pages"):
        return ""
    return (
        f"Visual queue: {summary['pages']} page(s) across {summary['files']} file(s) "
        f"await visual embedding. Run `carta embed --visual` to process them."
    )
```
Then in `run_embed` (after the embed loop, before return): build the summary by reading all sidecars (reuse the `discover_*` scan or a small `read_all_sidecars(repo_root)`), call `queue_summary` + `format_summary_line`, and `print(...)` it when non-empty (respect the existing verbose/progress convention).

- [ ] **Step 4: Run → pass**
Run: `/Library/Frameworks/Python.framework/Versions/3.12/bin/python3.12 -m pytest carta/embed/tests/test_visual_queue.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**
```bash
cd /Users/ian/dev/doc-audit-cc
git add carta/embed/visual_queue.py carta/embed/tests/test_visual_queue.py carta/embed/pipeline.py
git commit -m "feat(embed): end-of-pass visual-queue summary nudge"
```

---

## Task 5: `carta embed --visual` drainer

**Files:**
- Modify: `carta/cli.py` (`embed_p` subparser + `cmd_embed`)
- Modify: `carta/embed/pipeline.py` (`run_visual_embed`)
- Test: `carta/embed/tests/test_visual_drainer.py`

- [ ] **Step 1: Write failing test** — `carta/embed/tests/test_visual_drainer.py`
```python
from unittest.mock import MagicMock
from carta.embed import pipeline
from carta.embed.visual_queue import VISUAL_PENDING_KEY, VISUAL_DONE_KEY


def test_drainer_checkpoints_each_page(monkeypatch, tmp_path):
    # One sidecar with two pending pages; fake the per-page work to succeed.
    saved = {"sidecars": [{"current_path": "docs/x.pdf", "slug": "x",
                           VISUAL_PENDING_KEY: [1, 2], VISUAL_DONE_KEY: []}]}
    monkeypatch.setattr(pipeline, "_discover_visual_pending",
                        lambda repo_root: [("sc_path", saved["sidecars"][0])], raising=False)
    written = {}
    monkeypatch.setattr(pipeline, "_update_sidecar", lambda sc_path, updates: written.update(updates))
    monkeypatch.setattr(pipeline, "_visual_embed_one_page",
                        lambda sc, page, cfg, client, repo_root, verbose: True, raising=False)
    monkeypatch.setattr(pipeline, "is_colpali_available", lambda: True, raising=False)
    monkeypatch.setattr(pipeline, "QdrantClient", lambda **k: MagicMock())

    cfg = {"qdrant_url": "http://localhost:6333", "embed": {"visual_timeout_s": 0}}
    summary = pipeline.run_visual_embed(tmp_path, cfg)
    assert summary["pages_embedded"] == 2
    # both pages moved to done
    assert written.get(VISUAL_DONE_KEY) == [1, 2]
    assert written.get(VISUAL_PENDING_KEY) == []


def test_drainer_leaves_failed_page_pending(monkeypatch, tmp_path):
    sc = {"current_path": "docs/x.pdf", "slug": "x", VISUAL_PENDING_KEY: [1], VISUAL_DONE_KEY: []}
    monkeypatch.setattr(pipeline, "_discover_visual_pending", lambda r: [("sc", sc)], raising=False)
    monkeypatch.setattr(pipeline, "_update_sidecar", lambda *a, **k: None)
    def boom(*a, **k):
        raise RuntimeError("model crashed")
    monkeypatch.setattr(pipeline, "_visual_embed_one_page", boom, raising=False)
    monkeypatch.setattr(pipeline, "is_colpali_available", lambda: True, raising=False)
    monkeypatch.setattr(pipeline, "QdrantClient", lambda **k: MagicMock())
    summary = pipeline.run_visual_embed(tmp_path, {"qdrant_url": "x", "embed": {}})
    assert summary["pages_embedded"] == 0
    assert summary["pages_failed"] == 1
    assert sc[VISUAL_PENDING_KEY] == [1]  # left pending for retry


def test_drainer_preflight_when_visual_unavailable(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(pipeline, "is_colpali_available", lambda: False, raising=False)
    summary = pipeline.run_visual_embed(tmp_path, {"qdrant_url": "x", "embed": {}})
    assert summary.get("status") == "visual_unavailable"
    out = capsys.readouterr().out + capsys.readouterr().err
```

- [ ] **Step 2: Run → fail**
Run: `/Library/Frameworks/Python.framework/Versions/3.12/bin/python3.12 -m pytest carta/embed/tests/test_visual_drainer.py -v`
Expected: FAIL (AttributeError: `run_visual_embed`).

- [ ] **Step 3: Implement** in `carta/embed/pipeline.py`:
```python
from carta.embed.colpali import is_colpali_available
from carta.embed.visual_queue import move_to_done, VISUAL_PENDING_KEY, VISUAL_DONE_KEY


def _discover_visual_pending(repo_root):
    """Yield (sidecar_path, sidecar_dict) for sidecars with non-empty visual_pending."""
    out = []
    sidecars_dir = repo_root / ".carta" / "sidecars"
    if not sidecars_dir.is_dir():
        return out
    for sc_path in sidecars_dir.rglob("*.embed-meta.yaml"):
        sc = read_sidecar(sc_path)
        if sc and (sc.get(VISUAL_PENDING_KEY) or []):
            out.append((sc_path, sc))
    return out


def _visual_embed_one_page(sidecar, page, cfg, client, repo_root, verbose) -> bool:
    """OCR text + ColPali for a single 1-indexed page. Returns True on success.

    (a) glm-ocr text for the page -> hybrid text index via the existing text-embed path
    (b) ColPaliEmbedder.embed_pdf_pages(pdf, page_nums=[page]) -> _visual collection
    Adapt to the real OCR + visual-upsert helpers; raise on failure so the caller
    leaves the page pending.
    """
    from pathlib import Path
    pdf_path = (repo_root / sidecar["current_path"]).resolve()
    # (a) OCR text for this page -> text index. Reuse the router/OCR + upsert path used
    #     by _embed_one_file, scoped to [page]. (Integration seam — wire to real helpers.)
    # (b) ColPali for this page -> _visual collection. Reuse _embed_visual_pages_colpali
    #     machinery but scoped to page_nums=[page] via ColPaliEmbedder.embed_pdf_pages.
    _embed_visual_pages_colpali(pdf_path, sidecar, cfg, client, repo_root, verbose)  # adapt to page-scoped
    return True


def run_visual_embed(repo_root, cfg: dict, verbose: bool = False, progress=None) -> dict:
    """Pass-2: drain visual_pending pages (glm-ocr text + ColPali), per-page checkpoint."""
    summary = {"pages_embedded": 0, "pages_failed": 0, "files": 0}
    if not is_colpali_available():
        msg = ("carta embed --visual: the [visual] extra (torch+transformers) is not "
               "installed. Install with: pip install 'carta-cc[visual]'  (note: may "
               "require a Python 3.12 venv if torch wheels are unavailable for the "
               "current interpreter).")
        print(msg, flush=True)
        summary["status"] = "visual_unavailable"
        return summary
    client = QdrantClient(url=cfg["qdrant_url"], timeout=5)
    queued = _discover_visual_pending(repo_root)
    summary["files"] = len(queued)
    for sc_path, sc in queued:
        for page in list(sc.get(VISUAL_PENDING_KEY, []) or []):
            try:
                _visual_embed_one_page(sc, page, cfg, client, repo_root, verbose)
                move_to_done(sc, page)
                _update_sidecar(sc_path, {VISUAL_PENDING_KEY: sc[VISUAL_PENDING_KEY],
                                          VISUAL_DONE_KEY: sc[VISUAL_DONE_KEY]})
                summary["pages_embedded"] += 1
            except Exception as e:  # leave page pending for retry; never drop
                summary["pages_failed"] += 1
                print(f"  visual: page {page} of {sc.get('current_path')} failed: {e} "
                      f"(left pending)", flush=True)
    return summary
```
> INTEGRATION SEAM: `_visual_embed_one_page` must wire (a) the page-scoped glm-ocr text → text-index upsert (reuse the OCR + `upsert_chunks` path `_embed_one_file` uses) and (b) page-scoped ColPali (`ColPaliEmbedder.embed_pdf_pages(pdf, page_nums=[page])` → `_visual` collection upsert). Read `_embed_visual_pages_colpali` and `_embed_one_file` to reuse their helpers scoped to one page. Keep raising on failure.

Then wire the CLI in `carta/cli.py`:
- Add to `embed_p`: `embed_p.add_argument("--visual", action="store_true", help="Run the slow visual pass: drain visual_pending pages (OCR text + ColPali).")`
- In `cmd_embed`, near the top: `if getattr(args, "visual", False): from carta.embed.pipeline import run_visual_embed; from carta.config import load_config, find_config; cfg = load_config(find_config()); s = run_visual_embed(find_config().parent.parent, cfg, verbose=True); print(f"visual: embedded {s['pages_embedded']} page(s), {s['pages_failed']} failed across {s['files']} file(s)"); return` (adapt repo_root derivation to how `cmd_embed` already computes it).

- [ ] **Step 4: Run → pass + full suite**
Run: `/Library/Frameworks/Python.framework/Versions/3.12/bin/python3.12 -m pytest carta/embed/tests/test_visual_drainer.py -v && /Library/Frameworks/Python.framework/Versions/3.12/bin/python3.12 -m pytest -q`
Expected: 3 new tests PASS; full suite green.

- [ ] **Step 5: Commit**
```bash
cd /Users/ian/dev/doc-audit-cc
git add carta/embed/pipeline.py carta/cli.py carta/embed/tests/test_visual_drainer.py
git commit -m "feat(embed): carta embed --visual drainer (per-page OCR+ColPali, resumable)"
```

---

## Task 6: Docs (two-pass workflow)

**Files:**
- Modify: `README.md` (visual-embedding section), `AGENTS.md`

- [ ] **Step 1: Update README** — under the ColPali/visual section, add a "Two-pass visual embedding" subsection documenting: `carta embed` (fast text + marks image-heavy pages, prints the queue summary) → `carta embed --visual` (slow, resumable drain of OCR text + ColPali). Note `embed.two_pass_visual` (default on), `embed.visual_timeout_s`, the `[visual]` extra requirement (and the possible Python 3.12 venv caveat for torch), `colpali_scoped_paths` to limit which dirs get visual treatment, and `OLLAMA_MAX_LOADED_MODELS` guidance to avoid glm-ocr+ColPali co-residency memory crashes.

- [ ] **Step 2: Update AGENTS.md** — one line: image-heavy PDFs are embedded in two passes; run `carta embed` then `carta embed --visual`; scope visual work with `colpali_scoped_paths`.

- [ ] **Step 3: Commit**
```bash
cd /Users/ian/dev/doc-audit-cc
git add README.md AGENTS.md
git commit -m "docs: two-pass visual embedding workflow (carta embed + carta embed --visual)"
```

---

## Self-Review

**Spec coverage:**
- Pass-1 fast text + mark image-heavy → Task 3 ✓ (+ config Task 2).
- Sidecar-as-queue (`visual_pending`/`visual_done`) → Task 1 ✓.
- End-of-pass summary nudge → Task 4 ✓.
- `carta embed --visual` drainer, per-page checkpoint, resumable, leave-pending-on-failure → Task 5 ✓.
- OCR text + ColPali both per page → Task 5 (`_visual_embed_one_page`) ✓.
- `[visual]`/torch preflight + guidance (incl. 3.12-venv caveat) → Task 5 ✓.
- `visual_timeout_s` config → Task 2 ✓.
- Scoping via `colpali_scoped_paths`, `OLLAMA_MAX_LOADED_MODELS` guidance → docs Task 6 ✓ (scoping reuses shipped #17 behavior in `_embed_visual_pages_colpali`).
- Tests (marking, queue, drainer checkpoint/resume, preflight) → Tasks 1/3/5 ✓.

**Placeholder scan:** No TBD/TODO. Three explicitly-labelled INTEGRATION SEAMS (pass-1 loop interception in `_embed_one_file`; page-scoped OCR+ColPali in `_visual_embed_one_page`; `cmd_embed` repo_root derivation) are real wiring points against code the plan can't reproduce line-for-line — each names the exact functions to reuse and the invariant to preserve, not vague "handle it" hand-waving.

**Type consistency:** `VISUAL_PENDING_KEY`/`VISUAL_DONE_KEY` defined once in `visual_queue.py`, imported everywhere. `add_pending_pages`/`move_to_done`/`queue_summary`/`format_summary_line` signatures consistent between tests and impl. `run_visual_embed(repo_root, cfg, verbose, progress)` and helpers (`_discover_visual_pending`, `_visual_embed_one_page`) consistent across Task 5 test + impl + CLI call.

**Known build-time decision:** torch in Carta's interpreter vs a dedicated 3.12 subprocess venv — resolve in Task 5 by checking `pip index versions torch` for the active Python; if unavailable, implement the subprocess-venv path for the visual pass and document it. Flagged, not silently assumed.
