---
created: 2026-03-24T22:30:00.000Z
resolved: 2026-03-24T00:00:00.000Z
title: Fix _estimate_tokens for punctuation-heavy content
area: general
priority: high
files:
  - carta/embed/parse.py
---

## Problem

`_estimate_tokens` uses `len(text.split()) * 1.3` (word count). TOC pages in datasheets contain dot-leader sequences like `...........` that count as a single "word" but tokenize to ~80 tokens each. Chunk 3 of EN_DS_N32WB03x.pdf estimated 364 tokens but actual was ~1159+ (by char/4 heuristic). Undersized chunks then hit the token limit in Qdrant upsert.

## Resolution

Changed char estimate from `len(text) / 4` to `len(text) / 3`. Technical content (hex tables, register maps, dot leaders) tokenises at ~3 chars/token, not ~4. With `max_tokens: 800`, this limits chunks to ~2400 chars — safely under nomic-embed-text's 2048-token context limit even for dense datasheet pages.

The original `max()` fix (combining word and char estimates) was already in the codebase but `/4` was still too generous for hardware datasheets.
