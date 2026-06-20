# OCR trust handling — doubted provenance for diagram OCR — design

- **Date:** 2026-06-20
- **Status:** approved (pending spec review)
- **Issue:** follow-up from the petsense N32WB031 case study, where `llava` diagram OCR invented component facts (`K3=RESET`, `F1`) on an annotated board photo. Related: #76 (recall tests for the trusted table tier).
- **Scope owner:** Ian

## Context

Carta's visual pass (`carta embed --visual`) extracts text from page images two ways
(`carta/vision/router.py`):

- **`GLM_OCR_PROMPT`** (router.py:107–118) — for structured/table pages. Already a faithful
  *transcriber*: *"Output only the extracted content."* Reliable.
- **`LLAVA_PROMPT`** (router.py:120–124) — for image/diagram pages. **Interpretive**:
  *"Describe this technical diagram… include register names, waveform descriptions…"* This
  "describe + infer" framing is what fabricated `K3=RESET` / `F1` from a board photo — the
  visible label `32M Hz` was read correctly, but functions and designators **not present as
  text** were invented.

Both feed chunks into the `{project}_doc` collection (same as real PDF text-layer text),
with `doc_type == "image_description"`, `model_used` (`glm-ocr` | `llava`), and
`content_type`. So OCR text is **already distinguishable** in the payload — but search
never surfaces that, and an agent retrieving an OCR chunk gets plain text it may trust
verbatim. Meanwhile `carta focus` already returns the page **image** for ColPali `visual`
hits, but **not** for OCR *text* hits — so the one thing that would let an agent verify a
doubtful description (the actual page) isn't attached.

The principle (agreed in brainstorming): **OCR is a findability tool, not a source of
truth.** Keep it findable; mark the doubtful tier; route the agent to the image to verify.

## Goal

Make diagram-OCR text **visibly doubted** and **image-backed**, without hurting
findability or ranking:

1. A `text_source` trust tier on every search/focus result, derived at **read-time** from
   existing payload — so it applies **retroactively** to OCR chunks already embedded (incl.
   the petsense ones), no re-embed.
2. A **caveat** marker on the doubted (`ocr_visual`) tier in broad search (CLI + MCP).
3. The **page image** attached to `ocr_visual` hits in `carta focus`.
4. A **conservative `LLAVA_PROMPT`** so future diagram OCR transcribes rather than infers.

### Non-goals
- **No ranking/retrieval change.** `text_source` is additive metadata; the broad-eval
  recall@5 (≥ 0.984) must hold by construction. (We chose "caveat + image, same ranking"
  over de-prioritise/quarantine.)
- **No re-embed required** for the consumption side (tiers/caveat/image work off existing
  payload). The prompt fix improves *future* embeds only.
- **No new collection, no embed-schema change.** OCR chunks stay where they are.
- **Not touching the table tier's trust.** `glm-ocr` table OCR is treated as trusted (no
  caveat); validating that on weird table layouts is #76, out of scope here.

## The trust gradient

| Tier | Derived from | Treatment |
|------|--------------|-----------|
| `text_layer` | `doc_type != "image_description"` | trusted — no marker |
| `ocr_table` | `image_description` + (`model_used` ~ `glm-ocr` **or** `content_type == structured_text`) | reliable transcription — no caveat |
| `ocr_visual` | `image_description`, otherwise (the `llava` describe tier) | **doubted** — caveat + image |

Default-to-doubted is the safe fallback: an `image_description` chunk lacking
`model_used`/`content_type` (older chunk) classifies as `ocr_visual`.

## Components

### 1. `_text_source(payload: dict) -> str` (new, pure — `carta/embed/pipeline.py`)
```python
def _text_source(payload: dict) -> str:
    """Classify a hit's provenance from existing payload fields:
    "text_layer" (trusted PDF text), "ocr_table" (glm-ocr transcription, reliable),
    or "ocr_visual" (llava diagram description, doubted). Safe default: ocr_visual."""
    if payload.get("doc_type") != "image_description":
        return "text_layer"
    model = (payload.get("model_used") or "").lower()
    content = (payload.get("content_type") or "").lower()
    if "glm" in model or content == "structured_text":
        return "ocr_table"
    return "ocr_visual"
```
No I/O; trivially unit-testable. Single responsibility.

### 2. Conservative `LLAVA_PROMPT` (`carta/vision/router.py:120–124`)
Replace the "describe + infer" prompt with a transcription-first one — keep visible labels
and values (the findable content `32M Hz`, pin names — they *are* visible text), forbid
inference:
```python
LLAVA_PROMPT = (
    "Transcribe the text visible in this technical image for documentation search. "
    "List every visible text label, annotation, axis label, value with its unit, pin name, "
    "block label, and reference designator exactly as printed. "
    "Do NOT infer or guess component functions, designators, values, or connections that "
    "are not directly legible as text in the image. If something is unreadable, omit it. "
    "Output only the transcribed items, one per line."
)
```
`GLM_OCR_PROMPT` unchanged. (Improves *future* embeds; existing chunks keep their text.)

### 3. Surface `text_source` + correct page on result dicts
In the `run_search` and `_focus_deep` text result builders, add `text_source` and fix the
page read for OCR chunks (they carry `page_num`, not `page`):
```python
    "text_source": _text_source(payload),
    "page": payload.get("page") or payload.get("page_num"),
```
Both keys are additive; ranking/selection untouched. (ColPali `visual` hits already *are*
the page image and are never doubted; the visual builders set `text_source = "visual"` for
shape consistency, so every result dict carries the key.)

### 4. Caveat in broad search (CLI + MCP)
- **CLI `cmd_search`** (`carta/cli.py`): for `text_source == "ocr_visual"` hits, append a
  marker, e.g. `  ⚠ OCR-diagram, unverified — \`carta focus\` for the page image`.
- **MCP `carta_search`** (`_format_search_result`, `carta/mcp/server.py`): include
  `text_source` on every result, and a `caveat` field on `ocr_visual` hits:
  `"caveat": "OCR diagram description — unverified; call carta_focus for the page image."`

### 5. Image pairing for `ocr_visual` hits in focus (`carta/embed/pipeline.py`)
`carta focus`'s `_attach_page_images` today renders only `type == "visual"` hits. Extend the
condition to also render hits where `_text_source(payload) == "ocr_visual"` (an OCR *text*
hit), using the hit's `page` (now resolved from `page_num`). So a doubted diagram-OCR text
hit comes back with its rendered page, and the MCP/CLI surfaces return the image (base64 /
cache path) exactly as they do for ColPali hits.

## Data flow

`carta search "32MHz crystal"` → an `ocr_visual` hit returns with `text_source` +
caveat (and a hint to focus) → agent calls `carta focus --source <guide> "…"` → the same
`ocr_visual` text hit now carries the **page image** → agent reads the page, ignores the
doubtful OCR prose, and verifies against what's actually printed.

## Error handling (fail-open)
- `_text_source` is total; an unknown/empty payload → `ocr_visual` (safe: doubted).
- Image render for an `ocr_visual` hit reuses `render_page_png` (already fail-open → `None`
  on any failure); a hit that can't render just returns without an image, like today.
- No new failure modes on the broad-search path.

## Config & defaults
No new config. The caveat/tiering is always-on additive metadata; the conservative prompt
is a constant. (A future kill-switch can be added if a need appears — YAGNI.)

## Testing

1. **`_text_source` (unit):** `text_layer` for non-`image_description`; `ocr_table` for
   `glm-ocr`/`structured_text`; `ocr_visual` otherwise; missing model/content → `ocr_visual`.
2. **Result surfacing (unit, mocked Qdrant):** `run_search` hits carry `text_source`; an OCR
   chunk (payload `doc_type=image_description`, `page_num=3`, no `page`) surfaces `page == 3`
   and `text_source == "ocr_visual"`; a `glm-ocr` chunk surfaces `ocr_table`.
3. **Caveat (unit):** `_format_search_result` adds `caveat` + `text_source` on `ocr_visual`,
   omits the caveat on `ocr_table`/`text_layer`; `cmd_search` prints the marker only for
   `ocr_visual`.
4. **Focus image pairing (unit):** `_attach_page_images` renders an `ocr_visual` text hit
   (patched `render_page_png`) and leaves `ocr_table`/`text_layer` text hits imageless.
5. **Prompt (manual/eval):** re-OCR the page-3 board image with the new `LLAVA_PROMPT` and
   confirm it no longer asserts un-printed designators (`K3=RESET`, `F1`) while still
   capturing `32M Hz` / `32.768K` / pin names.
6. **No broad-eval regression:** `carta eval … -k 5` recall@5 unchanged (≥ 0.984) — tiering
   is additive, ranking untouched.

## Phasing
1. `_text_source` helper + surface `text_source`/page on result dicts (+ unit tests). The
   retroactive foundation; standalone-useful.
2. Caveat in broad search (CLI + MCP) + focus image-pairing for `ocr_visual` (+ tests).
3. Conservative `LLAVA_PROMPT` (+ manual re-OCR check). Independent; improves future embeds.

## Risk & rollback
- **Risk:** the conservative prompt extracts *less* and hurts findability. *Mitigation:* it
  still transcribes all visible labels/values (the findable content); only inference is
  removed. Validated by the page-3 re-OCR check. Rollback = revert one constant.
- **Risk:** `page_num`-fallback changes a page value somewhere unexpected. *Mitigation:*
  fallback only fires when `page` is absent (OCR chunks); text-layer hits unaffected.
- **Rollback:** tiering/caveat/image are additive; removing them restores prior output with
  no data change.

## Out of scope / follow-ups
- **#76** — recall/accuracy tests for the trusted `ocr_table` tier on weird table layouts;
  re-tier any layout class that mis-extracts.
- **Re-drain petsense visual with the new prompt** — the OCR chunks already embedded carry
  the old interpretive text; a `carta embed --visual` re-drain (after the prompt ships)
  replaces them. The consumption-side caveat/image already covers them in the meantime.
- **Explicit embed-time `text_source` field** — could stamp it at embed time so the
  read-time `doc_type` heuristic can eventually retire (not needed now; read-time derivation
  is sufficient and retroactive).
