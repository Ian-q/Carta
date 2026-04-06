# Carta Update Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `carta update` command, daily background version checks with notifications, and a fully automated release pipeline that syncs all version numbers from a single GitHub release tag.

**Architecture:** A new `carta/update/` module with two focused files — `checker.py` (PyPI fetch, cache, notification) and `updater.py` (install detection, upgrade subprocess). The checker is wired into all existing commands via a call at the end of each `cmd_*` function. A new GitHub Actions workflow replaces `publish.yml`, auto-bumping version numbers before building and publishing.

**Tech Stack:** Python stdlib (`json`, `datetime`, `subprocess`, `shutil`), `requests` (already a dependency), `importlib` (stdlib), GitHub Actions with `permissions: contents: write`.

---

## File Map

| File | Action | Responsibility |
|------|--------|---------------|
| `carta/update/__init__.py` | Create | Module marker (empty) |
| `carta/update/checker.py` | Create | PyPI fetch, 24h cache, notification string |
| `carta/update/updater.py` | Create | Install method detection, upgrade subprocess, `--check` output |
| `carta/tests/test_update.py` | Create | Tests for checker and updater |
| `carta/config.py` | Modify | Add `update_check: true` to DEFAULTS |
| `carta/cli.py` | Modify | Add `cmd_update`, register subcommand, wire `maybe_notify` into existing commands |
| `.github/workflows/release.yml` | Create | New unified release pipeline |
| `.github/workflows/publish.yml` | Delete | Replaced by release.yml |

---

## Task 1: `carta/update/checker.py` — version check and cache

**Files:**
- Create: `carta/update/__init__.py`
- Create: `carta/update/checker.py`
- Create: `carta/tests/test_update.py` (partial — checker tests only)

- [ ] **Step 1: Create the module init**

```python
# carta/update/__init__.py
```
(Empty file.)

- [ ] **Step 2: Write failing tests for checker**

```python
# carta/tests/test_update.py
import json
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# checker tests
# ---------------------------------------------------------------------------

def test_fetch_latest_returns_version_on_success():
    from carta.update.checker import _fetch_latest
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"info": {"version": "1.2.3"}}
    mock_resp.raise_for_status.return_value = None
    with patch("carta.update.checker.requests.get", return_value=mock_resp):
        assert _fetch_latest() == "1.2.3"


def test_fetch_latest_returns_none_on_network_error():
    from carta.update.checker import _fetch_latest
    with patch("carta.update.checker.requests.get", side_effect=Exception("timeout")):
        assert _fetch_latest() is None


def test_is_cache_stale_true_when_empty():
    from carta.update.checker import _is_cache_stale
    assert _is_cache_stale({}) is True


def test_is_cache_stale_true_when_old(tmp_path):
    from carta.update.checker import _is_cache_stale
    old_dt = (datetime.utcnow() - timedelta(hours=25)).isoformat()
    assert _is_cache_stale({"checked_at": old_dt}) is True


def test_is_cache_stale_false_when_fresh():
    from carta.update.checker import _is_cache_stale
    fresh_dt = datetime.utcnow().isoformat()
    assert _is_cache_stale({"checked_at": fresh_dt}) is False


def test_check_for_update_returns_message_when_newer_available(tmp_path):
    from carta.update.checker import check_for_update
    carta_dir = tmp_path / ".carta"
    carta_dir.mkdir()
    with patch("carta.update.checker._installed_version", return_value="0.3.0"), \
         patch("carta.update.checker._fetch_latest", return_value="0.4.0"):
        msg = check_for_update(carta_dir)
    assert msg is not None
    assert "0.4.0" in msg
    assert "carta update" in msg


def test_check_for_update_returns_none_when_up_to_date(tmp_path):
    from carta.update.checker import check_for_update
    carta_dir = tmp_path / ".carta"
    carta_dir.mkdir()
    with patch("carta.update.checker._installed_version", return_value="0.4.0"), \
         patch("carta.update.checker._fetch_latest", return_value="0.4.0"):
        assert check_for_update(carta_dir) is None


def test_check_for_update_returns_none_when_already_notified(tmp_path):
    from carta.update.checker import check_for_update
    carta_dir = tmp_path / ".carta"
    carta_dir.mkdir()
    # Write a fresh cache where notified == latest
    cache = {
        "checked_at": datetime.utcnow().isoformat(),
        "latest": "0.4.0",
        "notified": "0.4.0",
    }
    (carta_dir / "update-check.json").write_text(json.dumps(cache))
    with patch("carta.update.checker._installed_version", return_value="0.3.0"):
        assert check_for_update(carta_dir) is None


def test_check_for_update_uses_fresh_cache(tmp_path):
    """When cache is fresh, should not call PyPI."""
    from carta.update.checker import check_for_update
    carta_dir = tmp_path / ".carta"
    carta_dir.mkdir()
    cache = {
        "checked_at": datetime.utcnow().isoformat(),
        "latest": "0.4.0",
        "notified": "",
    }
    (carta_dir / "update-check.json").write_text(json.dumps(cache))
    with patch("carta.update.checker._installed_version", return_value="0.3.0"), \
         patch("carta.update.checker._fetch_latest") as mock_fetch:
        msg = check_for_update(carta_dir)
    mock_fetch.assert_not_called()
    assert msg is not None


def test_check_for_update_works_without_carta_dir():
    from carta.update.checker import check_for_update
    with patch("carta.update.checker._installed_version", return_value="0.3.0"), \
         patch("carta.update.checker._fetch_latest", return_value="0.4.0"):
        msg = check_for_update(None)
    assert msg is not None


def test_maybe_notify_prints_when_update_available(tmp_path, capsys):
    from carta.update.checker import maybe_notify
    carta_dir = tmp_path / ".carta"
    carta_dir.mkdir()
    with patch("carta.update.checker._installed_version", return_value="0.3.0"), \
         patch("carta.update.checker._fetch_latest", return_value="0.4.0"):
        maybe_notify(carta_dir, {"update_check": True})
    captured = capsys.readouterr()
    assert "0.4.0" in captured.out


def test_maybe_notify_silent_when_disabled(tmp_path, capsys):
    from carta.update.checker import maybe_notify
    carta_dir = tmp_path / ".carta"
    carta_dir.mkdir()
    with patch("carta.update.checker._fetch_latest", return_value="0.4.0"):
        maybe_notify(carta_dir, {"update_check": False})
    captured = capsys.readouterr()
    assert captured.out == ""
```

- [ ] **Step 3: Run tests to verify they fail**

```bash
cd /path/to/doc-audit-cc
pytest carta/tests/test_update.py -v 2>&1 | head -30
```
Expected: `ImportError` or `ModuleNotFoundError` for `carta.update.checker`.

- [ ] **Step 4: Implement `carta/update/checker.py`**

```python
# carta/update/checker.py
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import requests

from carta import __version__

PYPI_URL = "https://pypi.org/pypi/carta-cc/json"
CACHE_FILENAME = "update-check.json"
CHECK_INTERVAL_HOURS = 24


def _installed_version() -> str:
    return __version__


def _fetch_latest(timeout: float = 2.0) -> Optional[str]:
    """Fetch latest carta-cc version from PyPI. Returns None on any failure."""
    try:
        resp = requests.get(PYPI_URL, timeout=timeout)
        resp.raise_for_status()
        return resp.json()["info"]["version"]
    except Exception:
        return None


def _read_cache(carta_dir: Path) -> dict:
    try:
        return json.loads((carta_dir / CACHE_FILENAME).read_text())
    except Exception:
        return {}


def _write_cache(carta_dir: Path, latest: str, notified: str) -> None:
    data = {
        "checked_at": datetime.utcnow().isoformat(),
        "latest": latest,
        "notified": notified,
    }
    try:
        (carta_dir / CACHE_FILENAME).write_text(json.dumps(data, indent=2))
    except OSError:
        pass


def _is_cache_stale(cache: dict) -> bool:
    checked_at = cache.get("checked_at")
    if not checked_at:
        return True
    try:
        return datetime.utcnow() - datetime.fromisoformat(checked_at) > timedelta(hours=CHECK_INTERVAL_HOURS)
    except ValueError:
        return True


def _version_tuple(v: str) -> tuple:
    try:
        return tuple(int(x) for x in v.split("."))
    except (ValueError, AttributeError):
        return (0,)


def check_for_update(carta_dir: Optional[Path]) -> Optional[str]:
    """Return a notification string if an unnotified newer version is available, else None.

    Reads cache from carta_dir if provided. Re-fetches PyPI if cache is stale (>24h).
    Returns None if already up-to-date, PyPI unreachable, or this version was already notified.
    """
    installed = _installed_version()
    cache = _read_cache(carta_dir) if carta_dir else {}

    if _is_cache_stale(cache):
        latest = _fetch_latest()
        if latest is None:
            return None
        notified = cache.get("notified", "")
        if carta_dir:
            _write_cache(carta_dir, latest, notified)
    else:
        latest = cache.get("latest", installed)

    notified = cache.get("notified", "")

    if _version_tuple(latest) <= _version_tuple(installed):
        return None
    if notified == latest:
        return None

    # Mark this version as notified so we don't repeat it
    if carta_dir:
        _write_cache(carta_dir, latest, latest)

    return (
        f"carta {latest} is available (you have {installed}). "
        f"Run `carta update` to upgrade."
    )


def maybe_notify(carta_dir: Optional[Path], cfg: dict) -> None:
    """Print an update notification if a newer version is available.

    Respects update_check config key. Silently swallows all errors.
    """
    if not cfg.get("update_check", True):
        return
    try:
        msg = check_for_update(carta_dir)
        if msg:
            sep = "─" * 51
            print(f"\n{sep}\n{msg}\n{sep}")
    except Exception:
        pass
```

- [ ] **Step 5: Run checker tests to verify they pass**

```bash
pytest carta/tests/test_update.py -v -k "not updater"
```
Expected: all checker tests PASS.

- [ ] **Step 6: Commit**

```bash
git add carta/update/__init__.py carta/update/checker.py carta/tests/test_update.py
git commit -m "feat: add update checker with PyPI fetch and 24h cache"
```

---

## Task 2: `carta/update/updater.py` — install detection and upgrade

**Files:**
- Create: `carta/update/updater.py`
- Modify: `carta/tests/test_update.py` (append updater tests)

- [ ] **Step 1: Append failing updater tests to `carta/tests/test_update.py`**

Add these at the bottom of the file:

```python
# ---------------------------------------------------------------------------
# updater tests
# ---------------------------------------------------------------------------

def test_detect_install_method_returns_pipx_when_available():
    from carta.update.updater import _detect_install_method
    mock_result = MagicMock()
    mock_result.stdout = "carta-cc 0.3.5\n"
    with patch("carta.update.updater.shutil.which", return_value="/usr/bin/pipx"), \
         patch("carta.update.updater.subprocess.run", return_value=mock_result):
        assert _detect_install_method() == "pipx"


def test_detect_install_method_returns_pip_when_no_pipx():
    from carta.update.updater import _detect_install_method
    with patch("carta.update.updater.shutil.which", return_value=None):
        assert _detect_install_method() == "pip"


def test_detect_install_method_returns_pip_when_carta_not_in_pipx():
    from carta.update.updater import _detect_install_method
    mock_result = MagicMock()
    mock_result.stdout = "some-other-package 1.0\n"
    with patch("carta.update.updater.shutil.which", return_value="/usr/bin/pipx"), \
         patch("carta.update.updater.subprocess.run", return_value=mock_result):
        assert _detect_install_method() == "pip"


def test_run_update_returns_0_when_already_current(capsys):
    from carta.update.updater import run_update
    with patch("carta.update.updater._fetch_latest", return_value="0.3.5"), \
         patch("carta.update.updater._installed_version", return_value="0.3.5"):
        code = run_update(yes=True)
    assert code == 0
    assert "up to date" in capsys.readouterr().out


def test_run_update_returns_1_when_pypi_unreachable(capsys):
    from carta.update.updater import run_update
    with patch("carta.update.updater._fetch_latest", return_value=None):
        code = run_update(yes=True)
    assert code == 1
    assert "PyPI" in capsys.readouterr().err


def test_run_update_yes_runs_pipx_upgrade():
    from carta.update.updater import run_update
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = "carta-cc 0.3.6\n"
    with patch("carta.update.updater._fetch_latest", return_value="0.3.6"), \
         patch("carta.update.updater._installed_version", return_value="0.3.5"), \
         patch("carta.update.updater._detect_install_method", return_value="pipx"), \
         patch("carta.update.updater.subprocess.run", return_value=mock_result) as mock_run:
        code = run_update(yes=True)
    assert code == 0
    call_args = mock_run.call_args[0][0]
    assert call_args == ["pipx", "upgrade", "carta-cc"]


def test_run_update_yes_runs_pip_upgrade():
    from carta.update.updater import run_update
    import sys
    mock_result = MagicMock()
    mock_result.returncode = 0
    with patch("carta.update.updater._fetch_latest", return_value="0.3.6"), \
         patch("carta.update.updater._installed_version", return_value="0.3.5"), \
         patch("carta.update.updater._detect_install_method", return_value="pip"), \
         patch("carta.update.updater.subprocess.run", return_value=mock_result) as mock_run:
        code = run_update(yes=True)
    assert code == 0
    call_args = mock_run.call_args[0][0]
    assert call_args == [sys.executable, "-m", "pip", "install", "--upgrade", "carta-cc"]


def test_print_check_shows_available(capsys):
    from carta.update.updater import print_check
    with patch("carta.update.updater._fetch_latest", return_value="0.4.0"), \
         patch("carta.update.updater._installed_version", return_value="0.3.5"):
        print_check()
    out = capsys.readouterr().out
    assert "0.3.5" in out
    assert "0.4.0" in out
    assert "carta update" in out


def test_print_check_shows_up_to_date(capsys):
    from carta.update.updater import print_check
    with patch("carta.update.updater._fetch_latest", return_value="0.3.5"), \
         patch("carta.update.updater._installed_version", return_value="0.3.5"):
        print_check()
    assert "up to date" in capsys.readouterr().out
```

- [ ] **Step 2: Run to verify they fail**

```bash
pytest carta/tests/test_update.py -v -k "updater or detect or run_update or print_check"
```
Expected: `ImportError` for `carta.update.updater`.

- [ ] **Step 3: Implement `carta/update/updater.py`**

```python
# carta/update/updater.py
import shutil
import subprocess
import sys
from typing import Optional

from carta.update.checker import _fetch_latest, _installed_version, _version_tuple


def _detect_install_method() -> str:
    """Return 'pipx' if carta-cc is installed via pipx, otherwise 'pip'."""
    if shutil.which("pipx") is None:
        return "pip"
    try:
        result = subprocess.run(
            ["pipx", "list", "--short"],
            capture_output=True, text=True, timeout=5,
        )
        if "carta-cc" in result.stdout:
            return "pipx"
    except Exception:
        pass
    return "pip"


def run_update(yes: bool = False) -> int:
    """Upgrade carta-cc to the latest version. Returns exit code.
    
    Note: the runtime copy at .carta/carta/ is updated on the next `carta init`
    run in each project (bootstrap copy is idempotent).
    """
    installed = _installed_version()
    print(f"Checking for updates (installed: {installed})...")

    latest = _fetch_latest()
    if latest is None:
        print("Could not reach PyPI. Check your network connection.", file=sys.stderr)
        return 1

    if _version_tuple(latest) <= _version_tuple(installed):
        print(f"carta {installed} — up to date")
        return 0

    if not yes:
        try:
            answer = input(f"Upgrade carta {installed} → {latest}? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\nAborted.")
            return 0
        if answer not in ("y", "yes"):
            print("Aborted.")
            return 0

    method = _detect_install_method()
    if method == "pipx":
        cmd = ["pipx", "upgrade", "carta-cc"]
    else:
        cmd = [sys.executable, "-m", "pip", "install", "--upgrade", "carta-cc"]

    print(f"Running: {' '.join(cmd)}")
    result = subprocess.run(cmd, check=False)
    if result.returncode != 0:
        print("Upgrade failed.", file=sys.stderr)
        return result.returncode

    print(f"\ncarta updated to {latest}")
    print("Run `carta init` in your projects to update the local runtime copy.")
    return 0


def print_check() -> None:
    """Print current vs latest version (for --check flag)."""
    installed = _installed_version()
    latest = _fetch_latest()
    if latest is None:
        print("Could not reach PyPI.")
        return
    if _version_tuple(latest) > _version_tuple(installed):
        print(f"carta {installed} installed  →  {latest} available")
        print("Run `carta update` to upgrade.")
    else:
        print(f"carta {installed} — up to date")
```

- [ ] **Step 4: Run all update tests**

```bash
pytest carta/tests/test_update.py -v
```
Expected: all tests PASS.

- [ ] **Step 5: Commit**

```bash
git add carta/update/updater.py carta/tests/test_update.py
git commit -m "feat: add update/updater with install detection and upgrade logic"
```

---

## Task 3: Wire background check into existing commands

**Files:**
- Modify: `carta/config.py` — add `update_check` default
- Modify: `carta/cli.py` — call `maybe_notify` at the end of each command

- [ ] **Step 1: Add `update_check` to `DEFAULTS` in `carta/config.py`**

In `carta/config.py`, find the `DEFAULTS` dict. Add `"update_check": True` as a top-level key after the `"modules"` block:

```python
    "modules": {
        "doc_audit": True,
        "doc_embed": True,
        "doc_search": True,
        "session_memory": True,
        "proactive_recall": True,
    },
    "update_check": True,
}
```

- [ ] **Step 2: Write a failing config test**

Add to `carta/tests/test_config.py`:

```python
def test_update_check_defaults_to_true(tmp_path):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(yaml.dump(MINIMAL_CONFIG))
    cfg = load_config(cfg_path)
    assert cfg["update_check"] is True


def test_update_check_can_be_disabled(tmp_path):
    config = {**MINIMAL_CONFIG, "update_check": False}
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(yaml.dump(config))
    cfg = load_config(cfg_path)
    assert cfg["update_check"] is False
```

- [ ] **Step 3: Run to verify they fail**

```bash
pytest carta/tests/test_config.py::test_update_check_defaults_to_true carta/tests/test_config.py::test_update_check_can_be_disabled -v
```
Expected: `AssertionError` (key missing from config).

- [ ] **Step 4: Make the config change**

Edit `carta/config.py` — add `"update_check": True` to `DEFAULTS` as shown in Step 1.

- [ ] **Step 5: Run config tests to verify they pass**

```bash
pytest carta/tests/test_config.py -v
```
Expected: all PASS.

- [ ] **Step 6: Wire `maybe_notify` into `carta/cli.py`**

At the top of `carta/cli.py`, the imports are already in place (no new imports needed at module level — `maybe_notify` is imported locally inside each command to keep startup fast).

Add a helper at the top of `cli.py` after the existing imports:

```python
def _notify_if_update(cfg_path=None, cfg=None):
    """Call maybe_notify if we have a config context. Silently skips on error."""
    try:
        from carta.update.checker import maybe_notify
        carta_dir = cfg_path.parent if cfg_path else None
        maybe_notify(carta_dir, cfg or {})
    except Exception:
        pass
```

Then add `_notify_if_update(cfg_path, cfg)` at the end of each command:

**`cmd_scan`** — add before the final `print`:
```python
    issue_count = len(results["issues"])
    print(f"Results at {output_path}")
    _notify_if_update(cfg_path, cfg)
```

**`cmd_embed`** — add after `progress.summary(...)` call and before the error exit:
```python
    progress.summary(
        embedded=summary["embedded"],
        skipped=summary["skipped"],
        errors=len(summary["errors"]),
    )
    _notify_if_update(cfg_path, cfg)
    if summary["errors"]:
        sys.exit(1)
```

**`cmd_search`** — add at the very end of the function (after the results loop):
```python
    for r in results:
        print(f"[{r['score']:.2f}] {r['source']} — {r['excerpt']}")
    _notify_if_update(find_config(), load_config(find_config()))
```

Wait — `cmd_search` already called `find_config()` and `load_config()` at the top. Reuse those. Replace the body of `cmd_search` to capture them:

```python
def cmd_search(args):
    from carta.config import load_config
    cfg_path = find_config()
    cfg = load_config(cfg_path)
    if not cfg["modules"].get("doc_search"):
        print("doc_search module is disabled in config.", file=sys.stderr)
        sys.exit(1)
    from carta.embed.pipeline import run_search
    query = " ".join(args.query)
    try:
        results = run_search(query, cfg, verbose=True)
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    if not results:
        print(
            "No results found. If nothing is embedded yet, run `carta embed` first; "
            "otherwise try different wording."
        )
        _notify_if_update(cfg_path, cfg)
        return
    for r in results:
        print(f"[{r['score']:.2f}] {r['source']} — {r['excerpt']}")
    _notify_if_update(cfg_path, cfg)
```

**`cmd_init`** — no config yet, call with no args:
```python
def cmd_init(args):
    _check_path_conflict()
    from carta.install.bootstrap import run_bootstrap
    run_bootstrap(Path.cwd())
    _notify_if_update()
```

**`cmd_doctor`** — add before each `sys.exit`:
```python
    if not result.can_proceed():
        if not args.json:
            installer = AutoInstaller(interactive=False)
            installer.print_setup_guide(result)
        _notify_if_update()
        sys.exit(1)

    _notify_if_update()
    sys.exit(0)
```

- [ ] **Step 7: Run full test suite to verify nothing broke**

```bash
pytest carta/tests/ -v --ignore=carta/tests/test_update.py -x
```
Expected: all existing tests PASS.

- [ ] **Step 8: Commit**

```bash
git add carta/config.py carta/cli.py carta/tests/test_config.py
git commit -m "feat: wire update check into all commands, add update_check config key"
```

---

## Task 4: `cmd_update` — the explicit update command

**Files:**
- Modify: `carta/cli.py` — add `cmd_update`, register subparser

- [ ] **Step 1: Write failing CLI integration tests**

Add to `carta/tests/test_update.py`:

```python
# ---------------------------------------------------------------------------
# cmd_update CLI integration tests
# ---------------------------------------------------------------------------
import subprocess as _subprocess
import sys as _sys
import os as _os
from pathlib import Path as _Path


def _run_carta(args, cwd=None):
    repo_root = _Path(__file__).resolve().parents[2]
    env = _os.environ.copy()
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = str(repo_root) if not existing else f"{repo_root}{_os.pathsep}{existing}"
    return _subprocess.run(
        [_sys.executable, "-m", "carta.cli"] + args,
        capture_output=True, text=True,
        cwd=str(cwd) if cwd else None,
        env=env,
    )


def test_update_check_flag_exits_zero_and_prints_version():
    result = _run_carta(["update", "--check"])
    assert result.returncode == 0
    # Should print either "up to date" or an available version string
    assert result.stdout.strip() != ""


def test_update_subcommand_exists():
    result = _run_carta(["update", "--help"])
    assert result.returncode == 0
    assert "--check" in result.stdout or "check" in result.stdout
```

- [ ] **Step 2: Run to verify they fail**

```bash
pytest carta/tests/test_update.py::test_update_subcommand_exists -v
```
Expected: non-zero return code (command not found).

- [ ] **Step 3: Add `cmd_update` to `carta/cli.py`**

Add this function after `cmd_doctor`:

```python
def cmd_update(args):
    """Check for and apply carta updates."""
    from carta.update.updater import run_update, print_check
    if args.check:
        print_check()
        return
    code = run_update(yes=args.yes)
    sys.exit(code)
```

- [ ] **Step 4: Register the subparser in `main()`**

In `main()`, after the `doctor_p` block, add:

```python
    update_p = sub.add_parser("update", help="Update carta to the latest version")
    update_p.add_argument("--check", action="store_true", help="Show available version without upgrading")
    update_p.add_argument("--yes", "-y", action="store_true", help="Skip confirmation prompt")
```

And add `"update": cmd_update` to the dispatch dict:

```python
    dispatch = {
        "init": cmd_init,
        "scan": cmd_scan,
        "embed": cmd_embed,
        "search": cmd_search,
        "doctor": cmd_doctor,
        "update": cmd_update,
    }
```

- [ ] **Step 5: Run update CLI tests**

```bash
pytest carta/tests/test_update.py::test_update_subcommand_exists carta/tests/test_update.py::test_update_check_flag_shows_version_info -v
```
Expected: PASS.

- [ ] **Step 6: Run full test suite**

```bash
pytest carta/tests/ -v -x
```
Expected: all PASS.

- [ ] **Step 7: Commit**

```bash
git add carta/cli.py carta/tests/test_update.py
git commit -m "feat: add carta update command with --check and --yes flags"
```

---

## Task 5: Automated release pipeline

**Files:**
- Create: `.github/workflows/release.yml`
- Delete: `.github/workflows/publish.yml`

- [ ] **Step 1: Create `.github/workflows/release.yml`**

```yaml
name: Release

on:
  release:
    types: [published]

jobs:
  release:
    name: Sync versions, build, and publish
    runs-on: ubuntu-latest
    permissions:
      contents: write

    steps:
      - uses: actions/checkout@v4
        with:
          ref: main
          fetch-depth: 0

      - name: Parse tag version
        id: tag
        run: |
          TAG="${{ github.event.release.tag_name }}"
          VERSION="${TAG#v}"
          echo "version=$VERSION" >> "$GITHUB_OUTPUT"
          echo "Tag: $TAG  →  Version: $VERSION"

      - name: Update version numbers
        id: bump
        run: |
          VERSION="${{ steps.tag.outputs.version }}"
          python3 - <<EOF
          import re, sys

          version = "$VERSION"
          changed = []

          # carta/__init__.py
          path = "carta/__init__.py"
          with open(path) as f:
              old = f.read()
          new = re.sub(r'__version__ = "[^"]*"', f'__version__ = "{version}"', old)
          if new != old:
              with open(path, "w") as f:
                  f.write(new)
              changed.append(path)

          # .claude-plugin/plugin.json
          path = ".claude-plugin/plugin.json"
          with open(path) as f:
              old = f.read()
          new = re.sub(r'"version":\s*"[^"]*"', f'"version": "{version}"', old)
          if new != old:
              with open(path, "w") as f:
                  f.write(new)
              changed.append(path)

          # .claude-plugin/marketplace.json
          path = ".claude-plugin/marketplace.json"
          with open(path) as f:
              old = f.read()
          new = re.sub(r'"version":\s*"[^"]*"', f'"version": "{version}"', old)
          if new != old:
              with open(path, "w") as f:
                  f.write(new)
              changed.append(path)

          if changed:
              print(f"Updated: {', '.join(changed)}")
          else:
              print("Versions already in sync.")
          EOF

      - name: Commit and push version bumps
        run: |
          VERSION="${{ steps.tag.outputs.version }}"
          git config user.name "github-actions[bot]"
          git config user.email "github-actions[bot]@users.noreply.github.com"
          git add carta/__init__.py .claude-plugin/plugin.json .claude-plugin/marketplace.json
          if git diff --staged --quiet; then
            echo "Versions already in sync — no commit needed."
          else
            git commit -m "chore: sync version to ${VERSION}"
            git push origin HEAD:main
          fi

      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"

      - name: Build distribution
        run: |
          pip install build
          python -m build

      - name: Publish to PyPI
        run: |
          pip install twine
          twine upload dist/*
        env:
          TWINE_USERNAME: __token__
          TWINE_PASSWORD: ${{ secrets.PYPI_API_TOKEN }}
```

- [ ] **Step 2: Delete `publish.yml`**

```bash
rm .github/workflows/publish.yml
```

- [ ] **Step 3: Commit**

```bash
git add .github/workflows/release.yml
git rm .github/workflows/publish.yml
git commit -m "feat: unified release pipeline — auto-sync versions before PyPI publish"
```

---

## Task 6: Final verification

- [ ] **Step 1: Run full test suite one last time**

```bash
pytest carta/tests/ -v
```
Expected: all PASS, no regressions.

- [ ] **Step 2: Smoke test `carta update --check` manually**

```bash
carta update --check
```
Expected: prints either "up to date" or an available version string.

- [ ] **Step 3: Verify `carta --help` shows update subcommand**

```bash
carta --help
```
Expected: `update` appears in the subcommand list.

- [ ] **Step 4: Final commit if any loose changes**

```bash
git status
# If clean, nothing to do. If stray changes, stage and commit them.
```
