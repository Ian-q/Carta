![Carta](banner.png)

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
- **Semantic audit** (Claude) — reads the scanner output and checks changed doc pairs for contradictions: version numbers, API endpoints, config values, whatever matters in your domain. Writes a rolling `docs/AUDIT_REPORT.md` with stable `AUDIT-NNN` issue IDs that persist across runs.

### 2. Embed

Ingests your reference material — PDFs, datasheets, manuals, audio transcripts — into a local [Qdrant](https://qdrant.tech) vector store via [Ollama](https://ollama.ai). Generates `spec_summary` blocks for dense documents so the audit agent can cross-reference them without re-reading 200 pages.

### 3. Search

Natural language recall over everything that's been embedded. Ask Claude what the docs say about rate limiting, authentication flows, power supply constraints, sample naming conventions — whatever's in your knowledge base — and get cited answers back.

---

## Retrieval quality

Search is **hybrid** (dense + BM25 with Reciprocal Rank Fusion) by default, with an optional
**ColPali visual layer** for image-heavy PDF pages. Measured on a real technical-docs corpus
(~160 markdown docs + 214 datasheet PDFs, local models — `nomic-embed-text` + `Qdrant/bm25`):

**Text retrieval** — markdown eval, 20 queries:

| Pipeline | recall@5 | MRR |
|---|---:|---:|
| Dense only (cosine) | 0.550 | 0.402 |
| **Hybrid (BM25 + dense, RRF)** | **0.700** | **0.546** |

On an expanded **62-query** set over the same corpus (adds datasheet, supplier, and patent
reference docs): hybrid alone scores **0.790 / 0.641**, and the LLM reranker (`qwen3.5:9b`,
candidate pool 40) lifts it to **0.871 / 0.778** — with `rerank: applied on 61/62 queries`
confirming the reranker actually ran on every scored query but one.

**Visual retrieval** — datasheet eval, 14 queries:

| Pipeline | recall@5 | MRR |
|---|---:|---:|
| Text / OCR only | 0.500 | 0.429 |
| **+ ColPali visual (two-pass)** | **0.857** | **0.589** |

The datasheet set includes 6 "visual-only" queries whose answer lives on a diagram, package
drawing, or derating curve that text search structurally can't reach — ColPali lifts those from
**0/6 to 5/6**. Text and visual hits are fused by rank (RRF), so the visual layer never crowds
out text results.

> These are one project's eval sets, not a public benchmark — they show the *delta* each layer
> adds on real technical docs, not an absolute SOTA claim.

When `search.rerank.enabled` is true, `carta eval` also prints `rerank: applied on N/M queries`
— and **fails (exit 1)** if the reranker ran on zero queries, so a silent fail-open (wrong model
name, Ollama down, reasoning-model misconfig) can never masquerade as a reranked result.

**Comparing against public benchmarks.** To position Carta against standard suites:
- **[ViDoRe](https://huggingface.co/spaces/vidore/vidore-leaderboard) v1/v2** (nDCG@5) — the visual-document-retrieval benchmark ColPali/ColQwen2 are evaluated on; the most direct check of Carta's visual layer.
- **[BEIR](https://github.com/beir-cellar/beir)** / **[MTEB retrieval](https://huggingface.co/spaces/mteb/leaderboard)** / **[RTEB](https://huggingface.co/blog/rteb)** (nDCG@10) — standard text-retrieval generalization.
- **[FreshStack](https://fresh-stack.github.io/)** — hard RAG over technical docs + code; closest in spirit to Carta's use case.

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

**Version history:** [CHANGELOG.md](CHANGELOG.md). **Install (pipx, venv, PATH):** [docs/install.md](docs/install.md).

### Claude Code plugin (recommended)

Add the Carta marketplace to your `~/.claude/settings.json`:

```json
{
  "extraKnownMarketplaces": {
    "carta-cc": {
      "source": {
        "source": "github",
        "repo": "Ian-q/Carta"
      }
    }
  }
}
```

Then install and enable the `carta-cc` plugin via `/plugins` in Claude Code. That's it — hooks and skills are registered automatically. Run `/carta-init` in any project to bootstrap Carta there.

### CLI install (pip / uvx / curl)

For use without the Claude Code plugin, or if you want the `carta` command available directly:

```bash
# One-shot (no install required)
uvx --from carta-cc carta init

# Install as a CLI tool (recommended on macOS)
pipx install carta-cc
carta init

# Install directly (may require --user or a venv on macOS/PEP 668 systems)
python3 -m pip install carta-cc
carta init

# Or via curl
curl -fsSL https://raw.githubusercontent.com/Ian-q/Carta/main/carta/install/install.sh | bash
```

See **[docs/install.md](docs/install.md)** for pipx vs venv, PATH, PlatformIO conflicts, and `--pip-args` syntax.

---

## Setup (5 minutes)

**Prerequisites:**

```bash
# 1. Qdrant — run with persistence so collections survive restarts
docker run -d -p 6333:6333 -v ~/.carta/qdrant_storage:/qdrant/storage --name qdrant qdrant/qdrant

# 2. Ollama — install from ollama.ai, then pull required models
ollama pull nomic-embed-text   # text embeddings
ollama pull qwen3.5:0.8b       # hook judge (swap for larger model if preferred)
ollama pull qwen3-vl:8b        # vision describer for PDFs with figures/diagrams (needs Ollama >= 0.12.7)
ollama pull glm-ocr            # OCR for text/tables on image-heavy PDF pages
```

Both services are optional if you only want structural audit without embedding or search. See **[docs/install.md](docs/install.md)** for the full setup walkthrough and `carta doctor` to verify your environment.

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
| `/carta-init` | Bootstrap Carta in a new project (generates `.carta/config.yaml`) |
| `/doc-audit` | Structural + semantic audit, generates `docs/AUDIT_REPORT.md` |
| `/doc-embed` | Ingest PDFs, manuals, and audio transcripts into Qdrant |
| `/doc-search` | Natural language search over the embedded knowledge base |
| `carta remember` / `carta_remember` | Save a curated project note (quirk / bug-note / helpful-note) as a repo markdown file and embed it |

---

## Which audit command?

"Audit" spans several distinct workflows, and it's easy to reach for the wrong one:

| You want to… | Use | Output |
|---|---|---|
| Find structural doc issues (stale/broken `related:`, homeless/orphaned docs) | `carta scan` (no LLM) or the `/doc-audit` skill (structural **+** semantic) | `.carta/scan-results.json` / `docs/AUDIT_REPORT.md` |
| Check embedded-data integrity (orphaned sidecars, damaged/duplicate points) | `carta audit` | JSON report |
| Diagnose the environment (Qdrant/Ollama/models) | `carta doctor` (+ `--fix`) | stdout |
| Measure retrieval quality | `carta eval` | scores |

---

## Configuration

All settings live in `.carta/config.yaml` (generated by `carta init` from the template). Key fields:

```yaml
project_name: my-project           # namespaces your Qdrant collections
qdrant_url: http://localhost:6333   # required — where Qdrant is running
docs_root: docs/
stale_threshold_days: 30
contradiction_types:
  - version numbers
  - API endpoints
  - configuration values
  # add domain-specific ones: pin numbers, CAN IDs, SQL table names, etc.
anchor_doc: CLAUDE.md              # fallback comparison anchor
modules:
  doc_embed: true                  # set false to skip embed layer
  doc_search: true                 # set false to skip search
embed:
  ollama_model: nomic-embed-text:latest
```

### Search reranking

Hybrid retrieval (dense + BM25, RRF-fused) is the default. An optional second-stage **reranker**
reorders the candidate pool for better top-k precision:

```yaml
search:
  rerank:
    enabled: true
    backend: llm            # cross-encoder | llm
    model: BAAI/bge-reranker-base   # backend=cross-encoder (fastembed, local ONNX)
    llm_model: qwen3.5:0.8b         # backend=llm: one listwise Ollama call per search
    llm_timeout_s: 20
    candidate_pool: 40      # docs fetched before reranking (default 30; ~40 suits the llm backend)
```

- **`cross-encoder`** — fastembed `bge-reranker-base`, no Ollama, fast.
- **`llm`** — sends the query + candidate excerpts to a local Ollama model in a **single** call and
  reorders by its judgment. **Fail-open:** any error/timeout falls back to the fused order.
  Reasoning models are handled (`think` is disabled so the answer lands in the reply, not the
  thinking stream), and the parser tolerates a JSON array wrapped in stray prose.

  **Model strength matters a lot.** On a real technical-docs corpus (62-query eval), a strong
  reranker (`qwen3.5:9b`) lifted recall@5 **0.790 → 0.871** / MRR 0.641 → 0.778. The small default
  (`qwen3.5:0.8b`) is fast but can *degrade* ranking on harder corpora — use it for low latency,
  and a 9b-class model when retrieval quality is the priority (at higher per-query cost).

Reranking applies to explicit searches (`carta search`, the MCP `carta_search` tool, `carta
eval`). The proactive-recall hook **never reranks** (and never loads ColPali) — it fires on
every prompt and blocks submission, so it always uses the fast fused order; its gray-zone judge
handles relevance filtering.

### Graph-aware retrieval (opt-in)

An optional pre-rerank stage walks the `related:` frontmatter graph (undirected, 1 hop) from the
top hits and promotes graph-adjacent documents into the rerank candidate pool, so a relevant doc
that ranks too deep to be seen can still surface. **Fail-open**, and **off by default**:

```yaml
search:
  graph:
    enabled: false      # opt-in
    hops: 1
    seed_count: 10      # top fused hits that seed the walk
    candidate_depth: 50 # deep-fetch size when enabled
```

The benefit is realized *through the reranker* (it never displaces the top fused hits on its own).
It was measured **neutral** on a corpus where a strong reranker already floats in-pool docs; it is
most likely to help corpora with a rich, well-linked `related:` graph and relevant docs that rank
deep. The companion `related:` resolver (id/path normalization) and the `carta scan`
`noncanonical_related` check ship regardless and feed link-graph cleanup.

### Capturing notes (quirks, bug notes, helpful notes)

The write side of session memory. When you (or Claude) learn something durable about the
project, save it:

```bash
carta remember "EZKontrol bench tests silently fail unless motor CAN is powered" \
  --type quirk --title "EZKontrol bench power" --tags can,bench
```

or from Claude via the `carta_remember` MCP tool. Notes are plain markdown files with
`doc_type` frontmatter — `quirk` → `docs/quirks/`, `bug-note`/`helpful-note` → `docs/notes/`
(configurable via `memory.quirks_dir` / `memory.notes_dir`) — embedded into
`{project}_notes` and retrieved by the same hybrid search, reranker, and proactive-recall
hook as your docs. Search output labels them: `[quirk] docs/quirks/2026-06-11-….md`.

Notes are knowledge artifacts, not tool state: git-shareable, audited by `carta scan`
(staleness, links), exported by `carta export`, and still useful if you remove Carta.
Hand-written files work too — drop a markdown file in `docs/quirks/` (or set
`doc_type: quirk` in frontmatter anywhere) and `carta embed` routes it correctly.

---

## Visual Embedding (ColPali/ColQwen2)

Carta supports multimodal embedding of visually-rich PDF pages (datasheets, register maps, timing diagrams) using [ColPali](https://github.com/illuin-tech/colpali) and [ColQwen2](https://huggingface.co/vidore/colqwen2-v1.0) late-interaction retrieval.

Instead of converting visual content to text (lossy), this pathway:
1. Embeds each PDF page as 1,024 patch vectors (128-dim) directly into Qdrant's multi-vector collection
2. Stores the raw page PNG as a sidecar payload in `.carta/visual_cache/`
3. Enables visual search that returns actual page images alongside text results

**Enable visual embedding:**

```bash
# Install with visual dependencies
pip install 'carta-cc[visual]'
```

Then set in `.carta/config.yaml`:

```yaml
embed:
  colpali_enabled: true              # force on; default is null (auto: search _visual when it exists)
  colpali_model: "vidore/colqwen2-v1.0-hf"  # HF-native variant; or vidore/colpali-v1.3-hf for lower VRAM
  colpali_device: "cpu"              # "cpu", "cuda", or "mps"
  colpali_batch_size: 1              # pages per batch (1 for CPU)
  colpali_sidecar_path: ".carta/visual_cache/"
```

**Model Selection:**

| Model | VRAM | Speed | Quality | Best For |
|-------|------|-------|---------|----------|
| `vidore/colqwen2-v1.0-hf` | ~8GB | Slow | Highest | GPU servers |
| `vidore/colpali-v1.3-hf` | ~6GB | Medium | High | Balanced GPU |
| `vidore/colSmol-500M` | ~3GB | Medium | Good | CPU workstations |
| `vidore/colSmol-256M` | ~2GB | Fast | Fair | CPU-only/laptops |

**Notes:**
- Visual embedding is additive — existing text embedding pipeline is unchanged
- Pages are classified automatically; only visually-rich pages are embedded
- Visual collections are separate: `{project_name}_visual` (multi-vector) vs `{project_name}_doc` (text)
- Search returns both text and visual results; visual hits include base64-encoded PNGs

### Scoping heavy visual models

ColPali and the OCR/VLM vision pipeline are expensive. Several config knobs let you control exactly where and how they run.

**`colpali_scoped_paths` — restrict ColPali to specific directories or globs**

By default (`colpali_scoped_paths: []`) ColPali runs on every PDF once `colpali_enabled: true`. Set a non-empty list to restrict it to the directories or file patterns that actually contain visual-rich content:

```yaml
embed:
  colpali_enabled: true
  colpali_scoped_paths:
    - "docs/reference/datasheets/"   # trailing slash = directory prefix
    - "docs/diagrams/**/*.pdf"       # ** glob = recursive match
```

Matching rules:
- Entries ending with `/` match any file whose path starts with that directory prefix.
- All other entries are glob patterns: `*` matches within a single path segment; `**` matches across segments (including zero).
- Empty list (`[]`, the default) means no restriction — all PDFs receive ColPali embedding.

Files outside the configured scopes are silently skipped for ColPali; the normal text-extraction pipeline still runs on them.

**`colpali_device: mps` — Apple Silicon acceleration**

Set `colpali_device: "mps"` on Apple Silicon Macs to run ColPali on the GPU via Metal. Falls back to `cpu` automatically if MPS is unavailable.

**`vision_routing` — OCR/VLM routing mode**

Controls which model pipeline the smart router uses for non-pure-text pages:

| Mode | Behaviour |
|------|-----------|
| `auto` (default) | Heuristic routing: STRUCTURED_TEXT→OCR, TEXT_WITH_IMAGES→VLM, FLATTENED→OCR→VLM fallback |
| `ocr` | Force every non-pure-text page through OCR only; never call the VLM |
| `vision` | Force every non-pure-text page through the VLM only; never call OCR |
| `off` | No model calls at all — every page is treated as text-only |

```yaml
embed:
  vision_routing: "ocr"   # good default when glm-ocr handles your content well
```

**`vision_call_timeout_s` — per-call timeout**

The default is 300 seconds (raised from the old 120 s hardcoded value). Dense OCR tables can take several minutes on slower hardware; raise this if you see timeout errors:

```yaml
embed:
  vision_call_timeout_s: 600   # 10 minutes for very dense pages
```

**Recommended ColQwen successor model**

`vidore/colqwen2.5-v0.2` is the current recommended ColQwen successor. It loads via the same native `ColQwen2` transformers path and generally outperforms `colqwen2-v1.0-hf` on technical documents. Swap it in without any other config changes:

```yaml
embed:
  colpali_model: "vidore/colqwen2.5-v0.2"
```

### Two-pass visual embedding

Running glm-ocr and ColPali inline during a normal `carta embed` adds minutes per image-heavy page and blocks the fast text-extraction pass. The two-pass workflow separates these concerns:

**Pass 1 — fast text:**

```bash
carta embed
```

Extracts text from all files as normal. Pages classified as `TEXT_WITH_IMAGES` or `FLATTENED` are recorded in the sidecar's `visual_pending` list instead of being processed inline. At the end of the run, Carta prints a nudge:

```
Visual queue: 42 page(s) across 18 file(s) await visual embedding. Run carta embed --visual to process them.
```

**Pass 2 — slow, resumable visual processing:**

```bash
carta embed --visual
```

Drains the visual queue. For each pending page it runs:
1. glm-ocr text extraction → ingested into the hybrid text index
2. ColPali page-image embedding → stored in the `_visual` collection

Each page is checkpointed (`visual_pending → visual_done`) as it completes, so the pass is resumable — interrupt at any time and re-run; only unfinished pages are retried.

**Typical workflow:**

```bash
carta embed              # fast text; queues image-heavy pages, prints the nudge
carta embed --visual     # slow, resumable: OCR text + ColPali for queued pages
```

**Configuration:**

```yaml
embed:
  two_pass_visual: true      # default true — set false to revert to inline visual processing
  visual_timeout_s: 3600     # per-file timeout for the --visual pass (default: 3600 s)
  colpali_enabled: null      # null = auto (search _visual when it exists); true = force; false = off
```

Once a `_visual` collection has content, `carta search` includes it **automatically** (the
default `colpali_enabled: null` is "auto") and fuses visual hits with text by rank (RRF). The
readiness check runs before the ColPali model loads, so projects with no visual content pay
nothing. Set `colpali_enabled: false` to opt out entirely.

**Requirements for `--visual`:**

The `--visual` pass requires the optional `[visual]` extra (torch + transformers for ColPali):

```bash
pip install 'carta-cc[visual]'
```

If the extra is absent, `carta embed --visual` prints install guidance and exits cleanly — the text corpus is unaffected.

> **Python version note:** torch wheels may not be available for all Python versions. If installation fails, try a Python 3.12 venv: `python3.12 -m venv .venv && source .venv/bin/activate && pip install 'carta-cc[visual]'`

**Scoping and memory:**

Use `colpali_scoped_paths` (see above) to restrict which directories receive visual treatment. To avoid memory pressure from glm-ocr and ColPali loading simultaneously, set the Ollama concurrency limit before running the visual pass:

```bash
export OLLAMA_MAX_LOADED_MODELS=1
carta embed --visual
```

---

## Corpus integrity

`carta doctor` includes a corpus-integrity section that checks your embedded knowledge base for
data-quality issues — no Ollama calls required:

- **Slug collisions** — multiple files with the same filename stem that would have silently
  overwritten each other's Qdrant points (fixed in the embed pipeline; doctor flags any legacy cases).
- **Empty-text points** — points stored with an embedding-of-empty-string (common after PDF
  extraction failures) that are unfindable in practice.
- **Chunk-count mismatches** — sidecar says N chunks but Qdrant holds a different count, indicating
  a partial embed.
- **Stuck-stale sidecars** — files whose sidecar never transitioned back to `embedded` after a
  successful re-embed.

Findings are included in `carta doctor`'s JSON output alongside the existing environment checks.

**Repairing a corpus:**

```bash
carta embed --repair
```

Purges and force re-embeds all files flagged by the integrity checks, and fixes stuck-stale sidecars
in place. The summary reports: repaired / purged / flagged `extraction_failed` / queued-for-visual /
failed.

---

## Status-line progress widget

While `carta embed` runs, it writes `.carta/embed-status.json` with live progress. The `carta statusline` command reads that file and prints a compact segment for the Claude Code status line:

```
⠹ carta 24/47  big.pdf  19m
✓ carta 47 files · 3.2k chunks
✗ carta 24/47 · 2 errors
```

**Auto-wiring during `carta init`**

If your `~/.claude/settings.json` has a `statusLine.command` pointing to a `.sh` file, `carta init` will offer to wire the segment in automatically. It writes a `.bak` backup before editing.

**Manual snippet** — add this to your status-line script, before the line that prints `$parts`:

```bash
seg=$(command -v carta >/dev/null && carta statusline <<<"$input" 2>/dev/null)
[ -n "$seg" ] && parts="$parts │ $seg"
```

**CLI wiring**

```bash
carta statusline --install    # wire into the script found in settings.json
carta statusline --uninstall  # remove the wired block
```

`.carta/embed-status.json` is regenerated each run and should be gitignored (Carta does this automatically).

---

## Sharing an embedded project

Embedding is the expensive part — vision models run over every PDF page and ColPali
produces multi-vector visual embeddings, which can take hours on a capable GPU. A
collaborator working on the **same repository** doesn't need to repeat that. Once one
machine has embedded the project, it can hand the embedded state to another in one
command on each side.

**On the machine that has embedded the project:**

```bash
carta export                       # -> ./carta-<project>-<date>.tar.gz
carta export -o ~/share/carta.tar.gz
carta export --no-visual           # skip the _visual collection (smaller bundle)
```

This snapshots the project's Qdrant collections (`_doc`, `_notes`, `_session`, and
`_visual` unless `--no-visual`) and packs them with a copy of `config.yaml`, the
`sidecars/` metadata, and a manifest into a single `.tar.gz`. The visual collection
is included by default.

**On the receiving machine** (Qdrant running, same `qdrant/qdrant` version):

```bash
carta import carta-myproject-20260608.tar.gz
carta import bundle.tar.gz --project otherproject   # restore under a different name
carta import bundle.tar.gz --force                  # overwrite existing collections
```

Import restores each collection, writes `config.yaml` and any missing sidecars into
`.carta/`, and then `carta search` works immediately — no re-embedding. Existing
sidecars are never overwritten; existing collections block the import unless
`--force` is passed.

**Notes**

- **Querying still needs Ollama** (`nomic-embed-text`) to embed the search query —
  lightweight, runs on CPU. Visual search additionally loads ColPali/ColQwen2 at
  query time, so set `colpali_device: cuda` on a machine with an NVIDIA GPU.
- **Snapshots are Qdrant-version-coupled.** Run a matching `qdrant/qdrant` version on
  both sides; import warns (but proceeds) on a version mismatch.
- Keep your repo, Qdrant storage, and Ollama models on a native Linux filesystem
  rather than a slow mount (e.g. under WSL, use `/home/...`, not `/mnt/c`).

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

Issues and PRs welcome. The scanner, embed pipeline, and skill files are all designed to be readable and hackable.

---

## License

MIT
