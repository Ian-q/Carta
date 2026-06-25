# Doc Audit Report

<!-- audit_counter: 2 -->
<!-- Last run: 2026-06-25 | Audit #2 | Issues: 8 active, 19 resolved, 0 archived -->
<!-- Qdrant agent: ran but INCONCLUSIVE — doc-audit-cc corpus is empty (_doc/_quirk = 0 points); own docs not embedded. See AUDIT-027. -->
<!-- Structural scan: 64 issues, all in main repo (down from Audit #1's 173 raw / 62 main-repo; worktree+build+temp+.planning now excluded). -->
<!-- Drift pass: 9 live-surface doc clusters audited against code; 4 findings confirmed by adversarial re-verification, 0 rejected. -->

## Active Issues

### AUDIT-023 🆕 new — `docs/README.md` MCP tools table omits the shipped `carta_focus` tool
**Type:** doc_code_conflict
**Doc:** `docs/README.md:38-43` → `carta/mcp/server.py`
**Detail:** The MCP tools table lists only 4 Claude-initiated tools (`carta_search`, `carta_embed`, `carta_scan`, `carta_remember`) and omits `carta_focus`, which ships and is callable: defined at `carta/mcp/server.py:147`, registered via `mcp_server.add_tool(carta_focus)` at `server.py:202` (deliberate `add_tool()` rather than the decorator — see `server.py:196-201`). The README's own caveat string references `carta_focus` (`server.py:65`), and `CLAUDE.md:163` / `AGENTS.md` both enumerate it. The nav-layer doc undercounts the shipped MCP surface by one tool.
**Action:** Add a `carta_focus` row to the MCP tools table in `docs/README.md` matching `server.py:147-159` (`source`, `query=""`, `top_k`).
**Backlog:** [DOC-012](BACKLOG/TRIAGE.md#DOC-012)

### AUDIT-021 🆕 new — 43 `related:` frontmatter entries use bare slugs, not canonical paths
**Type:** noncanonical_related
**Docs:** `docs/superpowers/specs/*`, `docs/superpowers/plans/*` (43 entries)
**Detail:** NOT a broken-link problem — every entry resolves. It is a path-format issue introduced by the #48 frontmatter backfill: `related:` values store bare slugs (e.g. `2026-04-07-skill-installation-design`) instead of canonical repo-root POSIX paths. `check_noncanonical_related` (`carta/scanner/scanner.py:194`) only treats `(repo_root / entry)` as canonical when it exists as a literal path; these resolve via the Tier-3 id/stem fallback in `resolve_entry` (`carta/search/graph.py:61`), so they warn. Mostly plan↔spec cross-links; a few spec→spec cross-folder id resolutions.
**Action:** Mechanical sweep — rewrite each bare slug to its `suggested` canonical path (every finding carries `resolves:true` + a suggested path). Alternatively downgrade `noncanonical_related` to `info` (resolution already works), but docs keep drifting.
**Backlog:** [DOC-011](BACKLOG/TRIAGE.md#DOC-011)

### AUDIT-025 🆕 new — `config.yaml.example` claims DEFAULTS parity but drops 3 `excluded_paths`
**Type:** doc_code_conflict
**Doc:** `carta/install/config.yaml.example` → `carta/config.py`
**Detail:** Header line 7 states "Values below match the code DEFAULTS," but `excluded_paths` (lines 29-37) lists 7 entries while `carta/config.py:29-33` DEFAULTS lists 10 — missing `.claude/worktrees/`, `build/`, and `temp/` (the very exclusions #49 added). Because `_deep_merge` (`config.py:251-260`) replaces list values wholesale rather than unioning, a user who copies the example's 7-entry list **silently loses scan exclusion** for those three paths.
**Action:** Add `.claude/worktrees/`, `build/`, `temp/` to the example's `excluded_paths`, or soften the line-7 parity claim to "illustrative."
**Backlog:** [DOC-014](BACKLOG/TRIAGE.md#DOC-014)

### AUDIT-024 🆕 new — `docs/README.md` describes #48 frontmatter work as still pending
**Type:** stale_reference
**Doc:** `docs/README.md:54-56`
**Detail:** The note says per-doc `status`/`id`/`related` frontmatter "are being added … in #48" and "Until then, a spec's current status is noted in its own body." #48 shipped (commit `1e69de3`, issue CLOSED); 30/34 specs now carry frontmatter (29 `status: shipped`). Only the 4 newest specs lag, so the blanket "until then" framing is stale.
**Action:** Rewrite to reflect #48 shipped; drop the open-issue link and "being added/until then" future framing; note only the few newest specs may use body-only status.
**Backlog:** [DOC-013](BACKLOG/TRIAGE.md#DOC-013)

### AUDIT-022 ⚠️ persisting — 19 newer docs still lack YAML frontmatter
**Type:** missing_frontmatter
**Docs:** `docs/install.md`, `docs/README.md`, `docs/BACKLOG/TRIAGE.md`, newer superpowers specs/plans (`2026-06-13-reranker-rank-prior-design`, `2026-06-17-search-result-dedup`, `2026-06-18-carta-focus`, `2026-06-20-ocr-trust-handling`), `docs/audits/*`, `docs/testing/*`
**Detail:** Shrunken remnant of AUDIT-016 (was 48; #48 backfilled the bulk → `with_id` now 56/76). Recurs because newer docs are authored without the template; docs without an `id:` are absent from `doc_index`, weakening cross-reference/stale detection.
**Action:** Add the minimal template (`id`, `status`, `related`, `date`) to the 19 docs, prioritizing superpowers specs/plans. Consider a scaffold/bootstrap step that injects frontmatter on doc creation to stop the recurrence.
**Backlog:** [DOC-016](BACKLOG/TRIAGE.md#DOC-016)

### AUDIT-026 🆕 new — CLAUDE.md `:line` citations for `VECTOR_DIM` are off by a few lines
**Type:** stale_reference
**Doc:** `CLAUDE.md:57-58` → `carta/embed/embed.py`, `carta/install/bootstrap.py`
**Detail:** The 768-dim fact is correct, but the citations point at the wrong lines: `embed.py:17` is a `SparseVector` import (`VECTOR_DIM = 768` is at `embed.py:25`); `bootstrap.py:12` is blank (`VECTOR_DIMENSIONS = {...}` is at `bootstrap.py:13`). Brittle line-number citations have drifted.
**Action:** Update to `embed.py:25` / `bootstrap.py:13`, or cite the symbol names (`VECTOR_DIM` / `VECTOR_DIMENSIONS`) instead of line numbers.
**Backlog:** [DOC-015](BACKLOG/TRIAGE.md#DOC-015)

### AUDIT-027 🆕 new — Carta does not dogfood: its own repo corpus is unembedded
**Type:** coverage_gap
**Docs:** repo-wide (`doc-audit-cc_doc` / `doc-audit-cc_quirk` Qdrant collections = 0 points)
**Detail:** The Qdrant semantic-conflict pass was inconclusive because this repo's own docs are not embedded — `_doc`/`_quirk` hold 0 points, `_notes` holds 1. Carta cannot run its own search/stale-ref/cross-doc-conflict capabilities over its own documentation, and the audit's Qdrant agent had nothing to compare. Self-embedding would (a) make the audit's semantic pass real, (b) let the stale-ref hook operate on Carta's own docs, (c) be genuine dogfooding.
**Action:** Run `carta embed` (and `carta scan` first) in this repo; wire it into the dev/release workflow so the corpus stays current. Then re-run the audit's Qdrant probes for real cross-doc coverage.
**Backlog:** [DOC-017](BACKLOG/TRIAGE.md#DOC-017)

### AUDIT-019 ⚠️ persisting — Orphaned quirk note (accepted leaf)
**Type:** orphaned_doc
**Doc:** `docs/quirks/2026-06-11-pypi-index-lag-after-release-tagging.md`
**Detail:** No inbound `related:` links, no folder siblings (`scanner.py:493`). Unchanged from Audit #1. Quirk/note docs are leaf documents by design — intentional, not a defect.
**Action:** Accept as intentional, or optionally add a `related:` edge from `docs/install.md` / README troubleshooting. Lowest priority.
**Backlog:** [DOC-010](BACKLOG/TRIAGE.md#DOC-010)

## Resolved (this audit)

All 19 active findings from Audit #1 are resolved — they map 1:1 to backlog clusters DOC-001…DOC-010, shipped via GitHub issues **#43–#52 (all CLOSED)**. Structural metrics confirm: homeless 112→1, `with_id` 0→56, frontmatter 8→57, total scan issues 173→64.

- **AUDIT-001** ✅ — CLAUDE.md/AGENTS.md now carry full CLI/MCP/hook tables + module map (#43)
- **AUDIT-002** ✅ — sidecar path corrected to `.carta/sidecars/<rel>.embed-meta.yaml` (#44)
- **AUDIT-003** ✅ — skills no longer invoke non-existent `.carta/carta/cli.py` (#44)
- **AUDIT-004** ✅ — `config.yaml.example` schema matches DEFAULTS; `deep_scan`/`similarity_threshold`/`phi3.5` stubs gone (#45)
- **AUDIT-005** ✅ — vision model aligned to `qwen3-vl:8b` across README/install/preflight (#45)
- **AUDIT-006** ✅ — GSD planning machinery retired; CLAUDE.md no longer embeds frozen GSD blocks (#46)
- **AUDIT-007** ✅ — `_quirk`→`_notes` collection naming corrected (#46)
- **AUDIT-008** ✅ — "Which audit?" disambiguation added to README/CLAUDE.md (#50)
- **AUDIT-009** ✅ — HOOK-05 judge-timeout decided: fail-open = inject, canonized; needs-input closed (#51)
- **AUDIT-010** ✅ — `docs/README.md` agent navigation layer created (#47)
- **AUDIT-011** ✅ — `docs/BACKLOG/TRIAGE.md` exists; README issue-lifecycle link resolves (#47)
- **AUDIT-012** ✅ — README ColPali model ID `colqwen2-v1.0-hf` + `candidate_pool: 30` aligned (#45)
- **AUDIT-013** ✅ — `ollama pull glm-ocr` added to install prerequisites (#45)
- **AUDIT-014** ✅ — `.planning/PROJECT.md` stale requirements no longer in scan scope (`.planning/` excluded); GSD retired (#46)
- **AUDIT-015** ✅ — `carta_remember` documented in MCP surface (#43)
- **AUDIT-016** ✅ — superpowers frontmatter backfilled (#48); residual 19 newer docs tracked as AUDIT-022
- **AUDIT-017** ✅ — homeless docs 13→1; skill paths whitelisted, `build/` excluded (#52)
- **AUDIT-018** ✅ — scanner noise eliminated; `.claude/worktrees/`, `build/`, `temp/`, `.planning/` excluded (#49)
- **AUDIT-020** ✅ — audit process exists and runs (this is Audit #2)

## Archive

<!-- Issues resolved for 2+ audits. Kept for history. -->

*(empty — Audit #1's resolved set moves here at Audit #3)*
