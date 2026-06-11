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
- Filename `YYYY-MM-DD-<slug>.md` — slug from `title` (or first ~6 words of `text`),
  lowercase kebab-case; on collision append `-2`, `-3`, …
- File content: YAML frontmatter (`doc_type`, `title`, `created` ISO date, `tags` when
  given) + blank line + `text` verbatim.
- Embeds via the existing `run_embed_file(path, cfg)` (pipeline.py:1035) — sidecar stub,
  chunking, upsert all reuse the standard pipeline.
- **Ensures the target Qdrant collection exists** before embedding (so capture works on
  projects bootstrapped before this release, without re-init).
- Returns `{"path": <repo-relative>, "collection": <name>, "chunks": <int>}`; raises with a
  clear message on failure (callers map to their error shape). If embedding fails after the
  file was written, the file is kept and the error says so — the note is not lost, and a
  later `carta embed` picks it up.

### 2. Prerequisite fix: frontmatter `doc_type` override (induct.py)

- `generate_sidecar_stub()` / doc-type inference: a `doc_type:` key in the file's YAML
  frontmatter **wins** over parent-directory inference.
- Add `quirks` → `quirk` and `notes` → `helpful-note` to `_PATH_TYPE_MAP` so
  frontmatter-less hand-written files in those directories route correctly on (re-)embed.
- Existing behavior unchanged for files with neither frontmatter nor a mapped parent.

### 3. Prerequisite fix: `_notes` collection (bootstrap + doctor)

- Bootstrap creates `["doc", "session", "notes"]` (replacing `quirk` in the list; `_session`
  stays — it exists in deployed projects and scoped search lists it).
- `carta doctor`: new check — if `{project}_notes` is missing, create it (report as a fix);
  if a legacy empty `{project}_quirk` exists, report it as removable (do not auto-delete).

### 4. Surfaces

- **MCP tool** (mcp/server.py, FastMCP decorator pattern, alongside `carta_search`):
  `carta_remember(text: str, note_type: str = "helpful-note", title: str = "", tags: list[str] | None = None) -> dict`
  — returns the capture_note result or `{"error", "detail"}` matching the existing tools'
  error shape. Docstring guides the model on when to use which note_type.
- **CLI**: `carta remember "text..." --type quirk --title "..." --tags a,b` — prints the
  created path and collection; exit 1 with stderr message on error. Registered in the
  existing subparser block (cli.py).

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
  no frontmatter + unmapped path ⇒ unchanged behavior.
- **bootstrap/doctor**: collection list includes `notes`, not `quirk`; doctor creates
  missing `_notes`, flags legacy `_quirk`.
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
