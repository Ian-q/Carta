---
id: 2026-06-10-note-capture-design
title: "Note Capture — Quirks & Notes (v0.10.0) — Design"
status: shipped
related:
  - 2026-06-11-note-capture
date: 2026-06-10
---

# Note Capture — Quirks & Notes (v0.10.0) — Design

**Date:** 2026-06-10
**Status:** Approved (design discussed and trimmed in session)
**Target version:** 0.10.0 (new feature surface)

## Problem

Carta's pitch is "documentation **and session memory**," and the read side exists — the
`{project}_notes` collection is searched by scoped search, quirk/bug-note/helpful-note are
`PROTECTED_DOC_TYPES` in lifecycle cleanup — but **nothing can write a note**. There is no MCP
tool, CLI command, or skill that records a piece of knowledge and embeds it. Hand-writing a
quirk file doesn't work either, because of two pre-existing bugs:

1. **Collection mismatch:** `collection_for_doc_type()` (config.py:150) routes
   quirk/bug-note/helpful-note → `{project}_notes`, but bootstrap
   (install/bootstrap.py:359) creates `{project}_quirk` and never creates `_notes`.
2. **No frontmatter doc_type:** `infer_doc_type()` (induct.py:37) only maps parent
   directories (`datasheets/`, `manuals/`, `reference/`, `specs/`, `guides/`); a hand-written
   `docs/quirks/*.md` file infers `"unknown"` and lands in `_doc` (this happened in the
   ET-embed deployment).

## Positioning (why this and not claude-mem / Claude Code built-in memory)

Three memory systems coexist and must stay in **disjoint content lanes**:

| Lane | System | Content |
|---|---|---|
| What happened | claude-mem (personal, machine-local, auto) | Session history, observations |
| How to work with the user | Claude Code built-in memory | Preferences, feedback |
| **What's true about the project** | **Carta notes (this feature)** | Curated facts: quirks, bug notes, helpful notes |

Carta notes are **repo-resident markdown files** — git-versioned, team-shared (via git or
`carta export`), retrieved by the *same* hybrid search/reranker/hook as the project docs,
and maintained by `carta scan`/audit. The capture flow is a **promotion** ("graduation")
model: a discovery starts as ephemeral session history; when it proves durable, it is
promoted once into a Carta note and becomes project truth. This *reduces* cross-tool
duplication over time rather than adding a parallel store.

**Out of scope (explicitly):** a `session` note type (cut — session history is claude-mem's
lane; `modules.session_memory` remains a dormant placeholder for a possible future Stop-hook
auto-capture, which is a separate design problem). No automatic capture this cycle. No
coupling to claude-mem.

## Design

### 1. Core: `carta/memory/capture.py`

```python
def capture_note(cfg: dict, repo_root: Path, text: str, *,
                 note_type: str, title: str = "", tags: list[str] | None = None) -> dict
```

- Validates `note_type` ∈ {`quirk`, `bug-note`, `helpful-note`} (ValueError otherwise) and
  rejects empty/whitespace `text`.
- Destination directories from a new `memory:` config block (deep-merged defaults):
  ```yaml
  memory:
    quirks_dir: docs/quirks      # note_type: quirk
    notes_dir: docs/notes        # note_type: bug-note, helpful-note
  ```
  **Repo footprint policy (recorded rationale):** knowledge artifacts are content-named and
  blend into the user's docs tree (like `docs/adr/` convention; they must remain useful if
  Carta is removed — hence generic `doc_type:` frontmatter, not a carta-branded schema or a
  `docs/carta/` namespace). Tool artifacts stay contained in the single `.carta/` machine
  dir (the sidecar-relocation precedent). Containment-minded users can point these config
  keys at a namespaced dir. Nothing new is ever added at repo root.
- Filename `YYYY-MM-DD-<slug>.md` — slug from `title` (or first ~6 words of `text`),
  lowercase kebab-case; on collision append `-2`, `-3`, …
- File content: YAML frontmatter (`doc_type`, `title`, `created` ISO date, `tags` when
  given) + blank line + `text` verbatim.
- Embeds via the existing `run_embed_file(path, cfg)` (pipeline.py:1035) — sidecar stub,
  chunking, upsert all reuse the standard pipeline. (Collection auto-creation is already
  handled by `ensure_collection()` inside `upsert_chunks` — no extra plumbing.)
- Returns `{"path": <repo-relative>, "collection": <name>, "chunks": <int>}`; raises with a
  clear message on failure (callers map to their error shape). If embedding fails after the
  file was written, the file is kept and the error says so — the note is not lost, and a
  later `carta embed` picks it up.

### 2. Prerequisite fixes: doc_type routing end-to-end

Planning investigation found the routing gap is wider than bootstrap — `collection_for_doc_type`
(config.py:150) is currently **dead code**; nothing in the embed path calls it. Three fixes:

- **Frontmatter override (induct.py):** a `doc_type:` key in a markdown file's YAML
  frontmatter **wins** over parent-directory inference (reuses `scanner.parse_frontmatter`).
  Add `quirks` → `quirk` and `notes` → `helpful-note` to `_PATH_TYPE_MAP` so frontmatter-less
  hand-written files route correctly. Existing behavior unchanged otherwise. The sidecar
  stub's informational `collection` field uses `collection_for_doc_type` instead of
  hardcoded `_doc`.
- **Upsert routing (embed.py:184):** `upsert_chunks` hardcodes
  `coll_name = collection_name(cfg, "doc")`; change to
  `collection_for_doc_type(cfg, chunks[0].get("doc_type", "unknown"))` (batches are
  per-file, hence doc_type-homogeneous; image/visual doc_types still map to `_doc`, so all
  current behavior is preserved except note types, which now route to `_notes`).
- **Bootstrap (bootstrap.py:13,359,255):** create `["doc", "session", "notes"]` (replacing
  `quirk` in the list and in `VECTOR_DIMENSIONS`; `_session` stays — deployed projects have
  it and scoped search lists it). Update the success-message string.

### 3. Migration: none needed

`ensure_collection()` inside `upsert_chunks` auto-creates missing collections with the
correct schema, so the first capture on a pre-0.10.0 project creates `{project}_notes`
automatically. Legacy `{project}_quirk` collections are inert empties (the old routing never
wrote to them) and can be ignored or manually deleted. No doctor check is added (YAGNI —
revisit only if real confusion shows up).

### 4. Surfaces

- **MCP tool** (mcp/server.py, FastMCP decorator pattern, alongside `carta_search`):
  `carta_remember(text: str, note_type: str = "helpful-note", title: str = "", tags: list[str] | None = None) -> dict`
  — returns the capture_note result or `{"error", "detail"}` matching the existing tools'
  error shape. Docstring guides the model on when to use which note_type.
- **CLI**: `carta remember "text..." --type quirk --title "..." --tags a,b` — prints the
  created path and collection; exit 1 with stderr message on error. Registered in the
  existing subparser block (cli.py).
- **Bootstrap AGENTS.md text** (bootstrap.py:487): replace the phantom `/session-memory`
  skeleton with guidance for the real `carta_remember` MCP tool / `carta remember` CLI.

### 5. Recall labeling

Search results and hook-injected context label note-typed hits so recalled memory is
distinguishable from docs: `**Source: [quirk] docs/quirks/2026-06-10-….md (score: 0.91)**`.
Implementation: `run_search` already builds hit dicts from Qdrant payloads — include the
payload's `doc_type` in the hit dict; `cmd_search` output and hook `_inject()` prefix
`[<doc_type>]` for doc_types in {quirk, bug-note, helpful-note}. No change for plain docs.

### 6. Lifecycle / compatibility

- quirk/bug-note/helpful-note are already `PROTECTED_DOC_TYPES` — vectors survive source
  deletion (existing behavior, now reachable).
- `docs/quirks/` & `docs/notes/` are inside `docs_root`, so normal `carta embed` discovery,
  staleness scanning, and the audit cover captured notes with zero extra plumbing.
- `carta export`/`import` already include `_notes` by prefix discovery.
- `modules.session_memory` untouched (dormant).

## Testing

TDD per component:

- **capture core**: file written with exact frontmatter shape; slug from title vs text;
  collision suffixing; type→directory routing; invalid type/empty text rejected; collection
  ensured when missing; embed failure keeps the file and reports.
- **induct**: frontmatter doc_type beats path inference; `quirks/`/`notes/` path mapping;
  no frontmatter + unmapped path ⇒ unchanged behavior; stub collection field routed.
- **upsert routing**: chunks with doc_type quirk/bug-note/helpful-note upsert into
  `{project}_notes`; plain docs and image/visual chunks still go to `{project}_doc`.
- **bootstrap**: collection list includes `notes`, not `quirk`.
- **MCP + CLI wiring**: happy path and error shape for both surfaces.
- **labeling**: hook injection and search output prefix `[quirk]` etc.; plain docs
  unaffected.

**Live validation:** in a real project, `carta_remember` a quirk via MCP and one via CLI;
confirm file lands in `docs/quirks/`, vectors in `{project}_notes`, `carta search` returns
it labeled, and the hook surfaces it for a related prompt.

## Out of scope

- `session` note type, Stop-hook auto-capture, claude-mem bridges (`carta distill`).
- Plugin skill (`/remember`) — revisit after the MCP/CLI UX settles.
- Note editing/deletion commands (files are plain markdown; edit with any editor,
  re-embed handles updates).
