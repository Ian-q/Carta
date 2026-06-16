# Carta Pre-Re-Embed Code Audit — 2026-06-16

**Goal:** surface and fix issues before a full re-embed of real corpora (ET-embed).
**Audit target:** branch `audit/2026-06-16-comprehensive` cut from `origin/main` @ `9b08b17` (includes merged PRs #54–#68). Test baseline: **982 passed, 1 skipped — green.**
**Method:** 40-agent fan-out across 12 subsystems → independent adversarial verification of every high/critical finding → maintainer re-verified the embed/data path by hand. 76 raw findings → **27 confirmed high/critical, 48 medium/low, 1 refuted.**

> Scope note: this is a **code/correctness** audit, distinct from the doc-structure audit in `AUDIT_REPORT.md` (AUDIT-001..019).

---

## Executive summary

The codebase is well-engineered in the small (RRF math, chunking, point-ID scheme, LLM-rerank parsing, default visual-lane isolation are all solid and tested). The risk is concentrated in **integration seams and failure-accounting**, and one root cause dominates:

> **The embed pipeline derives "success" from *reaching the end of the function*, not from *persisting what it expected*. Ollama and Qdrant errors are swallowed (caught + printed, `return 0`) instead of surfaced.** A file is stamped `status:"embedded"` (and counted in the green summary, and its `file_hash` recorded) **before** the pipeline knows whether any vectors landed.

Consequences during a long ET-embed run against a real corpus:
- A transient Ollama blip zeroes out **whole files** → marked embedded, never retried ([CA-1]).
- A transient Qdrant blip drops **32-chunk batches** → marked embedded, never retried ([CA-4]).
- A non-768 embedding model → **every batch silently fails**, reported as success ([CA-6]).
- A partial upsert → file frozen **half-old/half-new**, never re-picked ([CA-7]).
- Because the write-path Qdrant client uses `timeout=5` (the shortest in the codebase; reads use 30) and there is **no retry anywhere** in the write path, these blips are *likely*, not theoretical, under the load a full re-embed puts on Qdrant/Ollama.

Secondary cross-cutting themes:
- **Re-embed cleanup is non-atomic with no safety net** — superseded points survive any cleanup hiccup and `run_search` applies **no generation/stale filter**, so stale chunks resurface ([CA-14, CA-16]). Integrity tooling is **blind to `_notes`** ([CA-15]).
- **Init creates the wrong collection schema** — `carta init` then `carta embed` permanently disables BM25+RRF hybrid retrieval ([CA-10]).
- **Verification tooling you'll use to trust the run is itself broken** — `carta audit` caps at the first 1000 points ([CA-17]); eval can silently report partially-degraded numbers ([CA-20, CA-26]); graph expansion is a dead no-op ([CA-23]).
- **No single-writer guarantee** across `--repair`/`--visual`/targeted/MCP embed paths → concurrent writers delete each other's points ([CA-2/5/12]).
- **MCP `carta_search` (the agent-facing surface) bypasses `run_search`** — no RRF/rerank/graph, merges by raw score so visual swamps text ([CA-11/18]).
- **The proactive-recall hook may be a runtime no-op** — emits `{"context":...}` where Claude Code expects `hookSpecificOutput.additionalContext` ([CA-8]); and it has no latency budget ([CA-9]).

---

## MUST-FIX before ET-embed (blockers)

These cause **silent data loss, wrong retrieval, or broken verification during/after the re-embed itself**.

### Group A — Embed pipeline: honest success accounting  *(the core blocker)*
| ID | Sev | Finding | Location |
|----|-----|---------|----------|
| CA-1 | **CRITICAL** | Total Ollama failure → file stamped `embedded` with 0 vectors; `file_hash` recorded so it's never re-picked by pending/stale discovery | `pipeline.py:569-578,599-660,1452-1455`; `embed.py:283-313` |
| CA-4 | HIGH | Whole 32-chunk batch lost on any Qdrant upsert error; `flush()` swallows it, returns 0, not in summary | `embed.py:267-275` |
| CA-6 | HIGH | `VECTOR_DIM=768` hardcoded, no `len(vec)` check; non-768 model → every batch silently rejected, reported success | `embed.py:25,98-129,230-265` |
| CA-7 | HIGH | Partial upsert leaves `status:"embedded"`; missing chunks never re-picked | `pipeline.py:653-658` vs `569-578` |
| (med) | MED | Non-overflow Ollama errors (5xx/timeout/reset) get **no retry** | `embed.py:86-95` |
| (med) | MED | Dense lane truncates oversized input; BM25 sparse lane does not → the two vectors of one point describe different text | `embed.py:72-95` vs `254-256` |

**Root fix:** derive success from *persisted == expected*; add `summary['failed']`/`'partial']` and a re-pickable sidecar status (e.g. `embed_failed`); raise the write-path Qdrant `timeout` well above 5s; add bounded retry on transient Ollama/Qdrant errors; assert `len(vec)==expected` and reconcile collection dim with the live model.

### Group B — Re-embed cleanup / stale duplicates
| ID | Sev | Finding | Location |
|----|-----|---------|----------|
| CA-14 | HIGH | Old-gen chunks become **permanent searchable duplicates** if `delete_other_points` hiccups — no query-time generation filter, nothing ever sets `orphaned_at`, so no background sweep | `lifecycle.py:171-189`; `pipeline.py:644-658,1768-1773` |
| CA-15 | HIGH | Integrity scan + `--repair` **blind to `_notes`** → every embedded note shows false count-mismatch (needless re-embed churn); deleted/duplicate note points never cleaned | `integrity.py:57`; `repair.py:39` |
| CA-16 | HIGH | Deleted source files keep all vectors in `_doc` on a normal re-embed (only warned, never purged) | `pipeline.py:1309-1317` |
| (med) | MED | `_visual` points never cleaned and IDs not generation-versioned → reclassified pages orphan | `embed.py:174-177` |

**Root fix:** make supersede recoverable — stamp existing points `orphaned_at=now` before re-upsert (so `cleanup_expired_orphans` is a real backstop) **and/or** add a `doc_generation` filter to `run_search`; purge orphaned/deleted-file points on the normal run; extend integrity+repair to `_notes`.

### Group C — Encoding robustness (real corpora have stray encodings)
| ID | Sev | Finding | Location |
|----|-----|---------|----------|
| CA-13 | HIGH | Non-UTF8 markdown → `UnicodeDecodeError` → file silently dropped, **no `extraction_failed` bookkeeping**, retried-and-fails forever, invisible in status | `parse.py:177`; `pipeline.py:1394-1441` |
| CA-22 | MED | `parse_frontmatter` crashes the **whole scan + graph/embed** on one non-UTF8 `.md` | `scanner.py:19` |
| (med) | MED | UTF-8 BOM defeats frontmatter strip + H1 title detection | `parse.py:177` |

**Root fix:** defensive decode (`utf-8-sig`, `errors="replace"`); write a durable `extraction_failed` sidecar on decode error.

### Group D — Init creates the wrong schema (degrades ALL retrieval)
| ID | Sev | Finding | Location |
|----|-----|---------|----------|
| CA-10 | HIGH | `carta init` creates **unnamed-dense** collections via raw HTTP; embed expects **named hybrid** (dense+bm25). `ensure_collection` early-returns on existing, so init-before-embed → hybrid BM25+RRF silently **off forever**. A re-embed faithfully fills the wrong schema. | `bootstrap.py:356-373` vs `embed.py:98-129` |
| CA-27 | LOW | Re-`init` rubber-stamps a pre-existing wrong/incompatible schema as success (200/409) | `bootstrap.py:362-369` |

**Root fix:** bootstrap should call `ensure_collection`/`ensure_visual_collection` (single source of truth) or replicate the named hybrid schema; GET-and-validate existing collections.

### Group E — Post-re-embed verification tooling (so you can trust the run)
| ID | Sev | Finding | Location |
|----|-----|---------|----------|
| CA-17 | HIGH | `audit.py` scroll pagination broken (`offset=len(points)`=1000 against UUID-keyed points) → audit **caps at first 1000 chunks**; >1000-point corpora get false orphan/disconnected/mismatch results | `audit.py:82-110` |
| CA-23 | MED | Graph expansion is a **silent no-op** in `run_search` (`repo_root` = `.carta` dir, not repo root) → "graph neutral" eval conclusion is unreliable | `pipeline.py:1647` |
| CA-20 | MED | Eval hard-fail gate catches only **total** rerank fail-open, not partial → a 0.8B reranker degrading on N/62 queries reports a trusted-looking "reranked" score | `cli.py:660-667` |
| CA-26 | LOW | Empty / empty-string `expect` silently forces guaranteed MISS / guaranteed HIT → deflates/inflates recall | `harness.py:36-42` |

**Root fix:** mirror `integrity._scroll_all` paging; fix `repo_root` to `parent.parent`; warn/exit on partial rerank; validate eval expectations.

### Group J — CLI footgun
| ID | Sev | Finding | Location |
|----|-----|---------|----------|
| CA-3 | HIGH | `--timeout 0` / `file_timeout_s 0` → `join(0)` flags **every** file TIMEOUT, embeds nothing, **exits 0** (docs imply 0 = unbounded) | `cli.py:199-201`; `pipeline.py:1401-1426` |

**Root fix:** treat `<=0` as unbounded (match `visual_timeout_s`); exit non-zero when `embedded==0 and timed_out>0`.

---

## HIGH — fix soon (concurrency, MCP, hook, visual)

### Group F — Single-writer / locking
- **CA-2 / CA-5 / CA-12 [HIGH]** — embed lock covers only `cmd_embed`'s full-pipeline branch; `--repair`, `--visual`, targeted `--files`, and **all MCP `carta_embed`** paths are lockless → concurrent writers' `delete_other_points` removes each other's just-written points. (`cli.py:262`; `mcp/server.py:350-384`; `repair.py:32`)
- **CA-21 [MED]** — MCP tools are sync `def` run on the event loop; a long `carta_embed(scope=all)` blocks all other JSON-RPC → client timeout, no progress/cancel. (`mcp/server.py:295-384`)
- **CA-25 [LOW]** — bare-PID lock + OS PID reuse can permanently block future re-embeds after a hard kill. (`cli.py:105-149`)

**Root fix:** one shared lock helper acquired by every mutating embed entry point; MCP returns `{"error":"busy"}` instead of racing; store PID+start-time or use `fcntl.flock`.

### Group G — MCP search parity
- **CA-11 / CA-18 [HIGH]** — `carta_search` imports but never calls `run_search`; it does per-collection dense-only queries and **sorts by raw score**, so once a `_visual` collection exists, ColPali MaxSim (~10–40) swamps text cosine (~0–1) for every query; no RRF/rerank/graph/visual-cap. The agent-facing surface diverges hard from CLI/hook. (`mcp/server.py:79-137`)

**Root fix:** route `carta_search` through `run_search` (extend it to accept scope), or at minimum reuse `_rrf_merge_collections` with `visual_max_ratio`.

### Group H — Hook
- **CA-8 [HIGH]** — `_inject()` emits `{"context": ...}`; Claude Code's `UserPromptSubmit` contract for added context is `{"hookSpecificOutput":{"hookEventName":"UserPromptSubmit","additionalContext":"..."}}` (or plain stdout). If correct, **the entire proactive-recall feature is a runtime no-op** while unit tests pass (they assert the hook's own dict, not what CC consumes). The project's own research doc flagged this exact risk. **⚠ Verify against current Claude Code docs before changing.** (`hook.py:223-225`)
- **CA-9 [HIGH]** — no latency budget around `run_search`; query-embed (`timeout=60`, ×4 attempts) + Qdrant (`timeout=10`) can block prompt submission ~60s, especially while ET-embed saturates Ollama. (`hook.py:92-96`)

**Root fix:** emit the documented shape (after verification); wrap search+judge in a total hook budget (2–4s) and fail open on timeout.

### Group I — Visual drain
- **CA-19 [HIGH]** — a page is marked `visual_done` even when ColPali wrote **no vector** (`embed_pdf_pages` swallows a per-batch failure and returns `[]`); never re-queued → silent visual loss. (`pipeline.py:949-971`)
- **CA-24 [MED]** — `visual_timeout_s` is **dead config** (never read); the `--visual` drain has no watchdog and can hang forever on one wedged page. (`pipeline.py:974-1074`)

**Root fix:** assert a vector was produced before `move_to_done`; wire `visual_timeout_s` into a per-page daemon-thread watchdog like `file_timeout_s`.

---

## Medium / Low backlog (48 findings, by theme)

- **Config/CLI:** list-replacing deep-merge drops default `excluded_paths` when user sets it; `CARTA_*_URL` honored only at init not runtime; no numeric/url type checks.
- **Embed/parse:** sparse generation serial in main thread (defeats workers); BOM/H1/code-fence title edge cases; `vision/chunking.py` table-preserving module + `preserve_tables` are **dead code** (OCR tables flattened/split mid-row); `sections_from_markdown` ignores H1 boundaries.
- **Re-embed:** plain `carta embed` re-run does **not** re-embed content-changed files (staleness only alerted, no bulk force path); sidecar status drift `"embedded"` vs plan's `"current"`.
- **Search/eval:** rerank nullifies visual lane (placeholder excerpt); `_parse_order` coerces JSON bool/float to ranks; "recall@k" is really hit-rate@k; malformed eval YAML crashes; non-positive `-k` → all-MISS.
- **Scanner/audit:** `detect_disconnected_files` exclusion never matches dir patterns; `get_changed_since_hash` silently returns `[]` on unreachable prev hash; orphaned-doc false positives on raw-vs-canonical `related:` keys.
- **MCP/install:** dead scope-coercion branch; `carta init` hard-aborts if a stale plugin-cache dir can't be removed; vector-dim truth duplicated across 3 sites; version-compare mishandles ragged/`v`-prefixed tuples.
- **Docs:** doc-audit report split-brain (`docs/AUDIT_REPORT.md` vs root); packaged `carta/skills/` diverged from `skills/`; CLAUDE.md retains v0.2/GSD framing (AUDIT-006 still open); bootstrap AGENTS.md documents only 3 commands; `config.yaml.example` omits `build/`,`temp/`; `carta hook` absent from README.
- **Hook:** empty/whitespace query still searched; gray-zone judge doc says "fail-open" but is fail-closed.
- **Visual:** MPS path lacks the segfault/dtype guard; test configs use non-`-hf` model IDs.

(Full per-finding detail with reproduction + fix is in the workflow result; IDs map to the verified findings list.)

## Refuted (1)
- **CRLF markdown breaks frontmatter stripping** — refuted: `extract_markdown_text`/hash path LF-normalizes before the regex runs, so the isolated regex's CRLF-intolerance is not reachable via the real call path.

---

## Recommended fix sequence
1. **Group A** (honest success accounting) — highest leverage; neutralizes the whole silent-loss family. TDD: simulate Ollama-all-fail, Qdrant-batch-fail, dim-mismatch, partial-upsert → assert re-pickable status + non-zero accounting.
2. **Group C** (encoding) — small, removes silent file drops.
3. **Group D/CA-10** (init hybrid schema) — ensures the re-embed actually populates hybrid.
4. **Group B** (cleanup/dupes/notes) — correctness of the re-embedded corpus over time.
5. **Group E** (audit/eval/graph verification) — so the post-embed checks are trustworthy.
6. **Group J/CA-3**, then **F (lock)**, **G (MCP search)**, **H (hook)**, **I (visual)**.

Each fix follows the project's Superpowers TDD flow (failing test → fix → green), grouped into focused PRs by root cause.
