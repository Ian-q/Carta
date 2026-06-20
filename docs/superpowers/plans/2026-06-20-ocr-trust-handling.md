# OCR Trust Handling Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Mark diagram-OCR text as a doubted (`ocr_visual`) trust tier — caveated in broad search, page-image-backed in focus — without re-embedding or changing ranking, plus a conservative diagram-OCR prompt.

**Architecture:** A read-time `_text_source(payload)` classifier derives a trust tier from payload fields that already exist (`doc_type`/`model_used`/`content_type`), so it applies retroactively to embedded OCR chunks. Result builders surface `text_source`; broad search adds a caveat on the `ocr_visual` tier; `carta focus` attaches the page image to `ocr_visual` text hits (reusing `render_page_png`). The `LLAVA_PROMPT` is rewritten to transcribe rather than infer, improving future embeds.

**Tech Stack:** Python 3.10+, qdrant-client (mocked in tests), pytest + unittest.mock. No new dependencies, no new config.

**Spec:** `docs/superpowers/specs/2026-06-20-ocr-trust-handling-design.md`

---

## File Structure

- **Modify** `carta/embed/pipeline.py` — add `_text_source` helper; surface `text_source` + page-fallback on the `run_search` and `_focus_deep` result builders; extend `_attach_page_images` to render `ocr_visual` text hits.
- **Modify** `carta/vision/router.py` — rewrite `LLAVA_PROMPT` (constant only).
- **Modify** `carta/mcp/server.py` — `_format_search_result` surfaces `text_source` + `caveat`; `carta_focus` formatter surfaces `text_source`.
- **Modify** `carta/cli.py` — `cmd_search` prints the caveat marker for `ocr_visual` hits.
- **Modify** `carta/tests/test_pipeline.py`, `carta/tests/test_mcp_server.py`, `carta/tests/test_cli.py` — tests.

> Verified facts: OCR text chunks land in `{project}_doc` (the text path) with `doc_type == "image_description"`, `model_used` (`glm-ocr`|`llava`), `content_type`, and `page_num` (NOT `page`). ColPali hits are `type == "visual"` in `{project}_visual`. The text result builders read `payload.get("page")` (→ `None` for OCR chunks today). `_format_search_result` and `cmd_search` already surface `page`/`section_heading` (from the search-anchors work).

---

## Task 1: `_text_source` trust classifier

**Files:**
- Modify: `carta/embed/pipeline.py` (add near the other search helpers, before `_focus_outline` ~line 1761)
- Test: `carta/tests/test_pipeline.py`

- [ ] **Step 1: Write the failing test**

```python
class TestTextSource:
    def test_text_layer_for_non_image_description(self):
        from carta.embed.pipeline import _text_source
        assert _text_source({"doc_type": "spec"}) == "text_layer"
        assert _text_source({}) == "text_layer"

    def test_ocr_table_for_glm_or_structured(self):
        from carta.embed.pipeline import _text_source
        assert _text_source({"doc_type": "image_description", "model_used": "glm-ocr"}) == "ocr_table"
        assert _text_source({"doc_type": "image_description", "content_type": "structured_text"}) == "ocr_table"

    def test_ocr_visual_for_llava(self):
        from carta.embed.pipeline import _text_source
        assert _text_source({"doc_type": "image_description", "model_used": "llava"}) == "ocr_visual"
        assert _text_source({"doc_type": "image_description", "content_type": "visual"}) == "ocr_visual"

    def test_ocr_visual_is_safe_default_when_unmarked(self):
        from carta.embed.pipeline import _text_source
        assert _text_source({"doc_type": "image_description"}) == "ocr_visual"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest carta/tests/test_pipeline.py::TestTextSource -v`
Expected: FAIL — `ImportError: cannot import name '_text_source'`.

- [ ] **Step 3: Implement the helper**

Add to `carta/embed/pipeline.py` just before `def _focus_outline`:

```python
def _text_source(payload: dict) -> str:
    """Classify a hit's provenance from existing payload fields.

    "text_layer" — real PDF text (trusted); "ocr_table" — glm-ocr transcription of
    structured text (reliable); "ocr_visual" — llava diagram description (doubted).
    Safe default for an unmarked image_description chunk is ocr_visual (doubted).
    """
    if payload.get("doc_type") != "image_description":
        return "text_layer"
    model = (payload.get("model_used") or "").lower()
    content = (payload.get("content_type") or "").lower()
    if "glm" in model or content == "structured_text":
        return "ocr_table"
    return "ocr_visual"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest carta/tests/test_pipeline.py::TestTextSource -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add carta/embed/pipeline.py carta/tests/test_pipeline.py
git commit -m "feat(search): add _text_source trust classifier (text_layer/ocr_table/ocr_visual)"
```

---

## Task 2: Surface `text_source` + page-fallback on `run_search` results

**Files:**
- Modify: `carta/embed/pipeline.py` (visual builder ~2122-2132; text builder ~2173-2183)
- Test: `carta/tests/test_pipeline.py`

- [ ] **Step 1: Write the failing test**

Add to the `TestRunSearch` class in `carta/tests/test_pipeline.py`:

```python
    def test_ocr_chunk_gets_ocr_visual_tier_and_page_from_page_num(self):
        from unittest.mock import patch, MagicMock
        from carta.embed.pipeline import run_search
        cfg = {
            "project_name": "test-project", "qdrant_url": "http://localhost:6333",
            "embed": {"ollama_url": "x", "ollama_model": "m", "colpali_enabled": False},
            "search": {"top_n": 5}, "modules": {"doc_search": True},
        }
        point = MagicMock(); point.score = 0.8
        # OCR chunk: image_description + llava + page_num (no "page" key)
        point.payload = {"file_path": "docs/board.pdf", "text": "32M Hz",
                         "doc_type": "image_description", "model_used": "llava", "page_num": 3}
        resp = MagicMock(); resp.points = [point]
        client = MagicMock(); client.query_points.return_value = resp
        with patch("carta.embed.pipeline.QdrantClient", return_value=client), \
             patch("carta.embed.pipeline.get_embedding", return_value=[0.0] * 768), \
             patch("carta.embed.pipeline.collection_is_hybrid", return_value=False), \
             patch("carta.search.scoped.get_search_collections", return_value=["test-project_doc"]), \
             patch("carta.embed.pipeline.find_config", return_value="/fake/.carta/config.yaml"):
            results = run_search("32mhz", cfg)
        assert results[0]["text_source"] == "ocr_visual"
        assert results[0]["page"] == 3   # resolved from page_num
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest carta/tests/test_pipeline.py::TestRunSearch::test_ocr_chunk_gets_ocr_visual_tier_and_page_from_page_num -v`
Expected: FAIL — `KeyError: 'text_source'`.

- [ ] **Step 3: Update the builders**

In `carta/embed/pipeline.py`, the `run_search` **text** builder (~2175-2183) becomes:

```python
                    coll_results.append({
                        "score": r.score,
                        "source": payload.get("file_path", payload.get("slug", "")),
                        "excerpt": payload.get("text", ""),
                        "type": "text",
                        "doc_type": payload.get("doc_type", ""),
                        "page": payload.get("page") or payload.get("page_num"),
                        "section_heading": payload.get("section_heading", ""),
                        "text_source": _text_source(payload),
                    })
```

The `run_search` **visual** builder (~2124-2132) becomes:

```python
                        coll_results.append({
                            "score": r.score,
                            "source": f"{payload.get('file_path', payload.get('slug', ''))} (page {payload.get('page_num', '?')})",
                            "excerpt": f"[Visual result] Page {payload.get('page_num', '?')} - {payload.get('file_path', '')}",
                            "type": "visual",
                            "doc_type": payload.get("doc_type", ""),
                            "page": payload.get("page_num"),
                            "section_heading": "",
                            "text_source": "visual",
                        })
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest carta/tests/test_pipeline.py::TestRunSearch -v`
Expected: PASS (new test + existing TestRunSearch tests green).

- [ ] **Step 5: Commit**

```bash
git add carta/embed/pipeline.py carta/tests/test_pipeline.py
git commit -m "feat(search): surface text_source + page-num fallback on run_search results"
```

---

## Task 3: Surface `text_source` + page-fallback on `_focus_deep` results

**Files:**
- Modify: `carta/embed/pipeline.py` (`_focus_deep` visual builder ~1830-1835; text builder ~1861-1867)
- Test: `carta/tests/test_pipeline.py`

- [ ] **Step 1: Write the failing test**

Add to the `TestRunFocus` class in `carta/tests/test_pipeline.py`:

```python
    def test_focus_ocr_chunk_gets_ocr_visual_tier_and_page(self):
        from unittest.mock import patch, MagicMock
        from carta.embed.pipeline import run_focus
        point = MagicMock(); point.score = 0.9
        point.payload = {"file_path": "docs/board.pdf", "text": "32M Hz callouts",
                         "doc_type": "image_description", "model_used": "llava", "page_num": 3}
        resp = MagicMock(); resp.points = [point]
        client = MagicMock(); client.query_points.return_value = resp
        with patch("carta.embed.pipeline.QdrantClient", return_value=client), \
             patch("carta.embed.pipeline.get_embedding", return_value=[0.0] * 768), \
             patch("carta.embed.pipeline.collection_is_hybrid", return_value=False), \
             patch("carta.embed.pipeline._ensure_file_path_index"), \
             patch("carta.embed.pipeline._attach_page_images", side_effect=lambda hits, *a, **k: hits), \
             patch("carta.search.scoped.get_search_collections", return_value=["test-project_doc"]), \
             patch("carta.embed.pipeline.find_config", return_value="/fake/.carta/config.yaml"):
            results = run_focus("docs/board.pdf", self.BASE_CFG, query="32mhz")
        assert results[0]["text_source"] == "ocr_visual"
        assert results[0]["page"] == 3
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest carta/tests/test_pipeline.py::TestRunFocus::test_focus_ocr_chunk_gets_ocr_visual_tier_and_page -v`
Expected: FAIL — `KeyError: 'text_source'`.

- [ ] **Step 3: Update the builders**

In `carta/embed/pipeline.py`, the `_focus_deep` **text** builder (~1861-1867) becomes:

```python
                    coll_results.append({
                        "score": r.score,
                        "source": payload.get("file_path", payload.get("slug", "")),
                        "excerpt": payload.get("text", ""), "type": "text",
                        "doc_type": payload.get("doc_type", ""),
                        "page": payload.get("page") or payload.get("page_num"),
                        "section_heading": payload.get("section_heading", ""),
                        "text_source": _text_source(payload)})
```

The `_focus_deep` **visual** builder (~1830-1835) becomes:

```python
                        coll_results.append({
                            "score": r.score,
                            "source": f"{payload.get('file_path', payload.get('slug', ''))} (page {payload.get('page_num', '?')})",
                            "excerpt": f"[Visual result] Page {payload.get('page_num', '?')} - {payload.get('file_path', '')}",
                            "type": "visual", "doc_type": payload.get("doc_type", ""),
                            "page": payload.get("page_num"), "section_heading": "",
                            "text_source": "visual"})
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest carta/tests/test_pipeline.py::TestRunFocus -v`
Expected: PASS (new test + existing TestRunFocus tests green).

- [ ] **Step 5: Commit**

```bash
git add carta/embed/pipeline.py carta/tests/test_pipeline.py
git commit -m "feat(search): surface text_source + page-num fallback on focus results"
```

---

## Task 4: Caveat on `ocr_visual` hits in broad search (CLI + MCP)

**Files:**
- Modify: `carta/cli.py` (`cmd_search` print loop ~307-314)
- Modify: `carta/mcp/server.py` (`_format_search_result` ~53-67)
- Test: `carta/tests/test_cli.py`, `carta/tests/test_mcp_server.py`

- [ ] **Step 1: Write the failing tests**

Add to `TestCmdSearchAnchors` in `carta/tests/test_cli.py`:

```python
    def test_search_marks_ocr_visual_hits_with_caveat(self, tmp_path, capsys):
        import argparse
        from unittest.mock import patch
        from carta import cli
        results = [
            {"score": 0.8, "source": "docs/board.pdf", "excerpt": "32M Hz", "doc_type": "image_description",
             "type": "text", "page": 3, "section_heading": "", "text_source": "ocr_visual"},
            {"score": 0.7, "source": "docs/spec.md", "excerpt": "intro", "doc_type": "",
             "type": "text", "page": 2, "section_heading": "Intro", "text_source": "text_layer"},
        ]
        args = argparse.Namespace(query=["x"], hops=0)
        with patch("carta.cli.find_config", return_value=tmp_path / ".carta" / "config.yaml"), \
             patch("carta.config.load_config", return_value={"modules": {"doc_search": True}}), \
             patch("carta.embed.pipeline.run_search", return_value=results):
            cli.cmd_search(args)
        lines = [l for l in capsys.readouterr().out.splitlines() if l.startswith("[")]
        assert "OCR" in lines[0] and "carta focus" in lines[0]   # doubted hit caveated
        assert "OCR" not in lines[1]                              # trusted hit clean
```

Add to `TestSearchAnchors` in `carta/tests/test_mcp_server.py`:

```python
    def test_format_adds_caveat_and_text_source_for_ocr_visual(self):
        import carta.mcp.server as server
        out = server._format_search_result(
            {"score": 0.8, "source": "docs/board.pdf", "excerpt": "32M Hz",
             "page": 3, "section_heading": "", "type": "text", "text_source": "ocr_visual"})
        assert out["text_source"] == "ocr_visual"
        assert "caveat" in out and "carta_focus" in out["caveat"]

    def test_format_no_caveat_for_trusted_text(self):
        import carta.mcp.server as server
        out = server._format_search_result(
            {"score": 0.7, "source": "docs/spec.md", "excerpt": "x",
             "page": 2, "section_heading": "Intro", "type": "text", "text_source": "text_layer"})
        assert out["text_source"] == "text_layer"
        assert "caveat" not in out
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest carta/tests/test_cli.py::TestCmdSearchAnchors::test_search_marks_ocr_visual_hits_with_caveat carta/tests/test_mcp_server.py::TestSearchAnchors::test_format_adds_caveat_and_text_source_for_ocr_visual -v`
Expected: FAIL — CLI: no "OCR" in output; MCP: `KeyError: 'caveat'` / missing `text_source`.

- [ ] **Step 3: Implement the caveats**

In `carta/cli.py`, the `cmd_search` print loop (~307-314) becomes:

```python
    for r in results:
        tag = f"[{r['doc_type']}] " if r.get("doc_type") in NOTE_DOC_TYPES else ""
        page = r.get("page")
        loc = f" p.{page}" if page is not None else ""
        heading = r.get("section_heading") or ""
        if heading:
            loc += f" §{heading}"
        caveat = ("  ⚠ OCR-diagram, unverified — `carta focus` for the page image"
                  if r.get("text_source") == "ocr_visual" else "")
        print(f"[{r['score']:.2f}] {tag}{r['source']}{loc} — {r['excerpt']}{caveat}")
```

In `carta/mcp/server.py`, `_format_search_result` (~53-67) becomes:

```python
def _format_search_result(r: dict) -> dict:
    """Shape a raw search hit into the MCP wire dict: rounded score, page/section anchors,
    trust tier, truncated excerpt, and (for visual hits) the page image. Defensive .get()."""
    item = {
        "score": round(r.get("score", 0.0), 4),
        "source": r.get("source", ""),
        "page": r.get("page"),
        "section_heading": r.get("section_heading", ""),
        "excerpt": (r.get("excerpt") or "")[:300],
        "text_source": r.get("text_source", "text_layer"),
    }
    if r.get("text_source") == "ocr_visual":
        item["caveat"] = "OCR diagram description — unverified; call carta_focus for the page image."
    if r.get("type") == "visual":
        item["type"] = "visual"
        if r.get("image_b64"):
            item["image_b64"] = r["image_b64"]
    return item
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest carta/tests/test_cli.py::TestCmdSearchAnchors carta/tests/test_mcp_server.py::TestSearchAnchors -v`
Expected: PASS (new + existing tests green).

- [ ] **Step 5: Commit**

```bash
git add carta/cli.py carta/mcp/server.py carta/tests/test_cli.py carta/tests/test_mcp_server.py
git commit -m "feat(search): caveat ocr_visual hits in broad search (CLI marker + MCP caveat/text_source)"
```

---

## Task 5: Page-image pairing for `ocr_visual` hits in focus

**Files:**
- Modify: `carta/embed/pipeline.py` (`_attach_page_images` ~1749-1758)
- Modify: `carta/mcp/server.py` (`carta_focus` formatter — surface `text_source`)
- Test: `carta/tests/test_pipeline.py`

- [ ] **Step 1: Write the failing test**

Add to `TestRenderPageImages` in `carta/tests/test_pipeline.py`:

```python
    def test_attach_images_also_covers_ocr_visual_text_hits(self, tmp_path):
        from unittest.mock import patch
        from carta.embed.pipeline import _attach_page_images
        hits = [
            {"type": "text", "text_source": "ocr_visual", "page": 3},   # doubted OCR → image
            {"type": "text", "text_source": "ocr_table", "page": 4},    # trusted table → none
            {"type": "text", "text_source": "text_layer", "page": 5},   # real text → none
            {"type": "visual", "text_source": "visual", "page": 6},     # ColPali → image (unchanged)
        ]
        with patch("carta.embed.pipeline.render_page_png", return_value=b"PNG"):
            out = _attach_page_images(hits, tmp_path / "board.pdf", tmp_path)
        assert out[0]["image_b64"]              # ocr_visual text hit gets the page image
        assert "image_b64" not in out[1]        # ocr_table untouched
        assert "image_b64" not in out[2]        # text_layer untouched
        assert out[3]["image_b64"]              # visual still works
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest carta/tests/test_pipeline.py::TestRenderPageImages::test_attach_images_also_covers_ocr_visual_text_hits -v`
Expected: FAIL — `out[0]` has no `image_b64` (only `type == "visual"` is rendered today).

- [ ] **Step 3: Extend `_attach_page_images`**

In `carta/embed/pipeline.py`, `_attach_page_images` (~1749-1758) becomes:

```python
def _attach_page_images(hits: list[dict], abs_source_path: Path, repo_root: Path,
                        embed_cfg: dict | None = None) -> list[dict]:
    """Attach a base64 page PNG to hits worth verifying against the page: ColPali visual
    hits and doubted ocr_visual (diagram-OCR) text hits. Mutates + returns hits."""
    import base64
    for hit in hits:
        wants_image = hit.get("type") == "visual" or hit.get("text_source") == "ocr_visual"
        if wants_image and hit.get("page"):
            png = render_page_png(abs_source_path, hit["page"], repo_root, embed_cfg)
            if png is not None:
                hit["image_b64"] = base64.b64encode(png).decode("ascii")
    return hits
```

Then surface `text_source` in the `carta_focus` formatter in `carta/mcp/server.py` — add this line to the `item` dict it builds (alongside `page`/`section_heading`):

```python
        "text_source": r.get("text_source", "text_layer"),
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest carta/tests/test_pipeline.py::TestRenderPageImages carta/tests/test_mcp_server.py::TestCartaFocus -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add carta/embed/pipeline.py carta/mcp/server.py carta/tests/test_pipeline.py
git commit -m "feat(focus): attach page image to ocr_visual text hits; surface text_source in carta_focus"
```

---

## Task 6: Conservative `LLAVA_PROMPT`

**Files:**
- Modify: `carta/vision/router.py:120-124`
- Test: `carta/tests/test_router.py` (create if absent) or `carta/vision/tests/` — use whichever exists; this plan creates `carta/tests/test_router.py`.

- [ ] **Step 1: Write the failing test**

Create `carta/tests/test_router.py`:

```python
def test_llava_prompt_transcribes_not_infers():
    from carta.vision.router import LLAVA_PROMPT
    p = LLAVA_PROMPT.lower()
    assert "transcribe" in p
    assert "not infer" in p              # forbids inference explicitly
    assert "describe this technical diagram" not in p   # the old interpretive opener is gone
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest carta/tests/test_router.py -v`
Expected: FAIL — current prompt opens "Describe this technical diagram" and lacks "transcribe"/"not infer".

- [ ] **Step 3: Rewrite the prompt**

In `carta/vision/router.py` (lines 120-124):

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

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest carta/tests/test_router.py -v`
Expected: PASS.

- [ ] **Step 5: Manual re-OCR sanity check (record result, do not gate)**

Render a known annotated-diagram page and OCR it with the vision model using the new prompt; confirm it transcribes visible labels (e.g. `32M Hz`, pin names) and no longer asserts un-printed designators/functions (`K3=RESET`, `F1`). Uses the N32WB031 board-guide page 3 that exhibited the original hallucinations:

```bash
/Users/ian/.local/pipx/venvs/carta-cc/bin/python - <<'PY'
import fitz, base64, json, urllib.request
from carta.vision.router import LLAVA_PROMPT
PDF = "/Users/ian/School/Elementrailer/petsense/docs/reference/datasheets/U1_N32WB031KEQ6-2/N32WB031_STB_V1.0.1/EN_UG_N32WB031_STB_Development_Board_User_Guide.pdf"; PAGE = 3
png = fitz.open(PDF)[PAGE-1].get_pixmap(dpi=200).tobytes("png")
body = {"model": "qwen3-vl:8b", "prompt": LLAVA_PROMPT,
        "images": [base64.b64encode(png).decode()], "stream": False, "options": {"temperature": 0}}
r = urllib.request.urlopen(urllib.request.Request("http://localhost:11434/api/generate",
    data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}), timeout=240)
print(json.loads(r.read())["response"])
PY
```
Record the output in the commit message or PR. (Ollama-dependent; skip with a note if unavailable.)

- [ ] **Step 6: Commit**

```bash
git add carta/vision/router.py carta/tests/test_router.py
git commit -m "feat(vision): conservative LLAVA_PROMPT — transcribe diagram labels, don't infer"
```

---

## Task 7: Acceptance — full suite + broad-eval no-regression

**Files:** none (verification only)

- [ ] **Step 1: Run the full test suite**

Run: `python -m pytest carta -q`
Expected: PASS (all green, including the new tests). If a pre-existing test asserted an exact result-dict shape, add `text_source` to its expectation and re-run.

- [ ] **Step 2: Broad-eval no-regression gate**

Run: `carta eval .carta/eval/et-embed.yaml -k 5`
Expected: recall@5 **≥ 0.984** (unchanged). `text_source` is additive metadata and ranking is untouched, so recall must hold by construction. If the eval set isn't available in this environment, record that the gate was skipped and why (run it in the ET-embed project with this branch).

- [ ] **Step 3: Manual end-to-end smoke (against the visual-embedded petsense project)**

Run from the petsense repo so config/collections resolve, using a carta build that has both
focus and torch (e.g. the installed `carta` ≥ 0.13.0 with the visual deps injected):

```bash
cd /Users/ian/School/Elementrailer/petsense
GUIDE="docs/reference/datasheets/U1_N32WB031KEQ6-2/N32WB031_STB_V1.0.1/EN_UG_N32WB031_STB_Development_Board_User_Guide.pdf"
carta search "32MHz crystal"            # an ocr_visual hit shows the ⚠ caveat marker
carta focus --source "$GUIDE" 32MHz crystal   # the ocr_visual hit writes a page PNG + prints the path
```
Expected: broad search marks the diagram-OCR hit with the caveat; focus returns the page image for it.

---

## Self-Review notes (for the executor)

- **Spec coverage:** trust gradient/`_text_source` → Task 1; surface `text_source`+page → Tasks 2–3; caveat in broad search → Task 4; focus image pairing → Task 5; conservative prompt → Task 6; no-regression → Task 7. Non-goals honored: no ranking change, no re-embed, no new config/collection.
- **Type consistency:** every result dict carries `text_source` ∈ {`text_layer`, `ocr_table`, `ocr_visual`, `visual`}. Text builders call `_text_source(payload)`; visual builders hardcode `"visual"`. `page` is `payload.get("page") or payload.get("page_num")` in text builders only (visual builders already use `page_num`). `_attach_page_images` renders when `type == "visual"` OR `text_source == "ocr_visual"`.
- **Retroactive:** all of Tasks 1–5 work off existing payload — no re-embed. Task 6 only affects future embeds; petsense re-drain is the spec follow-up.
