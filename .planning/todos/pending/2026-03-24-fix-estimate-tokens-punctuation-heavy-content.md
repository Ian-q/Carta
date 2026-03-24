---
created: 2026-03-24T22:30:00.000Z
title: Fix _estimate_tokens for punctuation-heavy content
area: general
priority: high
files:
  - carta/embed/parse.py
---

## Problem

`_estimate_tokens` uses `len(text.split()) * 1.3` (word count). TOC pages in datasheets contain dot-leader sequences like `...........` that count as a single "word" but tokenize to ~80 tokens each. Chunk 3 of EN_DS_N32WB03x.pdf estimated 364 tokens but actual was ~1159+ (by char/4 heuristic). Undersized chunks then hit the token limit in Qdrant upsert.

## Solution

```python
def _estimate_tokens(text: str) -> int:
    word_estimate = len(text.split()) * 1.3
    char_estimate = len(text) / 4
    return int(max(word_estimate, char_estimate))
```

`max()` of both heuristics catches punctuation-heavy content that confounds word count.
