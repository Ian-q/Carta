---
id: 2026-06-14-carta-status-command-design
title: "`carta status` — system-wide status command — design"
status: shipped
related:
  - 2026-06-14-carta-status-command
date: 2026-06-14
---

# `carta status` — system-wide status command — design

**Date:** 2026-06-14
**Status:** Approved design, pending implementation plan
**Author:** brainstormed with Claude (superpowers flow)

## Problem

Carta state is scattered and only visible while a command runs. To know where a
project stands — is an embed running, how far along, how many docs are pending or
stale — you have to either watch the `carta embed` TTY output live (lost once it
backgrounds or finishes) or already be sitting in that project. There is no way to
pull up a quick, ambient readout of carta's state from any terminal, and no way to
see at a glance what's happening across the several projects a user runs carta in.

The status-line widget (`carta statusline`, 2026-06-06) solved the *live, in-flight*
case for the current repo's status line. This is the complementary *on-demand
snapshot* case: a command you run anywhere to get the full picture.

## Goal

A `carta status` command that, run from any terminal, prints a quick snapshot of
carta's state: the project you're currently in (in detail — embed stage, file
counts, etc.), plus a compact one-line summary of every other carta project on the
machine. Local-only and instant by default; an opt-in `--check` flag adds live
Qdrant/Ollama health and point counts for the current project.

## Non-goals

- **No live / `watch` mode.** One-shot snapshot only. The status-line widget already
  covers continuous live progress.
- **No filesystem scan for discovery.** Other projects come from a registry that
  carta maintains as it runs (see D1), never from crawling the disk.
- **No cross-service fan-out.** `--check` pings services for the *current* project
  only — not every registered project (they may use different Qdrant/Ollama URLs;
  fanning out would be unbounded and slow). See D3.
- **No change to embed behavior, Qdrant collections, or sidecar formats.** Purely
  additive and read-only (apart from the small registry file).

## Decisions

- **D1 — discovery via a registry, not a scan.** A small JSON file at
  `~/.carta/registry.json` maps each known project's root path → its name, Qdrant
  URL, and a `last_seen` timestamp. Carta upserts the current project into it
  whenever it resolves a project config on `init`, `embed`, and `status`. The
  registry fills in naturally as the user runs carta; no disk crawl, deterministic
  and fast. The *current* project is always resolved fresh from cwd, so it shows
  correctly even when the registry is empty (fresh machine).
- **D2 — local-only by default, `--check` for network.** Default reads only `.carta/`
  files: never hangs, never needs a service up. `--check` additionally queries Qdrant
  (per-collection point counts) and Ollama (reachable?) for the current project, with
  short timeouts.
- **D3 — `--check` is current-project-only.** Other projects always render from local
  file state regardless of `--check`, keeping the command bounded and fast.
- **D4 — current project in detail, others as one-liners.** The project resolved from
  cwd gets a multi-line block (embed state, corpus breakdown, Qdrant URL); every other
  registered project gets a single summary line. Run outside any carta project → the
  detail block is skipped and only the registered-project list is shown.
- **D5 — reuse, don't duplicate.** Embed-run state resolution reuses
  `carta.statusline.read_status` / `resolve_state` / `_pid_alive` / `_fmt_elapsed` /
  `_fmt_chunks`; corpus counts reuse `carta.embed.induct.read_sidecar`. The status
  module gathers a snapshot dict; rendering is pure formatting over that dict.
- **D6 — `--json` for scriptability.** A `--json` flag emits the structured snapshot,
  matching the `doctor --json` / `audit` precedent. The dict the formatters render is
  the same dict that gets serialized — no second code path.

## Architecture

Three small, independently-testable units:

```
carta {init,embed,status}  ──upsert──>  ~/.carta/registry.json
                                              │
                                  load + prune dead entries
                                              ▼
carta status ──> gather_project_status(repo_root, …) ──> snapshot dict
   (cwd)          (reads .carta/ files; --check adds        │
                   Qdrant/Ollama for current only)          ▼
                                            format_current / format_other  ──> stdout
```

The registry is the only new piece of *state*; everything else is read-only
aggregation of artifacts that already exist (`embed-status.json`, `embed.lock`,
sidecars, config).

### Component 1 — project registry (`carta/registry.py`)

A new leaf module. Global carta state lives under a home directory resolved by
`_carta_home()` = `Path(os.environ["CARTA_HOME"])` if set, else `~/.carta`. The
`CARTA_HOME` override exists primarily so tests can point it at a tmp dir.

File: `~/.carta/registry.json`
```json
{
  "schema": 1,
  "projects": {
    "/Users/ian/dev/ET-embed": {
      "name": "ET-embed",
      "qdrant_url": "http://localhost:6333",
      "last_seen": 1749250000.0
    },
    "/Users/ian/dev/doc-audit-cc": {
      "name": "carta",
      "qdrant_url": "http://localhost:6333",
      "last_seen": 1749240000.0
    }
  }
}
```

Keyed by **absolute project-root path** (the repo root, i.e. `cfg_path.parent.parent`)
so re-registering the same project updates in place and two projects that happen to
share a `project_name` never collide.

Functions:
- `register_project(repo_root, name, qdrant_url, *, now=None) -> None` — upsert the
  entry (path key, `last_seen=now or time.time()`), prune dead entries (see below),
  write atomically (`registry.json.tmp` + `os.replace`), creating `~/.carta/` if
  needed. **Best-effort: every error is swallowed.** A registry write must never
  affect the outcome of the command that triggered it.
- `load_registry() -> list[dict]` — read + parse; return a list of
  `{"path", "name", "qdrant_url", "last_seen"}` entries, **pruning in memory** any
  whose `<path>/.carta/config.yaml` no longer exists (moved/deleted project).
  Returns `[]` on a missing/corrupt file. Does not write (pruning is persisted lazily
  on the next `register_project`).

Both functions never raise.

**Registration hook points.** A thin best-effort call wired into:
- `cmd_init` — after bootstrap (`repo_root = cwd`; name/url read from the freshly
  written config, or skipped if unavailable).
- `cmd_embed` and `cmd_status` — after `load_config`, using
  `repo_root = cfg_path.parent.parent`, `name = cfg["project_name"]`,
  `qdrant_url = cfg.get("qdrant_url")`.

So the first time carta runs in a project it self-registers, and it then appears in
`carta status` invoked from anywhere else.

### Component 2 — status gathering (`carta/status.py`)

Top-level module (path distinct from `carta/embed/status.py`, which is the embed-run
`StatusWriter`). Pure aggregation — no printing.

```python
def gather_project_status(
    repo_root: Path, *, name: str, qdrant_url: str | None,
    check: bool = False, ollama_url: str | None = None, now: float | None = None,
) -> dict
```

Returns a snapshot dict:
```python
{
  "name": "ET-embed",
  "path": "/Users/ian/dev/ET-embed",
  "qdrant_url": "http://localhost:6333",
  "embed": {
     "state": "running" | "done" | "failed" | "interrupted" | "idle" | "never",
     "current_idx": 24, "total": 47, "current_file": "big.pdf",
     "file_elapsed_s": 1140.0,          # when running
     "embedded": 340, "skipped": 5, "errors": 0, "chunks": 1240,
     "finished_at": 1749250000.0, "age_s": 720.0,   # when done/failed
  },
  "corpus": {"total": 345, "done": 340, "pending": 3, "stale": 2,
             "extraction_failed": 0, "other": 0},
  "check": None | {                      # present only when check=True
     "qdrant": {"reachable": True, "collections": {"ET-embed_doc": 12043,
                                                    "ET-embed_visual": 1201}},
     "ollama": {"reachable": True},
  },
}
```

How each part is computed (all reusing existing helpers):
- **embed** — `read_status(repo_root)` for `.carta/embed-status.json`, then map to a
  `state`:
  - lock (`.carta/embed.lock`) PID alive **or** `resolve_state(...) == "running"` →
    `running` (with idx/total/current_file/file_elapsed).
  - no status file → `never`.
  - `phase == "running"` but PID dead and no live lock → `interrupted`
    (a crashed run; render "previous run interrupted").
  - `phase in {done, failed}` → `done`/`failed` with last-run counters and
    `age_s = now - finished_at`.
  - otherwise → `idle`.
- **corpus** — walk `repo_root/.carta/sidecars/**/*.embed-meta.yaml`, `read_sidecar`
  each, tally by `status` into the buckets above (unrecognized → `other`). Empty/no
  sidecars dir → all zeros.
- **check** (only when `check=True`):
  - **Qdrant** — `QdrantClient(url=qdrant_url, timeout=2)`,
    `get_collections()`, keep those named `f"{name}_*"`, record
    `get_collection(c).points_count` per collection. Any failure →
    `{"reachable": False}`.
  - **Ollama** — `GET {ollama_url}/api/tags` with `timeout=2` →
    `{"reachable": bool}`. Any failure → `{"reachable": False}`.

  Both timeouts are short (2s) by design so `--check` degrades quickly when a
  service is down rather than hanging the command.

`gather_project_status` never raises; sub-parts degrade independently (a Qdrant
timeout still yields full local corpus/embed data).

Rendering (pure, `color` defaults to `sys.stdout.isatty()`):
- `format_current(snapshot, *, color=True) -> str` — the detailed block.
- `format_other(snapshot, *, color=True) -> str` — the one-liner.

### Component 3 — `carta status` CLI (`carta/cli.py`)

`cmd_status(args)`:
1. Resolve the current project: `find_config()`.
2. **If found:** `load_config`; `register_project(...)` (best-effort); gather the
   current snapshot with `check=args.check`; load the registry; gather a *lite*
   (local-only, `check=False`) snapshot for each **other** entry (path ≠ current
   repo root); sort others by `last_seen` desc.
3. **If `FileNotFoundError`** (not inside a carta project): no current block; gather
   lite snapshots for all registered projects. If the registry is also empty, print a
   short hint ("Not inside a carta project, and none registered yet — run carta in a
   project first.").
4. Output:
   - `--json` → `{"current": <snapshot|null>, "others": [...], "checked": bool}`.
   - otherwise → `format_current(current)` (if any), a blank line, then
     `Other projects (N):` and one `format_other(...)` line each.

New subparser `status` with flags `--check` (network for current project) and
`--json`. Registered in the `dispatch` table.

## Example output

Default (local-only), run from `~/dev/ET-embed`:
```
carta · ET-embed                                   /Users/ian/dev/ET-embed
  embed   idle — last run 12m ago: 340 embedded, 5 skipped, 0 errors (1.2k chunks)
  docs    345 total · 340 done · 3 pending · 2 stale
  qdrant  http://localhost:6333   (--check for live counts)

Other projects (2):
  doc-audit-cc   idle      881 docs · all done           ~/dev/doc-audit-cc
  some-repo      running   42/350 · embedding foo.pdf     ~/dev/some-repo
```

With `--check` (current project gains live service lines):
```
  qdrant  up · ET-embed_doc 12,043 pts · ET-embed_visual 1,201 pts
  ollama  up · http://localhost:11434
```

## Data flow

1. User runs `carta status` in `~/dev/ET-embed`.
2. CLI finds `.carta/config.yaml`, loads it, upserts ET-embed into the registry.
3. `gather_project_status` reads `embed-status.json`, `embed.lock`, and the sidecar
   tree → current snapshot (no network unless `--check`).
4. Registry is loaded (dead entries pruned); each other project gets a local-only
   snapshot.
5. Formatters render the detailed current block + one line per other project.

## Error handling

- Registry reads/writes are best-effort and never raise; a broken registry degrades
  to "no other projects."
- `gather_project_status` never raises; Qdrant/Ollama failures under `--check` render
  as `down`/`unreachable` while local data still shows.
- Not-in-a-project is a normal path, not an error (registry-only view).
- Corrupt/partial `embed-status.json` → treated as `idle`/`never` (atomic writes by
  the producer make this rare).
- Stale `phase=running` from a crash → `interrupted` (never phantom progress), same
  PID-liveness logic the status-line widget already uses.

## Testing (TDD)

- **registry** (`test_registry.py`): upsert creates `~/.carta/registry.json` under a
  tmp `CARTA_HOME`; re-register updates in place (keyed by path) and refreshes
  `last_seen`; `load_registry` prunes entries whose `config.yaml` is gone; corrupt /
  missing file → `[]`; write errors swallowed (no raise).
- **gather** (`test_status.py`): against a fabricated `.carta/` tree —
  - corpus tally across mixed sidecar statuses (incl. unknown → `other`); no sidecars
    dir → zeros.
  - embed state mapping: never (no file), running (live PID/lock, mocked), done/failed
    (counters + `age_s`), interrupted (phase=running + dead PID).
  - `check=True` with Qdrant/Ollama mocked: reachable populates counts; exceptions →
    `reachable: False` without breaking local data.
- **formatters** (`test_status.py`): `format_current` / `format_other` over snapshot
  dicts produce the expected fragments for each state; `color=False` is plain text;
  long filenames truncated.
- **CLI** (`test_cli.py` style): `carta status` in a project prints the detail block
  and registers it; from another dir lists it under "Other projects"; outside any
  project with empty registry prints the hint; `--json` emits valid JSON with
  `current`/`others`/`checked`; `--check` toggles the `check` block (services mocked).

## Files

- **New:** `carta/registry.py`, `carta/status.py`, `carta/tests/test_registry.py`,
  `carta/tests/test_status.py`.
- **Modified:** `carta/cli.py` (`cmd_status` + subparser + dispatch; best-effort
  `register_project` calls in `cmd_init`/`cmd_embed`/`cmd_status`),
  `carta/tests/test_cli.py` (status command coverage).

## Rollout / compatibility

- Purely additive. The only new persistent artifact is `~/.carta/registry.json`
  (global, outside any repo — nothing to add to project `.gitignore`).
- No change to Qdrant collections, sidecar metadata, embed/scan/search behavior, or
  the existing `carta statusline` widget.
- Works on a fresh machine with an empty registry: the current project always shows;
  the cross-project list grows as carta is run in more projects.
