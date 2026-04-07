# Carta Update — Design Spec

**Date:** 2026-04-05  
**Status:** Approved

---

## Overview

Add `carta update` command, daily background version checks with notifications, and a fully automated release pipeline that keeps the PyPI package version and Claude Code plugin manifest versions in sync from a single GitHub release tag.

---

## Architecture

Three new pieces:

1. `**carta/update/checker.py`** — reads PyPI JSON API, compares to installed version, caches result in `.carta/update-check.json`. Called at the end of any carta command when cache is stale.
2. `**carta/update/updater.py**` — detects install method (pipx vs pip), runs the appropriate upgrade subprocess, re-syncs the runtime copy in `.carta/carta/`.
3. `**.github/workflows/release.yml**` — replaces the existing `publish.yml`; fires on `release: published`, auto-bumps version numbers across all files, builds, and publishes to PyPI.

The checker and updater are intentionally separate — the background check never triggers an upgrade.

---

## `carta update` Command

### Flags


| Flag                   | Behaviour                                  |
| ---------------------- | ------------------------------------------ |
| `carta update`         | Check, prompt confirmation, upgrade        |
| `carta update --check` | Print current vs latest, exit (no upgrade) |
| `carta update --yes`   | Skip confirmation prompt                   |


### Upgrade flow

1. Fetch latest version from PyPI (or use cache if <24h old)
2. Compare to installed version — exit early with "already up to date" if same
3. Detect install method:
  - If `pipx` is on PATH and `carta-cc` appears in `pipx list` → `pipx upgrade carta-cc`
  - Otherwise → `pip install --upgrade carta-cc`
4. After successful upgrade, re-sync runtime copy in `.carta/carta/`
5. Print new version

### Config opt-out (background check only)

```yaml
# .carta/config.yaml
update_check: false   # disables the daily background nudge; carta update still works
```

---

## Background Check + Notification

### Cache file

Project-scoped: `.carta/update-check.json` (used by background checks within a carta project).
If no `.carta/` directory is found (e.g. `carta update` run outside a project), the check queries PyPI directly without caching.

**Format:**

```json
{
  "checked_at": "2026-04-05T18:00:00",
  "latest": "0.3.6",
  "notified": "0.3.6"
}
```

`notified` tracks the last version a notice was shown for — the nudge appears once per new version, not every day.

### Check trigger

At the end of any `carta` command (scan, embed, search, init, doctor, update), if:

- `update_check` is not `false` in config
- Cache is missing or `checked_at` is >24h ago

The check runs in a background thread with a 2s timeout — never blocks the command.

### Notification format

Appended after normal command output when a newer version is available:

```
─────────────────────────────────────────────────
carta 0.3.6 is available (you have 0.3.5). Run `carta update` to upgrade.
─────────────────────────────────────────────────
```

Once shown, `notified` is updated so it won't show again until the next new version.

### `carta update --check` output

```
carta 0.3.5 installed  →  0.3.6 available
Run `carta update` to upgrade.
```

or:

```
carta 0.3.5 — up to date
```

---

## Automated Release Pipeline

### New release flow

Create a GitHub release with tag `vX.Y.Z`. That's it — all version bumping and publishing is automated.

### Workflow: `.github/workflows/release.yml`

Replaces the existing `publish.yml`. Triggered on `release: published`.

**Steps:**

1. **Parse tag** — strip `v` prefix to get bare version (e.g. `0.3.8`)
2. **Version audit** — compare tag version against:
  - `carta/__init__.py` → `__version__`
  - `.claude-plugin/plugin.json` → `version`
  - `.claude-plugin/marketplace.json` → `metadata.version` and `plugins[0].version`
3. **Auto-bump** — update any file that is behind the tag version
4. **Commit & push** — if any files changed, commit `chore: sync version to X.Y.Z` to `main`; requires `permissions: contents: write`
5. **Build** — `python -m build` (picks up updated `__version__` from `carta/__init__.py`)
6. **Publish** — `twine upload dist/`* to PyPI using `PYPI_API_TOKEN` secret

**Note:** `pyproject.toml` uses `version = {attr = "carta.__version__"}`, so updating `__init__.py` before building is sufficient — no `pyproject.toml` changes needed.

---

## Error Handling

### `carta update` / background check


| Scenario                       | Behaviour                                                                                       |
| ------------------------------ | ----------------------------------------------------------------------------------------------- |
| PyPI unreachable (background)  | Silently skip — no error shown                                                                  |
| PyPI unreachable (`--check`)   | Print "Could not reach PyPI", exit 0                                                            |
| Unknown install method         | Print "Could not detect install method. Run manually: `pip install --upgrade carta-cc`", exit 1 |
| Upgrade subprocess fails       | Print stderr from pipx/pip, exit non-zero                                                       |
| Background thread timeout (2s) | Thread abandoned silently, no impact on command                                                 |


### Release workflow


| Scenario                                   | Behaviour                                                                 |
| ------------------------------------------ | ------------------------------------------------------------------------- |
| Tag version ≤ existing versions            | Skip all updates, log "versions already in sync", still build and publish |
| Commit/push fails (e.g. branch protection) | Fail workflow before publish step — no version mismatch                   |
| Build or publish fails                     | Standard workflow failure, no partial state                               |


---

## Files Created / Modified


| File                            | Change                                                                         |
| ------------------------------- | ------------------------------------------------------------------------------ |
| `carta/update/__init__.py`      | New module                                                                     |
| `carta/update/checker.py`       | New — PyPI fetch, cache read/write, notification logic                         |
| `carta/update/updater.py`       | New — install method detection, upgrade subprocess                             |
| `carta/cli.py`                  | Add `cmd_update`, register subcommand, wire background check into all commands |
| `carta/config.py`               | Add `update_check` config key with default `true`                              |
| `.github/workflows/release.yml` | New — replaces `publish.yml`                                                   |
| `.github/workflows/publish.yml` | Deleted                                                                        |


---

## Testing

- `checker.py`: mock PyPI HTTP call; test stale/fresh cache logic; test `notified` deduplication
- `updater.py`: mock `shutil.which` and subprocess; test pipx/pip detection paths; test "already up to date" short-circuit
- `cmd_update`: test `--check`, `--yes`, and interactive prompt paths
- Release workflow: manual smoke test on next release

