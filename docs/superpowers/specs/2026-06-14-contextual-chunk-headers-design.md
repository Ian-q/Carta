# Design — Contextual chunk headers (prepend doc title + section heading to the embedded text)

**Date:** 2026-06-14 · **Status:** approved (brainstorm) · **Target:** carta-cc next minor (≈0.13.0) · **Phase:** first-stage recall — the lever identified by [#19](https://github.com/Ian-q/Carta/issues/19) after #36 (visual dilution, shipped) and #37 (reranker blend, abandoned — eval-disproven) both flowed back to "the gold doc isn't surfaced by first-stage retrieval at a useful rank." Header-only is the first experiment; chunk-size, query-expansion, and graph levers are documented as follow-ups.

## Problem

Carta embeds each chunk's **body text only**. A document's identity — its title, and (for continuation chunks) its section — is never part of the vector input:

- **Markdown** (`parse.py:129`) splits on `##`/`###`; each section keeps its own heading inline, but the **document title (H1 / frontmatter `title`) is never attached**, and when a section is large enough to be sub-chunked (`parse.py:233-285`) only the *first* sub-chunk carries the heading — continuation chunks embed bare body.
- **PDF** keeps heading lines where they physically occur in the page text, but again **no document title** is prepended and continuation chunks lose section context.
- The document title/filename and `section_heading` are stored as **metadata only** (`pipeline.py:343-352`), never in the embedded text.

### Diagnostic evidence (read-only depth-check, 62-query ET-embed eval, 2026-06-14)

Per-lane gold-doc recall, probed to depth 300 (text lanes are first-stage; "full" adds the `_visual` collection):

| lane | r@5 | r@10 | r@20 | r@50 | MRR@10 |
|---|---|---|---|---|---|
| dense-only | **0.726** | 0.806 | 0.871 | 0.935 | 0.591 |
| sparse-only (BM25) | **0.871** | 0.887 | 0.903 | 0.935 | 0.732 |
| text-fused (RRF) | **0.887** | 0.903 | 0.919 | 0.952 | 0.728 |
| full (text+visual) | 0.839\* | 0.919 | 0.935 | 0.984 | 0.717 |

text-fused r@5 = 0.887 reproduces the shipped hybrid-alone baseline → methodology validated. (\*the full-lane @5 dip is a probe artifact: `visual_max_ratio` cap = `round(0.2 × pool)`, so a deep probe pool of 60 admits 12 visual slots vs 1 at the shipped `top_n`=5; not a regression.)

Two facts make the lever clear:

1. **The dense lane is the weak link.** BM25 alone (0.871) carries the system; dense is **14 points worse at @5** (0.726) and fusion barely improves on sparse alone. That gap is the headroom.
2. **The gold docs' identity is absent from their chunks**, confirmed by chunk inspection:

   | gold doc | title-stem present in | chunks |
   |---|---|---|
   | SAFETY-MCU-MESSAGES | 0 / 12 | |
   | TIMING_ARCHITECTURE | 0 / 10 | |
   | connector-map | 1 / 36 | |
   | power-architecture | 1 / 16 | |
   | FSM_GAIN_SCHEDULER | 0 / 19 (no H1) | |

   A query like *"Safety MCU CAN message IDs"* must match a chunk literally about *"0x220 Safety Status Frame"* — which never says "Safety MCU". The dense vector encodes the frame, not the doc.

First-stage recall@50 is fused **0.952** / full **0.984**: every gold doc except the OCR-pending `kingpin` patent (US-11965795 → #38) **is in the corpus and retrievable deep**. This is a representation/ranking problem, not a "content missing" one — exactly what a contextual header addresses.

## Goals / Non-goals

**Goals**
- **Prepend a compact contextual header to each chunk's embedded text:** `{doc_title} > {section_heading}`, resolved per file, applied to **every** chunk (including continuation chunks that today carry no identity).
- **Keep the stored payload `text` pristine** (raw body). The header enters the *embedding input* only, so search excerpts, the hook injection, and reranker text are unchanged.
- **One config knob:** `embed.chunking.contextual_header` (bool), defaulted in `config.py` DEFAULTS. Lets us A/B on the eval and lets any project opt out.
- **Lift the dense lane.** Leading indicator = dense-only r@5 rising from 0.726 toward sparse levels; headline = text-fused (hybrid-alone) r@5 above 0.887, no per-query regressions.
- **Back-compatible.** Collections embedded before this change keep working; the header is opt-in at embed time and mixed corpora are fine. Shipping the default requires a re-embed (documented).

**Non-goals (this phase)**
- **Chunk-size retune (800→400).** A real second lever (sharper dense vectors, shares the re-embed cost) but kept out to attribute the header's effect cleanly — the #37 lesson. Documented below.
- **Query expansion / acronym maps.** Reserve lever for the few genuinely vocabulary-mismatched misses (e.g. "telemetry *rates*" vs the doc's "timing"). Query-time, no re-embed; revisit if header leaves them missed.
- **`related:` graph expansion.** Latent value here (the misses are densely cross-linked), but `promote_graph_neighbors` (`graph.py:168`) never displaces the top seeds without a reranker, ET-embed runs rerank off, and it reads only frontmatter `related:` edges (not the body links these docs use). Future lever, not a substitute for the root-cause fix.
- **Embedding-model upgrade.** The escalation if headers don't close the dense gap; out of scope now.

## Design

### Title resolution — `resolve_doc_title(frontmatter_meta, pages, file_path)` (new, in `parse.py`)

Tiered, first non-empty wins:
1. `frontmatter_meta.get("title")` (markdown with frontmatter `title:`).
2. First H1 line (`# …`) found in `pages` text (markdown without `title:`; the H1 lives at the top of the `(intro)` section since the splitter only breaks on `##`/`###`).
3. Humanized filename stem (`file_path.stem` with `-`/`_`→space) — covers PDFs (no frontmatter) and H1-less markdown like FSM_GAIN_SCHEDULER.

### Header assembly — `build_chunk_header(doc_title, section_heading)` (new, in `parse.py`)

Returns the prefix string (or `""` for a clean no-op):
- Treat `""` and `"(intro)"` as "no heading".
- `title` + heading, distinct → `"{title} > {heading}"`; heading absent or `== title` → `"{title}"`; no title and no heading → `""`.
- Strip leading `#`s from the heading so the header reads cleanly.

The embed input is `f"{header}\n\n{body}"` when `header` is non-empty, else `body` unchanged. (Markdown first-sub-chunks already begin with their `##` heading, so the header mildly reinforces it; continuation chunks gain both title and section — the gap we are closing.)

### Integration point — `pipeline.py:335-352`

`frontmatter_meta` and `pages` are already in scope where `chunk_text` is called. Resolve the title once per file, then set a dedicated field on each chunk (do **not** mutate `text`):

```python
raw_chunks = chunk_text(pages, max_tokens=max_tokens, overlap_fraction=overlap_fraction)
if cfg["embed"]["chunking"].get("contextual_header", True):
    doc_title = resolve_doc_title(frontmatter_meta, pages, file_path)
    for c in raw_chunks:
        header = build_chunk_header(doc_title, c.get("section_heading", ""))
        if header:
            c["embed_text"] = f"{header}\n\n{c['text']}"
```

`embed_text` rides through the existing `enriched = [{**metadata, **chunk} …]` merge (`:352`). The empty-chunk gate (`:356`) still keys off `text`, so behaviour there is unchanged.

### Embed call sites — `embed.py upsert_chunks`

Embed `chunk.get("embed_text") or chunk["text"]` at **both** the sparse (`embed.py:248`) and dense (`:276`/`:289`) calls; continue to store `chunk["text"]` (not `embed_text`) in the payload. Net effect: dense **and** BM25 vectors see the header; the stored excerpt stays raw. When `embed_text` is absent (flag off, or a pre-existing chunk), the calls fall back to `text` — fully back-compatible.

### Config surface — `config.py` DEFAULTS

```yaml
embed:
  chunking:
    contextual_header: <swept-best>   # provisional true, pending the eval below
```

Deep-merge (`config.py`) means existing project configs inherit the key without edits. The shipped default is the value chosen by the eval, not guessed here.

### Data flow (unchanged except the embed input)

```
extract (pages, frontmatter_meta)                                  (unchanged)
  → chunk_text → raw_chunks                                        (unchanged)
  → resolve_doc_title + build_chunk_header → chunk["embed_text"]   (← new)
  → upsert_chunks: embed (embed_text or text); store text          (← embed input only)
search: query embed + fusion + rerank                              (unchanged)
```

## Testing (TDD)

Unit (no Qdrant/Ollama), in `carta/embed/tests/`:
1. **Title tiers:** frontmatter `title` wins; H1 fallback; filename fallback when neither.
2. **Header formatting:** title+heading → `"T > H"`; `(intro)`/empty heading → title only; title==heading dedupe; no title & no heading → `""`; leading `#`s stripped.
3. **Assembly:** with flag on, every non-empty chunk gets `embed_text` = header + body; `text` is untouched; flag off → no `embed_text`.
4. **Embed selection:** `upsert_chunks` embeds `embed_text` when present (assert via a fake embedder capturing inputs) and the **payload stores `text`**; absent → embeds `text` (back-compat).
5. **No-op guard:** a title-less, heading-less chunk produces byte-identical embed input to today.

Full suite green on 3.10–3.12 before PR, per house practice.

## Validation — eval (deliverable: the shipped default)

Re-embed the ET-embed **text** collection with the flag on (visual/ColPali untouched), then measure against the already-captured flag-off baseline (this corpus = current main = dense r@5 0.726 / text-fused r@5 0.887). Run from the ET-embed root with the unreleased checkout (`PYTHONPATH=<carta-checkout> ~/.local/pipx/venvs/carta-cc/bin/python -m carta eval …`, `caffeinate -ims` for long runs):

| Probe | metric | direction |
|---|---|---|
| `/tmp` depth-check (per-lane, all 62) | **dense-only r@5** (+ fused r@{5,10,20}) | leading indicator — expect dense ↑ from 0.726 |
| `carta eval et-embed.yaml -k 5` (hybrid-alone) | recall@5 / MRR | headline — beat 0.887, net-positive per-query (see rule) |
| `carta eval et-embed.yaml -k 10` / `-k 20` | recall@k | confirm depth movement |
| `carta eval et-embed-datasheets.yaml` (14q) | recall@5 | **must not regress** (header is text-side; sanity guard) |

**Success / decision rule (aggregate-first):**
- Ship default `true` if dense r@5 rises meaningfully **and** text-fused r@5 > 0.887 with net-positive per-query movement (gains − losses > 0, no systematic regression).
- If neutral or negative, **revert** (default `false`; re-embed ET-embed from `main`) and escalate to the next lever (chunk-size 800→400, then query-expansion). Record either outcome.

Before locking the shipped default, grow the eval by ~15–20 fresh queries (underrepresented doc types, **not** the 4 named misses) to confirm the win generalizes — the validation gate agreed during brainstorming. Record the full table in `RESULTS.md` (dated) and update the `et-embed-eval-workflow` project memory with the new baseline.

## Rollout / risk

- **Re-embed required to adopt** (corpus migration); idempotent and revertible. ET-embed re-embed is approved as the test bed.
- **Excerpts/hook unaffected** — payload `text` stays raw (a deliberate benefit of embedding `embed_text` rather than mutating `text`).
- **BM25 stats shift mildly** — title terms now recur across a doc's chunks. Likely net-positive for doc-name queries; measured by the eval, not assumed.
- **Token budget negligible** — the header is a handful of tokens against 800-token chunks; no truncation risk under nomic's 2048 context.
- **Fail-safe shape** — title resolution and header building are pure string ops with empty-string fallbacks; the embed path falls back to `text`. Nothing on this path can raise or break search.

## Future levers (out of scope, documented)

- **Chunk size 800→400** — sharper, less-diluted dense vectors; shares the re-embed cost. Natural second experiment if the header underdelivers or as a combined A/B once header is attributed.
- **Query expansion** — query-time acronym/synonym help for vocabulary-mismatched queries; no re-embed.
- **`related:` graph** — promising given how cross-linked these docs are, but needs rerank-on (or rerank-free promotion) and richer edges (body links, not just frontmatter `related:`).
- **Embedding-model upgrade** — the escalation if the dense lane stays weak after representation fixes.
