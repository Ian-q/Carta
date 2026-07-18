# Board conventions + cross-worktree session board

**Date:** 2026-07-18
**Status:** approved, pending implementation

## Problem

Carta's tracking has drifted from reality in two distinct ways.

**Duplicated status.** `docs/ROADMAP.md` opens by declaring "two trackers, two jobs" — itself as the
durable relational view, GitHub Projects as the live operational board. But it then carries a
Now/Next/Later table and a per-issue "why #19 and #10 are not live work" section, both of which are
*status*. Status is the board's job. The duplicate drifted: the file still lists #78 as the next
task 16 days after it shipped in PR #94, and still asks for a close-out of #10 that already
happened.

**Bookkeeping that nothing enforces.** Project #4 holds 4 items against 16 open issues. PR #91
("fix: surface query-embed failures (#79) + reconcile import status (#80)") merged 16 days ago with
no closing-issue references, so #79 and #80 are still open despite being delivered.

Separately, parallel worktrees have no coordination surface. Two stale worktrees
(`issue-78-visual-doc-generation`, `issue-89-slug-collision`) sat in the tree with zero commits —
one for work that had already shipped by another route, one never started. Nothing showed that at a
glance.

ET-embed solves both problems and the conventions are proven there. This ports the parts that fit a
solo repo with 16 open issues.

## Scope

Adopted:

- Board discipline — every open issue on Project #4, with `Area`/`Size`/`Status` set
- `blocked by` dependencies where sequencing is genuine
- The cross-worktree session board (SessionStart hook + task claims)
- A rewritten `ROADMAP.md` that holds no status

Deliberately **not** adopted:

- Start/Target date fields and the Roadmap/Gantt view — dates on a solo project would be invented,
  and inventing them is what gives a linter something to lint
- `roadmap_watchdog.py` and its weekly automation — its primary checks (date inversions, overdue
  blockers, cascade risk) are all date-derived, so without dates it has little to say

Consequence, accepted: nothing automatically catches a stale board. The counterweight is the
CLAUDE.md convention making board updates part of opening an issue. Revisit if the board drifts
again — the watchdog can be added later without reworking anything here.

Epics/sub-issues are also skipped. With 16 issues across 5 areas, epic parents would run ~1 per 4
children and mostly restate an `Area` value. The `Area` field already groups, at zero cost.

## Division of labor

This table is the invariant the whole design exists to protect.

| Fact | Owner |
|---|---|
| What is open; its status, size, area, assignee | GitHub issues + Project #4 |
| Sequencing between issues | `blocked by` dependencies |
| How subsystems relate; why approaches were taken or abandoned | `docs/ROADMAP.md` |
| What each parallel worktree is doing right now | session board (`.git/active-sessions.tsv`) |

**`ROADMAP.md` may not state per-issue status.** Any sentence in it that would become false when an
issue closes belongs on the board instead.

## Component 1 — Board backfill

Add an `Embed` option to the existing `Area` single-select. The current options (`Retrieval`,
`Vision/OCR`, `Hooks`, `Docs`, `Infra`) have no home for the embed pipeline, which is the largest
cluster of open work.

All 16 open issues onto the board:

| Issue | Area | Size |
|---|---|---|
| #103 un-truncatable chunk leaves doc partial | Embed | M |
| #99 export omits `.carta/companions/` | Embed | S |
| #98 uppercase `.PDF` classify results discarded | Embed | S |
| #97 single-source the supported-extension sets | Embed | M |
| #96 `run_embed_file` failure window | Embed | M |
| #89 induct slug ignores extension and directory | Embed | M |
| #93 doctor: reconcile `visual_done` vs point count | Infra | S |
| #80 import: status/doctor show never-embedded | Infra | S |
| #86 supersession judge slow on this host | Hooks | S |
| #83 `judge_timeout_s=5` silently no-ops | Hooks | S |
| #82 claude-md-sync duplicate headings collide | Hooks | S |
| #81 claude-md-sync section granularity | Hooks | M |
| #85 Unlimited-OCR as optional vision backend | Vision/OCR | L |
| #76 recall tests for complex table layouts | Vision/OCR | M |
| #79 query-embedding failure masked as "no results" | Retrieval | S |
| #19 grow the ET-embed eval set 62→~80q | Retrieval | L |

Dependencies, added only where sequencing is real:

- `#97 blocks #98` — the uppercase-suffix comparison is one of the duplicated extension sets #97
  single-sources
- `#97 blocks #89` — the slug fix lands on top of the same refactor

Before setting fields, verify #79 and #80 against merged PR #91 and close them if the work is
present. Do not close on the PR title alone — read the merged diff and confirm the behaviour.

## Component 2 — Session board

Three files, ported near-verbatim from ET-embed. Divergence would cost maintenance for no gain;
these scripts are proven in the repo they came from.

- **`.claude/hooks/active-board.sh`** — SessionStart hook. Records a heartbeat for the current
  worktree, then prints every worktree seen recently with its branch, live git dirty/clean state,
  claimed task, and idle time. 12-hour TTL. Always exits 0; a hook that aborts must not disrupt a
  session.
- **`tools/session-task.sh`** — `"<claim>"` to claim, `--clear` to release, no args to show. Claims
  truncate to 40 chars, tabs/newlines stripped.
- **`.claude/settings.json`** — currently `{}`, so the SessionStart wiring is a pure addition.

Design points inherited from ET-embed, each load-bearing:

- The board file lives at `<git-common-dir>/active-sessions.tsv`. `git rev-parse --git-common-dir`
  resolves to the same absolute path from every linked worktree, so all worktrees share one board;
  living inside `.git/` means it is never committed and needs no `.gitignore` entry.
- The settings command bootstraps to the **main worktree's** copy of the script via
  `git rev-parse --git-common-dir`, so every worktree runs the same script regardless of its own
  branch. Corollary: edits to the hook only take effect once they land on the branch the main
  checkout has out.
- One row per worktree, refreshed in place. A new session in a worktree **inherits** that worktree's
  existing claim — a worktree usually outlives a single session.
- Dirty/clean is recomputed at render time, so it is accurate even for rows written hours earlier.
- Awareness only. Sessions do not message each other.

This repo is Carta-dogfooded (`.carta/` is present), so the hook must stay additive and leave
Carta's own `UserPromptSubmit`/`Stop` hooks untouched — the same constraint ET-embed documents.

Claims are advisory, not locks. Nothing enforces them and a dead session can leave a stale claim.
When a claim looks wrong, confirm with the human rather than overriding it.

## Component 3 — ROADMAP.md rewrite

Remove:

- The Now/Next/Later table
- "Why #19 and #10 are not live work"
- The `Open` subgraph of the relationship mermaid (open-issue nodes are status)

Keep and refresh:

- How subsystems relate — the mermaid, reduced to shipped subsystems and durable relationships
- The development arc gantt of shipped cycles
- Design rationale that outlives any issue: why the reranker rank-prior was abandoned, why the
  recall lever is spent at 0.984, the Qdrant WAL root cause

Add a header line stating that status lives on the board, with a link.

## Component 4 — CLAUDE.md

Two sections under the existing conventions:

- **Issue tracking → board.** Every new issue goes on Project #4 with `Area` and `Size` set. Add a
  `blocked by` dependency when sequencing is genuine. Use `Closes #N` in PR bodies — #79/#80 are
  what happens otherwise.
- **Cross-worktree session board.** Read the board before choosing work; claim with
  `tools/session-task.sh "#NNN short description"` before starting; `--clear` when it lands. If the
  task is already claimed by another worktree, stop and ask rather than working it anyway or
  silently picking something else.

## Verification

- `tools/session-task.sh` round-trips: claim → show returns it → `--clear` → show returns empty
- The hook renders from the main checkout, and from a scratch worktree shows both rows
- The hook exits 0 outside a git repository
- A claim set in one worktree is visible from another
- Re-query Project #4 and confirm every issue in the backfill table is present with `Area`/`Size`
  populated, and both dependencies set. Note the board already holds #76, #19 (Todo) and #78, #10
  (Done); backfill adds the other 14, and closed items stay on the board rather than being removed —
  so assert per-issue presence, not a total count
- `ROADMAP.md` contains no per-issue status — grep for issue references and confirm each survivor is
  a durable relationship, not a state

## Risks

- **Stale claims.** A dead session leaves a claim until the 12h TTL. Advisory-by-design; the
  convention says confirm with the human.
- **Hook edits need a main-checkout landing.** Surprising the first time. Documented in CLAUDE.md.
- **No automated board enforcement.** Accepted above; watchdog remains available later.
