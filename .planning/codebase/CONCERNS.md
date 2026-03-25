# Codebase Concerns

**Analysis Date:** 2026-03-25

## Tech Debt

**Oversized scanner module:**
- Issue: `carta/scanner/scanner.py` is 694 lines — large monolithic file combining structural checks, git operations, frontmatter parsing, and date validation. Complex interdependencies between check functions make isolated testing difficult.
- Files: `carta/scanner/scanner.py`
- Impact: Changes to one check type risk breaking others. Test coverage increases linearly with file size. New contributors struggle to understand the full scope before adding checks.
- Fix approach: Refactor into separate modules: `checks/homeless_docs.py`, `checks/frontmatter.py`, `checks/staleness.py`, `checks/related_links.py`. Each module handles one concern. `scanner.py` becomes an orchestrator. Tests become focused and easier to reason about.

**Parse.py chunking complexity:**
- Issue: `chunk_text()` in `carta/embed/parse.py` (lines 62-166) has deeply nested control flow with a safety iteration counter (lines 128-134). The oversized paragraph fallback is defensive but adds 50+ lines of branching logic that's hard to verify.
- Files: `carta/embed/parse.py`
- Impact: Edge case paragraphs (massive tables, concatenated data without spaces) might trigger the safety check in unexpected ways. Difficult to reason about chunk boundaries when overlap + truncation interact.
- Fix approach: Extract oversized-paragraph handler to separate function `_chunk_oversized_paragraph()`. Add property-based tests (hypothesis) with generated pathological inputs (long runs of non-space characters, tables with no breaks). Document safety counter rationale.

**Chunk text safety iteration cap is context-dependent:**
- Issue: Safety iteration limit in `chunk_text()` uses `max(10_000, original_words_len * 50)`. The multiplier (50x word length) is arbitrary and scales unpredictably for 10K+ word paragraphs.
- Files: `carta/embed/parse.py` line 130
- Impact: A 1,000-word paragraph without breaks could allow 50K iterations before raising RuntimeError. Embedded files with broken formatting (missing newlines in tables/code) may process slowly or hang indefinitely.
- Fix approach: Cap the iteration limit to a fixed value independent of paragraph size (e.g., `max(10_000, 100_000)` flat). Document rationale. Add logs when approaching limit. Consider alternative: throw error immediately on first un-chunkable word instead of iterating.

**Git subprocess calls without timeout:**
- Issue: `get_file_last_commit_date()` and similar functions in `carta/scanner/scanner.py` call `subprocess.run(["git", ...])` without timeout parameter.
- Files: `carta/scanner/scanner.py` lines 206-218, and other git invocations
- Impact: On large repos or slow disks, `git log` can hang indefinitely, blocking the entire scan operation. No mechanism to interrupt.
- Fix approach: Add `timeout=10` parameter to all `subprocess.run()` calls invoking git. Catch `subprocess.TimeoutExpired`, log warning, skip that check gracefully. Test with a large repo.

**Hook command string escaping fragility:**
- Issue: Hook command in `carta/install/bootstrap.py` line 138: `bash -c '"$(git rev-parse --show-toplevel)/.carta/hooks/{script_name}"'` relies on shell expansion of `$()`. If project path contains quotes or special characters, command breaks.
- Files: `carta/install/bootstrap.py` line 138
- Impact: Projects with paths like `/path/with'quotes/` or `/path/with$(malicious)/` will fail silently or execute unintended commands.
- Fix approach: Use full absolute path in hook JSON instead of relying on shell expansion. Calculate path in Python, embed it directly. Or use array-form exec (avoiding shell): `["bash", "-c", "full/absolute/path/hooks/..."]`.

**Qdrant timeout hardcoded in pipeline:**
- Issue: `run_embed()` in `carta/embed/pipeline.py` line 86 uses `timeout=5` for preflight Qdrant check, but embed operations use default client timeout (potentially unbounded).
- Files: `carta/embed/pipeline.py` line 86
- Impact: If Qdrant becomes slow after preflight succeeds, embed can hang indefinitely. No consistent timeout strategy across operations.
- Fix approach: Extract timeout to config parameter `qdrant.timeout` (default 5s for ops, 3s for health checks). Pass to all QdrantClient() instantiations. Add per-operation timeout guards.

## Known Bugs

**Sidecar current_path schema inconsistency:**
- Symptoms: `carta/embed/induct.py` generates sidecar stubs but may not always include `current_path` field. Scanner relies on this field to validate sidecar/PDF co-location, causing false-positive drift warnings.
- Files: `carta/embed/induct.py`, `carta/embed/pipeline.py`, `carta/scanner/scanner.py`
- Trigger: Run `carta embed` on new PDFs, then `carta scan` — some sidecars report missing/stale `current_path`.
- Workaround: Manually edit `.embed-meta.yaml` to add `current_path: docs/path/to/file.pdf`. Regenerate sidecar with `carta embed`.
- Status: **Documented in plan task 2** (2026-03-25-cli-mcp-hybrid-and-bug-fixes.md); fix requires schema alignment in induct + pipeline + scanner fallback.

**Skills not discoverable in Claude Code after carta init:**
- Symptoms: BUG-001 from BUGS.md — after `carta init` succeeds, `/doc-audit` skill is not available in Claude Code. Error: "Unknown skill: doc-audit".
- Files: `carta/install/bootstrap.py` (skill install), `.claude/plugins/installed_plugins.json`, Claude Code plugin resolution
- Trigger: Run `carta init` in a fresh project, then type `/doc-audit` in Claude Code.
- Workaround: Manually restart Claude Code session after `carta init` (option B in BUGS.md). May require implementation of option A (update global plugin cache).
- Status: **Partially fixed** — bootstrap.py now registers plugin metadata correctly, but requires session restart. Long-term fix: determine whether Claude Code reads `.claude/skills/` on session restart.

## Security Considerations

**No input validation on Qdrant URL:**
- Risk: User-provided `CARTA_QDRANT_URL` is used directly in HTTP requests without URL validation. Malformed URLs or URLs pointing to unintended hosts could be exploited.
- Files: `carta/install/bootstrap.py` line 18, `carta/cli.py` (config loading)
- Current mitigation: Health check attempt (requests.get) will fail gracefully if URL is invalid.
- Recommendations:
  - Validate URL scheme (must be http/https)
  - Parse and validate hostname (no credentials in URL)
  - Use urllib.parse.urlparse() to validate structure before use
  - Document expected format in install guide

**Git command injection in hook path calculation:**
- Risk: If project root detection via `git rev-parse --show-toplevel` returns unexpected values, hook script path could escape intended directory.
- Files: `carta/install/bootstrap.py` line 138
- Current mitigation: git rev-parse is a read-only operation; injection risk is low.
- Recommendations:
  - Use absolute path embedded at install time instead of dynamic shell expansion
  - Validate project root matches expected pattern before embedding in hook
  - Add tests for edge case project names with special characters

**Frontmatter YAML parsing without safety limits:**
- Risk: `parse_frontmatter()` in `carta/scanner/scanner.py` uses `yaml.safe_load()` but doesn't limit document size or nesting depth. Malicious YAML could cause DoS.
- Files: `carta/scanner/scanner.py` lines 17-36
- Current mitigation: `yaml.safe_load()` mitigates code execution; nesting attacks are less severe.
- Recommendations:
  - Read only first 2KB of file for frontmatter parsing (YAML blocks should be small)
  - Add yaml loader with size/depth limits (if available in PyYAML)
  - Validate frontmatter keys against whitelist (status, related, last_reviewed, etc.)

## Performance Bottlenecks

**PDF extraction without streaming:**
- Problem: `extract_pdf_text()` in `carta/embed/parse.py` loads entire PDF into memory via `fitz.open()`, then iterates all pages. No streaming or incremental processing.
- Files: `carta/embed/parse.py` lines 7-46
- Cause: PyMuPDF (fitz) loads document wholly into memory for text extraction.
- Current behavior: Large PDFs (>50MB) will consume proportional RAM. Concurrent embeds share no resources.
- Improvement path:
  - For very large PDFs, consider splitting before embedding (`split_pdf_into_chunks()` utility)
  - Monitor memory usage in `run_embed()` and warn if >500MB
  - Document recommended PDF size limits (<20MB per file)
  - Consider alternative: use pdfplumber (lower memory footprint) for PDFs without complex formatting

**Qdrant upsert batching not implemented:**
- Problem: `upsert_chunks()` in `carta/embed/embed.py` upsets points to Qdrant without batching. Large documents (500+ chunks) cause individual HTTP requests.
- Files: `carta/embed/embed.py`
- Cause: Qdrant client is called once per chunk or in a single large batch.
- Current capacity: ~500 chunks per document before noticeable slowdown.
- Improvement path:
  - Implement batch upsert loop: process 32-64 chunks at a time
  - Add progress logging every batch (e.g., "upserted 128/500 chunks")
  - Tune batch_size via config parameter `embed.qdrant_batch_size`
  - Estimated speedup: 3-5x for 500+ chunk documents
  - **Documented in plan task 1**

**No progress indication for long embeds:**
- Problem: `run_embed()` processes files silently; no per-file or per-chunk progress output.
- Files: `carta/embed/pipeline.py` lines 71-150+
- Cause: Loop over `discover_pending_files()` doesn't log intermediate state.
- Current behavior: User has no visibility into progress for large document collections.
- Improvement path:
  - Print "Processing file X/N: filename" before embed starts
  - Log progress from batched upsert (see above)
  - Add elapsed time / ETA when processing multiple files
  - **Documented in plan task 1**

## Fragile Areas

**Scanner homeless_doc check is overly broad:**
- Files: `carta/scanner/scanner.py` lines 100-126
- Why fragile: Hardcoded whitelist of standard repo files (README.md, CHANGELOG.md, etc.) is not extensible. Projects with unconventional root docs (ARCHITECTURE.md, ROADMAP.md) are flagged as homeless.
- Safe modification: Configuration-driven whitelist. Add `homeless_doc_root_whitelist` to config.yaml, merge with defaults.
- Test coverage: `carta/scanner/tests/test_scanner.py` covers default behavior but lacks config override tests.

**Sidecar I/O race condition in concurrent embeds:**
- Files: `carta/embed/pipeline.py`, `_update_sidecar()` (lines 31-36)
- Why fragile: Multiple `carta embed` processes reading/writing the same `.embed-meta.yaml` file can corrupt state if both processes update simultaneously.
- Safe modification: Lock is in place (`_acquire_embed_lock()`), but only enforces single embed process globally. If two projects use the same `.carta/` path, race condition still possible.
- Test coverage: No concurrent sidecar tests; single-process lock test only.

**Bootstrap idempotency not fully tested:**
- Files: `carta/install/bootstrap.py`
- Why fragile: Running `carta init` twice in same project should be idempotent, but `.gitignore` appending (lines 231-241) doesn't deduplicate; hooks overwrite previous entries without merging.
- Safe modification:
  - `_update_gitignore()`: Check for duplicates before appending
  - `_register_hooks()`: Merge hook definitions instead of replacing (preserve other hooks)
  - Add test: `test_bootstrap_idempotent()` — run `run_bootstrap()` twice, verify no duplicates
- Test coverage: `carta/install/tests/test_bootstrap.py` covers initial run; lacks idempotency tests.

## Scaling Limits

**Single Qdrant instance assumed:**
- Current capacity: Three collections (`doc`, `session`, `quirk`), each configured for 768-dim vectors.
- Limit: Qdrant default memory limit is ~4GB per instance. At ~768-dim vectors with 1M chunks, expect ~3GB storage.
- Scaling path:
  - For >1M chunks per collection, scale horizontally (Qdrant cluster mode, not documented in current setup)
  - For >3 collections, shard by project (requires multi-instance bootstrap)
  - Current docs assume single local Qdrant; no guidance for production multi-instance setups

**Git operations scale linearly with repo size:**
- Current capacity: Works smoothly on repos <10K files, ~500MB total size.
- Limit: `get_file_last_commit_date()` calls git log per file — O(n) performance. Scanning 100K files = 100K git invocations.
- Scaling path:
  - Cache git log results across scan run
  - Use `git log --name-only` once to get all changed files at once
  - Add timeout and skip slow repos with warning

**No pagination for PDF extraction:**
- Current capacity: PDFs <20MB process in <5 seconds. PDFs >100MB may timeout or OOM.
- Limit: Single-pass full PDF load via PyMuPDF.
- Scaling path:
  - Split large PDFs before embedding (preprocess in `discover_pending_files()`)
  - Implement page-range extraction (embed pages 1-50, then 51-100 as separate documents)
  - Add validation: warn if PDF >50MB

## Dependencies at Risk

**PyMuPDF (fitz) maintenance risk:**
- Risk: PyMuPDF has infrequent releases; latest version may not support Python 3.13+.
- Impact: When Python 3.13 becomes standard, embedding PDFs could fail without dependency update.
- Migration plan:
  - Monitor PyMuPDF releases quarterly
  - Evaluate alternatives: pdfplumber (pure Python, actively maintained), PyPDF2 (minimal deps)
  - Add compatibility tests for Python 3.13 / 3.14 in CI pipeline
  - Document fallback if PyMuPDF unavailable

**Requests library for Qdrant/Ollama health checks:**
- Risk: Low version constraints allow any requests 2.x. Major breaking changes in requests 3.x possible.
- Impact: `_check_qdrant()`, `_check_ollama()` rely on requests.get() API; 3.x redesign could break health checks.
- Migration plan:
  - Pin to requests>=2.31,<3 in pyproject.toml
  - Quarterly check for requests 3.0 release notes
  - Add smoke test for health check endpoints

## Missing Critical Features

**No support for incremental embedding updates:**
- Problem: All pending sidecars are embedded every run. If 1 of 100 files changed, all 100 are re-embedded.
- Blocks: Efficient document updates for large knowledge bases. Users must manage sidecar status manually.
- Workaround: Manually set status to `done` in sidecar for files not needing update.

**No search result ranking or relevance tuning:**
- Problem: `carta search` returns vector similarity scores but no way to weight recency, document type, or anchor status.
- Blocks: Users cannot customize search behavior for their projects.
- Workaround: Manual post-processing of search results in consumer code.

**No support for other embedding models:**
- Problem: Hardcoded to Ollama + nomic-embed-text. No abstraction for other embedders (OpenAI, Cohere, etc.).
- Blocks: Projects needing proprietary embeddings or different model families.
- Workaround: Fork/modify source to replace embedding calls.

**No API documentation or schema versioning:**
- Problem: MCP server (new in v0.1.11) and CLI have no formal schema docs. Breaking changes to tool signatures would silently break consumers.
- Blocks: External tools (other agents, frameworks) integrating with Carta.
- Workaround: Read source code to understand contracts.

## Test Coverage Gaps

**Oversized paragraph handling in chunk_text:**
- What's not tested: Property-based tests with pathological inputs (1M-char paragraphs, tables with no whitespace, repeated punctuation).
- Files: `carta/embed/tests/test_embed.py`
- Risk: Safety iteration check could fail silently on edge cases; loop might not terminate.
- Priority: **High** — affects all PDF embedding; failures are user-visible hangs.

**Concurrent embed processes:**
- What's not tested: Two `carta embed` runs simultaneously (or near-simultaneously) with overlapping file sets.
- Files: `carta/cli.py` (lock handling), `carta/embed/tests/test_embed.py`
- Risk: Lock mechanism (`.carta/embed.lock`) may not prevent sidecar corruption or race conditions in Qdrant upserts.
- Priority: **Medium** — risk is low in typical single-user workflows, high in CI/CD pipelines.

**Scanner false positives on config edge cases:**
- What's not tested: Projects with complex excluded_paths patterns, multiple anchor_doc entries, non-standard docs_root.
- Files: `carta/scanner/tests/test_scanner.py`
- Risk: Scanner reports false homeless_docs or broken_related issues on valid configurations.
- Priority: **Medium** — impacts user trust in scan results.

**Bootstrap on fresh system without git:**
- What's not tested: Running `carta init` in directory outside git repo, or with git not installed.
- Files: `carta/install/bootstrap.py` (project name detection), `carta/install/tests/test_bootstrap.py`
- Risk: `_detect_project_name()` silently falls back to directory name; symlink-heavy projects may get unexpected names. Hook registration fails if git not found.
- Priority: **Low** — most users have git; fallback exists.

---

*Concerns audit: 2026-03-25*
