# Carta v0.1.10 Install Test Findings
**Test repo:** petsense
**Date:** 2026-03-24
**Tester:** Claude Code (automated walkthrough of install-test-guide.md)

---

## Bugs

### BUG-001 — `carta embed` has no concurrency lock (CRASH RISK)
**Severity:** High
**Step:** 7 (doc-embed)
**Description:** `carta embed` has no lock file or mutex. If multiple invocations start simultaneously (e.g., agent retries, parallel terminals), they all run concurrently — each loading PDFs into memory and calling Ollama. During testing, the command was launched 5-6 times in rapid succession (due to context-mode hook sending bash commands to background with no visible output, causing retry attempts). Result: ~5–6 concurrent embed processes + Ollama inference calls exhausted ~180GB of RAM and crashed the host machine.
**Fix:** Add a lock file (e.g., `.carta/embed.lock`) at the start of the embed pipeline. If the lock exists, print "carta embed is already running (PID: X). Exiting." and exit non-zero. Remove the lock on exit (use a trap for cleanup).

---

### BUG-002 — `pipx upgrade carta-cc` stops at 0.1.9, does not reach 0.1.10
**Severity:** Medium
**Step:** 1 (install)
**Description:** Running `pipx upgrade carta-cc` after 0.1.7 was installed upgraded to 0.1.9, not 0.1.10 (the latest on PyPI). Confirmed with `pip index versions carta-cc` that 0.1.10 was available. Required `pipx install carta-cc==0.1.10 --force` to get the correct version.
**Repro:** Install carta-cc 0.1.7, then run `pipx upgrade carta-cc`. Observe it lands on 0.1.9.
**Fix:** Investigate whether 0.1.10 package metadata or classifiers are causing pipx to skip it (e.g., missing `python_requires`, yanked flag, or pre-release marker misdetection).

---

### BUG-003 — Skills load from old cached version (0.1.6), not newly installed version (0.1.10)
**Severity:** Medium
**Step:** 5 (doc-audit), 7 (doc-embed)
**Description:** After `pipx install carta-cc==0.1.10 --force` and `carta init` (which reported "Registered 4 Carta skill(s) in global plugin cache (v0.1.10)"), both `/doc-audit` and `/doc-embed` loaded from `/Users/ian/.claude/plugins/cache/carta-cc/carta-cc/0.1.6/skills/`. The 0.1.6 skill files were served instead of 0.1.10.
**Impact:** Users running an upgrade may silently use stale skill logic without knowing it.
**Fix:** `carta init` should verify that the skill path written to `installed_plugins.json` matches the installed version, and warn or overwrite if a stale version is cached. Consider cleaning up old version directories during `init`.

---

### BUG-004 — `carta init` fires a false PATH warning
**Severity:** Low
**Step:** 2 (carta init)
**Description:** `carta init` printed:
> "Warning: 'carta' found on PATH at /Users/ian/.local/bin/carta does not match the running interpreter."
But `/Users/ian/.local/bin/carta` IS the correct carta-cc binary (installed via pipx). The warning appears to be comparing the running Python interpreter (pipx venv) with the PATH binary and flagging a mismatch, even when there is no real conflict. The real PATH conflict detection (PlatformIO at `.platformio/penv/bin/carta`) was correctly identified at install time.
**Fix:** Tighten the PATH conflict check to only warn when the resolved `carta` binary points to a *known-bad* path (e.g., `.platformio`), not whenever the venv Python and the resolved binary differ.

---

### BUG-005 — `carta embed` hangs indefinitely with zero output
**Severity:** High
**Step:** 7 (doc-embed)
**Description:** `carta embed` hangs with no stdout or stderr — not even a startup message. The command never returns. Confirmed via 120s, 180s, and 600s timeouts all expiring with zero output. Isolated testing confirmed all individual components work: imports, Qdrant connection, fitz PDF extraction, rglob, and Ollama embedding calls all complete in under 1 second each in isolation. The hang only manifests when running the full `carta embed` command as a subprocess, suggesting an environmental issue (possible interaction with the pipx venv, macOS sandbox, or a blocking call in the combined execution path).
**Note:** Estimated embed time is only ~45s for the two datasheets (72 + ~26 chunks × 0.46s/call), so this isn't a slowness issue — it's a genuine hang.
**Fix needed:** Add flush=True progress prints at each stage of `run_embed` so the hang location can be identified. Also: investigate whether the QdrantClient grpc channel initialization blocks indefinitely under certain conditions (macOS + pipx venv).

---

### BUG-006 — `carta embed` produces zero progress output
**Severity:** Medium
**Step:** 7 (doc-embed)
**Description:** `run_embed` (and `cmd_embed` in cli.py) prints nothing until the entire pipeline completes — then prints one line. For large repos with many PDFs, the user sees a blank terminal for potentially many minutes with no way to tell if the command is working or hung. This also made it impossible to diagnose BUG-005 (the hang), since there was no output to indicate which stage was blocking.
**Fix:** Add per-file progress lines to `run_embed` and per-chunk progress (or at least per-file chunk count) to `upsert_chunks`. E.g.:
```
Embedding docs/reference/datasheets/EN_DS_N32WB03x.pdf (72 chunks)...
  ✓ EN_DS_N32WB03x.pdf — 72 chunks embedded in 34s
```

---

## Issues / Friction

### ISSUE-001 — Agent pause scope is too broad (Steps 3–4 don't require skills)
**Severity:** Low
**Step:** 2 (agent pause note)
**Description:** The `⚠️ AGENT PAUSE` at the end of Step 2 says "Do not continue with Steps 3–8 in this session." But Steps 3 (config review) and 4 (`carta scan`) require no skills and work fine in the same session. Only Steps 5–8 need the newly registered skills.
**Fix:** Change pause scope to "Do not continue with Steps 5–8 in this session."

### ISSUE-002 — PDF datasheet filename is a hash (C5848F1CE024F49390770C487A10F08A.pdf)
**Severity:** Low
**Step:** 7 (doc-embed)
**Description:** One of the datasheets is named with an opaque MD5 hash. Not a carta bug, but worth noting as a UX finding: the `embed_induction_needed` scan warning showed this hash filename, making it unclear what component it covers. Added a note in the sidecar that it's likely the SHT40 datasheet.
**Suggestion:** Rename to `SHT40_datasheet.pdf` (or whatever it is) so the scan output and sidecar slug are human-readable.

### ISSUE-003 — 20 `missing_frontmatter` TRIAGE entries is very noisy on a fresh repo
**Severity:** Low
**Step:** 5 (doc-audit)
**Description:** On a brand-new repo with no Carta frontmatter, the `/doc-audit` skill creates one TRIAGE entry per doc (DOC-005 through DOC-024 for 20 markdown files). This floods TRIAGE.md on first run, making it hard to spot the more actionable issues (homeless_doc, embed_induction_needed).
**Suggestion:** Consider grouping `missing_frontmatter` issues as a single TRIAGE entry with a doc list, or marking them lower priority / bulk-actionable.

---

## What Worked Well

- Qdrant and Ollama readiness checks in `carta init` output are clear and include the URL (`Qdrant ready at http://localhost:6333`) — better than the guide's expected output which just showed "Qdrant ready."
- Hook scripts are clean stubs with correct permissions (`-rwxr-xr-x`) and use `git rev-parse --show-toplevel` (no hardcoded paths)
- `.gitignore` entries are correct and complete
- PlatformIO conflict was detected and printed at `pipx install` time — users get warned before `carta init`
- `carta scan` ran cleanly on first run, correct issue types detected
- Config auto-populated sensible defaults for this repo (`docs_root: docs/`, excluded `.pio/`, `.carta/`, etc.)
