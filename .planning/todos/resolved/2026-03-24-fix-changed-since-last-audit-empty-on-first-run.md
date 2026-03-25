---
created: 2026-03-24T22:05:52.524Z
title: Fix changed_since_last_audit empty on first run
area: general
priority: high
files:
  - carta/scanner.py
  - carta/state.py
---

## Problem

On first run after `carta init`, `changed_since_last_audit` returns `[]`. The 0.1.5 scanner returned `['CLAUDE.md', 'firmware/sdd-v2-arduino/README.md']` for the same repo state.

Two possible causes:
1. Baseline tracking reset — 0.1.6 init wiped whatever state 0.1.5 left, so there's no prior commit to diff against
2. The 0.1.5 run left state in a format 0.1.6 can't read, so it falls back to empty

The result is the semantic agent has nothing to prioritize, so it falls back to reading all docs manually rather than focusing on changed files.

## Solution

1. Inspect baseline tracking logic — what git ref or hash is stored after init vs after first audit
2. Confirm whether 0.1.6 clears state on init (intentional reset) or fails to read 0.1.5 state (regression)
3. If intentional: document that first run always returns empty changed_since and the semantic agent handles this gracefully
4. If regression: fix the state read path to be backwards-compatible or migration-aware
