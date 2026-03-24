---
created: 2026-03-24T22:30:00.000Z
title: Fix skill cache — old version wins after reinstall
area: general
priority: high
files:
  - carta/install/bootstrap.py
  - carta/plugin.py
---

## Problem

After `pipx install carta-cc==0.1.7`, both `~/.claude/plugins/cache/carta-cc/carta-cc/0.1.6` and `0.1.7` directories exist in the cache. `installed_plugins.json` correctly points to 0.1.7 but the skill resolver loads 0.1.6 (old version wins). All skills (carta-init, doc-audit, doc-embed, doc-search) run from the stale cache.

Installing a new version should remove or supersede the old cache directory so only the current version is present.

## Solution

During `carta init` (plugin registration), detect and remove older version directories from the cache:
1. List all subdirs under `~/.claude/plugins/cache/carta-cc/carta-cc/`
2. If any version dirs exist that are NOT the current version, remove them
3. Alternatively: always write to a fixed path (e.g. `cache/carta-cc/`) without a version subdirectory, relying on `installed_plugins.json` for versioning
