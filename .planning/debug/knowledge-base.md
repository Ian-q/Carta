# GSD Debug Knowledge Base

Resolved debug sessions. Used by `gsd-debugger` to surface known-pattern hypotheses at the start of new investigations.

---

## hook-config-grep-parser-fragility — Hook scripts misread YAML config via fragile grep pipeline
- **Date:** 2026-03-24
- **Error patterns:** grep, yaml, hook, config, false positive, proactive_recall, session_memory, true, adjacent
- **Root cause:** Both hook scripts used `grep -A1 'key' | grep -q 'true'` to read nested YAML config values. This false-positives on any adjacent key with value true, and breaks if the boolean is not on the immediately following line.
- **Fix:** Replace grep pipeline with `python3 -c "import yaml,sys; cfg=yaml.safe_load(open(...)); ..."` using yaml.safe_load to access the correct nested key path directly, with a fail-closed fallback.
- **Files changed:** carta/hooks/carta-prompt-hook.sh, carta/hooks/carta-stop-hook.sh
---

## docs-pip-args-syntax-and-version-refs — install-test-guide hard-codes version strings that go stale each release
- **Date:** 2026-03-24
- **Error patterns:** pip-args, version, 0.1.5, 0.1.4, 0.1.2, install-test-guide, hard-coded, stale, placeholder
- **Root cause:** install-test-guide.md hard-coded version strings (0.1.5, 0.1.4, 0.1.2) in expected output blocks and cache paths. These go stale with every release. The --pip-args syntax issue was confirmed in latest-log.txt but was not present in current committed docs.
- **Fix:** Replace all hard-coded version strings in install-test-guide.md with `<version>` placeholders.
- **Files changed:** docs/testing/install-test-guide.md
---

## pipx-silent-incomplete-venv — pipx exits 0 but carta entrypoint missing; guide lacks ensurepath step
- **Date:** 2026-03-24
- **Error patterns:** pipx, ensurepath, carta, entrypoint, missing, which carta, PlatformIO, PATH, venv, reinstall, exit code 0
- **Root cause:** pipx exits 0 even with partial/dirty venv state, and the install guide did not include a `pipx ensurepath` step or `which carta` verification check. carta init had no pre-check to warn when `which carta` resolved to a non-pipx path (e.g., PlatformIO binary).
- **Fix:** Added `pipx ensurepath` + `which carta` verification block to install-test-guide.md Step 1 with explicit instruction to restart shell and verify path is not .platformio. Added PATH conflict warning in carta/cli.py cmd_init via shutil.which check.
- **Files changed:** docs/testing/install-test-guide.md, carta/cli.py
---

## pipx-path-conflict-actionable-warning — pipx PATH conflict gives no actionable fix; users hit ModuleNotFoundError
- **Date:** 2026-03-24
- **Error patterns:** ModuleNotFoundError, PATH conflict, pipx, carta, PlatformIO, .platformio, export PATH, shutil.which, sys.prefix
- **Root cause:** cmd_init in cli.py never inspected sys.executable or PATH to detect whether the running carta binary was the correct pipx-installed one. When PlatformIO's carta binary was resolved first, Python's import system found PlatformIO's environment, causing ModuleNotFoundError. No actionable fix was ever printed.
- **Fix:** Added _check_path_conflict() in cli.py called at the top of cmd_init. It detects when the resolved `carta` binary on PATH differs from the executable currently running, and when the conflicting path matches known patterns (.platformio), prints both the warning and the actionable export PATH fix.
- **Files changed:** carta/cli.py, docs/testing/install-test-guide.md
---

## skill-cache-old-version-wins — stale skill version loaded after upgrade; old version dir never removed
- **Date:** 2026-03-24
- **Error patterns:** skill cache, old version, 0.1.6, 0.1.7, upgrade, plugin cache, versioned dir, skill resolver, installed_plugins.json, base directory
- **Root cause:** _install_skills() in bootstrap.py wrote a new versioned skill directory but never removed sibling directories from prior versions. After upgrade both 0.1.6 and 0.1.7 dirs coexisted and Claude Code's skill resolver loaded from the stale one.
- **Fix:** Added a cleanup loop at the start of _install_skills() that calls shutil.rmtree() on every sibling directory under version_parent whose name does not match the current version, before writing the new version's files.
- **Files changed:** carta/install/bootstrap.py
---

## carta-search-and-changed-since-bugs — carta search silent; changed_since_last_audit empty on first run
- **Date:** 2026-03-24
- **Error patterns:** carta search, silent, no output, changed_since_last_audit, empty, first run, QdrantClient, search, query_points, tracked_docs, docs_root, git ls-files
- **Root cause:** Bug 1: cmd_search() had no empty-result guard; run_search() swallows query_points() exceptions and returns [], causing the for-loop to print nothing. Bug 2: first-run fallback in run_scan() built changed_since from tracked_docs (only docs_root/*.md), missing all .md files outside docs/.
- **Fix:** Bug 1: Added `if not results: print("No results found."); return` in carta/cli.py before the for-loop. Bug 2: Replaced tracked_docs fallback with git ls-files filtering for .md/.embed-meta.yaml repo-wide; added get_initial_commit_hash() in scanner.py.
- **Files changed:** carta/cli.py, carta/scanner/scanner.py
---
