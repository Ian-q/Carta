# Contextual Chunk Headers Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prepend a per-chunk contextual header (`{doc_title} > {section_heading}`) to each chunk's *embedding input* — never the stored payload — so dense (and BM25) vectors carry document identity, lifting first-stage recall.

**Architecture:** Three pure helpers in `carta/embed/parse.py` (`resolve_doc_title`, `build_chunk_header`, `apply_contextual_headers`) compute an `embed_text` field on each chunk. `carta/embed/pipeline.py` calls them after `chunk_text`, gated by a new config flag. `carta/embed/embed.py` reads the embed input through one helper (`_embed_input`) at its three embed sites and excludes `embed_text` from the Qdrant payload (the payload keeps raw `text`). Validation is a re-embed + eval on the ET-embed corpus.

**Tech Stack:** Python 3.10+, pytest + unittest.mock, qdrant-client, Ollama (nomic-embed-text). No new dependencies.

**Spec:** [docs/superpowers/specs/2026-06-14-contextual-chunk-headers-design.md](../specs/2026-06-14-contextual-chunk-headers-design.md)

---

## File Structure

- **`carta/config.py`** — add `embed.chunking.contextual_header` to DEFAULTS (the on/off + A-B knob).
- **`carta/embed/parse.py`** — three new pure helpers: title resolution, header formatting, chunk-list application. Lives here because it's text/chunk shaping, beside `chunk_text`.
- **`carta/embed/embed.py`** — new pure `_embed_input(chunk)` helper; wire it into the 3 embed sites; exclude `embed_text` from payload.
- **`carta/embed/pipeline.py`** — call `resolve_doc_title` + `apply_contextual_headers` after `chunk_text`, behind the config flag.
- **`carta/embed/tests/test_contextual_header.py`** — new test module for all helpers + the embed-path payload test.

Existing patterns followed: helper naming (`_leading_underscore` for internals), `unittest.mock` for tests, chunk dicts as the unit of work.

---

## Task 1: Config flag `embed.chunking.contextual_header`

**Files:**
- Modify: `carta/config.py:88-92` (DEFAULTS `embed.chunking`)
- Test: `carta/embed/tests/test_contextual_header.py`

- [ ] **Step 1: Write the failing test**

Create `carta/embed/tests/test_contextual_header.py`:

```python
"""Tests for contextual chunk headers (issue #19)."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

from carta.config import DEFAULTS


def test_default_enables_contextual_header():
    assert DEFAULTS["embed"]["chunking"]["contextual_header"] is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest carta/embed/tests/test_contextual_header.py::test_default_enables_contextual_header -v`
Expected: FAIL — `KeyError: 'contextual_header'`

- [ ] **Step 3: Add the default**

In `carta/config.py`, the `chunking` block (currently lines 88-92) becomes:

```python
        "chunking": {
            "max_tokens": 800,
            "overlap_fraction": 0.15,
            "preserve_tables": True,  # NEW: keep markdown tables whole
            # Prepend "{doc_title} > {section_heading}" to each chunk's EMBEDDING
            # input (not the stored excerpt) so vectors carry doc identity. Re-embed
            # required to take effect. Set false to opt out. (issue #19)
            "contextual_header": True,
        },
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest carta/embed/tests/test_contextual_header.py::test_default_enables_contextual_header -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add carta/config.py carta/embed/tests/test_contextual_header.py
git commit -m "feat(embed): add embed.chunking.contextual_header config flag (#19)"
```

---

## Task 2: `resolve_doc_title` helper

Resolves a document's display title for the header. Tiers: frontmatter `title` → first H1 → humanized filename stem.

**Files:**
- Modify: `carta/embed/parse.py` (add helper near `chunk_text`)
- Test: `carta/embed/tests/test_contextual_header.py`

- [ ] **Step 1: Write the failing tests**

Append to `carta/embed/tests/test_contextual_header.py`:

```python
from carta.embed.parse import resolve_doc_title


def _pages(text):
    return [{"page": 1, "text": text, "headings": []}]


def test_title_prefers_frontmatter():
    title = resolve_doc_title({"title": "My Doc"}, _pages("# Other H1\nbody"), Path("x.md"))
    assert title == "My Doc"


def test_title_falls_back_to_h1():
    title = resolve_doc_title({}, _pages("# Real Title\n\nbody text"), Path("x.md"))
    assert title == "Real Title"


def test_title_h1_ignores_h2():
    # Only '# ' (H1) counts as a title, not '## '
    title = resolve_doc_title({}, _pages("## Section Only\n\nbody"), Path("FSM_GAIN_SCHEDULER.md"))
    assert title == "FSM GAIN SCHEDULER"


def test_title_falls_back_to_humanized_filename():
    title = resolve_doc_title({}, _pages("no heading here"), Path("docs/hardware/vcu/connector-map.md"))
    assert title == "connector map"


def test_title_blank_frontmatter_title_skipped():
    title = resolve_doc_title({"title": "   "}, _pages("# H1 Wins\nbody"), Path("x.md"))
    assert title == "H1 Wins"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest carta/embed/tests/test_contextual_header.py -k title -v`
Expected: FAIL — `ImportError: cannot import name 'resolve_doc_title'`

- [ ] **Step 3: Implement the helper**

In `carta/embed/parse.py`, add after `chunk_text` (after line 287):

```python
def resolve_doc_title(frontmatter_meta: dict, pages: list[dict], file_path: "Path") -> str:
    """Best-effort display title for a document, for contextual chunk headers.

    Tiers (first non-empty wins):
      1. frontmatter ``title:``
      2. the first H1 line (``# Title``) found in the extracted pages
      3. the humanized filename stem (``-``/``_`` -> space)
    """
    fm_title = (frontmatter_meta or {}).get("title")
    if isinstance(fm_title, str) and fm_title.strip():
        return fm_title.strip()
    for page in pages:
        for line in (page.get("text") or "").splitlines():
            m = re.match(r"#\s+(\S.*)", line.strip())
            if m:
                return m.group(1).strip()
    return file_path.stem.replace("-", " ").replace("_", " ").strip()
```

(`re` and `Path` are already imported in `parse.py`.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest carta/embed/tests/test_contextual_header.py -k title -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add carta/embed/parse.py carta/embed/tests/test_contextual_header.py
git commit -m "feat(embed): resolve_doc_title helper for contextual headers (#19)"
```

---

## Task 3: `build_chunk_header` helper

Formats the header string from a title + section heading, handling dedupe and empties.

**Files:**
- Modify: `carta/embed/parse.py`
- Test: `carta/embed/tests/test_contextual_header.py`

- [ ] **Step 1: Write the failing tests**

Append:

```python
from carta.embed.parse import build_chunk_header


def test_header_title_and_heading():
    assert build_chunk_header("VCU Power Architecture", "## 12V Rail") == "VCU Power Architecture > 12V Rail"


def test_header_strips_hashes_from_heading():
    assert build_chunk_header("Doc", "### Task 1") == "Doc > Task 1"


def test_header_intro_heading_is_title_only():
    assert build_chunk_header("Doc", "(intro)") == "Doc"


def test_header_empty_heading_is_title_only():
    assert build_chunk_header("Doc", "") == "Doc"


def test_header_dedupes_title_equal_heading():
    assert build_chunk_header("Timing architecture", "# Timing architecture") == "Timing architecture"


def test_header_heading_only_when_no_title():
    assert build_chunk_header("", "## Pinout") == "Pinout"


def test_header_empty_when_nothing():
    assert build_chunk_header("", "") == ""
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest carta/embed/tests/test_contextual_header.py -k header_ -v`
Expected: FAIL — `ImportError: cannot import name 'build_chunk_header'`

- [ ] **Step 3: Implement the helper**

In `carta/embed/parse.py`, add after `resolve_doc_title`:

```python
def build_chunk_header(doc_title: str, section_heading: str) -> str:
    """Compose the contextual header prefix for a chunk.

    Returns ``"{title} > {heading}"``, ``"{title}"``, ``"{heading}"``, or ``""``.
    Leading ``#`` markers are stripped from the heading; ``"(intro)"`` and blanks
    count as "no heading"; a heading equal to the title (case-insensitive) is
    deduped to the title alone.
    """
    title = (doc_title or "").strip()
    heading = (section_heading or "").lstrip("#").strip()
    if heading == "(intro)":
        heading = ""
    if heading and title and heading.lower() == title.lower():
        heading = ""
    if title and heading:
        return f"{title} > {heading}"
    return title or heading
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest carta/embed/tests/test_contextual_header.py -k header_ -v`
Expected: PASS (7 tests)

- [ ] **Step 5: Commit**

```bash
git add carta/embed/parse.py carta/embed/tests/test_contextual_header.py
git commit -m "feat(embed): build_chunk_header helper for contextual headers (#19)"
```

---

## Task 4: `apply_contextual_headers` helper

Sets `embed_text` on each chunk; leaves `text` untouched.

**Files:**
- Modify: `carta/embed/parse.py`
- Test: `carta/embed/tests/test_contextual_header.py`

- [ ] **Step 1: Write the failing tests**

Append:

```python
from carta.embed.parse import apply_contextual_headers


def test_apply_sets_embed_text_keeps_text():
    chunks = [{"text": "body one", "section_heading": "## Pinout", "chunk_index": 0}]
    apply_contextual_headers(chunks, "CTS Control Harness")
    assert chunks[0]["text"] == "body one"  # unchanged
    assert chunks[0]["embed_text"] == "CTS Control Harness > Pinout\n\nbody one"


def test_apply_title_only_when_no_heading():
    chunks = [{"text": "cont chunk", "section_heading": "", "chunk_index": 1}]
    apply_contextual_headers(chunks, "CTS Control Harness")
    assert chunks[0]["embed_text"] == "CTS Control Harness\n\ncont chunk"


def test_apply_no_embed_text_when_header_empty():
    chunks = [{"text": "x", "section_heading": "", "chunk_index": 0}]
    apply_contextual_headers(chunks, "")  # no title, no heading -> empty header
    assert "embed_text" not in chunks[0]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest carta/embed/tests/test_contextual_header.py -k apply -v`
Expected: FAIL — `ImportError: cannot import name 'apply_contextual_headers'`

- [ ] **Step 3: Implement the helper**

In `carta/embed/parse.py`, add after `build_chunk_header`:

```python
def apply_contextual_headers(chunks: list[dict], doc_title: str) -> list[dict]:
    """Set ``embed_text`` = "{header}\\n\\n{text}" on each chunk whose header is
    non-empty. The stored ``text`` is never modified — only the embedding input.
    Mutates and returns ``chunks``.
    """
    for chunk in chunks:
        header = build_chunk_header(doc_title, chunk.get("section_heading", ""))
        if header:
            chunk["embed_text"] = f"{header}\n\n{chunk['text']}"
    return chunks
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest carta/embed/tests/test_contextual_header.py -k apply -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add carta/embed/parse.py carta/embed/tests/test_contextual_header.py
git commit -m "feat(embed): apply_contextual_headers helper (#19)"
```

---

## Task 5: `_embed_input` in embed.py — wire embed sites + exclude from payload

`embed_text` becomes the embedding input at all three sites; the Qdrant payload keeps raw `text` and drops `embed_text`.

**Files:**
- Modify: `carta/embed/embed.py:226` (payload comprehension), `:248` (sparse), `:276` (dense, workers==1), `:289` (dense, threadpool); add `_embed_input` helper
- Test: `carta/embed/tests/test_contextual_header.py`

- [ ] **Step 1: Write the failing tests**

Append:

```python
from carta.embed.embed import _embed_input


def test_embed_input_prefers_embed_text():
    assert _embed_input({"text": "body", "embed_text": "Title > H\n\nbody"}) == "Title > H\n\nbody"


def test_embed_input_falls_back_to_text():
    assert _embed_input({"text": "body"}) == "body"


def test_embed_input_empty_embed_text_falls_back():
    assert _embed_input({"text": "body", "embed_text": ""}) == "body"


def test_upsert_embeds_header_but_payload_keeps_raw_text():
    captured = {}

    def fake_dense(text, **kw):
        captured["dense"] = text
        return [0.0] * 8

    def fake_sparse(text, **kw):
        captured["sparse"] = text
        return SimpleNamespace(indices=[1], values=[0.5])

    client = MagicMock()
    cfg = {
        "project_name": "t", "qdrant_url": "http://localhost:6333",
        "embed": {"ollama_url": "u", "ollama_model": "m", "embedding_workers": 1},
    }
    chunk = {
        "slug": "d", "file_path": "docs/d.md", "chunk_index": 0,
        "doc_type": "unknown", "doc_generation": 1,
        "text": "raw body", "embed_text": "D Title > Pinout\n\nraw body",
    }
    with patch("carta.embed.embed.get_embedding", side_effect=fake_dense), \
         patch("carta.embed.sparse.embed_sparse_document", side_effect=fake_sparse), \
         patch("carta.embed.embed.ensure_collection"), \
         patch("carta.embed.embed.collection_is_hybrid", return_value=True):
        from carta.embed.embed import upsert_chunks
        upsert_chunks([chunk], cfg, client=client)

    # Both vectors saw the header-augmented input
    assert "D Title > Pinout" in captured["dense"]
    assert "D Title > Pinout" in captured["sparse"]
    # Payload stores raw text and does NOT leak embed_text
    points = client.upsert.call_args.kwargs["points"]
    assert points[0].payload["text"] == "raw body"
    assert "embed_text" not in points[0].payload
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest carta/embed/tests/test_contextual_header.py -k "embed_input or upsert_embeds" -v`
Expected: FAIL — `ImportError: cannot import name '_embed_input'`

- [ ] **Step 3: Add `_embed_input` and wire the four sites**

In `carta/embed/embed.py`, add the helper near the top (after the vector-name constants, ~line 30):

```python
def _embed_input(chunk: dict) -> str:
    """Text fed to the embedders for a chunk: the contextual-header-augmented
    ``embed_text`` when present (see parse.apply_contextual_headers), else the raw
    ``text``. The Qdrant payload always stores raw ``text`` — only vectors see the header.
    """
    return chunk.get("embed_text") or chunk["text"]
```

Change the payload comprehension (line 226) from:

```python
        payload = {k: v for k, v in chunk.items() if k != "text"}
```
to:
```python
        payload = {k: v for k, v in chunk.items() if k not in ("text", "embed_text")}
```

Change the sparse embed (line 248) from:
```python
            sv = embed_sparse_document(chunk["text"])
```
to:
```python
            sv = embed_sparse_document(_embed_input(chunk))
```

Change the workers==1 dense embed (line 276) from:
```python
                vec = get_embedding(chunk["text"], ollama_url=ollama_url, model=model)
```
to:
```python
                vec = get_embedding(_embed_input(chunk), ollama_url=ollama_url, model=model)
```

Change the threadpool dense embed (line 289) from:
```python
                ex.submit(get_embedding, c["text"], ollama_url=ollama_url, model=model): c
```
to:
```python
                ex.submit(get_embedding, _embed_input(c), ollama_url=ollama_url, model=model): c
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest carta/embed/tests/test_contextual_header.py -k "embed_input or upsert_embeds" -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Run the full embed test module (no regressions)**

Run: `python -m pytest carta/embed/tests/test_embed.py -q`
Expected: PASS (existing upsert tests unaffected — `_embed_input` falls back to `text` when no `embed_text`)

- [ ] **Step 6: Commit**

```bash
git add carta/embed/embed.py carta/embed/tests/test_contextual_header.py
git commit -m "feat(embed): embed contextual header via _embed_input, keep payload raw (#19)"
```

---

## Task 6: Wire the pipeline to build headers

Call the helpers after `chunk_text`, gated by the config flag.

**Files:**
- Modify: `carta/embed/pipeline.py:20` (import), `:335` (after `chunk_text`)
- Test: full suite + manual seam check (the logic is fully covered by Tasks 2-4; this task is wiring)

- [ ] **Step 1: Add the import**

In `carta/embed/pipeline.py`, extend the existing parse import (line 20) from:

```python
from carta.embed.parse import extract_pdf_text, extract_pdf_text_and_classify, extract_markdown_text, chunk_text, _estimate_tokens
```
to:
```python
from carta.embed.parse import extract_pdf_text, extract_pdf_text_and_classify, extract_markdown_text, chunk_text, _estimate_tokens, resolve_doc_title, apply_contextual_headers
```

- [ ] **Step 2: Wire the call after `chunk_text`**

In `carta/embed/pipeline.py`, immediately after line 335 (`raw_chunks = chunk_text(...)`), insert:

```python
    if cfg.get("embed", {}).get("chunking", {}).get("contextual_header", True):
        doc_title = resolve_doc_title(frontmatter_meta, pages, file_path)
        apply_contextual_headers(raw_chunks, doc_title)
```

(`frontmatter_meta`, `pages`, and `file_path` are all in scope here — see `pipeline.py:310-345`.)

- [ ] **Step 3: Verify the wiring with a seam check**

Run:
```bash
python -c "
from carta.embed.parse import resolve_doc_title, apply_contextual_headers
pages=[{'page':1,'text':'# Safety MCU CAN Message Specification\n\nintro','headings':[]}]
import pathlib
title=resolve_doc_title({}, pages, pathlib.Path('docs/CAN/SAFETY-MCU-MESSAGES.md'))
chunks=[{'text':'0x220 Safety Status Frame','section_heading':'### 0x220','chunk_index':5}]
apply_contextual_headers(chunks, title)
print(repr(chunks[0]['embed_text']))
assert chunks[0]['embed_text'].startswith('Safety MCU CAN Message Specification > 0x220')
print('OK')
"
```
Expected: prints the `embed_text` with the title+heading prefix, then `OK`.

- [ ] **Step 4: Run the full test suite (no regressions)**

Run: `python -m pytest -q`
Expected: PASS (all existing tests + the new module). Note any preexisting unrelated failures separately.

- [ ] **Step 5: Commit**

```bash
git add carta/embed/pipeline.py
git commit -m "feat(embed): wire contextual headers into embed pipeline (#19)"
```

---

## Task 7: Validation — re-embed ET-embed + eval (decision gate)

Not a code change. Re-embed the ET-embed text corpus with the unreleased worktree code and measure against the captured baseline (dense r@5 0.726 / text-fused r@5 0.887 / full r@5 0.887). **Do not set the shipped default or merge until this gate is evaluated.**

- [ ] **Step 1: Re-embed ET-embed text collection with the new code**

Run (from the ET-embed root, using the worktree checkout + pipx venv):
```bash
cd ~/School/Elementrailer/ET-embed
PYTHONPATH=<worktree-path> ~/.local/pipx/venvs/carta-cc/bin/python -m carta embed --force
```
Expected: re-embeds the ~8,334 text chunks (visual/ColPali untouched). `contextual_header` defaults true, so headers are applied.

- [ ] **Step 2: Re-run the per-lane depth-check (leading indicator)**

Run:
```bash
cd ~/School/Elementrailer/ET-embed
PYTHONPATH=<worktree-path> OMP_NUM_THREADS=1 ~/.local/pipx/venvs/carta-cc/bin/python /tmp/carta_depthcheck.py
```
Expected: the AGGREGATE table prints. Compare **dense r@5** to the 0.726 baseline — this is the primary signal that headers helped the dense lane.

- [ ] **Step 3: Run the headline eval (hybrid-alone) at multiple depths**

Run:
```bash
cd ~/School/Elementrailer/ET-embed
for K in 5 10 20; do PYTHONPATH=<worktree-path> ~/.local/pipx/venvs/carta-cc/bin/python -m carta eval .carta/eval/et-embed.yaml -k $K; done
```
Expected: `recall@5` compared to 0.887 baseline; per-query `[rank]`/`[MISS]` lines for regression inspection.

- [ ] **Step 4: Guard the visual eval (must not regress)**

Run:
```bash
cd ~/School/Elementrailer/ET-embed
PYTHONPATH=<worktree-path> ~/.local/pipx/venvs/carta-cc/bin/python -m carta eval .carta/eval/et-embed-datasheets.yaml -k 5
```
Expected: recall@5 ≈ prior 0.857, not lower.

- [ ] **Step 5: Apply the decision rule**

- **Ship (proceed to Task 8)** if dense r@5 rises meaningfully **and** text-fused/full r@5 beats 0.887 with net-positive per-query movement (gains − losses > 0, no systematic regression) and the visual eval holds.
- **Revert** otherwise: set the default to `false`, re-embed ET-embed from `main` (`git -C <worktree> stash` not needed — re-embed with the main `carta`), and record the negative result. Escalate to the next lever (chunk-size 800→400, then query-expansion) in a follow-up.

- [ ] **Step 6: Record the result**

Append a dated section to `~/School/Elementrailer/ET-embed/.carta/eval/RESULTS.md` with the before/after table (dense/sparse/fused/full r@{5,10,20,50}) and the decision. No commit in the carta repo for this step.

---

## Task 8: Lock the default + generalization check (only if Task 7 passed)

**Files:**
- Modify: `carta/config.py` (confirm/adjust the default), `docs/superpowers/specs/2026-06-14-contextual-chunk-headers-design.md` (record outcome), `~/.claude/.../memory/project_et-embed-eval-workflow.md` (new baseline)

- [ ] **Step 1: Grow the eval (~15-20 fresh queries)**

Add new queries to `~/School/Elementrailer/ET-embed/.carta/eval/et-embed.yaml` targeting underrepresented doc types — **not** the 4 named misses. Verify each `expect` resolves to a real doc. Re-run Step 3 of Task 7 to confirm the gain generalizes (recall holds or improves on the grown set).

- [ ] **Step 2: Confirm the shipped default**

If validated, leave `contextual_header: True` in `carta/config.py`. If the generalization check is weaker than the 62-query result, reconsider (document the call). Run `python -m pytest -q` to confirm green.

- [ ] **Step 3: Update memory + spec status**

Update the `et-embed-eval-workflow` project-memory file with the new hybrid-alone baseline and the chosen default. Change the spec header `Status:` to `shipped` (or `reverted`) with the result line.

- [ ] **Step 4: Commit**

```bash
git add carta/config.py docs/superpowers/specs/2026-06-14-contextual-chunk-headers-design.md
git commit -m "chore(embed): confirm contextual_header default after eval validation (#19)"
```

- [ ] **Step 5: Finish the branch**

Use the `superpowers:finishing-a-development-branch` skill to open the PR (closes #19 or updates it with the result) and handle version bump per release practice.

---

## Self-Review

**Spec coverage:**
- Title resolution (frontmatter→H1→filename) → Task 2 ✓
- Header format + dedupe + `(intro)`/empty handling → Task 3 ✓
- `embed_text` on embed input only, payload raw → Tasks 4, 5 ✓
- Both dense + BM25 see the header → Task 5 (sparse `:248`, dense `:276`/`:289`) ✓
- Config flag `embed.chunking.contextual_header` → Task 1 ✓
- Pipeline integration at `pipeline.py:335` → Task 6 ✓
- Validation: re-embed + depth-check + `carta eval` hybrid-alone + visual guard + decision rule → Task 7 ✓
- Grow eval before locking default → Task 8 ✓
- Back-compat (fallback to `text` when no `embed_text`) → Task 5 (covered by `test_embed_input_falls_back_to_text` + full `test_embed.py` run) ✓

**Placeholder scan:** No TBD/TODO; every code step shows complete code; `<worktree-path>` in Task 7/8 is a real runtime substitution (the worktree dir chosen at execution), not a code placeholder.

**Type/name consistency:** `embed_text` (chunk key), `_embed_input` (embed.py), `resolve_doc_title`/`build_chunk_header`/`apply_contextual_headers` (parse.py), `embed.chunking.contextual_header` (config) — used identically across Tasks 1-6. Test file `carta/embed/tests/test_contextual_header.py` consistent throughout.
