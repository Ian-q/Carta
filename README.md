# Carta

> *Maps, connects, and remembers your documentation.*

**Carta is a Claude Code plugin that keeps your project docs honest** — auditing for contradictions, embedding reference material into a searchable knowledge base, and surfacing the right context exactly when you need it.

---

## The problem (or: how this got built)

Fast-moving projects accumulate documentation debt quietly. You write a spec. An AI agent writes a dozen more files based on it. The spec changes. Three weeks later, four different documents describe the same API endpoint four different ways, and nobody — human or AI — knows which one is right.

This problem gets worse the more you lean on AI agents to help you work. Agents are only as good as the context they can see, and when your `docs/` folder is a fog of contradictions and stale frontmatter, you're giving your agent a map that leads off a cliff.

Carta started as a happy accident. While working through a project with a lot of PDFs, datasheets, and fast-changing markdown — the kind of repo where the hardware changes on Thursday and the docs are still describing Wednesday — we built a small structural scanner to flag stale and broken cross-references. Then we added a semantic pass. Then a vector store. Then a `/doc-search` skill so Claude could query the embedded knowledge directly.

At some point we looked at what we had and realized: this is a thing. It works. It's small, it runs locally, it requires no new services beyond what an LLM-augmented developer already has running. So we generalized it.

---

## What Carta does

Three things, tightly integrated:

### 1. Audit

A two-pass system that runs on a schedule or on demand:

- **Structural scanner** (zero LLM calls) — detects stale docs, broken `related:` links, homeless markdown files, and orphaned content. Runs fast, runs often.
- **Semantic audit** (Claude) — reads the scanner output and checks changed doc pairs for contradictions: version numbers, API endpoints, config values, whatever matters in your domain. Writes a rolling `AUDIT_REPORT.md` with stable `AUDIT-NNN` issue IDs that persist across runs.

### 2. Embed

Ingests your reference material — PDFs, datasheets, manuals, audio transcripts — into a local [Qdrant](https://qdrant.tech) vector store via [Ollama](https://ollama.ai). Generates `spec_summary` blocks for dense documents so the audit agent can cross-reference them without re-reading 200 pages.

### 3. Search

Natural language recall over everything that's been embedded. Ask Claude what the docs say about rate limiting, authentication flows, power supply constraints, sample naming conventions — whatever's in your knowledge base — and get cited answers back.

---

## Good fits

Carta shines in projects where:

- **Docs outnumber the people who maintain them.** Research repos, hardware projects, API platforms — anywhere the documentation surface area is large relative to the team.
- **AI agents are generating or editing docs.** Agents don't track contradictions between files. Carta does.
- **Reference material lives outside version control.** PDFs, datasheets, vendor manuals, meeting transcripts — Carta pulls them into the same queryable knowledge base as your markdown.
- **The project changes fast.** Embedded firmware, evolving APIs, active research — anything where a doc written last Tuesday might already be wrong by Friday.

Less useful for: simple single-repo projects with a handful of docs, or projects where the docs are already the source of truth and rarely change.

---

## Quickstart

### Option 1: Claude Code plugin (recommended)

If you have [Superpowers](https://github.com/obra/superpowers) installed:

```
/carta-init
```

That's it. Carta copies the runtime into `.carta/` in your project, installs the pre-commit hook, and generates a config from the template.

### Option 2: pip / uvx

```bash
# One-shot (no install required)
uvx carta-cc init

# Or install globally
pip install carta-cc
carta init
```

### Option 3: curl

```bash
curl -fsSL https://raw.githubusercontent.com/carta-cc/carta-cc/main/install/install.sh | bash
```

---

## Setup (5 minutes)

**Prerequisites:**

- [Qdrant](https://qdrant.tech/documentation/quick-start/) running locally (Docker: `docker run -p 6333:6333 qdrant/qdrant`)
- [Ollama](https://ollama.ai) with `nomic-embed-text` pulled: `ollama pull nomic-embed-text`

Both are optional if you only want the structural audit and semantic contradiction detection (no embedding, no search). Set `embed.enabled: false` in `.carta/config.yaml` to skip the whole embed layer.

**After init:**

1. Edit `.carta/config.yaml` — set your `project_name`, `docs_root`, and `excluded_paths`
2. Add frontmatter to a few key docs:

```yaml
---
related:
  - CLAUDE.md
  - docs/api/endpoints.md
last_reviewed: 2026-03-20
---
```

3. Run your first audit: `/doc-audit` in Claude Code (or `carta scan`)
4. Embed your reference PDFs: `/doc-embed` (drop files into `docs/reference/`)
5. Query: `/doc-search what does the docs say about authentication?`

---

## Skills

| Skill | What it does |
|-------|-------------|
| `/carta-init` | Bootstrap Carta in a new project |
| `/doc-audit` | Structural + semantic audit, generates `AUDIT_REPORT.md` |
| `/doc-embed` | Ingest PDFs, manuals, and audio transcripts into Qdrant |
| `/doc-search` | Natural language search over the embedded knowledge base |

---

## Configuration

All settings live in `.carta/config.yaml` (generated by `carta init` from the template). Key fields:

```yaml
project_name: my-project           # namespaces your Qdrant collections
docs_root: docs/
stale_threshold_days: 30
contradiction_types:
  - version numbers
  - API endpoints
  - configuration values
  # add domain-specific ones: pin numbers, CAN IDs, SQL table names, etc.
anchor_doc: CLAUDE.md              # fallback comparison anchor
embed:
  enabled: true
  ollama_model: nomic-embed-text:latest
```

---

## Issue lifecycle

Carta assigns stable `AUDIT-NNN` IDs that survive across audit runs:

```
new → persisting → needs-input → resolved → archived
```

After `needs_input_at_audit_count` consecutive audits without resolution, an issue is escalated to `needs-input` and added to `docs/BACKLOG/TRIAGE.md` as a `DOC-NNN` item. The audit report is the single source of truth — no separate state file.

---

## What Carta doesn't do

- It doesn't replace your wiki or CMS.
- It doesn't auto-fix contradictions (it surfaces them; you or your agent decides what to do).
- It doesn't require a cloud service — everything runs locally by default.
- It doesn't add much overhead to projects with simple, stable docs.

---

## Contributing

Issues and PRs welcome. The scanner, embed pipeline, and skill files are all designed to be readable and hackable. See `docs/superpowers/specs/` for the full design spec.

---

## License

MIT
