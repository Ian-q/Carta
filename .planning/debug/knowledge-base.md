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
