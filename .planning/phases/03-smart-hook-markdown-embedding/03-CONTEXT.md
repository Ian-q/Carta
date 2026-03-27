# Phase 3: Smart Hook + Markdown Embedding - Context

**Gathered:** 2026-03-26
**Status:** Ready for planning

<domain>
## Phase Boundary

Deliver automatic context injection on UserPromptSubmit — relevant embedded documentation surfaces in Claude's session without manual recall, with a noise gate to prevent context pollution. Extend the embed pipeline to handle `.md` files alongside PDFs.

Two deliverables:
1. `carta/hook/` module implementing the three-tier threshold logic (fast-path inject / discard / Ollama judge)
2. Markdown embedding support in the existing pipeline

</domain>

<decisions>
## Implementation Decisions

### Hook Output Format
- **D-01:** Write `{"context": "...chunks..."}` JSON to stdout — the Claude Code UserPromptSubmit hook protocol. Claude receives it as a prepended context block.
- **D-02:** Each chunk formatted as: `**Source: path/file.md (score: 0.91)**\n> chunk text\n\n`. Bold source+score header, blockquote body, blank line between chunks.
- **D-03:** Section header inside the context string: `## Relevant documentation\n\n` before the chunk list.

### Hook Python Architecture
- **D-04:** New `carta/hook/` module with `hook.py` entry. Registered as `carta-hook` console_scripts entry in `pyproject.toml`. The bash stub `carta-prompt-hook.sh` just calls `carta-hook`.
- **D-05:** Hook reads stdin JSON `{"prompt": "..."}` from Claude Code (standard UserPromptSubmit contract). Parse with `json.loads(sys.stdin.read())`.
- **D-06:** Query extraction from prompt:
  - If prompt ≤ 500 chars: use as-is as search query
  - If prompt > 500 chars: call Ollama judge model to extract a concise 1–2 sentence search intent. Fall back to `prompt[-500:]` (last 500 chars) if Ollama times out or errors.
  - This handles long dictation inputs where the actual request is buried in stream-of-consciousness text.

### Threshold Logic (locked by success criteria)
- **D-07:** High threshold: `> 0.85` → inject immediately, no Ollama call.
- **D-08:** Low threshold: `< 0.60` → discard all candidates, hook exits silently (no `context` key in output).
- **D-09:** Gray zone: `0.60–0.85` → call Ollama judge. Inject on "yes"; discard on anything else. If judge call exceeds 3s, proceed without injection (unblock the prompt).
- **D-10:** Max 5 chunks injected in a single prompt regardless of score band.

### Config Schema Reconciliation
- **D-11:** Split existing `proactive_recall.similarity_threshold: 0.78` into two fields:
  - `high_threshold: 0.85` (inject without judge)
  - `low_threshold: 0.60` (discard below)
- **D-12:** Update `proactive_recall.max_results: 3` → `5` to match success criteria.
- **D-13:** Add `proactive_recall.judge_timeout_s: 3`.
- **D-14:** Keep `proactive_recall.ollama_model: "phi3.5-mini"` as judge model default.

### Ollama Judge Prompt
- **D-15:** Binary yes/no. System prompt: `"You decide if documentation is relevant to a coding prompt. Answer only 'yes' or 'no'."`.
- **D-16:** All gray-zone candidates passed in one call (up to 5 chunks), separated by `---`. One Ollama call decides the batch.
- **D-17:** Parse logic: `response.strip().lower().startswith("yes")`. Anything else (including "maybe", "possibly", verbose reasoning) → treat as no-inject.
- **D-18:** Judge prompt user turn format:
  ```
  Prompt: {user_prompt[:300]}

  Documentation candidates:
  {chunk1_excerpt}
  ---
  {chunk2_excerpt}

  Are any of these relevant?
  ```

### Markdown Embedding
- **D-19:** Chunking strategy: heading-aware split first. Split on `##` / `###` heading boundaries. If a section exceeds `max_tokens` (800), apply the existing `chunk_text()` overlap chunker on that section.
- **D-20:** Strip YAML frontmatter (the `---` block) before chunking. Store frontmatter key/values as Qdrant payload metadata fields (not in chunk text).
- **D-21:** Sidecar `file_type: markdown` for embedded `.md` files (per success criteria).
- **D-22:** Add `.md` to `_SUPPORTED_EXTENSIONS` in `carta/embed/pipeline.py`. Dispatch to markdown parser based on file extension.

### Claude's Discretion
- Implementation of the markdown heading parser (`carta/embed/parse.py` or new function in same file) — same module as `extract_pdf_text`, same pattern.
- Whether query extraction (D-06) uses the same Ollama endpoint as the judge or a shared `_call_ollama()` helper — Claude can factor this out.
- Exact excerpt length cap for judge prompt chunks (recommend ~200 chars to keep prompt compact for small model).

</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### Phase Definition
- `.planning/ROADMAP.md` §Phase 3 — Goal, requirements (HOOK-01 through HOOK-07, EMBED-01), and all 5 success criteria (exact thresholds, chunk cap, timeout, config location, file_type)
- `.planning/PROJECT.md` §Active, §Key Decisions — Architecture context, three-tier design rationale, constraint: Ollama judge ≤2B params

### Existing Code to Extend
- `carta/config.py` — `proactive_recall` defaults to reconcile per D-11 through D-14; `DEFAULTS` dict is the source of truth
- `carta/embed/pipeline.py` — `_SUPPORTED_EXTENSIONS`, `run_search()`, `_embed_one_file()` — all need extension for markdown
- `carta/embed/parse.py` — `chunk_text()` reusable for oversized markdown sections; `extract_pdf_text()` as pattern for new markdown extractor
- `carta/hooks/carta-prompt-hook.sh` — empty bash stub, becomes single-line `carta-hook` call
- `carta/mcp/server.py` — usage pattern for importing `run_search`, `find_config`, `load_config`

No external specs — requirements fully captured in decisions above and ROADMAP.md success criteria.

</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets
- `run_search(query, cfg)` in `carta/embed/pipeline.py` — already used by `carta_search` MCP tool; hook reuses this directly
- `chunk_text(text, max_tokens, overlap_fraction)` in `carta/embed/parse.py` — reuse for oversized markdown sections
- `find_config()` + `load_config()` in `carta/config.py` — same config loading pattern used by MCP server
- `FILE_TIMEOUT_S` constant in `carta/embed/pipeline.py` — reference for timeout patterns

### Established Patterns
- Module structure: `carta/{feature}/__init__.py` + `carta/{feature}/{feature}.py` + `carta/{feature}/tests/test_{feature}.py`
- Console entry points: see `carta-mcp` in `pyproject.toml` — `carta-hook` follows same pattern
- Stderr-only logging: MCP server sets precedent; hook should follow (hook writes JSON to stdout, logs to stderr)
- Structured error return: MCP tools return `{"error": "...", "detail": "..."}` on failure — hook should exit 0 with no context key (not write error JSON to stdout, which would corrupt the hook protocol)

### Integration Points
- `carta/hooks/carta-prompt-hook.sh` — entry point Claude Code invokes; becomes thin wrapper calling `carta-hook`
- `carta/install/bootstrap.py` `_register_hooks()` — already registers the hook path; no change needed unless hook filename changes
- `pyproject.toml` `[project.scripts]` — add `carta-hook = "carta.hook.hook:main"`

</code_context>

<specifics>
## Specific Ideas

- User dictates long prompts frequently — the >500 char Ollama query compression path (D-06) is a primary use case, not an edge case. The fallback to `prompt[-500:]` matters because Ollama might be unavailable.
- The "maybe" judge state was considered but rejected: small models are unreliable at 3-state outputs, and the threshold tiers already provide the gradient. A future backlog item could surface low-confidence matches as an async suggestion rather than pre-prompt injection.

</specifics>

<deferred>
## Deferred Ideas

- **Three-state judge (yes/no/maybe):** Surface "maybe" matches as a lower-priority async suggestion to Claude rather than pre-prompt injection. Interesting direction but adds significant complexity (separate delivery channel, UX for surfacing suggestions) and small models can't reliably produce calibrated 3-state output. Candidate for a future backlog phase.

</deferred>

---

*Phase: 03-smart-hook-markdown-embedding*
*Context gathered: 2026-03-26*
