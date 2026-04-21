# Sidecar Relocation: Co-located → `.carta/sidecars/`

**Date:** 2026-04-21
**Status:** Approved

## Problem

Carta currently places `.embed-meta.yaml` sidecar files next to their source documents (e.g. `docs/manuals/chip.embed-meta.yaml`). This pollutes the user's file explorer and git status with carta-internal metadata files that users should never need to touch.

## Goal

Move all sidecars into `.carta/sidecars/`, mirroring the repo's directory structure, so the workspace stays clean. `.carta/` is already gitignored and is the established home for all carta state.

## Chosen Approach: Mirror under `.carta/sidecars/`

Sidecars live at:
```
.carta/sidecars/{path/relative/to/repo_root}/{stem}.embed-meta.yaml
```

Example: `docs/manuals/chip.pdf` → `.carta/sidecars/docs/manuals/chip.embed-meta.yaml`

### Why this approach

- Path is human-readable and reversible
- Single pure function encapsulates the mapping
- Inverse lookup uses `current_path` already stored in every sidecar — no fragile filename parsing
- Scoped discovery (`.carta/sidecars/` rglob) is faster and can't accidentally pick up stray files
- Centralised cleanup: `rm -rf .carta/sidecars/` nukes all sidecar state

## Design

### Section 1: Path Resolution

A new helper `sidecar_path(file_path: Path, repo_root: Path) -> Path` in `carta/embed/induct.py` encapsulates the canonical mapping:

```python
def sidecar_path(file_path: Path, repo_root: Path) -> Path:
    rel = file_path.relative_to(repo_root)
    return repo_root / ".carta" / "sidecars" / rel.with_suffix(".embed-meta.yaml")
```

This replaces every hardcoded `file_path.parent / (file_path.stem + ".embed-meta.yaml")` pattern across the codebase.

**Inverse mapping:** Finding the source file from a sidecar reads `sidecar["current_path"]` (already stored in every sidecar) rather than inferring it from filesystem position. This removes the fragile string-replace pattern in `audit.py:73`.

**Discovery:** Changes from `repo_root.rglob("*.embed-meta.yaml")` to `(repo_root / ".carta" / "sidecars").rglob("*.embed-meta.yaml")` throughout.

### Section 2: Migration

On every `carta embed` run, a `migrate_sidecars(repo_root)` step runs before any other work. It scans the entire repo for co-located `*.embed-meta.yaml` files (old pattern) and for each one:

1. Computes the new `.carta/sidecars/...` path via `sidecar_path()`
2. Creates any missing parent directories
3. Moves the file with `shutil.move()`
4. Prints `migrated: {old_rel} → {new_rel}` to stdout

Already-migrated files won't match the old discovery pattern, so subsequent runs are a no-op with zero overhead. No separate migration command needed.

### Section 3: Orphan Detection

After migration, `carta embed` calls `detect_orphaned_sidecars(repo_root)` which walks `.carta/sidecars/` and checks each sidecar's `current_path` field against the filesystem. For any missing source file, it prints to stderr:

```
Warning: orphaned sidecar (source not found): .carta/sidecars/docs/manuals/old-chip.embed-meta.yaml
  → source was: docs/manuals/old-chip.pdf
  Run 'carta audit' for full orphan report.
```

`carta audit` gets a new `missing_source` check that lists all orphaned sidecars with their expected source paths. No automatic deletion — the user decides whether to delete or update the sidecar.

### Section 4: Affected Code Touchpoints

| File | Change |
|------|--------|
| `carta/embed/induct.py` | Add `sidecar_path()` helper; update `write_sidecar()` to create parent dirs |
| `carta/embed/pipeline.py` | Replace all hardcoded sidecar path constructions; scope rglob discovery; add `migrate_sidecars()` and `detect_orphaned_sidecars()` called at top of `run_embed()` |
| `carta/scanner/scanner.py` | Update `_iter_sidecar_files()` and all sibling-sidecar constructions; scope rglob to `.carta/sidecars/` |
| `carta/audit/audit.py` | Scope discovery; replace string-replace source inference with `sidecar["current_path"]`; add orphan check |
| `carta/embed/tests/test_embed.py` | Update sidecar path construction |
| `carta/tests/test_pipeline.py` | Update sidecar path construction |
| `carta/scanner/tests/test_scanner.py` | Update sidecar path construction |
| `carta/audit/tests/test_audit.py` | Update sidecar path construction |
| `carta/tests/test_mcp_server.py` | Update sidecar path construction |

**No changes to:** sidecar YAML schema, Qdrant payload format, CLI interface, or config format.

## Out of Scope (Follow-ups)

- Moving quirk/session memory docs into `.carta/` — separate feature, different scope
