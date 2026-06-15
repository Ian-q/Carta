---
id: 2026-06-08-carta-share-export-import-design
title: "Carta `share` — Export / Import of Embeddings"
status: shipped
related: []
date: 2026-06-08
---

# Carta `share` — Export / Import of Embeddings

**Date:** 2026-06-08
**Status:** Approved design, pre-implementation
**Branch:** `worktree-carta-share-export-import`

## Problem

Embedding a project's docs into Carta is expensive: vision models run over every
PDF page, ColPali produces multi-vector visual embeddings, and the whole pass can
take hours and a capable GPU. A collaborator who wants the *same* project's
knowledge graph should not have to re-run that ingestion. Today the only portable
artifacts are the source docs and `.carta/sidecars/` (status metadata); the actual
vectors live in a machine-local Qdrant instance with no Carta-native way to move
them.

**Goal:** Let one machine that has already embedded a project hand the embedded
state to another machine in a single command on each side, so the receiver can run
`carta search` immediately without any ingestion.

### Concrete motivating case

User A has fully embedded the project (text + visual). User B is setting up the
*same git repository* on a different machine (WSL2 + RTX 3070) and wants text and
visual search without re-embedding. Visual search is wanted by default because B's
work is PCB-centric (datasheets, schematics).

## Scope

**In scope:**
- `carta export` — bundle this project's Qdrant collections (+ config + sidecars +
  manifest) into a single portable `.tar.gz`.
- `carta import` — restore that bundle into the local Qdrant and wire up
  `.carta/` so `carta search` works immediately.

**Out of scope (YAGNI):**
- Portable/JSON vector format or cross-Qdrant-version migration. Snapshots are
  coupled to the Qdrant server version; export/import assumes both sides run the
  same (or compatible) `qdrant/qdrant` version and warns when they differ.
- Incremental / delta sync. Each bundle is a full snapshot set.
- Encryption or transport. Producing the file is the boundary; how it travels
  (USB, scp, cloud drive) is the user's choice.
- Re-keying point IDs or merging into a non-empty collection. Import targets fresh
  (or `--force`-overwritten) collections.

## Mechanism

Qdrant's native **snapshot API**, driven through the existing `qdrant-client`
dependency plus `requests` (already a Carta dependency) for the file
download/upload that the Python client does not wrap:

- **Create:** `client.create_snapshot(collection_name=...)` → snapshot name.
- **Download:** HTTP `GET {qdrant_url}/collections/{c}/snapshots/{name}` → bytes
  written to a file in the bundle.
- **Upload/restore:** HTTP `POST {qdrant_url}/collections/{c}/snapshots/upload`
  (multipart) — creates the collection from the snapshot file.
- **Discover:** `client.get_collections().collections`, filtered to names starting
  with `{project_name}_`.
- **Server version (for manifest + preflight):** HTTP `GET {qdrant_url}/` →
  `{"version": "x.y.z"}`.

Snapshots round-trip the `_visual` collection's ColPali multi-vectors natively —
the main reason snapshots were chosen over a scroll-and-reupsert format.

## Components

A new module `carta/share.py` holds the logic; thin command wrappers live in
`carta/cli.py`. The module exposes two top-level functions and small helpers,
following the existing `run_*` convention (cf. `run_embed`, `run_search`).

### `carta/share.py`

```
run_export(cfg, *, output_path=None, include_visual=True, verbose=True) -> Path
run_import(bundle_path, cfg, *, project=None, force=False, verbose=True) -> dict

# helpers (internal, underscore-prefixed)
_discover_collections(client, project_name, include_visual) -> list[str]
_qdrant_server_version(qdrant_url) -> str | None
_create_and_download_snapshot(client, qdrant_url, collection, dest_dir) -> Path
_upload_snapshot(qdrant_url, collection, snapshot_file) -> None
_build_manifest(...) -> dict
_read_manifest(bundle_dir) -> dict
_rewrite_collection_name(name, old_project, new_project) -> str
```

`run_export` and `run_import` take an already-loaded `cfg` (same pattern as the
rest of the codebase — CLI loads config, passes the dict). They construct their own
`QdrantClient(url=cfg["qdrant_url"], timeout=...)` exactly like the other modules.

### `carta/cli.py`

- `cmd_export(args)` / `cmd_import(args)` — load config via `find_config`, call the
  `run_*` function, translate exceptions to `sys.exit(1)` with a stderr message
  (matches existing command handlers).
- Two `sub.add_parser(...)` registrations:
  - `export`: `--no-visual` (store_false → `include_visual`), `-o/--output PATH`.
  - `import`: positional `bundle`, `--project NAME`, `--force`.

## Bundle format

A gzipped tar (`carta-<project>-<YYYYMMDD>.tar.gz` by default) containing:

```
manifest.json
config.yaml                # copy of .carta/config.yaml
snapshots/
  <project>_doc.snapshot
  <project>_notes.snapshot
  <project>_session.snapshot
  <project>_visual.snapshot     # omitted when --no-visual
sidecars/                   # copy of .carta/sidecars/ (recursive)
  ...
```

### `manifest.json`

```json
{
  "carta_version": "0.6.0",
  "qdrant_version": "1.12.4",
  "project_name": "myproject",
  "created_at": "2026-06-08T12:00:00Z",
  "include_visual": true,
  "collections": [
    {"name": "myproject_doc", "snapshot": "snapshots/myproject_doc.snapshot", "points": 1234},
    {"name": "myproject_visual", "snapshot": "snapshots/myproject_visual.snapshot", "points": 56}
  ]
}
```

Point counts come from `client.count(collection)` at export time and are shown to
the user on both sides for a sanity check.

## Data flow

### Export
1. Load config; construct Qdrant client; verify connectivity (clear error if Qdrant
   is unreachable — reuse the existing connectivity-error style).
2. Discover `{project}_*` collections; drop `_visual` if `--no-visual`. Error if
   zero collections found (nothing embedded yet).
3. For each collection: create a snapshot, download it into a temp `snapshots/`
   dir, record point count.
4. Copy `.carta/config.yaml` and `.carta/sidecars/` into the staging dir.
5. Write `manifest.json`.
6. Tar+gzip the staging dir to the output path. Clean up temp dir and the
   server-side snapshots (`client.delete_snapshot`) so Qdrant's snapshot store
   doesn't accumulate.
7. Print output path, size, and a one-line handoff hint.

### Import
1. Unpack bundle to a temp dir; read `manifest.json` (error clearly if missing /
   malformed — not a Carta bundle).
2. Determine target project name: `--project` if given, else manifest's
   `project_name`. Compute target collection names (rewrite prefix if `--project`).
3. Preflight:
   - Qdrant unreachable → hard error.
   - Server version ≠ manifest `qdrant_version` → **warn**, continue (snapshots are
     usually compatible across patch/minor; user opted into "latest Qdrant").
   - carta version mismatch → informational note only.
   - Any target collection already exists → **hard stop** listing them, unless
     `--force` (which deletes them first). Never silently overwrite.
4. Upload each snapshot to restore its collection.
5. Wire up `.carta/`:
   - If no local `.carta/config.yaml`, write the bundled one. If one exists, leave
     it but ensure `project_name` matches the restored collections; warn on
     mismatch with the exact fix.
   - Copy `sidecars/` into `.carta/sidecars/` (don't clobber newer local sidecars:
     only write files that are absent locally; report counts).
6. Print restored collections + point counts, and the next step
   (`carta search "..."`), plus a reminder to set `colpali_device: cuda` if the
   machine has an NVIDIA GPU and visual was included.

## Error handling

Follows existing conventions: specific exceptions before generic; clear stderr
message + `sys.exit(1)` at the CLI boundary; `run_*` functions raise rather than
exit so they stay testable. Partial-failure policy:

- Export: if a snapshot fails mid-run, abort and clean up the partial staging dir
  and any server-side snapshots already created; no half-bundle is written.
- Import: validate the full manifest and run all preflight checks *before*
  uploading anything. Once uploads begin, a mid-run failure leaves already-restored
  collections in place and reports which succeeded/failed (restoring is per
  collection and idempotent under `--force`).

## Testing

`carta/tests/test_share.py`, built test-first. The Qdrant client and the
`requests` download/upload calls are mocked; the tar round-trip and manifest
read/write run for real against `tmp_path`.

Cases:
- **Manifest:** `_build_manifest` shape; round-trips through `_read_manifest`.
- **Collection discovery:** prefix filtering; `--no-visual` excludes `_visual`;
  unrelated projects' collections excluded; zero-collection error.
- **Export happy path:** mocked client/snapshots → a real `.tar.gz` exists and
  contains manifest, config, expected snapshot entries, sidecars.
- **Export cleanup:** server-side `delete_snapshot` called; no temp dir left.
- **Import happy path:** real tar in → upload called per collection; sidecars and
  config written; summary dict correct.
- **Import preflight:** missing/garbage manifest errors; existing target
  collection blocks without `--force` and is deleted with `--force`; version
  mismatch warns but proceeds.
- **`--project` rename:** collection names rewritten on restore; config
  `project_name` reconciled.
- **CLI wiring:** `cmd_export`/`cmd_import` parse args and dispatch (mock `run_*`).

## Docs

- README: short "Sharing an embedded project" section (export on A, transfer,
  import on B; same-Qdrant-version note; WSL/`cuda` tip).
- `--help` text for both subcommands.

## Build sequence

1. `test_share.py` for manifest + discovery helpers → implement helpers.
2. Tests for `run_export` → implement.
3. Tests for `run_import` (preflight, restore, wiring) → implement.
4. CLI wiring tests → `cmd_export`/`cmd_import` + parsers.
5. README + help text.
6. Full suite green; code review; finish branch.
