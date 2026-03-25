---
created: 2026-03-24T22:05:52.524Z
title: Detect PlatformIO carta binary PATH collision on init
area: general
priority: medium
files:
  - carta/cli.py
  - docs/install.md
---

## Problem

PlatformIO ships a broken `carta` binary at `/Users/ian/.platformio/penv/bin/carta`. Any embedded/hardware developer using PlatformIO will hit this silently — `carta` resolves to the wrong binary with no error.

## Solution

In `carta init`, detect PATH shadowing:
```bash
which -a carta | grep -v "$(which carta)" | grep platformio
```
If a conflicting binary is found at a higher-priority PATH entry, print a warning:
```
⚠️  Warning: another 'carta' binary found at /Users/.../.platformio/penv/bin/carta
   This may shadow the doc-audit carta. Verify `which carta` points to the correct install.
```

Also add a note to the install guide for PlatformIO users.
