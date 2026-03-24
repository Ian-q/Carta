# /doc-audit Skill

Audit repository documentation for structural and semantic issues, assign stable AUDIT-NNN IDs, and update `AUDIT_REPORT.md` and `docs/BACKLOG/TRIAGE.md`.

---

## Step 1: Run or refresh the structural scanner

Check if `.carta/scan-results.json` exists and was written less than 1 hour ago (compare `run_at` timestamp to current time).

- If fresh: read it directly.
- If stale or missing: run the scanner first:

```bash
python .carta/carta/cli.py scan
```

Read `scan-results.json` and extract:
- `issues` list (structural findings)
- `changed_since_last_audit` list (docs to semantically check)
- `stats`

---

## Step 2: Load previous audit state

Check if `AUDIT_REPORT.md` exists.

- If it exists: read it and extract:
  - The `<!-- audit_counter: N -->` value (increment by 1 for this run).
  - All existing AUDIT-NNN entries and their current status.
- If it does not exist: start audit counter at 1, previous issues list is empty.

---

## Step 3: Spawn parallel subagents

### Structural Agent

For each entry in `issues` from the scan results:
1. Map the issue to any existing AUDIT-NNN with the same doc path + type. If matched, carry the existing ID. If new, assign the next AUDIT-NNN.
2. Classify status: `new` (no prior entry), `persisting` (was active last audit), `resolved` (was active, no longer in issues), `needs-input` (ambiguous — flag for human review).
3. Produce a finding record with: id, status, type, doc path, detail, suggested action.

### Semantic Agent

For each doc in `changed_since_last_audit`:
1. Read the document.
2. Check for: contradictions with other docs, stale version references, missing required sections, terminology drift.
3. Produce finding records in the same format as the Structural Agent.

### Qdrant Agent (optional)

If Qdrant is reachable (run `python .carta/carta/cli.py search "test" 2>&1 | head -1` — no error means reachable):
1. For each changed doc, run a semantic similarity search to find potentially conflicting or duplicate content in the knowledge graph.
2. Surface any high-similarity (>0.92) matches that are not the same document as additional findings.

If Qdrant is unreachable, skip this agent and note "Qdrant agent skipped — collection unreachable" in the report.

---

## Step 4: Assign AUDIT IDs and merge findings

1. Merge all findings from all agents.
2. De-duplicate by (doc_path, type) key.
3. Assign or carry AUDIT-NNN IDs in ascending order (never reuse an ID for a different issue).
4. Mark any previously active issues that have no corresponding finding as `resolved`.

---

## Step 5: Write AUDIT_REPORT.md

Write `AUDIT_REPORT.md` at the repo root with this structure:

```
# Doc Audit Report

<!-- audit_counter: <N> -->
<!-- Last run: <YYYY-MM-DD> | Audit #<N> | Issues: <X> active, <Y> resolved, <Z> archived -->

## Active Issues

[active + persisting + needs-input issues, newest first]

## Resolved (this audit)

[issues resolved this run]

## Archive

<!-- Issues resolved for 2+ audits. Kept for history. -->

[archived issues]
```

Each issue block format:

```
### AUDIT-NNN <emoji> <status> — <context>
**Type:** <type>
**Doc:** `path/to/doc.md` (or **Docs:** for conflicts)
**Detail:** <detail text>
**Action:** <what to do>
**Backlog:** [DOC-NNN](docs/BACKLOG/TRIAGE.md#DOC-NNN)  ← only if linked
```

Emoji key: 🆕 new | ⚠️ persisting | 🔵 needs-input | ✅ resolved

---

## Step 6: Append new items to TRIAGE.md

For each issue that is `new` or newly `needs-input` AND does not already have a `**Backlog:**` entry:

1. Read `docs/BACKLOG/TRIAGE.md`, find the highest `### DOC-(\d+)` number. Start from DOC-001 if none.
2. Append a new entry:

```markdown
### DOC-NNN [doc-audit] <short description>
**Source:** AUDIT-NNN (<status> since audit #N)
**Type:** <type>
**Docs:** `doc` → `related_doc` (or just `doc` for non-conflict issues)
**Action:** <specific action for a developer to take>
```

3. Add `**Backlog:** [DOC-NNN](docs/BACKLOG/TRIAGE.md#DOC-NNN)` to the matching issue block in `AUDIT_REPORT.md`.

Issues flagged `needs-input` get a `[needs-input]` note appended after the title.

---

## Completion

Report a summary:

> "Audit #N complete. X new issues, Y persisting, Z resolved. AUDIT_REPORT.md updated. N items appended to TRIAGE.md."
