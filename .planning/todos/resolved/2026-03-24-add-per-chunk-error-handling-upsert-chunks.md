---
created: 2026-03-24T22:30:00.000Z
resolved: 2026-03-24T00:00:00.000Z
title: Add per-chunk error handling in upsert_chunks
area: general
priority: high
files:
  - carta/embed/pipeline.py
  - carta/embed/qdrant_store.py
---

## Problem

`upsert_chunks` iterates chunks without try/except. One oversized chunk raises an exception that is caught at the pipeline level as a file-level error — discarding ALL other good chunks from that document. In testing, 5 bad chunks across 117 total caused both PDFs to fail entirely.

## Solution

Wrap the per-chunk upsert in try/except:
```python
for chunk in chunks:
    try:
        upsert_chunk(chunk)
    except Exception as e:
        print(f"Warning: skipping chunk {chunk.get('id', '?')} — {e}")
        continue
```

Good chunks continue; bad chunks log a warning and are skipped. File-level failure should only occur if ALL chunks fail.
