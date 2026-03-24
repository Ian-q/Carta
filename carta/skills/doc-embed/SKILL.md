---
name: doc-embed
description: Embed documents into the Carta knowledge graph and enrich newly embedded files with spec summaries.
---

# /doc-embed Skill

Embed documents into the Carta knowledge graph, enrich newly embedded files with spec summaries, and update sidecar `.embed-meta.yaml` files.

---

## Step 1: Run or refresh the structural scanner

Check if `.carta/scan-results.json` exists and was written less than 1 hour ago (compare `run_at` timestamp to current time).

- If fresh: read it directly.
- If stale or missing: run the scanner first:

```bash
python .carta/carta/cli.py scan
```

Identify any files flagged `embed_induction_needed` in the scan results — these require a sidecar `.embed-meta.yaml` before embedding.

---

## Step 2: Induction — handle un-inducted files

For each file flagged `embed_induction_needed`:

1. Check if a `.embed-meta.yaml` sidecar already exists alongside the file.
   - If yes: read it and confirm it has at minimum `slug`, `doc_type`, and `status` fields.
   - If no: create a minimal sidecar with:
     ```yaml
     slug: "<kebab-case-filename>"
     doc_type: "<inferred from parent directory>"
     status: pending
     indexed_at: null
     chunk_count: null
     collection: "<project_name>_doc"
     spec_summary: null
     notes: ""
     ```
2. Report how many files were inducted and how many already had sidecars.

---

## Step 3: Run the Python embed pipeline

Run the embed command:

```bash
python .carta/carta/cli.py embed
```

Wait for the command to complete. Capture stdout/stderr. The command will report which files were newly embedded and which were skipped (already up to date).

Parse the output to build a list of newly embedded files.

---

## Step 4: Spawn parallel agents

### Enrichment Agent

For each newly embedded document:

1. Read the `.embed-meta.yaml` sidecar for the file.
2. Read the source document (for PDFs use the Read tool, first 20 pages) to extract key information.
3. Produce a `spec_summary` block appropriate to the document type:
   - For datasheets/component specs: include `absolute_max`, `critical_design_rules`, `pin_assignments`, `key_sections` (all with page citations).
   - For manuals/guides: include `overview`, `key_procedures`, `warnings`, `index_sections`.
   - For general docs: include `summary`, `key_topics`, `related_docs`.
4. Write the `spec_summary` back to the `.embed-meta.yaml` sidecar.
5. Update sidecar `status` to `embedded`.
6. For each path listed in `used_in` in the sidecar: read the referenced document and check for potential spec conflicts. If found, append a TRIAGE entry to `docs/BACKLOG/TRIAGE.md`:

```markdown
### DOC-NNN [doc-embed] <component>: <short description>
**Source:** doc-embed enrichment (embedded <date>)
**Type:** spec_conflict
**Docs:** `<source_doc_path>` → `<used_in_path>`
**Action:** <specific verification needed, with page citation>
```

---

## Step 5: Update TRIAGE.md

If any TRIAGE entries were written in Step 4, confirm the file was updated and report the count.

---

## Completion

Report a summary:

> "Embed complete. N files newly embedded, M files enriched, K spec conflicts written to TRIAGE.md."
