---
title: Audit Command & Skill Design
date: 2026-04-07
status: approved
---

# Audit Command & Skill Design

## Overview

A new `carta audit` command and accompanying skill to detect and report inconsistencies across the three layers of Carta's embedding pipeline: source files, sidecar metadata (`.embed-meta.yaml`), and Qdrant chunks.

**Core principle:** Audit is read-only and reports to JSON; fixing is separate and agent-assisted.

## Motivation

The embedding pipeline involves three interdependent layers:
1. **Files** — Source documents (`.md`, `.pdf`) in `docs_root`
2. **Sidecars** — Metadata files (`.embed-meta.yaml`) tracking hash, mtime, sidecar_id
3. **Qdrant** — Vector chunks indexed by sidecar_id with payload metadata

Bugs, signal handling, or manual edits can cause mismatches (orphaned chunks, stale sidecars, missing metadata). Currently there's no way to detect or repair these inconsistencies systematically.

## Design

### Scope

**In scope:**
- `{project}_doc` collection (structured, file-backed chunks)
- Consistency checks between files, sidecars, and Qdrant
- Detection and reporting; optional agent-assisted repair

**Out of scope:**
- `{project}_session` and `{project}_quirk` collections (sparse, agent-generated, no file backing)
- Semantic quality assessment (only structural consistency)
- Automatic repair without user/agent oversight

### Issue Categories

Audit detects six types of inconsistencies:

| Category | Description | Example |
|----------|-------------|---------|
| **orphaned_chunks** | Chunks in Qdrant with `sidecar_id` that has no matching sidecar on disk | File deleted, chunks remain |
| **missing_sidecars** | Files exist and have chunks in Qdrant but no `.embed-meta.yaml` | Partial failure during embed |
| **stale_sidecars** | Sidecar exists but file `mtime` is newer (file changed post-embed) | User edited doc, didn't re-embed |
| **hash_mismatches** | File hash differs from sidecar's recorded hash (mtime may match due to touch) | File content changed, mtime same |
| **disconnected_files** | Discoverable files with no sidecar and no chunks in Qdrant | Never embedded or removed from Qdrant only |
| **qdrant_sidecar_mismatches** | Chunks in Qdrant don't match sidecar metadata (count, indices) | Partial upsert or corruption |

### Commands

#### `carta audit`

Scans files, sidecars, and `{project}_doc` collection. Reports to stdout (JSON).

```bash
$ carta audit [--output audit-report.json]
```

Output: JSON report (see JSON Schema below)
Exit code: 0 if audit runs successfully (regardless of issue count); non-zero on error

#### `carta audit --fix-interactive`

Takes the audit report JSON, groups issues by category, and interactively prompts Claude via MCP to recommend fixes. Applies fixes after user approval.

```bash
$ carta audit --output report.json
$ carta audit --fix-interactive --report report.json
```

Flow:
1. Load report.json
2. Group issues by category
3. For each category with fixable issues:
   - Sample issue details (first 3 orphaned chunks, all stale sidecars, etc.)
   - Call Claude MCP tool to ask: "Should we remove these? Keep them? Why?"
   - Show recommendation to user
   - If approved: apply fixes (delete from Qdrant, remove sidecars, etc.)
4. Write summary of applied fixes

### JSON Report Schema

```json
{
  "summary": {
    "total_issues": 62,
    "by_category": {
      "orphaned_chunks": 47,
      "missing_sidecars": 3,
      "stale_sidecars": 5,
      "hash_mismatches": 4,
      "disconnected_files": 2,
      "qdrant_sidecar_mismatches": 1
    },
    "scanned_at": "2026-04-07T14:32:00Z",
    "repo_root": "/path/to/repo",
    "project_name": "myproject",
    "collection_scanned": "myproject_doc"
  },
  "issues": [
    {
      "id": "orphaned_1",
      "category": "orphaned_chunks",
      "severity": "warning",
      "sidecar_id": "docs_test_md_xyz123",
      "chunk_ids": [42, 43, 44],
      "chunk_count": 3,
      "first_chunk_text": "First 100 characters of chunk 42...",
      "metadata": {
        "doc_type": "doc",
        "collection": "myproject_doc"
      }
    },
    {
      "id": "stale_1",
      "category": "stale_sidecars",
      "severity": "info",
      "file_path": "docs/api.md",
      "sidecar_path": "docs/api.md.embed-meta.yaml",
      "last_embedded": "2026-03-20T10:15:00Z",
      "file_mtime": "2026-04-05T14:32:00Z",
      "days_stale": 12
    },
    {
      "id": "disconnected_1",
      "category": "disconnected_files",
      "severity": "info",
      "file_path": "docs/orphaned.md",
      "reason": "File exists, no sidecar, no chunks in Qdrant"
    }
  ]
}
```

Each issue includes:
- `id`: Unique identifier for this issue (for fixing)
- `category`: Issue type (from list above)
- `severity`: "error" (data inconsistency), "warning" (semantic concern), "info" (minor)
- Category-specific fields with enough detail for Claude to assess semantic value

### Implementation Structure

**New module:** `carta/audit/audit.py`
- Core logic: iterate files, query Qdrant, compare, detect mismatches
- Functions: `run_audit()`, `detect_orphaned_chunks()`, `detect_stale_sidecars()`, etc.
- Returns: dict matching JSON schema (not yet serialized)

**CLI integration:** `carta/cli.py`
- New command: `cmd_audit()`
- Handles `--output` and `--fix-interactive` flags
- Calls `run_audit()`, serializes to JSON, optionally triggers interactive fix flow

**New skill:** `audit-embed`
- When to run: before big doc refactors, after merge conflicts in docs/, if suspecting stale chunks
- How to read JSON: category summaries, severity levels, example interpretation
- Bonus: can also periodically search quirks/session collections to reorganize based on project state (manual Claude task)
- Tips: common issues and what they mean

### Error Handling

- If Qdrant unreachable: report error, suggest checking Qdrant service
- If file disappears mid-scan: treat as disconnected (report and continue)
- If sidecar is malformed YAML: report as error, don't try to parse
- Lock file conflicts: standard lock behavior (fail with message)

### Testing

- Unit tests for each detection function (mocked Qdrant)
- Integration test: create temp repo with intentional inconsistencies, verify audit catches all
- JSON schema validation test

## Alternatives Considered

### Option A: Auto-repair without agent
Simpler, but orphaned chunks might have semantic value. Rejected.

### Option B: Interactive agent during `carta audit`
More seamless UX, but adds external dependency to every audit. Rejected in favor of two-command flow.

### Option C: Separate `carta repair` command
More granular, but users expect `audit --fix`. Stick with unified interface.

## Success Criteria

- Audit completes in <5s on repos with 100s of files
- All six issue types detected reliably (verified by integration test)
- JSON output is machine-readable and contains enough detail for Claude to make decisions
- Skill teaches Claude when/how to use audit effectively
- `--fix-interactive` flow is responsive (agent calls are batched by category)

## Non-Goals

- Fixing quirks/session collections (handled separately by Claude semantic searches)
- Automatic repair without oversight
- Real-time monitoring (audit is on-demand)
