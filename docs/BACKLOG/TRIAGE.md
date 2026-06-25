# Documentation Backlog

Triage items sourced from doc-audit runs. Each entry maps to one or more `AUDIT-NNN` findings in [`AUDIT_REPORT.md`](../AUDIT_REPORT.md).

> **Note:** [#33](https://github.com/Ian-q/Carta/issues/33) (relocate the audit report into `docs/`) is **resolved** — the report now lives at [`docs/AUDIT_REPORT.md`](../AUDIT_REPORT.md), matching the in-repo skill + README references.

---

## Open

> From **Audit #2** (2026-06-25). Audit #1's entire set (DOC-001…DOC-010) is now Completed — see below.

### DOC-012 [doc-audit] Add `carta_focus` to the docs/README.md MCP tools table
**Source:** AUDIT-023 (new, **error**)
**Type:** doc_code_conflict
**Docs:** `docs/README.md` → `carta/mcp/server.py`
**Action:** Add a `carta_focus` row to the MCP tools table (`docs/README.md:38-43`) matching `carta/mcp/server.py:147-159` (`source`, `query=""`, `top_k`). It is a registered, shipping MCP tool (`add_tool()` at `server.py:202`) but undocumented in the nav layer. Highest-severity Audit #2 finding (the nav doc undercounts the live MCP surface).

### DOC-011 [doc-audit] Rewrite bare-slug `related:` entries to canonical repo-root paths
**Source:** AUDIT-021 (new, warning — 43 entries)
**Type:** noncanonical_related
**Docs:** `docs/superpowers/specs/*`, `docs/superpowers/plans/*`
**Action:** `related:` frontmatter stores bare slugs (e.g. `2026-04-07-skill-installation-design`) that resolve only via the Tier-3 id fallback (`carta/search/graph.py:61`), so `check_noncanonical_related` (`carta/scanner/scanner.py:194`) warns. Mechanical sweep: rewrite each to its `suggested` canonical path (every finding carries `resolves:true` + a suggested path). Side effect of the #48 frontmatter backfill. Alt: downgrade the rule to `info`.

### DOC-014 [doc-audit] Fix `config.yaml.example` excluded_paths parity (silent exclusion loss)
**Source:** AUDIT-025 (new, warning)
**Type:** doc_code_conflict
**Docs:** `carta/install/config.yaml.example` → `carta/config.py`
**Action:** Header (line 7) claims parity with DEFAULTS, but `excluded_paths` omits `.claude/worktrees/`, `build/`, `temp/` (present in `config.py:29-33`). Because `_deep_merge` replaces lists wholesale (`config.py:251-260`), copying the example silently drops those exclusions. Add the three entries, or soften the parity claim.

### DOC-013 [doc-audit] Refresh docs/README.md note about #48 frontmatter
**Source:** AUDIT-024 (new, warning)
**Type:** stale_reference
**Docs:** `docs/README.md:54-56`
**Action:** The note says frontmatter "are being added … in #48" / "until then status is in the body." #48 shipped (30/34 specs now have frontmatter). Rewrite to past tense; drop the open-issue link; note only the few newest specs may lag.

### DOC-016 [doc-audit] Backfill frontmatter on 19 newer docs; add a scaffold to stop recurrence
**Source:** AUDIT-022 (persisting from AUDIT-016 / DOC-006)
**Type:** missing_frontmatter
**Docs:** `docs/install.md`, `docs/README.md`, newer superpowers specs/plans, `docs/audits/*`, `docs/testing/*`
**Action:** Add the minimal `id`/`status`/`related`/`date` template to the 19 docs (prioritize superpowers specs/plans so they join `doc_index`). Add a bootstrap/scaffold step that injects frontmatter on doc creation.

### DOC-015 [doc-audit] Fix CLAUDE.md `VECTOR_DIM` line-number citations
**Source:** AUDIT-026 (new, info/low)
**Type:** stale_reference
**Docs:** `CLAUDE.md:57-58` → `carta/embed/embed.py`, `carta/install/bootstrap.py`
**Action:** Citations point at wrong lines (`embed.py:17`→`:25`; `bootstrap.py:12`→`:13`). Fix the line numbers, or cite symbols (`VECTOR_DIM` / `VECTOR_DIMENSIONS`) to avoid brittle drift. The 768-dim claim itself is correct.

### DOC-017 [doc-audit] Self-embed Carta's own repo (dogfooding + makes the audit's Qdrant pass real)
**Source:** AUDIT-027 (new, info)
**Type:** coverage_gap
**Docs:** repo-wide (`doc-audit-cc_doc` / `_quirk` collections empty)
**Action:** Carta's own docs are not embedded (`_doc`/`_quirk` = 0 points), so the audit's Qdrant cross-doc-conflict pass is inert and the stale-ref hook can't run over Carta's own docs. Run `carta embed` here and wire it into the dev/release workflow.

### DOC-010 [doc-audit] Whitelist or relocate homeless skill and quirk paths
**Source:** AUDIT-019 (persisting — orphaned quirk note only)
**Type:** orphaned_doc / homeless_doc
**Docs:** `docs/quirks/2026-06-11-pypi-index-lag-after-release-tagging.md`, `AUDIT_REPORT.md`
**Action:** Mostly resolved by #52 (homeless 13→1). Remaining: the pypi-lag quirk is an accepted leaf note (optionally add an inbound `related:`). `AUDIT_REPORT.md` (the last homeless doc) is now relocated to `docs/AUDIT_REPORT.md` — resolves [#33](https://github.com/Ian-q/Carta/issues/33) and clears the homeless flag.

---

## Completed

> Audit #1 backlog — all shipped via GitHub issues #43–#52 (CLOSED). Reconciled 2026-06-25.

### DOC-001 ✅ Refresh agent entry points (CLAUDE.md + AGENTS.md) — #43
Full CLI/MCP/hook tables + module map added; `carta_remember` documented. *(AUDIT-001, AUDIT-015)*

### DOC-002 ✅ Fix Carta skill docs — CLI paths and sidecar relocation — #44
Invocation standardized to `carta` / `python -m carta`; sidecar paths corrected to `.carta/sidecars/`. *(AUDIT-002, AUDIT-003)*

### DOC-003 ✅ Align install and config docs with code defaults — #45
`config.yaml.example` schema, `qwen3-vl:8b`, ColPali `-hf` IDs, `glm-ocr` prereq, `candidate_pool: 30` aligned. *(AUDIT-004, AUDIT-005, AUDIT-012, AUDIT-013)*

### DOC-004 ✅ Regenerate or retire stale GSD/planning codebase docs — #46
GSD planning machinery retired; CLAUDE.md no longer embeds frozen GSD blocks; `_quirk`→`_notes`. *(AUDIT-006, AUDIT-007, AUDIT-014)*

### DOC-005 ✅ Add docs navigation layer for AI agents — #47
`docs/README.md` created as the agent entry/nav layer. *(AUDIT-010, AUDIT-011, AUDIT-020)*

### DOC-006 ✅ Add frontmatter to superpowers spec/plan corpus — #48
Bulk backfill done (`with_id` 0→56). Residual 19 newer docs carried forward as **DOC-016**. *(AUDIT-016)*

### DOC-007 ✅ Scanner: exclude worktrees and build artifacts — #49
`.claude/worktrees/`, `build/`, `temp/`, `.planning/` excluded; scan issues 173→64. *(AUDIT-018)*

### DOC-008 ✅ Document audit command disambiguation — #50
"Which audit?" table added (`carta scan` / `/doc-audit` vs `carta audit` / `carta doctor` vs `carta eval`). *(AUDIT-008)*

### DOC-009 ✅ Resolve HOOK-05 judge timeout semantics — #51
Decision recorded: fail-open = inject; docs/code/tests aligned. *(AUDIT-009)*

### DOC-010 ↩ Whitelist or relocate homeless skill and quirk paths — #52
Homeless 13→1 (skill paths whitelisted, `build/` excluded). Residual orphan/homeless carried forward in **Open** above. *(AUDIT-017, AUDIT-019)*
