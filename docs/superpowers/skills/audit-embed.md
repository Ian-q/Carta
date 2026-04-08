---
name: audit-embed
description: Run and interpret Carta audit reports for embed pipeline consistency
---

# Carta audit and embed

Use this skill when the user runs `carta audit`, reviews `audit-report.json`, or needs to reconcile sidecars with Qdrant.

## When to use

- After `carta embed` or before a release, to find orphaned chunks, stale sidecars, or hash drift.
- When interpreting categories in the JSON report (see project docs).

## Commands

- `carta audit` — write structured report (default `audit-report.json` in repo root).
- `carta embed` — re-embed changed files after fixes.
