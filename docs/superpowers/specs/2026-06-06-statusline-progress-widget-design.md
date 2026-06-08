# Carta status-line progress widget — design

**Date:** 2026-06-06
**Status:** Approved design, pending implementation plan
**Author:** brainstormed with Claude (superpowers flow)

## Problem

Running the full `carta embed` workflow takes a long time — minutes to hours for
large document sets, with individual large PDFs (e.g. a 475-page manual) sometimes
sitting "in flight" for 15–20 minutes. When an agent kicks off `carta embed` in the
background, the user has no ambient signal of progress: they can't tell whether it's
advancing, stuck on a big file, finished, or dead. Today the only feedback is the
TTY progress bar (lost when run in the background) and the append-only
`CARTA_PERF_LOG` JSONL (completion events only — no in-flight state, no total).

## Goal

Surface live `carta embed` progress as a compact segment in Claude Code's status
line, so that while an embed runs in the background the user sees forward motion and
can spot a stalled file — without manual polling and without crowding the existing
status line.

## Non-goals

- Showing embeds from a *different* repo/worktree than the current working directory
  (see Decision D1 — cwd-scoped for v1).
- Progress for `carta scan` / `carta search` (fast operations; not worth a widget).
- A heartbeat thread or any new long-lived process.
- Replacing the user's existing status line.

## Decisions

- **D1 — cwd-scoped (v1).** The segment reflects the embed for the repo that Claude
  Code reports as the current working directory. An embed in another worktree is not
  shown. A global cross-repo registry is deferred.
- **D2 — content:** spinner · `current/total` · current filename · elapsed-on-file.
- **D3 — integration:** carta auto-wires a segment call into the user's existing
  status-line script, with a `[y/N]` confirm and a `.bak` backup. Built on a
  `carta statusline` segment-printer primitive.
- **D4 — edges:** empty when idle; a ~30s finish flash after a run; crashed/stale
  runs are detected (dead PID / stale heartbeat) and rendered as idle (never phantom
  progress).

## Architecture

Three pieces, decoupled entirely through one file on disk. They never communicate
directly, which is what makes the widget independent of *how* the embed was launched
(background Bash, MCP `carta_embed`, another terminal):

```
carta embed  ──writes──>  .carta/embed-status.json  <──reads──  carta statusline
 (any launch path)            (live run state)                  (segment printer,
                                                                 run by Claude Code
                                                                 on each render)
```

### Component 1 — status file: `.carta/embed-status.json`

Single JSON object, written **atomically** (write to `embed-status.json.tmp`, then
`os.replace`) so a reader never sees a half-written file. Lives in the repo's
existing `.carta/` directory, so it is naturally per-repo / per-worktree.

```json
{
  "schema": 1,
  "pid": 48213,
  "host": "ians-mac.local",
  "phase": "running",
  "started_at": 1749250000.0,
  "updated_at": 1749251140.0,
  "finished_at": null,
  "total": 47,
  "current_idx": 24,
  "current_file": "EN_UM_N32WB03x.pdf",
  "current_file_started_at": 1749250000.0,
  "embedded": 23,
  "skipped": 1,
  "errors": 0,
  "chunks": 2334
}
```

Field notes:
- `phase`: `"running"` | `"done"` | `"failed"`.
- `pid` / `host`: liveness check inputs for the reader (see Component 3).
- `updated_at`: bumped at every write; used as a fallback staleness signal when the
  reader can't trust the PID (host mismatch).
- `current_file`: basename only (no path) — it's all the widget shows.
- `current_file_started_at`: the reader computes "elapsed on this file" from this and
  the current wall clock, so a file stuck for 19 min shows growing time with **no**
  heartbeat thread.

### Component 2 — pipeline writes (`carta/embed/status.py`)

A small `StatusWriter` class, written fresh in `carta/embed/status.py`, called from
`run_embed` in `carta/embed/pipeline.py` at the same lifecycle points the perf-log
writer already hooks:

- **run start** (after `discover_pending_files`): write `phase=running`, `pid`,
  `host`, `started_at`, `total`, zeroed counters.
- **each file start**: update `current_idx`, `current_file`,
  `current_file_started_at`, `updated_at`.
- **each file done / skip / error / timeout**: update `embedded`/`skipped`/`errors`
  and `chunks` running totals, `updated_at`.
- **run end** (incl. on exception): write `phase=done` (or `failed`),
  `finished_at`, clear `current_file`.

Properties:
- **Independent of the TTY `Progress` object.** Background runs are not TTYs — that's
  exactly when this is needed — so the writer runs whenever the CLI embed runs,
  regardless of `progress`/verbose. (MCP `carta_embed` passes `progress=None`; the
  status writer is gated separately, see config flag.)
- **Best-effort, never fatal.** Any write error is swallowed with a stderr warning
  (mirrors `_write_perf_log_entry`). A failed status write must never break an embed.
- **Config-gated.** New config key `embed.status_file` (bool, default `true`). When
  false (or in tests), the writer is a no-op and no file is created. MCP server may
  opt in or out via the same flag.
- **Cleanup:** the file is left on disk after a run (carries the finish-flash state +
  last-run summary); it is overwritten at the next run start. A stale `running` file
  from a crash is handled by the reader, not by cleanup.

### Component 3 — `carta statusline` (the segment printer)

New CLI subcommand `carta statusline`. Reads Claude Code's session JSON from **stdin**
(the existing user script already has `$input`; it's passed via
`carta statusline <<<"$input"`). Extracts cwd
(`.workspace.current_dir // .cwd`), walks up to find the nearest `.carta/`, reads
`embed-status.json`, applies the staleness rules below, and prints the segment to
stdout.

Hard requirements:
- **Fast (<30 ms).** Pure file read + JSON parse. No Qdrant, no Ollama, no network,
  no config-driven service calls. Runs on every status-line render.
- **Never throws.** Any exception (missing file, corrupt JSON, missing `.carta`, bad
  stdin) → print nothing, exit 0. The status line must never show a stack trace or
  break the user's line.

Staleness / state resolution:
1. No file, or unreadable → **idle** (print nothing).
2. `phase=running`:
   - same `host` and `pid` alive (`os.kill(pid, 0)`) → **running** → render progress.
   - same `host` and `pid` dead → **crashed** → idle (print nothing).
   - different `host` → trust `updated_at`: within ~60s → running, else idle.
3. `phase in {done, failed}`:
   - `finished_at` within ~30s → **finish flash**.
   - older → idle.

Rendered output (ANSI; colors chosen to match the existing dim p10k style):
```
running:   ⠹ carta 24/47  EN_UM_N32WB03x.pdf  19m
finished:  ✓ carta 46 files · 2.3k chunks
failed:    ✗ carta 12/47 · 3 errors
idle/dead: (prints nothing — empty string)
```
Details:
- Spinner frame is derived from `int(now / 0.1) % len(frames)` so it animates across
  successive renders without per-process state (reuses `_SPINNER_FRAMES` from
  `carta/ui/progress.py`).
- Current filename truncated to a fixed width (e.g. 28 chars, `…` elision), reusing
  the `_truncate` helper from `carta/ui/progress.py`.
- Elapsed-on-file formatted compactly (`19m`, `4s`, `1h3m`).
- The segment never contains the leading separator; the wiring snippet owns the `│`.

### Component 4 — auto-wiring (`carta init`, `carta statusline --install/--uninstall`)

On `carta init` (and on demand via `carta statusline --install`), carta inspects
`settings.json` for a `command`-type `statusLine`:

- If it points to an existing, writable script file and our marker block is **not**
  present: with `[y/N]` confirm, copy the script to `<script>.bak`, then append an
  idempotent, marker-guarded block:
  ```bash
  # >>> carta statusline >>>
  seg=$(command -v carta >/dev/null && carta statusline <<<"$input" 2>/dev/null)
  [ -n "$seg" ] && parts="$parts │ $seg"
  # <<< carta statusline <<<
  ```
  (The snippet assumes the conventional `$input` / `parts` variables; if absent, it
  degrades to printing the segment guidance rather than guessing — see below.)
- If the marker block already exists → **no-op** (idempotent).
- If `statusLine` is missing, inline (not a script file), or unwritable → **refuse
  gracefully**: print the manual one-line snippet and instructions, change nothing.
- `--uninstall` removes the marker block and leaves the rest of the script intact.

This is the riskiest component (it edits a user file), so it is conservative by
construction: explicit confirm, backup, idempotent via markers, reversible.

> **Open implementation question for the plan:** the snippet hard-codes the `$input`
> and `parts` variable names from the user's current script. The plan must decide how
> to detect/verify those (or fall back to manual instructions) so we don't append a
> block that references variables that don't exist in an arbitrary user's script.

## Data flow (running embed)

1. Agent launches `carta embed` in the background.
2. `run_embed` writes `embed-status.json` (`phase=running`) and updates it per file.
3. Claude Code renders the status line (several times/sec); each render runs the user
   script, which calls `carta statusline <<<"$input"`.
4. `carta statusline` reads `embed-status.json`, sees `phase=running` + live PID,
   prints `⠹ carta 24/47  big.pdf  19m`.
5. Embed finishes → `phase=done`, `finished_at=now`. For ~30s the segment shows
   `✓ carta 46 files · 2.3k chunks`, then renders empty.

## Error handling

- Status writes are best-effort; failure warns to stderr and never aborts the embed.
- `carta statusline` is exception-proof; any failure → empty output, exit 0.
- Corrupt/partial status file → reader treats as idle (atomic writes make this rare).
- Crash leaves a stale `phase=running` file → reader's PID-liveness check renders it
  idle, so no phantom progress.

## Testing

- **StatusWriter** (`test_status.py`): correct JSON at each transition (start, file
  start, file done/skip/error, run end incl. exception path); atomic replace
  (no `.tmp` left behind); `embed.status_file=false` → no file written.
- **Staleness/resolution** (`test_statusline.py`): dead-PID→idle, alive→running,
  done-recent→flash, done-old→idle, failed→flash, host-mismatch→`updated_at` window,
  missing/corrupt file→idle. `os.kill` and clock mocked.
- **Segment rendering**: running/flash/failed/idle strings; long-filename truncation;
  compact elapsed formatting; never raises on malformed input; stays within perf
  budget (simple timing sanity check).
- **Wiring** (`test_statusline_install.py`): appends marker block + creates `.bak`;
  re-run is idempotent; `--uninstall` removes only the block; inline/missing/unwritable
  statusLine refused gracefully with manual instructions; confirm-declined → no change.

## Files

- **New:** `carta/embed/status.py` (StatusWriter), `carta/statusline.py` (reader +
  install/uninstall), tests `carta/tests/test_status.py`,
  `carta/tests/test_statusline.py`, `carta/tests/test_statusline_install.py`.
- **Modified:** `carta/embed/pipeline.py` (call StatusWriter in `run_embed`),
  `carta/cli.py` (`carta statusline` subcommand + `--install/--uninstall`),
  `carta/config.py` (default `embed.status_file: true`),
  `carta/install/bootstrap.py` (offer wiring during `carta init`).

## Rollout / compatibility

- Purely additive: no change to Qdrant collections, sidecar metadata, or existing
  embed behavior. Default-on status file is a new artifact in `.carta/` (add to
  `.gitignore` guidance alongside the existing `.carta` ignores).
- Works with or without the wiring step — the `carta statusline` command is usable
  standalone for users who prefer to paste it themselves.
