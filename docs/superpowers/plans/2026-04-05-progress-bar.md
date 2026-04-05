# Interactive Progress Bar Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an in-place animated progress bar to `carta embed` and `carta scan` that shows per-file and sub-step progress with ANSI color in TTY environments, degrading to plain scrolling text for agents and pipes.

**Architecture:** A new `Progress` context manager in `carta/ui/progress.py` auto-detects TTY vs plain mode. Pipeline functions (`run_embed`, `_embed_one_file`, `run_scan`) accept an optional `progress` parameter and call its methods; the CLI constructs the `Progress` object and passes it in. The MCP server and test code pass `progress=None` (the default) and are unaffected.

**Tech Stack:** Python stdlib only — `sys`, `os`, `time`, `signal`. No new dependencies.

---

## File Map

| File | Action | Responsibility |
|------|--------|---------------|
| `carta/ui/__init__.py` | Create | Package init, exports `Progress` |
| `carta/ui/progress.py` | Create | `Progress` class — TTY/plain rendering |
| `carta/tests/test_progress.py` | Create | Unit tests for Progress |
| `carta/embed/pipeline.py` | Modify | Add `progress` param to `run_embed`, `run_embed_file`, `_embed_one_file` |
| `carta/scanner/scanner.py` | Modify | Add `progress` param to `run_scan` |
| `carta/cli.py` | Modify | Construct `Progress` in `cmd_embed` and `cmd_scan` |

---

## Task 1: Create `Progress` — plain-mode output (TDD)

**Files:**
- Create: `carta/ui/__init__.py`
- Create: `carta/ui/progress.py`
- Create: `carta/tests/test_progress.py`

- [ ] **Step 1: Write failing tests for plain-mode output**

Create `carta/tests/test_progress.py`:

```python
"""Tests for carta/ui/progress.py."""

import sys
from io import StringIO
from unittest.mock import patch

import pytest

from carta.ui.progress import Progress


def make_plain(total=3):
    """Return a Progress instance forced into plain mode (non-TTY)."""
    with patch("sys.stdout") as mock_stdout:
        mock_stdout.isatty.return_value = False
        p = Progress(total=total)
    p._tty = False  # ensure plain mode regardless of test runner TTY
    return p


class TestPlainMode:
    def test_file_prints_header(self, capsys):
        p = make_plain(total=3)
        p.file(idx=1, name="foo.pdf")
        captured = capsys.readouterr()
        assert "[1/3]" in captured.out
        assert "foo.pdf" in captured.out

    def test_step_prints_message(self, capsys):
        p = make_plain(total=3)
        p.file(idx=1, name="foo.pdf")
        p.step("extracting 5 pages")
        captured = capsys.readouterr()
        assert "extracting 5 pages" in captured.out

    def test_done_prints_ok_line(self, capsys):
        p = make_plain(total=3)
        p.file(idx=1, name="foo.pdf")
        p.done(chunks=42, elapsed=3.7)
        captured = capsys.readouterr()
        assert "OK" in captured.out
        assert "42" in captured.out
        assert "foo.pdf" in captured.out

    def test_skip_prints_skip_line(self, capsys):
        p = make_plain(total=3)
        p.file(idx=2, name="bar.pdf")
        p.skip(reason="LFS pointer")
        captured = capsys.readouterr()
        assert "SKIP" in captured.out
        assert "LFS pointer" in captured.out

    def test_error_prints_to_stderr(self, capsys):
        p = make_plain(total=3)
        p.file(idx=3, name="baz.pdf")
        p.error("Qdrant timeout")
        captured = capsys.readouterr()
        assert "ERROR" in captured.err
        assert "Qdrant timeout" in captured.err

    def test_summary_prints_counts(self, capsys):
        p = make_plain(total=3)
        p.summary(embedded=2, skipped=1, errors=0)
        captured = capsys.readouterr()
        assert "2" in captured.out
        assert "1" in captured.out

    def test_summary_shows_errors_when_nonzero(self, capsys):
        p = make_plain(total=3)
        p.summary(embedded=1, skipped=0, errors=2)
        captured = capsys.readouterr()
        assert "2" in captured.out  # errors count

    def test_scan_step_prints_message(self, capsys):
        p = make_plain(total=0)
        p.scan_step("checking frontmatter")
        captured = capsys.readouterr()
        assert "checking frontmatter" in captured.out

    def test_scan_done_prints_summary(self, capsys):
        p = make_plain(total=0)
        p.scan_done(elapsed=0.8, issue_count=5)
        captured = capsys.readouterr()
        assert "5" in captured.out

    def test_context_manager_enters_and_exits(self):
        p = make_plain(total=1)
        with p as ctx:
            assert ctx is p

    def test_exit_does_not_raise_on_clean_exit(self):
        p = make_plain(total=1)
        with p:
            pass  # no active line written — should not crash
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
cd /Users/ian/dev/doc-audit-cc
python -m pytest carta/tests/test_progress.py -v 2>&1 | head -30
```

Expected: `ModuleNotFoundError: No module named 'carta.ui'`

- [ ] **Step 3: Create `carta/ui/__init__.py`**

```python
from carta.ui.progress import Progress

__all__ = ["Progress"]
```

- [ ] **Step 4: Create `carta/ui/progress.py` with plain-mode implementation**

```python
"""Interactive progress reporting for carta embed and scan.

Auto-detects TTY vs plain mode. Pass a Progress instance into pipeline
functions; pass progress=None to suppress all output (used by MCP server
and tests).
"""

import os
import sys
import time

_SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

# ANSI escape codes
_RESET  = "\033[0m"
_BOLD   = "\033[1m"
_DIM    = "\033[2m"
_GREEN  = "\033[32m"
_RED    = "\033[31m"
_CYAN   = "\033[36m"
_CLR    = "\r\033[K"   # move to line start + clear to end


class Progress:
    """Context manager for progress reporting during embed and scan.

    TTY mode: in-place spinner lines with ANSI color.
    Plain mode: scrolling print statements (same as verbose=True output).

    Usage::

        with Progress(total=12) as p:
            p.file(idx=1, name="foo.pdf")
            p.step("extracting 10 pages")
            p.done(chunks=80, elapsed=5.2)
        p.summary(embedded=10, skipped=1, errors=1)
    """

    def __init__(self, total: int = 0):
        self._total = total
        self._idx = 0
        self._name = ""
        self._frame = 0
        self._start: float = 0.0
        self._tty = sys.stdout.isatty()
        self._no_color = "NO_COLOR" in os.environ
        self._active = False  # True while a \r spinner line is "open"

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    def __enter__(self) -> "Progress":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self._active and self._tty:
            sys.stdout.write("\n")
            sys.stdout.flush()
        return False  # never suppress exceptions

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _c(self, code: str, text: str) -> str:
        """Apply ANSI code unless plain mode or NO_COLOR."""
        if not self._tty or self._no_color:
            return text
        return f"{code}{text}{_RESET}"

    def _spin(self) -> str:
        ch = _SPINNER_FRAMES[self._frame % len(_SPINNER_FRAMES)]
        self._frame += 1
        return ch

    def _elapsed(self) -> float:
        return time.monotonic() - self._start if self._start else 0.0

    # ------------------------------------------------------------------
    # Embed progress API
    # ------------------------------------------------------------------

    def file(self, idx: int, name: str) -> None:
        """Signal that a new file is starting."""
        self._idx = idx
        self._name = name
        self._start = time.monotonic()
        if not self._tty:
            print(f"  [{idx}/{self._total}] Embedding: {name} ...", flush=True)

    def step(self, msg: str) -> None:
        """Report a sub-step within the current file."""
        if self._tty:
            sp   = self._c(_CYAN, self._spin())
            idx  = self._c(_DIM, f"{self._idx}/{self._total}")
            name = self._c(_BOLD, self._name)
            sub  = self._c(_DIM, f"▸ {msg}")
            el   = self._c(_DIM, f"{self._elapsed():.0f}s")
            sys.stdout.write(f"{_CLR}{sp}  {idx}  {name}  {sub}  {el}")
            sys.stdout.flush()
            self._active = True
        else:
            print(f"    {msg}", flush=True)

    def done(self, chunks: int, elapsed: float) -> None:
        """Signal that the current file completed successfully."""
        if self._tty:
            check  = self._c(_GREEN, "✓")
            idx    = self._c(_DIM,  f"{self._idx}/{self._total}")
            name   = self._c(_BOLD, self._name)
            chunks_s = self._c(_DIM, f"{chunks} chunks")
            el_s   = self._c(_DIM,  f"{elapsed:.1f}s")
            sys.stdout.write(f"{_CLR}{check}  {idx}  {name}  {chunks_s}  {el_s}\n")
            sys.stdout.flush()
            self._active = False
        else:
            print(
                f"  [{self._idx}/{self._total}] OK: {self._name}"
                f" — {chunks} chunk(s) in {elapsed:.1f}s",
                flush=True,
            )

    def skip(self, reason: str) -> None:
        """Signal that the current file was skipped."""
        if self._tty:
            dash   = self._c(_DIM, "–")
            idx    = self._c(_DIM, f"{self._idx}/{self._total}")
            name   = self._c(_DIM, self._name)
            reason_s = self._c(_DIM, f"skipped: {reason}")
            sys.stdout.write(f"{_CLR}{dash}  {idx}  {name}  {reason_s}\n")
            sys.stdout.flush()
            self._active = False
        else:
            print(
                f"  [{self._idx}/{self._total}] SKIP ({reason}): {self._name}",
                flush=True,
            )

    def error(self, msg: str) -> None:
        """Signal that the current file errored."""
        if self._tty:
            x      = self._c(_RED,  "✗")
            idx    = self._c(_DIM,  f"{self._idx}/{self._total}")
            name   = self._c(_BOLD, self._name)
            err    = self._c(_RED,  f"ERROR: {msg}")
            sys.stdout.write(f"{_CLR}{x}  {idx}  {name}  {err}\n")
            sys.stdout.flush()
            self._active = False
        else:
            print(
                f"  [{self._idx}/{self._total}] ERROR: {self._name}: {msg}",
                file=sys.stderr,
                flush=True,
            )

    def summary(self, embedded: int, skipped: int, errors: int) -> None:
        """Print final embed summary line."""
        if self._tty:
            parts = [
                self._c(_GREEN, f"Embedded: {embedded}"),
                self._c(_DIM,   f"Skipped: {skipped}"),
            ]
            if errors:
                parts.append(self._c(_RED, f"Errors: {errors}"))
            print("  ".join(parts), flush=True)
        else:
            print(
                f"Embedded: {embedded}, Skipped: {skipped}, Errors: {errors}",
                flush=True,
            )

    # ------------------------------------------------------------------
    # Scan progress API
    # ------------------------------------------------------------------

    def scan_step(self, msg: str) -> None:
        """Report the current scan check phase."""
        if self._tty:
            sp  = self._c(_CYAN, self._spin())
            lbl = self._c(_BOLD, "Scanning")
            sub = self._c(_DIM,  msg)
            sys.stdout.write(f"{_CLR}{sp}  {lbl}  {sub}")
            sys.stdout.flush()
            self._active = True
        else:
            print(f"  {msg}", flush=True)

    def scan_done(self, elapsed: float, issue_count: int) -> None:
        """Print final scan summary line."""
        if self._tty:
            check = self._c(_GREEN, "✓")
            lbl   = self._c(_BOLD, "Scan complete")
            n     = self._c(_DIM if issue_count == 0 else _RED, f"{issue_count} issue(s)")
            el    = self._c(_DIM, f"{elapsed:.1f}s")
            sys.stdout.write(f"{_CLR}{check}  {lbl} — {n}  {el}\n")
            sys.stdout.flush()
            self._active = False
        else:
            print(f"Scan complete — {issue_count} issue(s)", flush=True)
```

- [ ] **Step 5: Run tests to confirm they pass**

```bash
python -m pytest carta/tests/test_progress.py -v
```

Expected: all 12 tests pass.

- [ ] **Step 6: Commit**

```bash
git add carta/ui/__init__.py carta/ui/progress.py carta/tests/test_progress.py
git commit -m "feat: add Progress class for interactive embed/scan status"
```

---

## Task 2: Add TTY-mode tests

**Files:**
- Modify: `carta/tests/test_progress.py`

- [ ] **Step 1: Add TTY-mode tests**

Append to `carta/tests/test_progress.py`:

```python
class TestTTYMode:
    """TTY-mode methods write ANSI sequences to stdout — verify no exceptions and content."""

    def _make_tty(self, total=3):
        p = Progress(total=total)
        p._tty = True
        p._no_color = False
        return p

    def test_file_in_tty_does_not_print(self, capsys):
        p = self._make_tty()
        p.file(idx=1, name="foo.pdf")
        captured = capsys.readouterr()
        # file() is silent in TTY mode (first output comes from step/done)
        assert captured.out == ""

    def test_step_writes_to_stdout(self, capsys):
        p = self._make_tty()
        p.file(idx=1, name="foo.pdf")
        p.step("extracting 5 pages")
        captured = capsys.readouterr()
        assert "foo.pdf" in captured.out
        assert "extracting 5 pages" in captured.out
        assert "\r" in captured.out  # in-place rewrite

    def test_done_writes_newline(self, capsys):
        p = self._make_tty()
        p.file(idx=1, name="foo.pdf")
        p.done(chunks=10, elapsed=1.0)
        captured = capsys.readouterr()
        assert captured.out.endswith("\n")
        assert "foo.pdf" in captured.out

    def test_skip_writes_newline(self, capsys):
        p = self._make_tty()
        p.file(idx=2, name="bar.pdf")
        p.skip(reason="LFS pointer")
        captured = capsys.readouterr()
        assert captured.out.endswith("\n")
        assert "LFS pointer" in captured.out

    def test_error_writes_newline(self, capsys):
        p = self._make_tty()
        p.file(idx=3, name="baz.pdf")
        p.error("Qdrant timeout")
        captured = capsys.readouterr()
        assert captured.out.endswith("\n")
        assert "Qdrant timeout" in captured.out

    def test_exit_clears_active_spinner_line(self, capsys):
        p = self._make_tty()
        p.file(idx=1, name="foo.pdf")
        p.step("working...")         # sets _active = True
        p.__exit__(None, None, None)
        captured = capsys.readouterr()
        # Should have written a newline to terminate the spinner line
        assert "\n" in captured.out

    def test_exit_no_extra_newline_when_not_active(self, capsys):
        p = self._make_tty()
        # No step() called — _active is False
        p.__exit__(None, None, None)
        captured = capsys.readouterr()
        assert captured.out == ""

    def test_no_color_suppresses_ansi(self):
        p = Progress(total=1)
        p._tty = True
        p._no_color = True
        result = p._c("\033[32m", "hello")
        assert result == "hello"
        assert "\033" not in result

    def test_scan_step_writes_spinner(self, capsys):
        p = self._make_tty(total=0)
        p.scan_step("checking frontmatter")
        captured = capsys.readouterr()
        assert "checking frontmatter" in captured.out
        assert "\r" in captured.out

    def test_scan_done_writes_newline(self, capsys):
        p = self._make_tty(total=0)
        p.scan_done(elapsed=0.8, issue_count=3)
        captured = capsys.readouterr()
        assert captured.out.endswith("\n")
        assert "3" in captured.out
```

- [ ] **Step 2: Run all progress tests**

```bash
python -m pytest carta/tests/test_progress.py -v
```

Expected: all 22 tests pass.

- [ ] **Step 3: Commit**

```bash
git add carta/tests/test_progress.py
git commit -m "test: add TTY-mode coverage for Progress"
```

---

## Task 3: Integrate `_embed_one_file` with progress

**Files:**
- Modify: `carta/embed/pipeline.py` (lines 105–262, `_embed_one_file`)

- [ ] **Step 1: Update `_embed_one_file` signature and add step() calls**

Replace the `_embed_one_file` function signature and its `verbose` print calls:

```python
def _embed_one_file(
    file_path: Path,
    file_info: dict,
    cfg: dict,
    client,
    repo_root: Path,
    max_tokens: int,
    overlap_fraction: float,
    verbose: bool = False,
    progress=None,
) -> tuple[int, dict]:
```

Inside the function body, replace the three `if verbose: print(...)` blocks:

```python
    # Replace: if verbose: print(f"    extracting {file_path.suffix} text...", flush=True)
    if progress:
        progress.step(f"extracting {file_path.suffix} text")
    elif verbose:
        print(f"    extracting {file_path.suffix} text...", flush=True)
```

```python
    # Replace: if verbose: print(f"    extracted {len(pages)} page(s); chunking...", flush=True)
    if progress:
        progress.step(f"chunking {len(pages)} page(s)")
    elif verbose:
        print(f"    extracted {len(pages)} page(s); chunking...", flush=True)
```

```python
    # Replace: if verbose: print(f"    built {len(raw_chunks)} chunk(s); embedding + upserting...", flush=True)
    if progress:
        progress.step(f"embedding {len(enriched)} chunks → Qdrant")
    elif verbose:
        print(f"    built {len(raw_chunks)} chunk(s); embedding + upserting...", flush=True)
```

Also add step calls around the vision/ColPali paths (both inside the `if file_path.suffix == ".pdf":` block):

```python
    # Before the ColPali block:
    if colpali_enabled:
        if progress:
            progress.step("ColPali: embedding visual pages")
        try:
            ...
```

```python
    # Before the intelligent extraction block:
    if progress:
        progress.step("extracting image descriptions")
    try:
        from carta.vision.router import extract_image_descriptions_intelligent
        ...
```

- [ ] **Step 2: Verify existing pipeline tests still pass**

```bash
python -m pytest carta/tests/test_pipeline.py -v
```

Expected: all tests pass (they don't pass a `progress` argument, so default `None` is used).

- [ ] **Step 3: Commit**

```bash
git add carta/embed/pipeline.py
git commit -m "feat: add progress param to _embed_one_file with step() calls"
```

---

## Task 4: Integrate `run_embed` outer loop with progress

**Files:**
- Modify: `carta/embed/pipeline.py` (`run_embed` and `run_embed_file`)

- [ ] **Step 1: Update `run_embed` signature**

Change:
```python
def run_embed(repo_root: Path, cfg: dict, verbose: bool = False) -> dict:
```
To:
```python
def run_embed(repo_root: Path, cfg: dict, verbose: bool = False, progress=None) -> dict:
```

- [ ] **Step 2: Update the file loop in `run_embed`**

The loop starting at line 628. Replace the per-file print blocks and the executor call:

```python
    for idx, file_info in enumerate(pending, start=1):
        file_path: Path = file_info["file_path"]
        sidecar_path: Path = file_info["sidecar_path"]

        # LFS guard
        if is_lfs_pointer(file_path):
            if progress:
                progress.file(idx, file_path.name)
                progress.skip("LFS pointer")
            elif verbose:
                print(f"  [{idx}/{total}] SKIP (LFS pointer): {file_path.name}", flush=True)
            summary["skipped"] += 1
            continue

        if progress:
            progress.file(idx, file_path.name)
        elif verbose:
            print(f"  [{idx}/{total}] Embedding: {file_path.name} ...", flush=True)
        t0 = time.monotonic()

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(
                _embed_one_file,
                file_path, file_info, cfg, client, repo_root,
                max_tokens, overlap_fraction, verbose, progress,
            )
            try:
                count, sidecar_updates = future.result(timeout=FILE_TIMEOUT_S)
                _update_sidecar(sidecar_path, sidecar_updates)
                elapsed = time.monotonic() - t0
                if progress:
                    progress.done(chunks=count, elapsed=elapsed)
                elif verbose:
                    print(f"  [{idx}/{total}] OK: {file_path.name} — {count} chunk(s) in {elapsed:.1f}s", flush=True)
                summary["embedded"] += 1
            except concurrent.futures.TimeoutError:
                if progress:
                    progress.skip(f"timeout after {FILE_TIMEOUT_S}s")
                elif verbose:
                    print(
                        f"  [{idx}/{total}] TIMEOUT: {file_path.name} exceeded {FILE_TIMEOUT_S}s -- skipping",
                        flush=True,
                    )
                print(
                    f"  TIMEOUT: {file_path.name} exceeded {FILE_TIMEOUT_S}s",
                    file=sys.stderr, flush=True,
                )
                summary["skipped"] += 1
            except Exception as e:
                elapsed = time.monotonic() - t0
                if progress:
                    progress.error(str(e))
                print(
                    f"  [{idx}/{total}] ERROR: {file_path.name} ({elapsed:.1f}s): {e}",
                    file=sys.stderr, flush=True,
                )
                summary["errors"].append(f"Error processing {file_path.name}: {e}")
```

- [ ] **Step 3: Update `run_embed_file` signature**

Change:
```python
def run_embed_file(path: Path, cfg: dict, force: bool = False, verbose: bool = False) -> dict:
```
To:
```python
def run_embed_file(path: Path, cfg: dict, force: bool = False, verbose: bool = False, progress=None) -> dict:
```

Pass `progress` through to `_embed_one_file` in the `run_embed_file` call:

```python
    count, sidecar_updates = _embed_one_file(
        file_path, file_info, cfg, client, repo_root, max_tokens, overlap_fraction, verbose, progress
    )
```

- [ ] **Step 4: Run all pipeline tests**

```bash
python -m pytest carta/tests/test_pipeline.py -v
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add carta/embed/pipeline.py
git commit -m "feat: add progress param to run_embed and run_embed_file"
```

---

## Task 5: Integrate `run_scan` with progress

**Files:**
- Modify: `carta/scanner/scanner.py` (`run_scan`)

- [ ] **Step 1: Update `run_scan` signature**

Change:
```python
def run_scan(
    repo_root: Path,
    cfg: dict,
    output_path: Optional[Path] = None,
    reference_date: Optional[date] = None,
    verbose: bool = False,
) -> dict:
```
To:
```python
def run_scan(
    repo_root: Path,
    cfg: dict,
    output_path: Optional[Path] = None,
    reference_date: Optional[date] = None,
    verbose: bool = False,
    progress=None,
) -> dict:
```

- [ ] **Step 2: Add `scan_step` calls at each phase in `run_scan`**

Add a `t0 = time.monotonic()` near the top of the function body (just after the `ref_date` line):

```python
    import time
    t0 = time.monotonic()
    ref_date = reference_date or date.today()
```

Then add progress calls before each major block:

```python
    # Before: issues.extend(check_homeless_docs(repo_root, cfg))
    if progress:
        progress.scan_step("checking structure")
    issues.extend(check_homeless_docs(repo_root, cfg))
    issues.extend(check_nested_docs_folders(repo_root, cfg))
```

```python
    # Before the per-doc loop:
    if progress:
        progress.scan_step(f"checking frontmatter and links ({len(tracked_docs)} docs)")
    threshold = cfg.get("stale_threshold_days", 30)
    for doc_path in tracked_docs:
        ...
```

```python
    # Before embed file type checks:
    if progress:
        progress.scan_step("checking embeddable files")
    issues.extend(check_embed_induction_needed(repo_root, cfg))
    issues.extend(check_embed_lfs_not_pulled(repo_root, cfg))
    issues.extend(check_embed_transcript_unprocessed(repo_root, cfg))
```

```python
    # Before sidecar checks:
    if progress:
        progress.scan_step(f"checking sidecars ({len(sidecar_files)} files)")
    for sidecar_path in sidecar_files:
        ...
```

```python
    # After writing output, before return:
    if progress:
        progress.scan_done(elapsed=time.monotonic() - t0, issue_count=len(issues))
    return result
```

- [ ] **Step 3: Run existing scanner tests (if any)**

```bash
python -m pytest carta/tests/ -k "scan" -v
```

Expected: all existing scan tests pass.

- [ ] **Step 4: Commit**

```bash
git add carta/scanner/scanner.py
git commit -m "feat: add progress param to run_scan with scan_step() calls"
```

---

## Task 6: Wire up in `cli.py`

**Files:**
- Modify: `carta/cli.py`

- [ ] **Step 1: Update `cmd_embed` to create and pass Progress**

Replace the current `cmd_embed` function body from the `summary = run_embed(...)` call to the end of the function:

```python
def cmd_embed(args):
    from carta.config import load_config
    from carta.embed.pipeline import run_embed, discover_pending_files
    from carta.ui import Progress

    cfg_path = find_config()
    cfg = load_config(cfg_path)
    if not cfg["modules"].get("doc_embed"):
        print("doc_embed module is disabled in config.", file=sys.stderr)
        sys.exit(1)

    # FT-5: Concurrency lock — only one embed process at a time (atomic create + stale PID).
    lock_path = cfg_path.parent / "embed.lock"
    _acquire_embed_lock(lock_path)

    def _remove_lock():
        try:
            lock_path.unlink(missing_ok=True)
        except OSError:
            pass

    atexit.register(_remove_lock)

    def _signal_handler(signum, frame):
        _remove_lock()
        sys.exit(128 + signum)

    for _sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(_sig, _signal_handler)

    # Discover pending count upfront so Progress knows the total.
    # run_embed will also call discover_pending_files internally — that's fine,
    # it's a cheap filesystem scan.
    repo_root = cfg_path.parent.parent
    pending = discover_pending_files(repo_root)

    with Progress(total=len(pending)) as progress:
        summary = run_embed(repo_root, cfg, verbose=False, progress=progress)
    progress.summary(
        embedded=summary["embedded"],
        skipped=summary["skipped"],
        errors=len(summary["errors"]),
    )
    if summary["errors"]:
        sys.exit(1)
```

- [ ] **Step 2: Update `cmd_scan` to create and pass Progress**

Replace:
```python
def cmd_scan(args):
    from carta.config import load_config
    from carta.scanner.scanner import run_scan
    cfg_path = find_config()
    cfg = load_config(cfg_path)
    if not cfg["modules"].get("doc_audit"):
        print("doc_audit module is disabled in config.", file=sys.stderr)
        sys.exit(1)
    output_path = cfg_path.parent / "scan-results.json"
    results = run_scan(cfg_path.parent.parent, cfg, output_path=output_path, verbose=True)
    issue_count = len(results["issues"])
    print(f"Scan complete: {issue_count} issue(s). Results at {output_path}")
```

With:
```python
def cmd_scan(args):
    from carta.config import load_config
    from carta.scanner.scanner import run_scan
    from carta.ui import Progress
    cfg_path = find_config()
    cfg = load_config(cfg_path)
    if not cfg["modules"].get("doc_audit"):
        print("doc_audit module is disabled in config.", file=sys.stderr)
        sys.exit(1)
    output_path = cfg_path.parent / "scan-results.json"
    with Progress() as progress:
        results = run_scan(
            cfg_path.parent.parent, cfg,
            output_path=output_path,
            verbose=False,
            progress=progress,
        )
    issue_count = len(results["issues"])
    print(f"Results at {output_path}")
```

Note: `scan_done()` already prints the "Scan complete — N issue(s)" line, so the final `print` just shows the output path.

- [ ] **Step 3: Run full test suite**

```bash
python -m pytest carta/tests/ -v
```

Expected: all tests pass.

- [ ] **Step 4: Smoke test in a terminal (TTY mode)**

In a terminal (not piped), run from any carta-initialized repo:

```bash
carta scan
```

Expected: spinner animates through scan phases, then a green `✓  Scan complete — N issue(s)` line.

```bash
carta embed
```

Expected: per-file spinner lines with sub-steps, completed files scroll into history with `✓`.

- [ ] **Step 5: Smoke test plain mode (non-TTY)**

```bash
carta embed 2>&1 | cat
```

Expected: scrolling plain-text lines with no ANSI codes or `\r`.

- [ ] **Step 6: Final commit**

```bash
git add carta/cli.py
git commit -m "feat: wire Progress into cmd_embed and cmd_scan"
```
