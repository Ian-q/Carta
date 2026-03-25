---
status: awaiting_human_verify
trigger: "carta embed has no concurrency lock, no pre-flight service check, and no progress output — causing machine crashes and undiagnosable hangs"
created: 2026-03-24T00:00:00Z
updated: 2026-03-24T00:00:00Z
---

## Current Focus

hypothesis: Three discrete bugs in pipeline.py and cli.py — confirmed by direct code inspection
test: Read pipeline.py and cli.py in full
expecting: Confirm absence of lock, pre-flight check, and progress output
next_action: Apply all three fixes to pipeline.py and cli.py

## Symptoms

expected:
- Only one `carta embed` process runs at a time; concurrent launches are rejected with a clear message
- `carta embed` fails fast with a clear error if Qdrant/Docker is not reachable
- `carta embed` prints per-file progress so users (and agents) can see what's happening

actual:
- Multiple concurrent `carta embed` invocations all run simultaneously; 5-6 parallel runs exhausted ~180GB RAM and crashed the host machine
- When Qdrant is unreachable (Docker not running), `carta embed` hangs indefinitely with zero output (120s, 180s, 600s timeouts all expired)
- `carta embed` prints nothing until the entire pipeline finishes, then prints one line — no way to distinguish a hang from slow progress

errors:
- Machine OOM crash from parallel embeds
- Silent hang (no error, no output) when Qdrant is down

reproduction:
- Launch `carta embed` multiple times in rapid succession (e.g. from parallel agent sub-tasks)
- Stop Docker Desktop, then run `carta embed`

timeline: Discovered in v0.1.10 field test on petsense repo (2026-03-24). Machine crashed during install agent test due to parallel sub-agents each launching embed independently.

## Eliminated

(none — bugs confirmed by direct inspection, not by hypothesis elimination)

## Evidence

- timestamp: 2026-03-24T00:00:00Z
  checked: carta/embed/pipeline.py cmd_embed() and run_embed()
  found: No lock file mechanism anywhere. No pre-flight Qdrant check at cmd_embed level. run_embed() prints nothing during processing — summary only returned, printed by cmd_embed after full completion.
  implication: All three bugs (FT-5, FT-6, FT-7) confirmed present.

- timestamp: 2026-03-24T00:00:00Z
  checked: carta/embed/pipeline.py run_embed() Qdrant check
  found: run_embed() does have a try/except around QdrantClient(timeout=5) + get_collections(), but it only appends to errors and returns — it does NOT print anything. User sees zero output while waiting for the timeout to expire.
  implication: FT-6 fix should print a clear error message immediately and exit, not silently return. The cmd_embed caller prints errors only at the end, so user sees nothing during the 5s wait.

- timestamp: 2026-03-24T00:00:00Z
  checked: carta/cli.py cmd_embed()
  found: Lock file must be managed here (before run_embed is called) so it covers the entire embedding process. cfg_path.parent is the .carta directory — lock path should be cfg_path.parent / "embed.lock".
  implication: Lock logic belongs in cmd_embed, not inside run_embed.

## Resolution

root_cause: |
  FT-5: cmd_embed() has no lock file guard. Multiple invocations run fully in parallel.
  FT-6: run_embed() silently appends a Qdrant error and returns; cmd_embed prints nothing until after the call returns (5s timeout already spent with zero output visible to user). Also, QdrantClient() itself may block before get_collections() is reached.
  FT-7: run_embed() emits no output during processing; only a summary dict is returned. cmd_embed prints one line after completion.

fix: |
  FT-5: Add lock file in cmd_embed using .carta/embed.lock. Read PID from lock if exists, print message and sys.exit(1). Write own PID to lock, register atexit + signal handlers to remove it.
  FT-6: Print an immediate error message when Qdrant is unreachable and exit non-zero (not silently return).
  FT-7: Add print(flush=True) progress statements in run_embed: startup banner, per-file start, per-file completion with chunk count and timing.

verification: Applied; regression test confirms behavior.
files_changed:
  - carta/embed/pipeline.py
  - carta/cli.py
