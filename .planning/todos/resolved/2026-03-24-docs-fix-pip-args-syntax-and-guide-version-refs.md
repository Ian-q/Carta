---
created: 2026-03-24T22:30:00.000Z
title: Docs — fix --pip-args syntax and version placeholders
area: docs
priority: low
files:
  - docs/testing/install-test-guide.md
  - docs/install.md
---

## Problem

1. Guide uses `--pip-args=--no-cache-dir` (equals syntax) which pipx rejects with "unrecognized arguments". Correct syntax: `--pip-args "--no-cache-dir"` (space-separated).
2. Guide hard-codes version numbers (0.1.5) in expected output strings and cache paths. These go stale every release. Should use `<version>` placeholder.
