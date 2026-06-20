# Changelog

All notable changes to **carta-cc** are documented here. The format is loosely based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

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
