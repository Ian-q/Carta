---
phase: 01-pipeline-reliability-mcp-foundation
plan: "01"
subsystem: embed-pipeline
tags: [embed, pipeline, reliability, batching, timeout, verbose, sidecar]
dependency_graph:
  requires: []
  provides: [PIPE-01, PIPE-02, PIPE-03, PIPE-04, PIPE-05]
  affects: [carta/embed/embed.py, carta/embed/parse.py, carta/embed/induct.py, carta/embed/pipeline.py, carta/scanner/scanner.py, carta/cli.py]
tech_stack:
  added: [concurrent.futures]
  patterns: [ThreadPoolExecutor per-file timeout, batch accumulation flush, verbose param guard]
key_files:
  created: []
  modified:
    - carta/embed/embed.py
    - carta/embed/parse.py
    - carta/embed/induct.py
    - carta/embed/pipeline.py
    - carta/scanner/scanner.py
    - carta/cli.py
    - carta/embed/tests/test_embed.py
decisions:
  - "Batch size 32 chosen per plan spec — balances HTTP round-trip reduction vs payload size"
  - "FILE_TIMEOUT_S=300 — 5 minutes per file; uses ThreadPoolExecutor(max_workers=1) to isolate"
  - "overlap_cap = len(take)//4 (25%) — prevents runaway overlap in oversized paragraph path"
  - "Safety counter max(10, original_words_len * 2) — tight enough to catch true stalls quickly"
  - "current_path inserted after doc_type in sidecar stub dict for logical ordering"
  - "All stdout prints in pipeline.py guarded by verbose param; stderr prints always emitted"
metrics:
  duration_minutes: 8
  completed_date: "2026-03-25"
  tasks_completed: 2
  files_modified: 7
---

# Phase 01 Plan 01: Pipeline Reliability Fixes Summary

**One-liner:** Five discrete embed pipeline reliability fixes — batch Qdrant upserts at 32, per-file 300s timeout via ThreadPoolExecutor, overlap capped at 25% of take, verbose=False stdout suppression, and sidecar current_path with auto-heal pass.

## Tasks Completed

| Task | Description | Commit |
|------|-------------|--------|
| 1 | Batch upsert (PIPE-01), overlap cap (PIPE-03), sidecar current_path (PIPE-05) | d41cc83 |
| 2 | Per-file timeout (PIPE-02), verbose suppression (PIPE-04), heal pass (PIPE-05), CLI updates | d4c890e |

## What Was Built

**PIPE-01 — Batch upsert:** `upsert_chunks()` in `embed.py` now accumulates `PointStruct` objects into a `batch` list and flushes every 32 points (or at end-of-loop). Each `get_embedding()` failure skips that chunk via `continue` without breaking the batch. Result: 64 chunks = 2 HTTP calls instead of 64.

**PIPE-02 — Per-file timeout:** Extracted `_embed_one_file()` from `run_embed()` loop. Each file is submitted to a `ThreadPoolExecutor(max_workers=1)` and retrieved with `future.result(timeout=FILE_TIMEOUT_S)`. On `TimeoutError`, file is skipped with a stderr warning; pipeline continues to next file.

**PIPE-03 — Overlap cap:** In `chunk_text()` oversized-paragraph path, overlap is now capped at `len(take) // 4` (25% of the taken slice) before applying `min(overlap_words, overlap_cap)`. Safety counter lowered from `max(10_000, N*50)` to `max(10, N*2)` to catch true stalls quickly.

**PIPE-04 — Verbose suppression:** `run_embed`, `run_search`, `run_scan` all accept `verbose: bool = False`. Every `print()` to stdout is guarded by `if verbose:`. `print(..., file=sys.stderr)` calls are never guarded — always emitted. CLI call sites pass `verbose=True` so human-initiated commands remain chatty.

**PIPE-05 — Sidecar current_path:** `generate_sidecar_stub()` now includes `"current_path": str(rel_path)` in every new stub. `_heal_sidecar_current_paths()` scans existing sidecars missing the field and back-fills from the co-located source file; called at the start of `run_embed()`.

## Tests Added (11 new)

- `test_upsert_chunks_batches_at_32` — 64 chunks → 2 upsert calls
- `test_upsert_chunks_remainder_flushed` — 10 chunks → 1 upsert call, 10 points
- `test_upsert_chunks_skips_bad_embedding` — embedding failure on 1 of 10 → count=9
- `test_chunk_text_overlap_cap_25_percent` — 5000-word paragraph terminates, no empty chunks
- `test_chunk_text_safety_counter_lowered` — pathological input completes, <500 chunks
- `test_generate_sidecar_stub_includes_current_path` — stub contains correct relative path
- `test_run_embed_verbose_false_no_stdout` — zero stdout when verbose=False
- `test_run_embed_verbose_true_has_stdout` — non-empty stdout when verbose=True
- `test_embed_one_file_timeout` — FILE_TIMEOUT_S=1, slow file → skipped=1
- `test_heal_sidecar_current_paths` — heals sidecar with matching PDF
- `test_heal_sidecar_skips_missing_source` — skips sidecar without source file

Also updated two existing tests (`test_upsert_chunks_calls_qdrant`, `test_upsert_chunks_bad_chunk_does_not_kill_good_chunks`) to reflect batching behavior (1 flush call instead of N individual calls).

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] Updated existing upsert tests for batching behavior**
- **Found during:** Task 1 GREEN phase
- **Issue:** `test_upsert_chunks_calls_qdrant` asserted `call_count == 3` (one per chunk); after batching, 3 chunks produce 1 flush call
- **Fix:** Updated assertion to `call_count == 1`; same fix for `test_upsert_chunks_bad_chunk_does_not_kill_good_chunks` (2→1)
- **Files modified:** `carta/embed/tests/test_embed.py`
- **Commit:** d41cc83

## Known Stubs

None — all pipeline changes wire to real behavior.

## Self-Check: PASSED

- All 7 modified files exist
- Commits d41cc83 and d4c890e confirmed in git log
- Key artifacts verified: BATCH_SIZE=32, FILE_TIMEOUT_S=300, overlap_cap, current_path, verbose=True in CLI
- 58/58 tests pass
