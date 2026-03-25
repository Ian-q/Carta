---
status: resolved
resolution: "Hooks in carta/hooks/*.sh already use python3 + yaml.safe_load on .carta/config.yaml (no grep). Verified 2026-03-24."
created: 2026-03-24T22:30:00.000Z
title: Harden hook config grep parser against false positives
area: general
priority: low
files:
  - carta/install/hooks/
---

## Problem

Both hooks use `grep -A1 'key' config | grep -q 'true'` which works by coincidence — currently matches `modules.proactive_recall: true` from a different section. If any sub-key under a section block were `true`, it would produce a false positive. Low risk for stubs now, but needs hardening before Plan 2 logic runs.

## Solution

Replace grep-based config parsing with a proper YAML parser in the hooks, or use `python3 -c "import yaml; ..."` to read and access the specific key path. This eliminates false-positive risk from adjacent `true` values.
