---
status: resolved
trigger: "After installing carta-cc 0.1.7, skills still load from 0.1.6 cache. Both version dirs exist; old version wins."
created: 2026-03-24T00:00:00Z
updated: 2026-03-24T00:00:00Z
---

## Current Focus

hypothesis: carta init does not clean up old version directories in the cache, so Claude Code's skill resolver picks up the alphabetically/lexicographically first or oldest directory
test: Read install/bootstrap.py and plugin.py to trace the cache installation path and look for cleanup logic
expecting: No cleanup of old version dirs after install
next_action: Read relevant source files to find where cache dir is written and whether old versions are purged

## Symptoms

expected: After `carta init` with 0.1.7, all skills load from ~/.claude/plugins/cache/carta-cc/carta-cc/0.1.7/
actual: Skills load from ~/.claude/plugins/cache/carta-cc/carta-cc/0.1.6/ despite installed_plugins.json pointing to 0.1.7. Both 0.1.6 and 0.1.7 dirs exist.
errors: No error — silent wrong-version loading. Detected because skill's "Base directory" showed 0.1.6 path.
reproduction: Install carta-cc==0.1.6, then install carta-cc==0.1.7 (upgrade), run carta init, restart Claude Code session, invoke any skill — loads from 0.1.6 cache
started: Discovered in 0.1.7 install test; 0.1.6 was the prior version

## Eliminated

## Evidence

- timestamp: 2026-03-24T00:00:00Z
  checked: carta/install/bootstrap.py _install_skills() lines 143-183
  found: Writes new version dir at ~/.claude/plugins/cache/carta-cc/carta-cc/{version}/skills and updates installed_plugins.json, but NEVER removes old version directories under ~/.claude/plugins/cache/carta-cc/carta-cc/
  implication: Both 0.1.6 and 0.1.7 dirs coexist; Claude Code's skill resolver picks up the old one (likely alphabetically first or by discovery order)

## Resolution

root_cause: _install_skills() in bootstrap.py installs skills into a versioned subdirectory (~/.claude/plugins/cache/carta-cc/carta-cc/{version}/skills) but never removes sibling directories from older versions. After an upgrade, both 0.1.6 and 0.1.7 dirs coexist and Claude Code's skill resolver loads from the stale one.
fix: Added a cleanup loop at the start of _install_skills() that iterates version_parent and calls shutil.rmtree() on any directory whose name does not match the current version, before writing the new version's files.
verification: Human confirmed. Fix resolves stale-version loading after upgrade.
files_changed: [carta/install/bootstrap.py]
