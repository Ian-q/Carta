---
id: 2026-06-15-integrity-scanner-stale-discovery-design
title: Integrity-scanner & stale-discovery robustness (post-v0.11.0)
status: shipped
related: []
date: 2026-06-15
related_issue: Ian-q/Carta#40, Ian-q/Carta#39, Ian-q/Carta#11
---

# Integrity-scanner & stale-discovery robustness (post-v0.11.0)

A cluster of post-v0.11.0 integrity/sidecar issues that share the same code
(the `.carta/sidecars/` walks). Ships as one PR; #11 is closed as
already-resolved.

## Findings

- **#11 is already implemented.** `carta/audit/audit.py::detect_missing_source_sidecars`
  is the first-class orphan audit category #11 asked for — exact schema
  (`id`, `category`, `severity`, `sidecar_path`, `missing_source`), wired into
  `run_audit`, with all three requested tests. Shipped in `a9bd34f`
  (2026-04-21), two weeks before #11 was filed. Only residual: the pipeline-side
  `detect_orphaned_sidecars` duplicate. **Close #11; fold the dedup into this PR.**
- **#40 is real** — two bugs in `carta/embed/integrity.py::scan_corpus_integrity`.
- **#39 is real** — `discover_stale_files` reads a sidecar status that nothing
  writes anymore.

## Design

### Shared helper — `induct.py::iter_canonical_sidecars(repo_root)`

Yields `(sidecar_path, data)` for every sidecar under `.carta/sidecars/` that:
parses to a `dict`, has a `current_path`, and is **canonically located** —
its on-disk path under `sidecars/` equals `Path(current_path).with_suffix(".embed-meta.yaml")`
(the inverse of `sidecar_path`). Misplaced/nested junk copies (e.g.
`.carta/sidecars/.worktrees/x/.carta/sidecars/foo.embed-meta.yaml` whose
`current_path` resolves to a real repo file) are skipped. One place for the
walk + path-validation, reused by the functions below.

### #40 — `scan_corpus_integrity`

1. **Slug collisions are informational, not "affected".** With path-based point
   IDs, multiple files sharing a filename stem is healthy coexistence. Keep
   `slug_collisions` in the report but drop it from `affected_files`; genuine
   legacy-collision damage still surfaces via `count_mismatches` (a fully
   shadowed file shows fewer Qdrant points than its sidecar `chunk_count`).
2. **Junk-sidecar defense.** Use `iter_canonical_sidecars` for the sidecar walk
   so nested/misplaced copies no longer produce phantom stuck-stale entries and
   false count mismatches that repair can never converge on.

### #39 — `discover_stale_files` (MCP `carta_embed scope='stale'`)

`mark_sidecar_stale` only stamps the Qdrant payload `status: stale`; nothing
writes `status: stale` into the sidecar YAML, so the old status read returned
nothing. Redefine staleness as **content drift, computed on demand**: a file is
stale when it exists, its sidecar records a `file_hash`, and
`compute_file_hash(file) != file_hash`. Walk via `iter_canonical_sidecars`.

### #11 dedup — `detect_orphaned_sidecars`

Reimplement on top of `iter_canonical_sidecars` (drops duplicated walk logic and
gains the junk-skip). Keep it as the embed-time stderr warning source.

## Testing (TDD)

- `iter_canonical_sidecars`: canonical kept; nested/misplaced junk skipped;
  non-dict skipped; missing `current_path` skipped.
- `scan_corpus_integrity`: slug collisions reported but absent from
  `affected_files`; nested junk sidecar ignored. Update
  `test_affected_files_unions_everything`.
- `discover_stale_files`: stale when hash differs; not stale when hash matches;
  skipped when no recorded hash / source missing; junk skipped. Replace the old
  status-based tests in `test_pipeline.py` and `test_mcp_server.py`.
- `detect_orphaned_sidecars`: existing tests stay green; add a junk-skip test.

## Acceptance

- `carta doctor` reports clean on a corpus whose only "issue" is same-slug files.
- `embed --repair` no longer re-embeds same-slug files indefinitely.
- Nested junk sidecar trees no longer pollute the scan.
- MCP `carta_embed scope='stale'` returns files whose content changed since embed.
- #11 closed; no duplicated orphan-detection walk.

## Out of scope

- `_visual` collection integrity (#38).
- Auto-deletion/triage of orphaned sidecars; a standalone `carta migrate`.
