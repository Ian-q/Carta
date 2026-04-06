# Design: Embed Vision Progress — Per-Page Feedback & Post-File Summary

**Date:** 2026-04-06  
**Status:** Approved

## Problem

When `carta embed` processes a large PDF with vision extraction, the progress spinner freezes
on "extracting image descriptions" for the entire duration — which can exceed 300s on dense
datasheets. The user has no visibility into whether the process is still working, which page
it's on, or what strategy is being applied. This makes timeouts hard to diagnose.

## Goal

- Show real-time per-page vision progress in the spinner sub-message during processing
- Replace the spinner line with a verbose per-strategy summary once each file completes
- No new flags or modes — all output is default

---

## Architecture

Four components change. No new files.

### 1. `carta/vision/router.py` — `SmartRouter.extract_pdf()`

**Change:** Move the `progress_callback` call to *after* routing each page (currently fires before).
Update the callback signature from `(page_num, total_pages)` to:

```python
callback(page_num: int, total_pages: int, page_class: str, model_used: str, char_count: int)
```

| Field | Values | Notes |
|---|---|---|
| `page_class` | `"pure_text"`, `"structured_text"`, `"text_with_images"`, `"flattened"` | From `profile.page_class.name.lower()` |
| `model_used` | `"skip"`, `"glm-ocr"`, `"llava"` | `"skip"` for PURE_TEXT pages |
| `char_count` | integer ≥ 0 | Sum of `len(c["text"])` for all chunks produced for that page; 0 for skipped pages |

The callback is already wrapped in `try/except` in the router — no change to error handling.

### 2. `carta/embed/pipeline.py` — `_embed_one_file()`

**Change:** Build a closure `_vision_callback` and a local `_page_events` list. Wire the closure
into `extract_image_descriptions_intelligent()` as `progress_callback`.

```python
_page_events: list[dict] = []

def _vision_callback(page_num, total_pages, page_class, model_used, char_count):
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

img_descs = extract_image_descriptions_intelligent(
    file_path, cfg, progress_callback=_vision_callback
)
```

After extraction, stash events as a temp key (prefixed `_` to signal non-sidecar):

```python
sidecar_updates["_vision_events"] = _page_events
```

### 3. `carta/embed/pipeline.py` — `run_embed()`

**Change:** After `future.result()`, pop `_vision_events` from `sidecar_updates` before writing
the sidecar, then call `progress.vision_done()` if the list is non-empty.

```python
count, sidecar_updates = future.result(timeout=FILE_TIMEOUT_S)
vision_events = sidecar_updates.pop("_vision_events", [])
_update_sidecar(sidecar_path, sidecar_updates)
elapsed = time.monotonic() - t0
if progress:
    progress.done(chunks=count, elapsed=elapsed)
    if vision_events:
        progress.vision_done(vision_events)
```

`run_embed_file()` (single-file path, no progress instance) is unaffected.

### 4. `carta/ui/progress.py` — `Progress.vision_done()`

**New method.** Groups events by strategy, computes consecutive page ranges, prints a
compact block immediately below the done line.

```
  pure-text: 180 pages — 1-100, 102-115, 117-246
  structured: 1 page — 101  (GLM-OCR)
  image: 1 page — 116  (LLaVA)
```

**Page range algorithm:** Iterate events sorted by page number, group consecutive pages with
the same `model_used` value into ranges. Output `N-M` for spans > 1, bare `N` for single pages.

**Strategy display labels:**

| `model_used` | Label |
|---|---|
| `"skip"` | `pure-text` |
| `"glm-ocr"` | `structured` + `(GLM-OCR)` suffix |
| `"llava"` | `image` + `(LLaVA)` suffix |
| other | raw value |

In TTY mode the block prints in dimmed color below the green done line.
In plain mode it prints as regular stdout lines.

---

## Data Flow

```
SmartRouter.extract_pdf()
  └─ per page: fires callback(page_num, total, class, model, chars)
       └─ _vision_callback in _embed_one_file
            ├─ appends event to _page_events
            └─ calls progress.step("vision: page N/T → model → chars")

_embed_one_file returns (count, sidecar_updates)
  └─ sidecar_updates["_vision_events"] = _page_events

run_embed pops _vision_events before sidecar write
  ├─ _update_sidecar(sidecar_path, sidecar_updates)   ← clean, no temp key
  ├─ progress.done(chunks, elapsed)
  └─ progress.vision_done(vision_events)              ← prints summary block
```

---

## Spinner Appearance (TTY)

During processing:
```
⠹  2/9  EN_DS_N32WB03x.pdf  ▸ vision: page 37/246 → GLM-OCR → 412 chars  12s
```

After file completes:
```
✓  2/9  EN_DS_N32WB03x.pdf  80 chunks  47.3s
  pure-text: 180 pages — 1-36, 38-246
  structured: 1 page — 37  (GLM-OCR)
```

---

## Error Handling

- **Partial failure:** if vision extraction throws mid-PDF, `_page_events` is partial. `vision_done()`
  renders whatever was collected. No change to existing error/skip handling.
- **Complete vision failure:** `_page_events` remains empty. `vision_done()` is not called. Existing
  warning messages on stderr are preserved.
- **Timeout:** the 300s `FILE_TIMEOUT_S` guard fires; the thread is abandoned. `_page_events` is
  never returned to `run_embed`. No change to skip behavior. The last spinner step message
  visible before timeout helps identify which page stalled.

---

## Testing

| Test | What to verify |
|---|---|
| `test_vision.py` | Mock router; assert callback fires with 5-arg signature after routing, not before |
| `test_pipeline.py` | Assert `_vision_events` is absent from `sidecar_updates` passed to `_update_sidecar` |
| `test_pipeline.py` | Assert `progress.vision_done()` is called when events are non-empty |
| `test_progress.py` | Unit test `vision_done()` with a known event list; verify range formatting and strategy grouping |

---

## Out of Scope

- Interactive expand/collapse (Ctrl+O style) — deferred
- `--debug` flag or separate verbosity levels
- Changes to `run_embed_file()` (single-file path)
- ColPali visual embedding path — same timeout risk but separate concern
