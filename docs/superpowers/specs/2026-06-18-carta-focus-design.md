# `carta focus` — focused, file-scoped retrieval — design

- **Date:** 2026-06-18
- **Status:** approved (pending spec review)
- **Issue:** follow-up from the search-result-dedup work (#73) and the first-stage-recall thread (#19) — the broad search now finds the right *file*; this adds the "go deep in that file" half.
- **Scope owner:** Ian

## Context

Carta has exactly **one** retrieval surface today: `run_search` (`carta/embed/pipeline.py:1776`)
fans a query across every project collection (text + visual), fuses by RRF, and — since
the v0.12.4 dedup default (#73) — collapses results to **one chunk per file**. That
default is tuned for **breadth** ("which documents are relevant"), which is the *opposite*
of what an agent needs once it has located the right file and must extract a specific
answer from it (**depth** — several passages, a register table, a circuit figure, all from
the *same* doc).

Three concrete gaps motivate a focused mode:

1. **No way to aim at one file.** There is zero payload filtering anywhere in the pipeline
   (verified). An agent cannot say "search, but only inside `datasheet.pdf`."
2. **Page anchors are computed but dropped.** Every chunk is stamped with `page` and
   `section_heading` at chunk time (`carta/embed/parse.py:204`), and the upsert stores
   *all* chunk keys except the raw text (`carta/embed/embed.py:256`) — so `page` and
   `section_heading` are sitting in the Qdrant payload right now. But `run_search` returns
   only `{score, source, excerpt, type, doc_type}` (`pipeline.py:1930`), dropping them.
   The agent is told *which file* but not *which page*.
3. **Tables/figures extract poorly as text.** A register bit-field table or pin-out
   extracts as scrambled linear text (column order and merged cells collapse), so even a
   correctly-retrieved table chunk is untrustworthy as text. The agent wants to *see* the
   page. Carta already renders and caches page PNGs for ColPali
   (`carta/embed/colpali.py:443`), and PyMuPDF (an existing dependency) can render any
   page on demand.

### The agent workflow this serves

The realistic agent loop for a technical datasheet is two-step, and step 3 is the missing
rung:

1. **Locate** — `carta search "gyro sensitivity"` → returns the relevant file.
2. **Orient/Deepen** — focus on that file: get many page-anchored passages, an outline of
   its sections, and the register-table page *as an image*.
3. **Verify** — open the exact page(s) at full fidelity (the agent's own PDF reader, or
   the image Carta returns) and answer.

Carta's highest-leverage role is **routing the agent to the exact page**, not
re-implementing PDF reading. The data to do so already exists; this feature surfaces it.

## Goal

Add a distinct **focus** capability — `carta focus` (CLI) and `carta_focus` (MCP) — that,
given a file, returns deep, page-anchored results from *that file only*, with table/figure
pages delivered as images. Plus a shared foundation: surface `page` + `section_heading` on
**every** search result (broad included).

### Non-goals

- **No re-embed, no chunking/embedding changes.** This is a read-path feature over the
  existing index.
- **No table-vs-prose classifier for text chunks.** "Is this text chunk a table?" is not
  detectable without new analysis; the visual lane already isolates image-heavy pages and
  that is the signal we use (see Mechanism). The residual "table embedded only as text"
  case is a known limitation (Follow-ups).
- **No broad-search ranking changes.** Anchor surfacing is pure metadata; selection/order
  is untouched.
- **No fuzzy file resolution in v1.** `source` is matched exactly (with one normalization,
  below). Substring resolution for human CLI use is a noted follow-up.

## Mechanism

Two independently-shippable pieces. The engine is shared; focus is a thin orchestration
over the same per-collection query/scroll helpers `run_search` already uses, with a file
filter and dedup off.

### Piece 1 — Anchor surfacing (shared foundation, benefits all search)

In the per-collection result builders inside `run_search`, add `page` and
`section_heading` to each result dict, read straight from the payload:

- Text hits (`pipeline.py:1930-1938`): `"page": payload.get("page")`,
  `"section_heading": payload.get("section_heading", "")`.
- Visual hits (`pipeline.py:1881-1889`): `"page": payload.get("page_num")` (already read
  for the `source` string), `"section_heading": ""`.

Purely additive — existing consumers read results by key, so extra keys are inert. Broad
search immediately gains "≈ p.47, §6.3" context. No ranking, selection, or count change.

### Piece 2 — `run_focus` (the focused engine) with three modes

`run_focus(source, cfg, *, query="", limit=_FOCUS_DEFAULT_LIMIT)` resolves the
collections exactly like `run_search` (`get_search_collections(cfg, "repo")`), but every
Qdrant call carries a **file filter** on the `file_path` payload key
(`models.Filter(must=[FieldCondition(key="file_path", match=MatchValue(value=source))])`).
The same key exists on both text and visual points, so one filter scopes both lanes.

- **Mode A — Outline (`query` empty, `pages` None).** `client.scroll` each collection with
  the file filter and `with_payload=True` (no embedding call), collect
  `(page, section_heading, chunk_index)`, and return the distinct sections in page order —
  a synthetic table of contents straight from the payloads. This is the cheapest possible
  fix for "which page?": the agent pulls the structure, then picks where to read.

- **Mode B — Deep query (`query` set).** Same retrieval path as `run_search` (hybrid
  BM25+dense with RRF, optional rerank), but: file filter on; **dedup forced off** (every
  text passage of one file shares the same `source`=`file_path`, so dedup would collapse
  all of them to one — off is mandatory, not a tuning choice); limit defaults to
  `_FOCUS_DEFAULT_LIMIT` (**15**); **no cross-file graph expansion** (a single file makes
  it a no-op); and the **visual cap is disabled** (`visual_max_ratio=1.0`) so the file's
  table/figure pages are *not* throttled the way broad search suppresses them — surfacing
  those pages is the entire point. Returns up to `limit` page-anchored passages from the
  one file; visual-lane hits (the file's image-heavy pages — exactly its tables/figures)
  carry a rendered page image (see Piece 3). Mode B is only *legible* once Piece 1 ships:
  with one shared `source`, `page`/`section_heading` are what distinguish the passages —
  so anchor surfacing is a hard prerequisite, not just a nicety.

- **Mode C — Explicit page render, DEFERRED to a follow-up.** Adds a `pages` parameter
  (e.g. `pages="47-48"`) → render those pages + return their extracted text/anchors
  regardless of lane. Closes the "table embedded only as text, agent has no file access"
  case for remote MCP clients. Specified here for completeness; **not in the v1 build, and
  the `pages` param is not added until then** (YAGNI — Modes A/B cover the primary
  local-agent flow). Pulled in only if the remote case proves it necessary.

`source` normalization: a trailing `" (page N)"` suffix (the shape of a *visual* hit's
`source` from broad search) is stripped to recover the bare `file_path` before filtering,
so an agent can pass back either form verbatim.

### Piece 3 — Page-image rendering (shared helper)

`render_page_png(file_path: Path, page: int, cfg) -> bytes | None`:

1. Fast path: if a ColPali cache PNG exists (`colpali.py:443` naming,
   `pdf_cache_dir / f"page_{page:04d}.png"`), read and return it.
2. Else render on demand via PyMuPDF at the configured DPI.
3. Non-PDF source, page out of range, or render failure → `None` (caller degrades to
   anchors-only for that hit). Pure of project state beyond reading the source file.

This makes images work even on text-only projects that never ran ColPali; the cache is an
optimization, not a requirement.

## Components & interfaces

### `run_search` change (Piece 1)
Add `page` + `section_heading` to both result builders. ~2 lines each. No signature change.

### `run_focus(...)` (new, `carta/embed/pipeline.py`)
Returns `list[dict]`; each hit:
`{score, source, page, section_heading, excerpt, type, doc_type, image_b64?}`.
- `image_b64` present only on visual hits (Mode B) and only when `render_page_png`
  succeeded. The **engine** produces base64 (a transport-neutral form); the CLI surface
  rewrites it to a file path (below).
- Outline mode (A) returns hits with `excerpt=""`, `type="outline"`, populated
  `page`/`section_heading`.

### `render_page_png(...)` (new, `carta/embed/pipeline.py` or `carta/embed/parse.py`)
As specified in Piece 3.

### `file_path` payload index (new, lazy + idempotent)
Filtering is *correct* without an index (Qdrant full-scans), but a keyword payload index
makes it fast. `run_focus` lazily ensures it
(`create_payload_index(..., field_name="file_path", field_schema="keyword")`, ignoring
"already exists") on existing collections — no re-embed — and collection creation adds it
going forward.

### MCP — `carta_focus` (new tool, `carta/mcp/server.py`)
`carta_focus(source: str, query: str = "", top_k: int = 15) -> list[dict]`. Returns the
`run_focus` shape with `image_b64` inline for visual hits — the calling agent sees the
page without any filesystem access. Tool description explicitly frames it as the
go-deep-in-one-file companion to `carta_search`, and notes the no-query outline mode.
Mirrors the existing tool registration pattern (`carta_search`/`carta_embed`/`carta_scan`/
`carta_remember`).

### CLI — `carta focus` (new subcommand, `carta/cli.py`)
`carta focus --source PATH [query ...] [--limit N]`. `cmd_focus` calls `run_focus`, then:
- prints score / page / section / excerpt per hit;
- for visual hits, **writes the PNG bytes to `.carta/cache/focus/<slug>-p<N>.png` and
  prints the path** (terminals can't render inline images; a path is exactly what an agent
  consumes — it `Read`s the PNG at full fidelity). The transient `image_b64` is not printed
  raw.
- Respects the `doc_search` module flag, matching `cmd_search` (`cli.py:289`).

## Result shapes (summary)

| Mode | Trigger | Per-hit keys | Image |
|------|---------|--------------|-------|
| Broad (existing) | `carta search` | `+ page, section_heading` (new) | none |
| Outline (A) | `focus`, no query | `page, section_heading, type="outline"` | none |
| Deep (B) | `focus` + query | full + anchors | visual hits: `image_b64` (MCP) / path (CLI) |

## Error handling (fail-open, house style)

- `source` not found / file never embedded → empty list + a clear stderr/`note` line, not
  an exception.
- File deleted since embed → anchors still returned from the index; `render_page_png`
  returns `None`, image silently omitted.
- PyMuPDF render failure / page out of range → anchors-only for that hit.
- Non-PDF (markdown) source → no images; passages + anchors only.
- Per-collection fetch errors reuse `run_search`'s existing skip-on-404 /
  raise-on-transport handling.

## No-regression argument

- **Broad eval (62-query ET-embed, recall@5 ≥ 0.984).** Piece 1 adds payload-derived keys
  to result dicts without touching fetch depth, fusion, dedup, cap, or truncation —
  ranking and the returned doc set are identical. Recall is therefore unchanged by
  construction; confirmed by re-running `carta eval .carta/eval/et-embed.yaml -k 5`.
- **Consumers tolerate extra keys.** The hook, eval, and MCP/CLI formatters read results
  by key, not by exact dict shape; added keys are inert. Verified during Phase 1.
- **`run_focus` is new and additive** — no existing call path changes behavior.

## Config & defaults

No new config section required.
- `_FOCUS_DEFAULT_LIMIT = 15` — module constant in `pipeline.py` (overridable per call via
  `top_k` / `--limit`; promote to config only if a tuning need appears — YAGNI).
- Image cache under `.carta/cache/focus/` (CLI only).
- Focus honors the existing `modules.doc_search` flag; no new toggle.

## Testing (TDD)

1. **Piece 1 (unit, mocked Qdrant):** text and visual result builders surface
   `page`/`section_heading` from payload; absent keys degrade to `None`/`""`; broad
   `run_search` output is otherwise byte-identical (selection/order unchanged).
2. **`run_focus` file filter (integration, mocked Qdrant):** results come only from
   `source`; dedup is off (multiple chunks of one file returned); `_FOCUS_DEFAULT_LIMIT`
   honored; cross-file graph expansion not invoked.
3. **Outline mode (A):** empty query → distinct `(section_heading, page)` in page order via
   `scroll`, no embedding call issued.
4. **`render_page_png` (unit):** ColPali cache hit returns cached bytes; cache miss renders
   via PyMuPDF; non-PDF / out-of-range / failure → `None`.
5. **`source` normalization:** `"foo.pdf (page 12)"` and `"foo.pdf"` both filter to
   `foo.pdf`.
6. **Surfaces:** `carta_focus` MCP returns `image_b64` for a visual hit; `carta focus` CLI
   writes a PNG to cache and prints its path; both respect `doc_search` disabled.
7. **Acceptance:** broad eval recall@5 unchanged (≥ 0.984); a focused fixture
   (answer-spans-multiple-chunks; answer-in-a-table) returns the relevant passages with
   correct page anchors and an image for the table page.

## Phasing

1. **Anchor surfacing** on all search results + tests (tiny; independently shippable; the
   broad search benefits immediately).
2. **Focus engine** — `run_focus` Modes A/B, `render_page_png`, lazy `file_path` payload
   index + tests.
3. **Surfaces** — `carta_focus` MCP tool + `carta focus` CLI + tests + docs (CLAUDE.md
   "Carta surface" table, README, AGENTS.md).

## Risk & rollback

- **Risk:** unindexed `file_path` filter is slow on large collections. *Mitigation:* lazy
  keyword index; filtering is correct (if slower) even before it exists.
- **Risk:** on-demand PyMuPDF render adds latency on image hits. *Mitigation:* ColPali
  cache fast-path; render only visual-lane hits (≤ the visual share of `limit`), lazily.
- **Risk:** extra result keys break a consumer. *Mitigation:* Phase 1 audit of hook/eval/
  formatters; keys are additive.
- **Rollback:** the feature is new surface area — removing the `carta focus` / `carta_focus`
  entry points disables it with no effect on broad search. Piece 1 is a metadata add with
  no behavioral coupling.

## Out of scope / follow-ups (tracked, not in this build)

- **Mode C — explicit `--pages` render** (remote-MCP agent needs a text-only table page as
  an image). Cheap to add; deferred until the remote case demands it.
- **Substring / fuzzy `source` resolution** for human CLI use (resolve a unique substring
  against indexed `file_path`s).
- **Broad search → focus hint**: have `carta search` results suggest the focus follow-up
  (e.g. an injected "↳ `carta focus --source X` to go deeper").
- **Table-aware text chunks**: a classifier flagging table chunks so text-lane table hits
  can auto-attach images (needs new analysis at embed time).
