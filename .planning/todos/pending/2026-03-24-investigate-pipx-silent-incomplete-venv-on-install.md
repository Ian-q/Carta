---
created: 2026-03-24T22:30:00.000Z
title: Investigate pipx silent incomplete venv on first install
area: general
priority: medium
files:
  - docs/install.md
---

## Problem

First `pipx install carta-cc==0.1.7 --pip-args="--no-cache-dir"` ran in background and succeeded per log (exit 0), but the `carta` entrypoint was missing from the venv. `pipx reinstall` recovered. Root cause unclear — possibly a race condition with a prior failed install leaving a dirty venv state.

## Solution

Investigate whether this is reproducible. If it is, add a post-install check to `carta init` or the guide:
```bash
which carta || echo "ERROR: carta entrypoint not found — run: pipx reinstall carta-cc"
```
