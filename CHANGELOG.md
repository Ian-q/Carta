# Changelog

All notable changes to **carta-cc** are documented here. The format is loosely based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

## [0.17.0] — 2026-08-30

**Retrieval is trustworthy again.** Five silent failures across every consumer of the search path, plus the instrumentation whose absence let them survive.

The through-line is one mistake in five places: **a number that looks like a similarity usually isn't.** Carta ranks with four incomparable scales — dense cosine (0–1), BM25, *two* RRF layers (intra-collection at `rrf_k`, then cross-collection fusion), and ColPali MaxSim, a sum over query tokens an order of magnitude larger. Every defect here is a comparison between two of them: a cosine threshold applied to RRF output in the recall hook and again in the stale scan; a merge sorting MaxSim against cosine in the MCP server.

They compounded. `carta_search` returned `[]` on every hybrid collection, which hid the fact that it also buried text under page images — so fixing the first exposed the second, and until both landed the repair was not observably delivered on any corpus with visual pages. The recall gate and the stale-scan gate were the same bug in two consumers; only one was found first. And `--trace`, the tool built to make exactly this class of failure visible, could not see the intermediate pools it claimed to report on, so it blamed ingestion for ranking losses.

Where a number's scale is now load-bearing, the code says which scale it is on and why — see the design rationale in `docs/ROADMAP.md`.

### Fixed

- **The stale-scan gate thresholded a score that was never a similarity — the same defect as the recall hook, in the other consumer.** `stale_scan` gated `hits[0]["score"] < 0.65`, but its search config leaves hybrid on, so `run_search` returns **RRF** output — `sum of 1/(k+rank)` with k=2. A candidate ranked **first in the dense lane** but absent from sparse fuses to exactly `1/2 = 0.5` and was dropped before the judge ever saw it. For this feature that is a **missed warning**, and a missed supersession is invisible: it looks exactly like no supersession. PR #123 fixed one `run_search` hook consumer; this is the other. The gate now reads the dense lane's **raw cosine** against a new `hooks.stale_scan.candidate_dense_threshold` (0.55), and a hit with no cosine — BM25-only on a hybrid collection — is **judged, never silenced**, since it cannot be measurably irrelevant and supersession often turns on an identifier only the sparse lane matches. `candidate_threshold` is now **accepted but ignored**, so existing configs do not silently keep a mis-scaled value. Deliberately *not* ported from the recall hook: its lane-agreement clause. Agreement is evidence of topicality, which is what proactive recall wants; supersession wants the **contradicting** document, which frequently agrees in one lane only.
- **The scan could not have worked at any threshold, because it only ever saw 5 candidates.** It called `run_search` with no depth override and so inherited `search.top_n` (5) — a superseding document fused at rank 6–29 was retrieved, embedded and ranked, then truncated away *before* the gate. The relevance floor could have been tuned perfectly and still recovered nothing. The scan now searches to `hooks.stale_scan.candidate_depth` (30); judge cost is unchanged at one call per chunk. Relatedly, the gate no longer reads `hits[0]` alone: `score` is intra-collection while ordering comes from `fused_score`, so the first hit is not necessarily the best candidate — it now takes the best *plausible* candidate in fused order.
- **A stale-scan cut short by its judge budget was indistinguishable from a clean one.** The `max_judge_calls` overflow notice sat *after* an early return taken when there were no findings — so a scan that burned all 30 judge calls, found nothing, and skipped the rest of the diff printed exactly what a clean scan prints: nothing. That inverts the whole point of the counter, and the feature's own design doc states the requirement ("No silent caps: `max_judge_calls` overflow is surfaced"). Worse, `scan_claude_md` never propagated `skipped_overflow` at all, so `carta claude-md check` could not report it in any circumstance — and since it joins every unpinned section into one document, a large CLAUDE.md exceeds the cap routinely, leaving `/claude-md-sync` to read a truncated scan as "in sync". Coverage caveats now print regardless of findings, are carried through the JSON, and the skill is told to treat them like `judge_errors`: a `0 findings` result alongside one is not a clean bill of health. (The gate fix above is what makes this branch common — it widens what reaches the judge.)
- **Note on #84.** Its precision validation was measured with this loss in place, and `eval_supersession.py` calls the judge directly with hand-written excerpts — it never exercises `run_search` or the gate. So that result speaks to curated candidates, not to the weaker single-lane candidates the corrected gate admits. The floors on both surfaces remain **uncalibrated**; `max_judge_calls` is the real cost bound.
- **`carta search --trace` blamed ingestion for ranking losses.** The trace advertised that it distinguishes the stages a result can be lost at, but `format_trace` was handed only the **final** list — every intermediate pool inside `run_search` is a local that dies with the call. So a document that was retrieved, embedded, fused and merely out-ranked was byte-for-byte indistinguishable from one that is not in the corpus, and the tool printed `never entered retrieval: check ingestion, not ranking.` — sending the operator to the embed pipeline for a **ranking** problem. A confidently wrong diagnosis is worse than none, and this is the exact failure class the trace was built to remove. `run_search` now takes a keyword-only `trace_stages` out-param recording a snapshot at each narrowing it actually performs; the trace names the stage that dropped the document and shows the ranks proving it was there. Two corrections to the original report while fixing it: **dedup is not a loss stage** for tracing (it keys on `source`, the same field the needle matches, so a collapsed duplicate always leaves a twin that still prints — it changes rank, never removes a traceable document), and the **dominant** loss is plain `top_n` truncation, which the report omitted entirely — with shipped defaults most of a 30-deep pool exits there. Rerank truncation is a sixth stage named in neither the docstring nor the design spec. Stages that did not run are omitted rather than recorded empty, so a loss is never attributed to a narrowing that never executed. **Without stage data the trace no longer asserts a cause it cannot establish.** Two narrowings needed care because a snapshot of their *output* cannot say why a document is missing from it: `_apply_visual_cap` enforces the visual quota **and** hard-truncates to `top_n`, so treating it as a stage made every plain rank loss print as "dropped by the visual cap" — the wrong knob; it now reports exactly the hits the quota rejected, and that membership overrides the stage verdict. Likewise a document beyond `rerank.candidate_pool` is never scored by the cross-encoder at all, so it is now reported as cut by the `candidate_pool` slice rather than blamed on reranker quality.
- **The `--trace` collections line named collections that were never searched.** It was recomputed from config after the fact, so a collection that 404'd, or a visual collection skipped as not-ready, was still reported as queried. It now reports the collections that actually returned a lane.
- **`carta hook check` could not see its own blind spot.** `stale_scan` calls `run_search` with no depth override, so it only ever sees `top_n` (default 5) fused results — a superseding document ranked below that never reaches the judge **at any threshold**, and a missed warning is invisible by construction. The scan now counts candidates that were retrieved and fused but cut before the gate, and reports the count. This is measurement only: #121 owns lifting the ceiling.
- **`carta_search` (MCP) buried every text hit under page images.** The tool merged all collections with one score-descending sort, but its two producers stamp `score` from incomparable metrics: text hits carry dense **cosine** (~0.4–0.8) and visual hits carry ColPali **MaxSim**, a sum over query tokens (~10–40). One comparator over both means every visual page outranks every text chunk, so on any project with a populated `_visual` collection `carta_search` returned nothing but page images regardless of the query. This is the same defect `run_search` fixed for the CLI in 0.12.0 (#36) — the MCP server has an independent merge path and never received it. It stayed invisible until now because the collections it would have buried were themselves returning `[]` (the `using=` bug above): **fixing that search bug is what exposed this ranking one**, and until now the MCP repair was not observably delivered on any corpus with visual pages. MCP now fuses through the same `_rrf_merge_collections` the CLI uses — rank, never magnitude — with text lanes passed first so ties break toward text. Two consequences worth stating plainly: **cross-collection ordering on the MCP path is now rank-based for text-vs-text too** (a rank-0 hit in `_notes` no longer beats a rank-0 hit in `_doc` on cosine alone), matching the CLI's eval-validated behaviour on a surface no eval currently covers; and the visual **cap** now applies to MCP, borrowed from `search.fusion.visual_max_ratio`. That 0.2 was swept against `run_search`'s 30-deep, deduped, hybrid pool — the MCP path is dense-only and undeduped, so on this surface it is a **borrowed** number, not a calibrated one. `carta_search` does **not** adopt `_dedupe_by_source`; that is a separate, separately-evaluated behaviour change.
- **A shallow `carta_search` could return no page image at all.** The visual cap is `round(visual_max_ratio * limit)`, a fraction of the *requested* depth — and on the MCP path that depth is chosen by the calling agent, not by config. At 0.2 the cap rounds to **0** for `top_k` 1–2, suppressing the visual lane outright on exactly the narrow questions where one page image is the whole answer. The cap now keeps a floor of one slot when the visual lane has hits (`visual_floor`, new and defaulted to 0, so the CLI's swept behaviour is untouched).
- **`carta_search` (MCP) returned nothing on every hybrid collection.** Named-vector collections — the schema every current `carta init` creates — require `using=` on `query_points`; without it Qdrant answers **400**. The 400 was caught as a bare `RuntimeError` and skipped as "this collection is missing", so the tool reported *no results* rather than an error, for every query, in every project embedded since hybrid landed. Two failure modes were being conflated under one exception type; they are now distinct (`CollectionMissing` → skip, the next collection may still answer; `QdrantQueryError` → surface, since a query failure is identical for every collection). Relatedly, a genuine 404 is now classified on the HTTP **status code** rather than by matching `"not found"` in the message — an unrelated 500 or a proxy error page whose body happens to contain that phrase was being silently swallowed as a missing collection.
- **The proactive-recall hook thresholded a score that was never a similarity.** With hybrid on, `run_search` returns **RRF** scores whose scale depends on `rrf_k` and lane count — at k=2 the ceiling is `1/2 + 1/2 = 1.0` — but the hook compared them against `high_threshold` 0.85 / `low_threshold` 0.60, calibrated for **cosine**. Most real hits scored below 0.60 and were dropped in silence, and the fast path fired only at exactly 1.0 (a hit ranked first in *both* lanes). The gate now reads two different signals for two different questions: **lane rank for agreement, the dense lane's raw cosine for relevance.** Measurably-low dense cosine (`< low_threshold`) → silent; else top-`agree_rank` in both lanes → inject; else the small Ollama judge. The asymmetry is deliberate — a top hit with no dense score (BM25-only) is never silenced, only judged, because dropping it is the exact bug being fixed. Non-hybrid collections still return a plain cosine and keep the legacy thresholds unchanged.
- **Hook traces are written outside the repo.** An earlier build on this branch wrote them to `<repo>/.carta/traces/`, and the record's `query` is the prompt **verbatim** for prompts ≤500 chars. `.carta/` is a tracked directory in carta projects, `_update_gitignore` runs only at `carta init` (never on update), and the hook shim execs the *globally installed* `carta-hook` — so a `pipx upgrade` would have started dropping prompt text into a directory existing projects' `.gitignore` had never heard of. They now live at **`~/.carta/traces/<project>/hook-YYYY-MM.jsonl`**, machine-level state alongside `~/.carta/registry.json`, where no repo can commit them.

### Added

- **`carta search --trace <substring>`** — per-stage ranks for every result whose path matches: BM25 rank, dense rank, the dense lane's raw cosine, intra-collection RRF score, cross-collection `fused_rank`/`fused_score`, and the final output position. The three score-shaped numbers are labelled as three different things, because they are. A retrieval miss can happen at five stages (never retrieved, one lane only, demoted by fusion, collapsed by dedup, dropped by the visual cap) and they are indistinguishable from the outside; this says which one. A document that was never retrieved at all reports so explicitly — that is an ingestion problem, not a ranking one.
- **Hook calibration traces** — one JSONL record per invocation (query, collections, hit count, the top hit's lane ranks, `dense_score`, `score` and `fused_score`, zone, judge verdict, latency) at `~/.carta/traces/<project>/`. This is the data the gate's thresholds should be tuned against rather than by feel (#118), so the record includes `dense_score` — the number `low_threshold` actually gates on. Without it a record could not explain its own `zone`. Rotates monthly by the record's own timestamp. Fail-open: any error in emission is swallowed, and the hook still exits 0.
- **`proactive_recall.trace`** (default `true`) — switch trace emission off. A config predating the key still traces.
- **`search.hybrid.rrf_k`** (default `2`) — the RRF damping constant is no longer hardcoded. `2` is what Qdrant's server-side fusion used, so the default is behaviour-identical; the literature more often uses 60. **Changing it shifts ordering — do it against an eval, not by feel.**
- **Per-lane ranks and scores on every hybrid hit** (`lane_ranks`, `dense_score`, plus `fused_score`/`fused_rank` from cross-collection fusion). These make `--trace`, the trace record and the new gate possible. Both visual branches and the MCP text path build their hit dicts inline and carry none of these fields, so every consumer reads them with `.get()`.

### Notes

- **Retrieval ordering is unchanged.** Dense+sparse fusion moved client-side (Qdrant's server-side `FusionQuery` cannot report per-lane ranks) but reproduces the server's RRF at k=2 exactly; verified against the previous implementation over 20,000 randomised lane configurations — zero differences in order or score. The recorded fields are additive.
- The old gate's "silent" zone was **structurally unreachable**: `hits[0]` is the fusion argmax, and a hit deep in both lanes (`1/5 + 1/5 = 0.4`) can never outscore a dense rank-0-only hit (`1/2`). Over 20,000 randomised fusions the deepest "best lane rank" ever seen at `hits[0]` was 2. All three zones are now reachable, and there are tests driving real fusion output through the real gate that prove it.

## [0.16.1] — 2026-07-30

### Fixed
- **`carta-hook` no longer imports torch on every prompt.** The hook loads `run_search` from `carta.embed.pipeline` on *every* prompt and blocks submission. A module-level `from carta.embed.colpali import is_colpali_available` pulled in torch + transformers transitively — **purely to read a boolean** — whether or not ColPali was enabled. The hook explicitly forces ColPali off and still paid it. Measured on a torch-capable interpreter: `import carta.embed.pipeline` **4.29 s → 1.22 s**, modules loaded 3207 → 1111. **~3.1 s saved per prompt, in every project.** Same class as the 0.7.1 fix one layer lower: 0.7.1 stopped ColPali *loading a model* per prompt; it never stopped the module being *imported*. Every other colpali import in `pipeline.py` was already function-local — line 41 was the lone holdout.
- **Four drainer tests were asserting nothing.** They patched `pipeline.is_colpali_available` with `raising=False`; once that import became function-local the attribute vanished from `pipeline`, and `raising=False` silently made the patch a no-op, so the tests exercised the real function and `run_visual_embed` bailed early. Repointed to `carta.embed.colpali` (the module that defines the name) with `raising=False` dropped, so a stale target fails loudly.
- **`CLAUDE.md` misdocumented `CARTA_QDRANT_URL` / `CARTA_OLLAMA_URL`** as general overrides. They are **seed values**, read only by `bootstrap.py` at `carta init` and by `preflight.py`; `load_config` never consults them. Setting one at runtime does not redirect `carta search`, the MCP tools, or the hook — those follow `.carta/config.yaml`.

### Added
- **A guard for the silent-no-op monkeypatch class.** `test_no_monkeypatch_targets_a_missing_module_attribute` scans every test module for `raising=False` setattrs whose attribute is absent from the target. This defect had hit `test_visual_drainer.py` twice; it now reports the exact `file:line` instead of surfacing as an unrelated assertion failure three layers downstream.
- The lazy-import invariant is pinned by a **static (AST)** test rather than a `sys.modules` probe. A runtime check passes vacuously wherever torch is absent — including CI and any environment without the `[visual]` extra — i.e. it would have guarded nothing exactly where the regression is cheapest to reintroduce.

## [0.16.0] — 2026-07-30

Demand-driven deep scanning: a document can now be marked high-priority mid-session and get treatment a one-size pipeline cannot justify for every page.

**The incident that motivated it.** A vector-CAD supplier schematic sat in a corpus as `status: embedded` with **one chunk** — its title block — and `visual_pages: 0`. Its labels are outline text, so `page.get_text()` returned almost nothing and a text-layer search concluded, wrongly, that a component drawn plainly on the sheet did not exist. Three causes stacked: the file's last sidecar generations came from paths that never queued visual pages, the project's visual backlog was never drained, and even a completed OCR pass answers "what labels are on this sheet" rather than "what connects to what".

### Added

- **`carta flag <path> --reason "…"`** — marks a source high-priority for deep scanning. Writes `priority`, `deep_scan`, `deep_scan_reason`, `deep_scan_requested_at` to the sidecar; `carta flag` alone lists flagged docs, `--clear` removes the mark. Flagging a PDF **force-queues every page** (`visual_done` reset, `visual_pending` = all pages), because a deep scan is a redo and must not inherit a past misclassification. Paths are validated to resolve inside `docs_root` (`..` traversal included).
- **Flagged-first drain ordering** — `carta embed --visual` now processes flagged files first (oldest request first), then files under the new **`embed.visual_triage_paths`** prefixes, then everything else in discovery order. `deep_scan` flips to `done` when a flagged file drains cleanly.
- **Deep extraction tier** — flagged pages (and auto-detected vector drawings) render at **`embed.deep_scan.dpi`** (default 300) and are **tiled** (`tile_px` 1280, `tile_overlap` 0.15) with two prompts per tile: the existing transcription prompt plus a **structure prompt** that asks what the drawing shows, what connects to what, and what sits between which elements. Chunks carry `tile` and `extraction` (`transcription`|`structure`). A failed render or prompt on one tile warns and continues — it never aborts the page.
- **`PageClass.VECTOR_DRAWING`** — pages with no raster images, dense vector paths (`deep_scan.vector_min_paths`, default 50) and little extractable text (`vector_text_max_chars`, default 1000) now classify as vector drawings and route to the deep tier. This fires *before* the text-length test, so a title block can no longer push a CAD sheet into the text lane.
- **Enrichment documents** — a human- or model-authored structured extraction of a visual source (`schematic.pdf.extraction.md`). Location is per project via **`embed.enrichment.repo_visible`**: a committed sibling of the source, or `.carta/companions/` (default). Its chunks carry an `enriches` payload pointing at the source; the source's sidecar records `enrichment_path` and `enrichment_source_hash`, so **`carta status` and `carta doctor` report an enrichment as stale once the source changes** — and as **orphaned** when the source is gone or renamed.
- **`embed.vision_render_dpi`** (default 150) — the standard vision render DPI is no longer a hardcoded literal.

### Fixed

- **`colpali_scoped_paths` no longer gates OCR/vision coverage.** It silently gated both pass-1 queueing and the entire `--visual` drain, so an out-of-scope PDF got **zero** visual coverage rather than "no ColPali vectors" — a cost knob acting as a correctness switch. It now scopes only the ColPali embed step; OCR and the deep tier cover every file.
- **The fail-closed classification path says what it cost.** When page classification raises, the file still falls back to text-only extraction, but the warning now states that visual queueing was skipped and the file has no visual coverage until it is flagged or re-embedded.
- **A re-embed can no longer shrink a pending deep scan.** A text re-embed or `--repair` between flagging and draining replaced `visual_pending` with just the image-heavy pages, silently dropping exactly the pages a force-queue exists to keep. Pending pages are now unioned while `deep_scan` is `requested`.
- **Enrichment staleness cannot be stamped blind.** Recording an enrichment against a source with no computed hash (a flag-created stub, or no sidecar yet) stored an empty hash, which made the source permanently un-stale. The hash is computed from disk when missing; an unreadable source warns and stamps nothing. Relatedly, the stamp is now written only after the embed's final success accounting, so a failed upsert no longer marks an enrichment as ingested.
- **`tile_rects` cannot hang the drain.** An `embed.deep_scan.tile_overlap` of `15` (meaning 15%) or a non-positive `tile_px` produced a non-advancing step and an infinite loop — which, unlike an exception, no per-page guard can absorb. Values are clamped at the call site and rejected outright by `tile_rects`.
- **`mcp` is pinned below 2.0.** `mcp 2.0.0` (2026-07-28) removed the `mcp.server.fastmcp` module `carta/mcp/server.py` imports, and the dependency carried no upper bound — so a fresh install began failing at import with nothing in this repo having changed. The ceiling is `mcp>=1.7.1,<2`; lifting it means porting the server to the 2.x API.
- **`carta doctor` no longer suggests `--repair` for problems repair cannot fix.** Stale enrichments need the extraction re-verified against the changed source; orphaned ones need the source relocated or the doc re-pointed.

### Notes

- Existing projects are unaffected until they opt in: every new config key defaults to today's behaviour, no sidecar field is required, and non-flagged non-vector pages take the same path as before.
- The deep tier is deliberately expensive — two model calls per tile — which is why it is demand-driven rather than the default. A large sheet can be many tiles; budget an overnight drain accordingly.

## [0.15.0] — 2026-07-28

### Fixed
- **The proactive-recall hook can no longer stall prompt submission against an unreachable backend.** The hook always failed open — every path exits 0 and the prompt proceeds — so this was never a correctness bug; it was a **duration** bug. Two timeouts sat on its path: a 60 s Ollama query embed and a 10 s Qdrant client applied *per collection*, giving a worst case of `60 + 10 × n_collections` ≈ **80 s on every prompt**. That ceiling never bound in practice because the backend was on localhost, where a dead service returns `ECONNREFUSED` instantly. Pointing `qdrant_url` at a **remote host** changes the failure mode without changing a line of code: a peer that is down drops packets with no RST, so `requests` waits out the full connect timeout. The hook now runs under a wall-clock budget and stays silent when it expires. This is the prerequisite for running Carta's vector store on another machine (#106).

### Added
- **`proactive_recall.search_timeout_s`** (default `3`) — wall-clock budget covering the hook's query embed *and* its Qdrant queries together.
- **`run_search(..., timeout_s=None)`** — optional budget. Implemented as a single deadline rather than a per-call timeout, because a per-call value is not a bound: the same 3 s across one embed and N collections is 3 s × (1+N). The query embed and the Qdrant client are each clamped to the time remaining, and once the budget is spent the loop stops querying further collections and returns the results it has rather than raising — the hook's noise gate already exits silently on an empty set, so an outage degrades to silence instead of a stderr line on every prompt.
- **`get_embedding(..., timeout=60)`** — explicit per-request timeout. The 60 s default is unchanged and still suits the ingest path.

### Notes
- **No behaviour change for any caller but the hook.** `timeout_s=None` is byte-identical to previous behaviour, so `carta search`, the MCP tools and `carta eval` keep their existing timeouts — a slow explicit search over a wide candidate pool is legitimate, while a slow prompt is not. A global search timeout was considered and rejected for that reason. Pinned by `test_run_search_without_budget_keeps_legacy_timeouts`, which asserts the exact legacy values and passed *before* the implementation existed.
- An outer `ThreadPoolExecutor` wrapper is **not** a valid fix here and was not used: `__exit__` waits for the abandoned thread, so it cannot free a blocked inner call — the same reasoning already documented in `_call_ollama_judge`.

## [0.14.0] — 2026-06-20

### Added
- **OCR trust handling — diagram-OCR is marked doubted.** Every search result now carries a read-time `text_source` trust tier — `text_layer` (real PDF text), `ocr_table` (glm-ocr transcription of structured/scanned text — trusted), `ocr_visual` (llava diagram description — **doubted**) — derived from chunk payload that already exists, so it applies *retroactively* to embedded OCR chunks with no re-embed. Broad search (`carta search` CLI / `carta_search` MCP) flags `ocr_visual` hits with a caveat, and `carta focus` attaches the rendered page image to them so an agent verifies against the page rather than trusting hallucination-prone diagram prose. Trusted text-layer and table-OCR hits are unmarked. Additive metadata only — no ranking change.

### Changed
- **The diagram-OCR prompt now transcribes instead of interprets.** `LLAVA_PROMPT` (the vision-model prompt for image/diagram pages) was rewritten to transcribe visible labels, values, pin names, and reference designators exactly as printed and to *not* infer functions, designators, or values that aren't legible — eliminating fabricated facts (e.g. inventing a component's function) while preserving the findable labels (`32M Hz`, pin names, etc.). Applies to newly embedded image pages; the table-OCR path (`GLM_OCR_PROMPT`) is unchanged.

## [0.13.0] — 2026-06-20

### Added
- **`carta focus` (CLI) and `carta_focus` (MCP) — file-scoped deep retrieval.** The two-step partner to `carta search`: locate a file, then go *deep* in it. Deep mode returns up to 15 page-anchored passages from a single file (dedup off, visual cap off) so multi-part answers and register tables aren't collapsed away; an empty query returns the file's section/page outline — a synthetic table of contents straight from the payloads, no embedding. Table/figure pages come back as images: the MCP tool returns base64 inline, the CLI writes the PNG to `.carta/cache/focus/` and prints the path — rendered on demand via PyMuPDF with the ColPali page cache as a fast path. The engine reuses the existing hybrid/RRF/rerank query path with a `file_path` payload filter on every lane; read-path only, no re-embed and no change to broad-search ranking.
- **Page and section anchors on every search result.** `carta search` (CLI) now prints ` p.N §heading` when present (omitted for page-less hits), and `carta_search` (MCP) returns `page` + `section_heading`. The data was already stamped into the Qdrant payload at chunk time; search now surfaces it, so even broad results point at "≈ p.47, §6.3" instead of just naming the file.

## [0.12.4] — 2026-06-18

### Fixed
- **`carta search` returned duplicate-chunk results, burying distinct docs.** `run_search` truncated the fused candidate pool to `top_n` without de-duplicating, so several high-ranked chunks of the same document — or the same visual page repeated — could fill the shown results (e.g. 3 of the top-5 being one spec doc), even when a relevant document sat just below at the 5th *distinct* position. `run_search` now fetches a deeper candidate pool, de-duplicates by source, and applies the visual-share cap (#36) against the final `top_n`. On the ET-embed eval, recall@5 **0.952 → 0.984** / MRR 0.855 → 0.875 with **zero regressions** — it recovered both the RJ45/CTS-harness and FSM-gain-scheduler docs that duplicate chunks had been crowding out (the lone remaining miss is a patent not yet OCR'd into the index). Provably non-decreasing at the doc level. Gated by `search.dedupe_results` (default on).

## [0.12.3] — 2026-06-17

### Fixed
Three follow-ups surfaced during the 0.12.2 ET-embed closeout.
- **Two-pass visual *queueing* ignored `colpali_scoped_paths`.** 0.12.2 fixed the `--visual` *drain* to skip out-of-scope sources, but pass-1 queueing still marked their image-heavy pages `visual_pending` — so out-of-scope docs (e.g. patents) re-queued on every `carta embed` and inflated the "N pages await visual" count with pages the drain would never process. Queueing now honors `colpali_scoped_paths` (the same check the inline ColPali path already applied), so out-of-scope files get pass-1 text only, no phantom pending pages. The zero-extractable-text OCR-rescue path stays ungated, preserving scanned docs' only route to any indexed text.
- **Scanner emitted phantom `sidecar_path_drift` for nested junk sidecar copies.** `_iter_sidecar_files` walked every `*.embed-meta.yaml` under `.carta/sidecars/`, including stray sidecar trees replicated into `.carta/sidecars/.worktrees/<wt>/.carta/sidecars/…` by worktree checkouts or imports; each copy's `current_path` was resolved against the real repo, producing false `sidecar_path_drift` / `sidecar_broken_related` findings for sources it didn't own. The walk now skips non-canonical copies (a sidecar's location under `sidecars/` must equal where its `current_path` maps), mirroring `induct.iter_canonical_sidecars`. Removes the dead `_SIDECAR_SKIP_DIRS` constant this supersedes.
- **`carta embed` auto-induction missed uppercase `.PDF`.** New-file discovery globbed `*.pdf` / `*.md` case-sensitively, so an uppercase-extension file was perpetually flagged `embed_induction_needed` by the scanner (which lowercases suffixes) yet never auto-inducted — embeddable only via an explicit `carta embed <file>`. Discovery now matches supported extensions case-insensitively.

## [0.12.2] — 2026-06-17

### Fixed
- **Visual drain ignored `colpali_scoped_paths`.** Only the inline ColPali path enforced scope; the two-pass `--visual` drain processed every queued page, so out-of-scope docs (e.g. patents) got ColPali-embedded anyway — on ET-embed ~2126 of 2252 pages, burning the slow OCR+ColPali pass on excluded docs and polluting the `_visual` collection with low-value figures. `run_visual_embed` now skips out-of-scope sources.
- **Hook judge inner timeout exceeded its outer budget.** `_call_ollama_judge` hardcoded a 4s Ollama timeout while the worker-thread budget is the configured `judge_timeout_s` (default 3s); since the executor waits for the thread on exit, the hook could block ~4s despite a 3s budget. The inner now tracks the outer budget.
- **`carta doctor --json` / `audit --json` output was corrupted** when a newer version was available: the update-available notice printed to stdout. It now goes to stderr, keeping machine-readable stdout clean.

### Changed — performance
- **MPS productionized.** `colpali_device` now defaults to `"auto"` (MPS > CUDA > CPU); `CARTA_COLPALI_DEVICE` overrides at run time; a load-time guard falls back to CPU if the GPU device can't run the model instead of failing every page. ColPali no longer runs on CPU-by-default on Apple Silicon.
- **Ollama `keep_alive`** is now set on every request (embed, rerank, hook judge, query extraction), configurable via `CARTA_OLLAMA_KEEP_ALIVE` (default `"10m"`; `"-1"` keeps models resident). Stops models reloading across idle gaps — chiefly the prompt-submit hook.

## [0.12.1] — 2026-06-16

### Fixed — scanner false positives
Follow-up to the 0.12.0 audit, cutting `carta scan` noise so the real doc-hygiene signal isn't buried. On the ET-embed corpus these two fixes removed 19 false findings (`homeless_doc` 34→18, `orphaned_doc` 6→3) with no real finding lost.
- **Exclude vendored / virtualenv / scratch dirs.** `_ALWAYS_EXCLUDED_DIRS` covered only `.claude/worktrees/`, `build/`, `temp/`, so commonly-gitignored machine dirs were still walked and reported — e.g. 15 `tmp/` scratch docs and a `.pixi/.../site-packages/` vendored file flagged as `homeless_doc`. Added `tmp/`, `dist/`, `node_modules/`, `.venv/`, `venv/`, `.pixi/`, `site-packages/`, `.tox/` (matched as a path substring, so envs nested under a subproject are caught too). These live in `_ALWAYS_EXCLUDED_DIRS` rather than `DEFAULTS["excluded_paths"]` because an existing `config.yaml` replaces `excluded_paths` wholesale, so defaults never reach installed repos.
- **Exempt READMEs from the `orphaned_doc` check.** A directory README is a navigational index discoverable by structure, not islanded knowledge — so having no inbound `related:` links is expected, not a problem (mirrors the existing homeless-check README skip). Sole-README section indexes are no longer false-flagged as orphans.

## [0.12.0] — 2026-06-16

### Fixed — pre-ET-embed reliability & correctness audit (CA-1..CA-27)
A 40-agent audit before a full corpus re-embed surfaced 27 high/critical issues; the blocker set is fixed here. Full report + disposition: `docs/audits/2026-06-16-pre-reembed-code-audit.md`.
- **Honest embed success accounting (CA-1/4/6/7).** The pipeline marked a file `embedded` from reaching the end of the run, not from confirming its vectors persisted — so a transient Ollama/Qdrant blip silently zeroed out whole files (or dropped 32-chunk batches) with a green summary and never retried. Files that don't fully persist now get a re-pickable `embed_failed`/`partial` status (re-discovered and retried on the next run, counted separately, non-zero CLI exit); transient batch upserts retry instead of dropping on first error; the write-path Qdrant client timeout is raised 5s→30s; and a non-768-dim embedding model fails loudly (`EmbedDimError`) instead of silently upserting nothing.
- **Encoding robustness (CA-13/22).** A UTF-8 BOM or a single non-UTF8 byte no longer silently drops a markdown file from the index or crashes the whole `carta scan` / graph build — markdown is read with `utf-8-sig` + `errors="replace"`.
- **`carta init` builds the hybrid schema (CA-10/27).** Init created unnamed-dense collections over raw HTTP while the embed path expects named dense+bm25; because existing collections are skipped, an init-before-embed project was permanently stuck on dense-only retrieval (BM25+RRF hybrid silently off). Init now routes through `ensure_collection` (single source of truth) and warns on a pre-existing legacy collection instead of rubber-stamping it.
- **Integrity scan & cleanup (CA-14/15).** `carta doctor` no longer reports every embedded `_notes` document as a false `sidecar N vs qdrant 0` count-mismatch (it now scrolls `_notes` and routes each sidecar's count to the collection its doc_type lands in); stale-generation cleanup (`delete_other_points`) retries transient errors and warns loudly on final failure.
- **Trustworthy verification tooling (CA-17/20/23/26).** `carta audit` scrolls **all** points via the Qdrant cursor (was silently capped at the first 1000 on real corpora); `carta eval` warns on a partial reranker fail-open and rejects blank/empty expectations that silently skewed recall; graph-aware search now uses the real repo root (it was globbing the empty `.carta` dir — a silent no-op).
- **`--timeout 0` / `file_timeout_s: 0` means unbounded (CA-3)**, matching `visual_timeout_s` — not a literal `join(0)` that instantly timed out every file and still exited 0.
- **Single-writer embed lock (CA-2/5/12).** `--repair`, `--visual`, targeted `--files`, and the MCP `carta_embed` tool now share one `.carta/embed.lock` with the full pipeline (new `carta/embed/lock.py`), so concurrent writers can't delete each other's just-written points; MCP returns a `busy` error instead of racing.

### Fixed
- **Visual pool dilution (#36).** Cross-collection RRF fusion interleaved text and visual
  hits ~1:1 by rank, so once a `_visual` collection had content ~half of every query's
  candidate pool was visual — including pure-text questions — halving effective text depth.
  A new `search.fusion.visual_max_ratio` knob caps the visual lane's share of the fused pool
  (default `0.2`; cap = round(ratio × pool size); freed slots backfill with deeper text).
  `1.0` restores the old behaviour, and pure-text corpora are unaffected. The cap lives in
  the cross-collection merge, so it lifts both the hybrid-alone results and the rerank pool.
  Measured on the 62-query ET-embed eval: hybrid-alone recall@5 **0.839 → 0.887** (3 misses
  recovered, none regressed), the 14-query visual eval held flat at 0.857, and the reranked
  path unchanged at 0.935.

### Fixed
- **Point-ID collision (data loss).** Point IDs were hashed from the filename stem only, so
  two files with the same stem (e.g. multiple `README.md` in different subdirectories) silently
  overwrote each other's Qdrant points on every embed. IDs now hash the repo-relative file path;
  visual page IDs fixed identically.
- **Stale-generation cleanup.** Re-embeds stamped old points `stale` but left them searchable
  forever. Old-generation points are now deleted after a **complete** upsert; partial upserts
  keep the previous generation and print a warning. Pass-2 OCR chunks carry the file's
  generation; `visual_done` resets on content change so affected pages re-queue.
- **Empty-chunk guard.** PDF extraction failures produced points with identical
  embedding-of-empty-string vectors (1,400+ observed in a real corpus). Empty chunks are dropped
  at upsert; files yielding zero usable text are flagged `extraction_failed` with a loud warning
  and counted separately in the embed summary.
- **Sidecar status bookkeeping.** A successful re-embed now ends with `status: embedded`
  instead of permanently `stale` (169/971 sidecars stuck in a real corpus). `carta embed FILE`
  (force) now truly re-embeds even when the file hash is unchanged.

### Added
- **`carta doctor` corpus-integrity section.** Detects slug collisions, empty-text points,
  sidecar/Qdrant chunk-count mismatches, and stuck-stale sidecars; findings merged into
  doctor's JSON output alongside the existing environment checks.
- **`carta embed --repair`.** Purges and force re-embeds files with integrity issues; fixes
  stuck-stale sidecars in place. Summary distinguishes: repaired / purged / flagged
  `extraction_failed` / queued-for-visual / failed.

## [0.10.0] — 2026-06-11

### Added
- **Note capture — the write side of session memory.** `carta_remember` (MCP tool) and
  `carta remember` (CLI) save curated project knowledge as plain markdown files with
  `doc_type` frontmatter — `quirk` → `docs/quirks/`, `bug-note`/`helpful-note` →
  `docs/notes/` (paths configurable via `memory.quirks_dir`/`memory.notes_dir`) — and embed
  them into `{project}_notes` through the standard pipeline. Notes are git-shareable repo
  docs: they show up in `carta scan`/audit, export with `carta export`, and survive
  re-embeds. Search results and proactive-recall injections label them (`[quirk] …`).
- **Frontmatter `doc_type` override.** A `doc_type:` key in markdown frontmatter now wins
  over parent-directory inference, and `quirks/` / `notes/` directories map to note types —
  hand-written notes route correctly on (re-)embed.

### Fixed
- **`collection_for_doc_type` was dead code — note types never reached `_notes`.**
  `upsert_chunks` hardcoded the `_doc` collection; it now routes by the batch's doc_type.
  `carta init` creates `{project}_notes` (instead of the never-used `_quirk`); existing
  projects need no migration — the collection is auto-created on first capture.

## [0.9.1] — 2026-06-10

### Fixed
- **The proactive-recall hook never pays reranker latency.** The hook forced ColPali off but
  passed `search.rerank` through untouched, so enabling the LLM reranker (10s+/call with a strong
  model) made every prompt submission block on a rerank call. The hook now forces
  `search.rerank.enabled` off in its search config (mirroring the colpali-off override) — you can
  enable `search.rerank` for explicit `carta search` without slowing every prompt.
- **`carta eval` can no longer mistake a silently broken reranker for a result.** Both reranker
  backends stamp `rerank_score` only when they actually ran; `run_search` now exposes that signal
  via an optional `stats` out-param, and `carta eval` prints `rerank: applied on N/M queries` and
  **exits 1** when rerank was requested but applied on zero queries. (The 0.8.0 reranker shipped
  fully fail-open and the eval reported its numbers as a win — this class of failure is now a hard
  error.) Verified live on a 62-query technical-docs eval: hybrid 0.790 recall@5 / 0.641 MRR →
  with `qwen3.5:9b` rerank 0.871 / 0.778, reported as `rerank: applied on 61/62 queries`.

### Changed
- CI workflows bumped to Node 24 action releases (`actions/checkout@v5`,
  `actions/setup-python@v6`) ahead of GitHub's 2026-06-16 forced migration.

## [0.9.0] — 2026-06-10

### Fixed
- **LLM reranker now works with reasoning models.** `search.rerank backend: llm` sends
  `think: false`, so a reasoning model (e.g. the default `qwen3.5:0.8b`) returns its answer in
  `message.content` instead of the `thinking` stream — previously `content` was empty (or the
  model thought to the context limit and timed out), so the reranker silently failed open on
  **every** query for anyone on the default config.
- **LLM reranker parser tolerates noisy replies.** A valid JSON array followed by trailing tokens,
  or wrapped in leading prose, now parses (leading-value `raw_decode` + regex fallback) instead of
  failing open. With both fixes, a strong reranker (`qwen3.5:9b`) measured recall@5 **0.750 → 0.900**
  / MRR 0.539 → 0.699 on a technical-docs corpus. The small default `qwen3.5:0.8b` is fast but can
  *degrade* ranking on harder corpora — pick the model to fit your latency/quality budget.

### Added
- **`related:` entry resolver** — search-time normalization mapping any entry style (exact path,
  missing-`docs/`-prefix, bare id/slug, `.embed-meta.yaml` drift) to a canonical repo-root path,
  with `..`-escape guarding. Builds an undirected adjacency (forward ∪ backlinks), memoized by max
  doc mtime.
- **`carta scan` `noncanonical_related` check** — flags `related:` entries that resolve only via a
  fallback tier (with the suggested canonical path) or don't resolve at all, feeding link-graph
  cleanup. `check_broken_related` is now resolver-aware so the two checks partition cleanly (one
  finding per entry, not two).
- **Graph-aware retrieval (`search.graph`, opt-in / off by default).** Undirected 1-hop `related:`
  expansion that promotes graph-adjacent deep docs into the rerank candidate pool (fail-open). The
  mechanism is verified — it pulls a rank-33 doc to rank-12, into the pool — but measured **neutral**
  on a corpus where a strong reranker already floats in-pool docs (it rescores independent of input
  order). Shipped off by default; most likely to help corpora with a rich `related:` graph and
  relevant docs that rank deep. Knobs: `enabled`, `hops`, `seed_count`, `candidate_depth`.

## [0.8.0] — 2026-06-09

### Added
- **LLM reranker backend** for `search.rerank` (`backend: llm`). A single listwise Ollama call
  reorders the candidate pool (`llm_model`, default `qwen3.5:0.8b`; `llm_timeout_s`). Opt-in — the
  default `cross-encoder` (fastembed `bge-reranker-base`) path is unchanged. **Fail-open:** any
  Ollama error/timeout/parse failure returns the fused order, so search is never worse than today.
  Measured on a technical-docs corpus: recall@5 0.700 → **0.750**, MRR 0.546 → **0.589** with
  `qwen3.5:0.8b` (a 9b model gave no recall gain — small model is the default).

## [0.7.1] — 2026-06-08

### Fixed
- **ColPali no longer segfaults on macOS.** torch-CPU dispatched ColPali matmuls to Apple's *multithreaded* Accelerate `cblas_sgemm`, which intermittently SIGSEGV'd (exit 139, "Python crashed") during `carta embed --visual`. Carta now pins BLAS to a single thread on Darwin (`carta._compat`, applied at import before torch loads) plus `torch.set_num_threads(1)` in the visual drain. Slower per page, but stable.
- **Status-line tracks the visual pass.** `run_visual_embed` (`carta embed --visual`) now drives `StatusWriter`, so the status-line widget shows live progress during the drain instead of freezing on pass-1's final state.

## [0.7.0] — 2026-06-08

### Added
- **`carta export` / `carta import`** — share a project's embeddings between machines via a portable archive (safe tar extraction, snapshot cleanup).

## [0.6.0] — 2026-06-08

### Added
- **Two-pass visual embedding.** `carta embed` extracts text fast and queues image-heavy PDF pages (`visual_pending`) instead of blocking on inline vision; `carta embed --visual` is a slow, resumable drain that runs glm-ocr (→ hybrid text index) + ColPali (→ `_visual` collection) per page. No more datasheet timeouts. (#20)
- **Status-line embed-progress widget** + `carta statusline` subcommand (segment + idempotent install/uninstall); `carta init` offers to wire it. Live `.carta/embed-status.json` written during `carta embed`. (#23)

### Changed
- **Visual search is on by default (auto).** `embed.colpali_enabled` is now tri-state: `null`/unset = **auto** (search the `_visual` collection when it exists and is non-empty), `true` = force on, `false` = hard opt-out. The readiness check runs *before* loading ColPali, so projects with no visual content pay nothing. Two-pass output is now visible to search without a separate flag. (#27)

### Fixed
- **Cross-collection result fusion.** `run_search` now fuses text (cosine/RRF, ~0–1) and visual (ColPali MaxSim, ~10–40) hits by **rank** (Reciprocal Rank Fusion) instead of incomparable raw scores. Previously visual hits crowded out every text hit when the visual collection was enabled (recall collapsed to 0); now text and visual interleave. (#21)
- **`[visual]` extra now installs `accelerate`** — transformers ≥5 routes `from_pretrained(device_map=…)` through it, so `carta embed --visual` failed to load ColPali without it. (#22)
- **Reranker over-fetch.** Fetch `candidate_pool` candidates before reranking, then truncate to `top_n`, so the cross-encoder can rescue lower-ranked relevant docs instead of only reordering the top-`n`. (#18)
- **Proactive-recall hook is text-only** — it no longer loads ColPali (~9 s) on every prompt now that visual search auto-enables. (#28)
- **Visual-drainer tests no longer require the torch `[visual]` extra**, fixing a CI failure red on `main` since #20. (#25)

### Docs
- README: measured retrieval-quality tables (hybrid 0.550→0.700; two-pass visual 0.500→0.857 on a datasheet eval) + pointers to public benchmarks (ViDoRe, BEIR/MTEB/RTEB, FreshStack). (#26)

## [0.5.0] — 2026-06-06

### Added
- **Hybrid retrieval**: BM25 sparse + dense vectors fused via Qdrant Reciprocal Rank Fusion (`search.hybrid`). Sparse encoding via fastembed (`Qdrant/bm25`). New `[hybrid]` extra. (#14)
- **Local cross-encoder reranker** second stage (`search.rerank`, opt-in) via fastembed `TextCrossEncoder` (`BAAI/bge-reranker-base`). (#14)
- **Retrieval eval harness** + `carta eval` CLI — recall@k / MRR against a query→expected-doc YAML set. (#14)
- **ColPali directory scoping** (`embed.colpali_scoped_paths`) — restrict visual embedding to globbed paths instead of all PDFs. (#17)
- **Live `vision_routing` modes** (`auto`/`ocr`/`vision`/`off`) — previously a no-op key. (#17)
- **Configurable vision/OCR call timeout** (`embed.vision_call_timeout_s`, default 300; was hardcoded 120). (#17)
- README/AGENTS guidance on relegating heavy visual models to specific directories. (#17)

### Changed
- Default vision model `qwen2.5vl:7b` → `qwen3-vl:8b` (requires Ollama ≥0.12.7). (#16)

### Fixed
- Schema-coherent hybrid upsert — never silently drops a batch into a schema-mismatched collection; `collection_is_hybrid` narrowed to 404-only. (#14)
- Sparse tests skip cleanly when the `[hybrid]` extra is absent (`pytest.importorskip`); base CI green. (#15)

> Note: 0.4.x was tagged without changelog entries; 0.5.0 consolidates the retrieval-quality + routing-control work merged since 0.4.7.

## [0.3.2] — 2026-04-05

### Fixed
- Synced `plugin.json` and `marketplace.json` versions to match package version (were stuck at `0.3.0`).
- Replaced `${user_config.*}` template variables in dev `.mcp.json` with hardcoded localhost defaults (substitution syntax only works in plugin manifests, not hand-written project `.mcp.json` files).
- Removed stale `UserPromptSubmit`/`Stop` hook registrations from `.claude/settings.json` that pointed to missing `.carta/hooks/` scripts; plugin-native `hooks/hooks.json` handles these.

### Changed
- README: removed Superpowers dependency from plugin install instructions; replaced with direct marketplace install (`extraKnownMarketplaces` → `Ian-q/Carta`).
- README: reorganised install section — plugin path is primary, pip/uvx/curl unified under "CLI install".
- README: fixed curl URL (`carta-cc/carta-cc` → `Ian-q/Carta`).
- `skills/carta-init/SKILL.md`: corrected Step 3 verification — `carta init` copies hook scripts to `.carta/hooks/` but does not write `.claude/settings.json` (plugin-native handles Claude Code hook registration).

## [0.3.1] — 2026-04-04

### Fixed
- Fixed PyPI publish failure caused by version mismatch (tag v0.3.1 vs package 0.3.0).
- Fixed CI workflow `claude plugin validate` command with correct path argument.
- Fixed `plugin.json` schema to include required `type` and `title` fields for userConfig.

## [0.3.0] — 2026-04-04

### Added
- **Native Claude Plugin Architecture** 
  - Integrated `.mcp.json` support.
  - Added auto-registration hooks in `hooks/hooks.json`.
  - Removed legacy cache and shifted to canonical `.claude-plugin/plugin.json`.
- **Multimodal Visual Search**
  - Added Visual Embeddings (ColPali / ColQwen2) using native transformers API.
  - Expanded search scope to include visual collections directly from Qdrant.
  - Implemented structured PDF chunking with dual-extraction and table preservation.
- **Bootstrapping Enhancements**
  - Added graceful degradation when Qdrant is unreachable during `carta init`.
  - Added auto-creation of `AGENTS.md` and default slash commands.

### Changed
- Reorganized test suite (moved `conftest.py` out of `carta/` to repo root).
- Migrated away from deprecated `colpali-engine` API.

## [0.2.0] — 2026-04-01

### Added
- **Collection scoping module** (`carta/search/scoped.py`) for multi-project search
  - `get_search_collections(cfg, scope)` with `repo`/`shared`/`global` scope levels
  - `discover_collections(qdrant_url)` - discovers Carta collections from Qdrant
  - `filter_by_permission()` - project filtering with `include`/`exclude`/`all` modes
  - Global collections support (`carta_global_*` collections)
- **Multi-platform MCP support**
  - `.mcp.json` for Claude Code MCP registration
  - `.opencode.json` for OpenCode MCP registration
  - Both created automatically during `carta init`
- **Scoped search in MCP tool**
  - `carta_search(query, top_k, scope)` with default `scope="repo"`
  - Searches across multiple collections and merges results
  - Secure default: only current project collections
- **Lifecycle tracking in sidecars**
  - `current_path` field for hash-based drift detection
  - `file_hash` and `file_mtime` fields
  - Generation tracking for stale document detection
  - `status` field: `embedded` | `stale` | `orphaned`
- **Vision model pipeline for PDFs**
  - PyMuPDF-based image extraction from PDF pages
  - Ollama vision model integration (LLaVA/moondream2)
  - Automatic image description generation and embedding
  - Sidecar tracking with `image_chunks` and `vision_status`
- **Document lifecycle management**
  - `mark_stale()` - marks documents as stale when content changes
  - `cleanup_orphans()` - removes orphaned chunks from deleted documents
  - Healed sidecars automatically during embed operations
- **Smart hook v0.2** (Phase 3)
  - Automatic context injection on UserPromptSubmit
  - Three-zone score routing (high >0.85, gray 0.60-0.85, low <0.60)
  - Ollama judge for gray-zone queries (3s timeout, fail-open)

### Changed
- **MCP-first architecture** - `.mcp.json` is sole registration point
  - Removed v0.1.x plugin cache system
  - Added automatic cleanup of old plugin cache on `carta init`
- **Bootstrap hardened**
  - Post-deletion assertion for plugin cache cleanup
  - Parent-glob aware .gitignore updates
  - Portable `exec` quoting for hooks
- **Updated command structure**
  - `carta-hook` registered as console script
  - Shell stubs use exec delegation pattern

### Removed
- **Plugin cache system** (v0.1.x compatibility)
  - `~/.claude/plugins/carta/` directory no longer used
  - `~/.claude/plugins/cache/carta-cc/` directory no longer used
  - Automatic cleanup on `carta init`

### Fixed
- **stdout pollution in MCP server** - all logging now goes to stderr
- **sys.exit in MCP server** - returns structured errors instead
- **Hook fail-open logic** - returns True (inject) on timeout, not False
- **Collection naming** - consistent `{project}_{type}` namespacing
- **PDF embedding** - batch upserts with per-file timeout

[0.2.0]: https://github.com/Ian-q/Carta/compare/v0.1.11...v0.2.0

## [0.1.11] — 2026-03-24

### Added

- **`docs/install.md`** — single source for pipx, venv `PATH`, `--pip-args` syntax, PlatformIO conflicts, and post-install smoke checks.
- **Embed concurrency lock** (`.carta/embed.lock`) with atomic create and stale-PID cleanup.
- **Qdrant preflight** and **per-file progress** for `carta embed`; clearer **`carta search`** messages (empty index vs Qdrant/query errors).
- **Homeless-doc** defaults: root-file whitelist (e.g. `CHANGELOG.md`, `AGENTS.md`) and anchor path basename matching.
- **Skill cache**: warnings when replacing stale plugin metadata or removing old version directories; **PlatformIO** PATH note when another `carta` exists on `PATH`.

### Fixed

- **`carta scan`** now passes `.carta/scan-results.json` into the scanner so **`changed_since_last_audit`** baselines match the file the CLI uses.
- **`run_embed`** returns structured errors on Qdrant failure (no `sys.exit` inside the pipeline).
- **`upsert_chunks`** uses a bounded Qdrant client timeout when no client is passed in.

### Docs

- **README** links to `docs/install.md`.
- **Install test guide** defers install details to `docs/install.md` and updates first-run / baseline notes.

[0.1.11]: https://github.com/Ian-q/Carta/compare/v0.1.10...v0.1.11
