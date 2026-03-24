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

## pipx-path-conflict-actionable-warning — pipx PATH conflict gives no actionable fix; users hit ModuleNotFoundError
- **Date:** 2026-03-24
- **Error patterns:** ModuleNotFoundError, PATH conflict, pipx, carta, PlatformIO, .platformio, export PATH, shutil.which, sys.prefix
- **Root cause:** cmd_init in cli.py never inspected sys.executable or PATH to detect whether the running carta binary was the correct pipx-installed one. When PlatformIO's carta binary was resolved first, Python's import system found PlatformIO's environment, causing ModuleNotFoundError. No actionable fix was ever printed.
- **Fix:** Added _check_path_conflict() in cli.py called at the top of cmd_init. It detects when the resolved `carta` binary on PATH differs from the executable currently running, and when the conflicting path matches known patterns (.platformio), prints both the warning and the actionable export PATH fix.
- **Files changed:** carta/cli.py, docs/testing/install-test-guide.md
---
