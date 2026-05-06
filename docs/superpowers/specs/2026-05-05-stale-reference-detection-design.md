---
title: Stale-reference detection (v1) — design spec
status: draft
date: 2026-05-05
related_issue: Ian-q/Carta#10
---

# Stale-reference detection (v1) — design spec

## Motivation

When the project migrated from micro-ROS to a COBS+JSON serial bridge (April 2026), three documents kept stale references to the old transport for weeks before a human noticed:

1. `docs/hardware/vcu/teensy-io-allocation.md` — section heading "micro-ROS UART"
2. `docs/hardware/vcu/jetson-interface-spec.md` — "UART1 — micro-ROS" listed as primary real-time path
3. `docs/MICROROS_UART_TRANSPORT.md` — entire doc describing the replaced transport

Carta's semantic search retroactively surfaced all three in seconds — but only because someone knew to ask. The replacement spec (`cobs-json-serial-bridge-design.md`) was already in the graph the whole time. Stale-reference detection should be **ambient**, not on-demand.

Issue [#10](https://github.com/Ian-q/Carta/issues/10) proposes four mechanisms (pre-commit hook, PR/CI scan, query-time hook extension, CLAUDE.md guidance). This spec covers **v1 only**: the data model and a single audit-category surface. The other three mechanisms become small additive layers on top of v1.

## Goals

- Establish a durable convention for marking documents as superseded.
- Add a `stale_reference` audit category that flags any doc whose chunks semantically match content inside a superseded doc.
- Avoid bespoke schema work — extend the frontmatter convention Carta already extracts and propagates.
- Keep the data layer forward-compatible so future detection routines can write derived facts without changing source files.

## Non-goals (v1)

- Section / sub-path-level supersession. Whole-doc only for v1.
- Pre-commit / pre-PR / CI-annotation surfaces. Layer on after audit category proves accurate.
- Query-time extension to `carta-hook`. Layer on later.
- A CLI for *writing* the supersession frontmatter (e.g. `carta supersede <old> --by <new>`). Anyone — agent, human, future Carta routine — can write the frontmatter; v1 just reads it.
- Auto-inference of supersession from doc content (e.g. LLM pass detecting "replaces X"). Deferred.

## Data model

### Frontmatter (durable, source-of-file)

A document marks itself superseded by adding two fields to its YAML frontmatter:

```yaml
---
status: superseded
superseded_by: docs/superpowers/specs/2026-04-09-cobs-json-serial-bridge-design.md
---
```

Optionally, the *replacement* doc may declare the inverse:

```yaml
---
supersedes:
  - docs/MICROROS_UART_TRANSPORT.md
---
```

Both directions are supported but only one needs to be set; Carta infers the other at audit time when only one side is present. If both are set and disagree, the `superseded_by` side on the older doc wins (it's more local to the deprecation).

Frontmatter is intentionally tool-agnostic: a human, a Claude session, or a future `carta supersede` CLI may write it. Carta's job in v1 is purely to read.

### Sidecar (Carta-owned, regenerable)

Sidecars stay forward-compatible by reserving a `derived:` namespace for fields Carta routines compute later (e.g. `derived.last_audit_check`, `derived.inferred_deprecations`). **v1 does not write to sidecars.** This section is documented purely to set the contract for future routines.

### Qdrant chunk payload (no schema change required)

`carta/embed/pipeline.py` already extracts frontmatter and stores it in chunk metadata under a `frontmatter` key (`pipeline.py:144-165`). New frontmatter fields propagate automatically — no pipeline changes required for v1. Detection can filter on `metadata.frontmatter.status == "superseded"` via Qdrant's filter API.

## Detection logic

New audit category `stale_reference` in `carta/audit/audit.py`.

For each non-superseded doc `D`:

1. Load all chunks of `D` from Qdrant.
2. For each chunk `c`:
   - Search Qdrant for the top-1 nearest neighbor restricted to chunks **outside `D`**.
   - If the neighbor has `metadata.frontmatter.status == "superseded"` and `score >= threshold`, emit a finding.
3. Aggregate per-doc; deduplicate findings that point to the same superseded source from the same doc (keep the highest-scoring one).

Default threshold: `0.85`. Configurable via `audit.stale_reference.score_threshold`.

The "exclude D's own chunks" requirement is implemented via a Qdrant filter on the chunk's `slug` or `sidecar_id` field — whichever is already indexed.

### Why chunk-level

Carta already chunks at embed time and chunks already have offsets and excerpts. They give us:
- A natural unit for similarity comparison (already what `suggest_related` uses).
- A useful excerpt for the audit report (first ~120 chars of the chunk).
- No new primitives.

## Audit issue shape

Standard `carta audit` issue, indexed by stable `AUDIT-NNN` ID and category `stale_reference`. Severity: `warning`.

```json
{
  "id": "AUDIT-042",
  "category": "stale_reference",
  "severity": "warning",
  "file": "docs/hardware/vcu/teensy-io-allocation.md",
  "chunk_excerpt": "## micro-ROS UART\n\nThe Teensy publishes telemetry over...",
  "references_superseded": "docs/MICROROS_UART_TRANSPORT.md",
  "canonical_replacement": "docs/superpowers/specs/2026-04-09-cobs-json-serial-bridge-design.md",
  "score": 0.91
}
```

Rendered into `AUDIT_REPORT.md` and `TRIAGE.md` using the existing audit reporting pipeline. Suppression: same convention as other audit issues (add the AUDIT-ID to whatever triage-ignore list `carta audit` already supports — to be matched to existing pattern during implementation).

## Configuration

```yaml
audit:
  stale_reference:
    enabled: true
    score_threshold: 0.85
```

`enabled: true` by default. Disable per-project by setting to `false`.

## Migration / adoption

- v0.4.x users see no change until they add `status: superseded` to a doc and re-run `carta embed` (so chunk payloads pick up the new frontmatter).
- The audit category produces zero findings in any project without supersession metadata — adoption is opt-in by writing frontmatter.

## Testing

- Unit tests for the detection function: synthetic chunk fixtures with/without `status: superseded` in metadata, threshold behaviour, "exclude self" filter, dedupe.
- Integration test against a temp Qdrant collection seeded with two docs (one superseded, one referencing it) to confirm an end-to-end finding.
- Existing `carta audit` JSON-shape and TRIAGE-rendering tests extended to cover the new category.

## Out-of-scope follow-ups (sketched, not committed)

Each of these becomes a small additive layer once v1 lands:

- **Pre-commit hook (#1).** Runs the same detection on staged chunks only. New CLI: `carta hook install pre-commit`.
- **PR/CI diff scan (#2).** `carta audit --diff origin/main...HEAD --report github-annotations`. Reuses `stale_reference` detection, restricted to files in the diff.
- **Query-time hook extension (#3).** Extend existing `carta-hook` to inject a "this concept has a superseded source" hint when the user's prompt matches a superseded chunk above threshold.
- **CLAUDE.md guidance (#4).** Expand the `anchor_doc` injection to mention `/doc-search` and the new `stale_reference` audit category.
- **Section-level supersession (Design B).** Frontmatter schema for marking specific sub-paths/sections as superseded inside otherwise-current docs.
- **Auto-inference.** LLM pass during embed that detects "replaces X" / "supersedes Y" phrases in newly-embedded docs and writes back to the older doc's frontmatter (with confirmation).
