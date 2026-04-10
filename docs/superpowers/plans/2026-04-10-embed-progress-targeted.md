# Embed Progress Bar + Targeted File Embed Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a visual `[=====>    ]` progress bar to the embed spinner, and let users run `carta embed file.pdf` to embed a specific file immediately without the concurrency lock.

**Architecture:** Feature 1 is contained entirely in `carta/ui/progress.py` — a new `_bar()` helper added to the `Progress` class, wired into the existing `_write_embed_line()`, `done()`, `skip()`, and `error()` methods. Feature 2 adds an optional `files` positional arg to the `embed` CLI subparser; when files are present, `cmd_embed` skips lock acquisition and calls the already-existing `run_embed_file()` per path with `force=True`.

**Tech Stack:** Python 3.10+ stdlib only. `carta/ui/progress.py`, `carta/cli.py`. No new dependencies.

---

### Task 1: Progress Bar — `_bar()` helper and updated display methods

**Files:**
- Modify: `carta/ui/progress.py`
- Test: `carta/tests/test_progress_bar.py` (create)

---

- [ ] **Step 1: Write failing tests for `_bar()`**

Create `carta/tests/test_progress_bar.py`:

```python
"""Tests for Progress._bar() helper."""
import sys
import os
import pytest

# Ensure carta package is importable
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[2]))


from carta.ui.progress import Progress


def _plain_bar(p: Progress) -> str:
    """Get bar text with NO_COLOR so ANSI codes are stripped."""
    original = p._no_color
    p._no_color = True
    result = p._bar()
    p._no_color = original
    return result


def test_bar_empty_at_zero():
    p = Progress(total=10)
    p._idx = 0
    assert _plain_bar(p) == "[----------]"


def test_bar_full_at_total():
    p = Progress(total=10)
    p._idx = 10
    assert _plain_bar(p) == "[==========]"


def test_bar_halfway():
    p = Progress(total=10)
    p._idx = 5
    bar = _plain_bar(p)
    # 5/10 = 50% → 5 filled chars
    assert bar == "[====>     ]"


def test_bar_one_of_ten():
    p = Progress(total=10)
    p._idx = 1
    bar = _plain_bar(p)
    assert bar == "[>         ]"


def test_bar_unknown_total():
    """total=0 means unknown — render all dashes."""
    p = Progress(total=0)
    p._idx = 0
    assert _plain_bar(p) == "[----------]"


def test_bar_idx_exceeds_total():
    """Guard: idx > total should clamp to full bar."""
    p = Progress(total=5)
    p._idx = 99
    assert _plain_bar(p) == "[==========]"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /Users/ian/dev/doc-audit-cc
python -m pytest carta/tests/test_progress_bar.py -v 2>&1 | head -30
```

Expected: `AttributeError: 'Progress' object has no attribute '_bar'`

- [ ] **Step 3: Implement `_bar()` in `progress.py`**

In `carta/ui/progress.py`, add this method to the `Progress` class, after `_elapsed()` (around line 132):

```python
def _bar(self) -> str:
    """Render a 10-char filled progress bar. Caller need not hold _lock."""
    width = 10
    if self._total == 0:
        bar_inner = "-" * width
    else:
        filled = min(width, round(self._idx / self._total * width))
        if filled == 0:
            bar_inner = "-" * width
        elif filled == width:
            bar_inner = "=" * width
        else:
            bar_inner = "=" * (filled - 1) + ">" + " " * (width - filled)
    return self._c(_DIM, f"[{bar_inner}]")
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
python -m pytest carta/tests/test_progress_bar.py -v
```

Expected: all 6 tests PASS.

- [ ] **Step 5: Wire `_bar()` into `_write_embed_line()`**

Replace the existing `_write_embed_line` method body in `carta/ui/progress.py`:

```python
def _write_embed_line(self) -> None:
    """Redraw embed spinner line. Caller must hold _lock."""
    sp   = self._c(_CYAN, self._spin())
    bar  = self._bar()
    idx  = self._c(_DIM, f"{self._idx}/{self._total}")
    name = self._c(_BOLD, self._name)
    sub  = self._c(_DIM, f"▸ {self._current_msg}")
    el   = self._c(_DIM, f"{self._elapsed():.0f}s")
    sys.stdout.write(f"{_CLR}{sp}  {bar} {idx}  {name}  {sub}  {el}")
    sys.stdout.flush()
```

- [ ] **Step 6: Add bar to `done()` completion line**

Replace the TTY branch of `done()` in `progress.py`:

```python
def done(self, chunks: int, elapsed: float) -> None:
    """Signal that the current file completed successfully."""
    if self._tty:
        with self._lock:
            self._active = False
            self._current_msg = ""
            check    = self._c(_GREEN, "✓")
            bar      = self._bar()
            idx      = self._c(_DIM,   f"{self._idx}/{self._total}")
            name     = self._c(_BOLD,  self._name)
            chunks_s = self._c(_DIM,   f"{chunks} chunks")
            el_s     = self._c(_DIM,   f"{elapsed:.1f}s")
            sys.stdout.write(f"{_CLR}{check}  {bar} {idx}  {name}  {chunks_s}  {el_s}\n")
            sys.stdout.flush()
    else:
        print(
            f"  [{self._idx}/{self._total}] OK: {self._name}"
            f" — {chunks} chunk(s) in {elapsed:.1f}s",
            flush=True,
        )
```

- [ ] **Step 7: Add bar to `skip()` completion line**

Replace the TTY branch of `skip()` in `progress.py`:

```python
def skip(self, reason: str) -> None:
    """Signal that the current file was skipped."""
    if self._tty:
        with self._lock:
            self._active = False
            self._current_msg = ""
            dash     = self._c(_DIM, "–")
            bar      = self._bar()
            idx      = self._c(_DIM, f"{self._idx}/{self._total}")
            name     = self._c(_DIM, self._name)
            reason_s = self._c(_DIM, f"skipped: {reason}")
            sys.stdout.write(f"{_CLR}{dash}  {bar} {idx}  {name}  {reason_s}\n")
            sys.stdout.flush()
    else:
        print(
            f"  [{self._idx}/{self._total}] SKIP ({reason}): {self._name}",
            flush=True,
        )
```

- [ ] **Step 8: Add bar to `error()` completion line**

Replace the TTY branch of `error()` in `progress.py`:

```python
def error(self, msg: str) -> None:
    """Signal that the current file errored."""
    if self._tty:
        with self._lock:
            self._active = False
            self._current_msg = ""
            x    = self._c(_RED,  "✗")
            bar  = self._bar()
            idx  = self._c(_DIM,  f"{self._idx}/{self._total}")
            name = self._c(_BOLD, self._name)
            err  = self._c(_RED,  f"ERROR: {msg}")
            sys.stderr.write(f"{_CLR}{x}  {bar} {idx}  {name}  {err}\n")
            sys.stderr.flush()
    else:
        print(
            f"  [{self._idx}/{self._total}] ERROR: {self._name}: {msg}",
            file=sys.stderr,
            flush=True,
        )
```

- [ ] **Step 9: Run full test suite to verify no regressions**

```bash
python -m pytest carta/tests/ -v 2>&1 | tail -20
```

Expected: all existing tests still pass; `test_progress_bar.py` all pass.

- [ ] **Step 10: Commit**

```bash
git add carta/ui/progress.py carta/tests/test_progress_bar.py
git commit -m "feat(ui): add visual progress bar to embed spinner"
```

---

### Task 2: Targeted Embed — `carta embed file.pdf`

**Files:**
- Modify: `carta/cli.py`
- Test: `carta/tests/test_embed_targeted.py` (create)

---

- [ ] **Step 1: Write failing tests for targeted embed CLI**

Create `carta/tests/test_embed_targeted.py`:

```python
"""Tests for carta embed <files> targeted path."""
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch, call
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def _make_args(files):
    args = MagicMock()
    args.files = files
    return args


@patch("carta.cli.find_config")
@patch("carta.cli.load_config")
@patch("carta.embed.pipeline.run_embed_file")
def test_targeted_calls_run_embed_file(mock_run_embed_file, mock_load_config, mock_find_config, tmp_path):
    """When files are passed, run_embed_file is called for each, lock is skipped."""
    from carta.cli import cmd_embed

    cfg_path = tmp_path / ".carta" / "config.yaml"
    cfg_path.parent.mkdir(parents=True)
    cfg_path.touch()
    mock_find_config.return_value = cfg_path
    mock_load_config.return_value = {
        "modules": {"doc_embed": True},
        "qdrant_url": "http://localhost:6333",
        "embed": {"ollama_url": "http://localhost:11434", "ollama_model": "nomic-embed-text"},
    }
    mock_run_embed_file.return_value = {"status": "ok", "chunks": 42}

    pdf = tmp_path / "test.pdf"
    pdf.touch()

    with patch("carta.cli._acquire_embed_lock") as mock_lock, \
         patch("carta.ui.Progress") as MockProgress:
        mock_progress = MagicMock()
        mock_progress.__enter__ = MagicMock(return_value=mock_progress)
        mock_progress.__exit__ = MagicMock(return_value=False)
        MockProgress.return_value = mock_progress

        with pytest.raises(SystemExit) as exc_info:
            cmd_embed(_make_args([str(pdf)]))

        assert exc_info.value.code == 0
        # Lock must NOT be acquired for targeted embed
        mock_lock.assert_not_called()
        # run_embed_file called with force=True
        mock_run_embed_file.assert_called_once_with(
            Path(str(pdf)), mock_load_config.return_value, force=True, progress=mock_progress
        )


@patch("carta.cli.find_config")
@patch("carta.cli.load_config")
@patch("carta.embed.pipeline.run_embed_file")
def test_targeted_missing_file_exits_1(mock_run_embed_file, mock_load_config, mock_find_config, tmp_path):
    """FileNotFoundError from run_embed_file causes exit(1)."""
    from carta.cli import cmd_embed

    cfg_path = tmp_path / ".carta" / "config.yaml"
    cfg_path.parent.mkdir(parents=True)
    cfg_path.touch()
    mock_find_config.return_value = cfg_path
    mock_load_config.return_value = {
        "modules": {"doc_embed": True},
        "qdrant_url": "http://localhost:6333",
        "embed": {},
    }
    mock_run_embed_file.side_effect = FileNotFoundError("no such file: ghost.pdf")

    with patch("carta.ui.Progress") as MockProgress:
        mock_progress = MagicMock()
        mock_progress.__enter__ = MagicMock(return_value=mock_progress)
        mock_progress.__exit__ = MagicMock(return_value=False)
        MockProgress.return_value = mock_progress

        with pytest.raises(SystemExit) as exc_info:
            cmd_embed(_make_args(["ghost.pdf"]))

        assert exc_info.value.code == 1


@patch("carta.cli.find_config")
@patch("carta.cli.load_config")
@patch("carta.embed.pipeline.run_embed_file")
def test_targeted_multiple_files_all_processed(mock_run_embed_file, mock_load_config, mock_find_config, tmp_path):
    """All files are processed even if one errors; exit 1 if any errors."""
    from carta.cli import cmd_embed

    cfg_path = tmp_path / ".carta" / "config.yaml"
    cfg_path.parent.mkdir(parents=True)
    cfg_path.touch()
    mock_find_config.return_value = cfg_path
    mock_load_config.return_value = {
        "modules": {"doc_embed": True},
        "qdrant_url": "http://localhost:6333",
        "embed": {},
    }
    mock_run_embed_file.side_effect = [
        {"status": "ok", "chunks": 10},
        FileNotFoundError("missing.pdf not found"),
        {"status": "ok", "chunks": 5},
    ]

    with patch("carta.ui.Progress") as MockProgress:
        mock_progress = MagicMock()
        mock_progress.__enter__ = MagicMock(return_value=mock_progress)
        mock_progress.__exit__ = MagicMock(return_value=False)
        MockProgress.return_value = mock_progress

        with pytest.raises(SystemExit) as exc_info:
            cmd_embed(_make_args(["a.pdf", "missing.pdf", "b.pdf"]))

        assert exc_info.value.code == 1
        assert mock_run_embed_file.call_count == 3
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python -m pytest carta/tests/test_embed_targeted.py -v 2>&1 | head -30
```

Expected: `TypeError` or `AttributeError` — `args.files` not recognized by current `cmd_embed`.

- [ ] **Step 3: Add `files` arg to embed subparser in `cli.py`**

In `carta/cli.py`, in the `main()` function, replace:

```python
sub.add_parser("embed")
```

with:

```python
embed_p = sub.add_parser("embed")
embed_p.add_argument(
    "files",
    nargs="*",
    help="Specific file(s) to embed immediately (skips full pipeline and lock)",
)
```

- [ ] **Step 4: Add targeted fast-path to `cmd_embed()` in `cli.py`**

In `carta/cli.py`, in `cmd_embed()`, add the targeted fast-path **before** the lock acquisition block. Insert after the module-disabled check (after line ~117) and before the `# FT-5` comment:

```python
    # Targeted embed: one or more specific files, no lock, no discovery scan.
    if getattr(args, "files", None):
        import time
        from carta.embed.pipeline import run_embed_file

        files = args.files
        embedded = 0
        errors = []

        with Progress(total=len(files)) as progress:
            for idx, file_arg in enumerate(files, start=1):
                file_path = Path(file_arg)
                progress.file(idx, file_path.name)
                t0 = time.monotonic()
                try:
                    result = run_embed_file(file_path, cfg, force=True, progress=progress)
                    elapsed = time.monotonic() - t0
                    progress.done(chunks=result.get("chunks", 0), elapsed=elapsed)
                    embedded += 1
                except FileNotFoundError as e:
                    progress.error(str(e))
                    errors.append(str(e))
                except Exception as e:
                    elapsed = time.monotonic() - t0
                    progress.error(str(e))
                    errors.append(f"{file_path.name}: {e}")

        progress.summary(embedded=embedded, skipped=0, errors=len(errors))
        _notify_if_update(cfg_path, cfg)
        sys.exit(1 if errors else 0)
```

Also add `from pathlib import Path` at the top of `cmd_embed` if `Path` isn't already imported at module level. (It is — `from pathlib import Path` is at the top of `cli.py` already.)

- [ ] **Step 5: Run tests to verify they pass**

```bash
python -m pytest carta/tests/test_embed_targeted.py -v
```

Expected: all 3 tests PASS.

- [ ] **Step 6: Run full test suite**

```bash
python -m pytest carta/tests/ -v 2>&1 | tail -20
```

Expected: all tests pass, no regressions.

- [ ] **Step 7: Smoke test manually (optional but recommended)**

```bash
# From the repo root, with Qdrant running:
carta embed --help
# Should show: positional argument [files ...]

# Try a real file:
carta embed docs/some-existing-doc.md
# Should show progress bar and embed without acquiring lock
```

- [ ] **Step 8: Commit**

```bash
git add carta/cli.py carta/tests/test_embed_targeted.py
git commit -m "feat(embed): support targeted file embed via carta embed <files>"
```
