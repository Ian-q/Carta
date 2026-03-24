---
created: 2026-03-24T22:30:00.000Z
title: Auto-exempt anchor_doc files from homeless_doc check
area: general
priority: medium
files:
  - carta/scanner/scanner.py
  - carta/scanner/checks/homeless_doc.py
---

## Problem

CLAUDE.md is configured as `anchor_doc: CLAUDE.md` but the homeless_doc scanner still flags it as outside docs/. Anchor docs are explicitly named "important" root-level files — they should be auto-exempted from the homeless_doc check without requiring the user to add them to excluded_paths.

## Solution

In the homeless_doc check, before flagging a file, check if its basename matches `cfg.get("anchor_doc")` or any value in a `anchor_docs` list. If so, skip it.
