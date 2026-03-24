---
created: 2026-03-24T22:05:52.524Z
title: Warn on stale skill cache version mismatch during init
area: general
priority: medium
files:
  - carta/init.py
  - carta/plugin.py
---

## Problem

After `carta init` registers 0.1.6 skills, an active Claude Code session silently continues using the cached 0.1.5 skills. The guide says to restart Claude Code, but there's no in-session warning — the version mismatch is invisible and can cause subtle divergence if skills changed between versions.

## Solution

During `carta init`, check if a plugin cache already exists with a different version:
1. Read the cached plugin metadata version
2. If it differs from the installing version, print a warning:
   ```
   ⚠️  Skill cache version mismatch: cached=0.1.5, installing=0.1.6
      Restart Claude Code to load the updated skills.
   ```
3. Optionally force-clear the stale cache so the next session starts fresh
