---
created: 2026-03-24T22:05:52.524Z
title: Fix carta search silent empty output — verify Qdrant fix
area: general
priority: high
files:
  - carta/search.py
  - carta/qdrant_client.py
---

## Problem

In 0.1.5, `carta search` crashed with `'QdrantClient' object has no attribute 'search'`. In 0.1.6 the crash is gone but the command produces no output at all — collections exist but are empty. It's unclear whether the Qdrant fix was implemented correctly or the error is being silently swallowed.

The `/doc-audit` skill checks `carta search "test" 2>&1 | head -1` — no error means the tool is considered reachable, so the semantic agent would have run against an empty index without knowing it.

## Solution

1. Locate the Qdrant search path and confirm whether the `query_points` API migration actually fixed the attribute error
2. Check if there's a try/except swallowing the error silently instead of surfacing it
3. Add explicit output (e.g. "No results — collection may be empty") when search returns 0 results so empty vs broken is distinguishable
4. Verify embeddings are being written to Qdrant on `carta init` / first audit run
