# Design: Embed Progress Bar + Targeted File Embed

**Date:** 2026-04-10  
**Status:** Approved

---

## Overview

Two additions to `carta embed`:

1. **Progress bar** — a visual `[=====>    ] 3/12` bar in the TTY spinner line so the user can see how far along the embed run is at a glance.
2. **Targeted embed** — `carta embed file.pdf` embeds one or more specific files immediately, bypassing the full pipeline and concurrency lock, to get documents into Qdrant for search without waiting.

---

## Feature 1: Progress Bar

### Motivation

The existing `Progress` spinner already shows `{idx}/{total}` but the ratio is easy to miss in a dense spinner line. A filled bar gives an immediate gestalt sense of "how far along."

### Changes

**`carta/ui/progress.py`**

- Add `_bar(self) -> str` helper:
  - Width: 10 characters.
  - Filled portion: `round(self._idx / self._total * 10)` characters, using `=` for interior, `>` for the leading edge.
  - If `self._total == 0`, render `----------` (unknown total).
  - Returns `[=====>    ]` style string; ANSI dim-colored in TTY mode.

- Update `_write_embed_line()` to prepend the bar:
  ```
  ⠋  [=====>    ] 3/12  foo.pdf  ▸ embedding 45 chunks → Qdrant  2s
  ```

- Update `done()`, `skip()`, and `error()` completion lines to also include the bar so scrollback history is readable:
  ```
  ✓  [=========>] 9/12  foo.pdf  80 chunks  5.2s
  –  [=========>] 9/12  bar.pdf  skipped: LFS pointer
  ✗  [=========>] 9/12  baz.pdf  ERROR: timeout
  ```

**Non-TTY / plain mode:** unchanged. `[3/12]` prefix is sufficient for log/pipe output and is already in place.

### Constraints

- No new dependencies. Pure string manipulation.
- Bar logic must not raise on edge cases: `_total == 0`, `_idx > _total` (shouldn't happen but guard anyway).

---

## Feature 2: Targeted File Embed

### Motivation

After downloading a new datasheet the user wants to embed it immediately and search it, without triggering a full `discover_pending_files` scan and without waiting for the embed lock. The result is provisional — the sidecar won't have audit IDs — but the file will be searchable right away.

### CLI Changes

**`carta/cli.py` — argument parsing**

```python
embed_p = sub.add_parser("embed")
embed_p.add_argument("files", nargs="*", help="Optional specific file(s) to embed")
```

**`carta/cli.py` — `cmd_embed()`**

When `args.files` is non-empty, take the fast path:

1. Skip lock acquisition entirely.
2. Skip `discover_pending_files`.
3. Call `run_embed_file(path, cfg, force=True)` for each path in `args.files`.
4. Use `Progress(total=len(args.files))` for consistent display.
5. Collect results: count successes, collect errors.
6. Print summary via `progress.summary(...)`.
7. Exit 1 if any file errored; exit 0 otherwise.

When `args.files` is empty, existing full-pipeline behavior is unchanged.

### Pipeline Changes

**`carta/embed/pipeline.py` — `run_embed_file()`**

No changes to the function itself. It already:
- Accepts an absolute or repo-relative path.
- Creates a sidecar stub via `generate_sidecar_stub()` if one doesn't exist.
- Respects `force=True` to bypass mtime/hash skipping.
- Returns `{"status": "ok", "chunks": int}` on success.
- Raises `FileNotFoundError` if the path doesn't exist.

The CLI passes `force=True` unconditionally on the targeted path — if the user explicitly named a file they want it embedded now.

### Error Handling

- `FileNotFoundError` for a missing path: print `Error: path does not exist: <path>` to stderr, count as error.
- Multiple files: process all regardless of individual failures, report all errors at the end.
- Exit 1 if any errors occurred.

### Sidecar State

Files embedded via targeted path get a basic sidecar (`slug`, `doc_type`, `status: embedded`, `chunk_count`, `indexed_at`). They will not have:
- Audit IDs (`AUDIT-NNN`)
- Structural scan metadata

This is expected and acceptable. The file is searchable. Running `carta scan` + `carta embed` later will reconcile the sidecar properly.

---

## What Is Not Changing

- Non-TTY output format for full pipeline runs.
- Lock behavior for full `carta embed` runs (no files specified).
- `run_embed_file` internals — no changes needed there.
- Any scan, search, or audit pipeline.
