---
title: Interactive Progress Bar for carta embed and carta scan
date: 2026-04-05
status: approved
---

# Interactive Progress Bar for `carta embed` and `carta scan`

## Problem

When `carta embed --all` runs on a repo with large PDFs (datasheets, reference manuals), it can run
for 15+ minutes with no indication of whether it is stuck or just slow. The current `--verbose` flag
emits scrolling lines but gives no sub-step visibility within a file, making it impossible to tell
if the process is hung at extraction, chunking, Ollama embedding, or Qdrant upsert.

## Goals

- Show real-time per-file and sub-step progress during `carta embed`
- Show a spinner with current check name during `carta scan`
- Zero new dependencies (pure stdlib + ANSI escape codes)
- Degrade gracefully to plain scrolling output in non-TTY environments (agents, pipes, CI)
- Do not change the programmatic API used by the MCP server or tests

## Non-Goals

- No alternate screen buffer / curses
- No ETA calculation (too noisy for variable-length PDF work)
- No changes to MCP tool behavior or return values
- No changes to test harness output

---

## Architecture

### New module: `carta/ui/progress.py`

A `Progress` class used as a context manager. Constructed at the CLI layer and passed into pipeline
functions as an optional argument. Pipeline code never imports from `carta.ui` directly — it only
calls methods on the object it receives.

Auto-detects rendering mode at instantiation:

- **TTY mode** (`sys.stdout.isatty() == True`): in-place `\r` line rewrites, ANSI color/bold/dim,
  braille spinner animation cycling through `⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏`
- **Plain mode**: standard `print()` scrolling lines, no control characters — output is functionally
  identical to the current `verbose=True` behavior

The context manager installs a `SIGINT` handler on entry that clears the current spinner line before
re-raising, preventing a corrupted cursor position on Ctrl+C.

### Public API

```python
with Progress(total=N) as p:
    p.file(idx, name)           # new file started — prints/renders file header
    p.step(msg)                 # sub-step within current file (extracting / chunking / embedding)
    p.done(chunks, elapsed)     # file completed successfully
    p.skip(reason)              # file skipped (LFS pointer, already embedded, timeout)
    p.error(msg)                # file errored
p.summary(embedded, skipped, errors)  # final summary line after context exits
```

When `progress=None` (the default), pipeline functions guard with `if progress:` before calling
methods. There are a small number of call sites so this is not onerous.

### TTY rendering

The active spinner line is rewritten in-place on each `p.step()` call:

```
⠸  3/12  docs/reference/big-datasheet.pdf
         ▸ embedding 312 chunks...     0:00:14
```

On `p.done()` the spinner line is replaced with a completed line and scrolls into history:

```
✓  3/12  docs/reference/big-datasheet.pdf       312 chunks  14.2s
```

On `p.skip()`:
```
–  3/12  docs/reference/big-datasheet.pdf       skipped: LFS pointer
```

On `p.error()`:
```
✗  3/12  docs/reference/big-datasheet.pdf       ERROR: Qdrant timeout
```

Final summary line after all files:
```
Embedded: 10   Skipped: 1   Errors: 1
```

Color scheme (ANSI, degrades to no-color if `NO_COLOR` env var is set or non-TTY):

| Element        | Style              |
|----------------|--------------------|
| `✓` done       | green              |
| `✗` error      | red                |
| `–` skip       | dim                |
| spinner `⠸`    | cyan               |
| filename       | bold               |
| sub-step `▸`   | dim                |
| chunk count    | dim                |
| elapsed time   | dim                |

### Plain mode rendering

```
  [3/12] Embedding: big-datasheet.pdf ...
    extracting 47 pages...
    chunking → 312 chunks...
    embedding + upserting...
  [3/12] OK: big-datasheet.pdf — 312 chunk(s) in 14.2s
```

This matches the current `verbose=True` output closely enough to avoid surprising existing users or
breaking log scrapers.

---

## Integration Points

### `pipeline.py:run_embed`

`run_embed` and `run_embed_file` gain an optional `progress: Optional[Progress] = None` parameter.

The outer file loop calls:
```python
progress.file(idx, file_path.name)
# ...
progress.done(count, elapsed)   # or .skip() / .error()
```

The `verbose` parameter is retained for backwards compatibility but is superseded when `progress` is
provided. When `progress` is not `None`, `verbose` print statements are suppressed in favor of the
progress object.

### `pipeline.py:_embed_one_file`

Gains `progress: Optional[Progress] = None`. Calls `progress.step(msg)` at each sub-stage:

1. `progress.step(f"extracting {suffix} text...")`
2. `progress.step(f"chunking {len(pages)} pages → {len(chunks)} chunks")`
3. `progress.step(f"embedding + upserting {len(enriched)} chunks")`
4. (If vision enabled) `progress.step(f"extracting image descriptions")`
5. (If ColPali enabled) `progress.step(f"ColPali: embedding visual pages")`

### `scanner.py:run_scan`

`run_scan` gains `progress: Optional[Progress] = None`.

Scan is fast (typically <2s) but has many sequential checks. A single spinner line shows the current
check category:

```
⠸  Scanning docs/  checking frontmatter...
⠸  Scanning docs/  checking related links...
⠸  Scanning docs/  checking sidecars...
✓  Scan complete — 3 errors, 7 warnings, 12 info          0.8s
```

No per-file breakdown needed for scan.

### `cli.py`

`cmd_embed` and `cmd_scan` construct `Progress` and pass it in:

```python
with Progress(total=len(pending)) as p:
    run_embed(repo_root, cfg, progress=p)
```

`carta/ui/__init__.py` exports `Progress` so the import is `from carta.ui import Progress`.

---

## Hang Visibility

Because `progress.step()` is called *before* each slow operation, the last displayed sub-step
indicates exactly where a hang occurred:

| Last step shown              | Likely cause                        |
|------------------------------|-------------------------------------|
| `extracting N pages`         | PyMuPDF slow on large/complex PDF   |
| `chunking N pages`           | Unusual — pure Python, should be fast |
| `embedding + upserting`      | Ollama slow or unresponsive          |
| `extracting image descriptions` | LLaVA/GLM-OCR model slow          |
| `ColPali: embedding visual pages` | ColPali model slow on CPU       |

---

## `NO_COLOR` and environment handling

The `Progress` class respects the [`NO_COLOR`](https://no-color.org/) convention: if `NO_COLOR` is
set in the environment, all ANSI codes are suppressed even in TTY mode, producing plain bold-free
text with the spinner and symbols still present.

Non-TTY is detected via `sys.stdout.isatty()`. When false, plain mode is used unconditionally
regardless of `NO_COLOR`.

---

## Testing

- Unit tests for `Progress` in `carta/tests/test_progress.py`
- Test plain-mode output matches expected strings (capfd fixture)
- Test TTY-mode methods don't raise (mock `isatty=True`, capture output)
- Test `None` progress is a no-op (call all methods, assert no exception)
- Existing embed and scan tests are unaffected: they pass `progress=None` (default)

---

## File Changes Summary

| File | Change |
|------|--------|
| `carta/ui/__init__.py` | New — exports `Progress` |
| `carta/ui/progress.py` | New — `Progress` class |
| `carta/embed/pipeline.py` | Add `progress` param to `run_embed`, `run_embed_file`, `_embed_one_file` |
| `carta/scanner/scanner.py` | Add `progress` param to `run_scan` |
| `carta/cli.py` | Construct `Progress` in `cmd_embed` and `cmd_scan` |
| `carta/tests/test_progress.py` | New — unit tests |
