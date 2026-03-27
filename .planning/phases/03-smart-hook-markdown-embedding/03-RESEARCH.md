# Phase 3: Smart Hook + Markdown Embedding - Research

**Researched:** 2026-03-26
**Domain:** Claude Code hook protocol, Ollama HTTP API, Qdrant search, markdown chunking
**Confidence:** HIGH (core architecture from locked decisions + verified codebase) / MEDIUM (hook stdout format — documented issue)

---

<user_constraints>
## User Constraints (from CONTEXT.md)

### Locked Decisions

- **D-01:** Hook writes `{"context": "...chunks..."}` JSON to stdout — Claude Code UserPromptSubmit hook protocol
- **D-02:** Each chunk formatted as: `**Source: path/file.md (score: 0.91)**\n> chunk text\n\n`
- **D-03:** Section header inside context string: `## Relevant documentation\n\n` before chunk list
- **D-04:** New `carta/hook/` module with `hook.py` entry. Registered as `carta-hook` console_scripts entry in `pyproject.toml`. Bash stub `carta-prompt-hook.sh` becomes single-line `carta-hook` call
- **D-05:** Hook reads stdin JSON `{"prompt": "..."}` — parse with `json.loads(sys.stdin.read())`
- **D-06:** Query extraction: prompt ≤ 500 chars → use as-is; prompt > 500 chars → call Ollama to extract 1–2 sentence intent; fall back to `prompt[-500:]` on Ollama timeout/error
- **D-07:** High threshold: `> 0.85` → inject immediately, no Ollama call
- **D-08:** Low threshold: `< 0.60` → discard all candidates, exit silently (no `context` key in output)
- **D-09:** Gray zone `0.60–0.85` → call Ollama judge; inject on "yes"; discard on anything else; if judge exceeds 3s, proceed without injection
- **D-10:** Max 5 chunks injected per prompt regardless of score band
- **D-11:** Split existing `proactive_recall.similarity_threshold: 0.78` into `high_threshold: 0.85` and `low_threshold: 0.60`
- **D-12:** Update `proactive_recall.max_results: 3` → `5`
- **D-13:** Add `proactive_recall.judge_timeout_s: 3`
- **D-14:** Keep `proactive_recall.ollama_model: "phi3.5-mini"` as judge model default
- **D-15:** Binary yes/no judge. System prompt: `"You decide if documentation is relevant to a coding prompt. Answer only 'yes' or 'no'."`
- **D-16:** All gray-zone candidates passed in one call (up to 5 chunks). One Ollama call decides the batch.
- **D-17:** Parse logic: `response.strip().lower().startswith("yes")`. Anything else → no-inject.
- **D-18:** Judge prompt user turn format: `Prompt: {user_prompt[:300]}\n\nDocumentation candidates:\n{chunk1_excerpt}\n---\n{chunk2_excerpt}\n\nAre any of these relevant?`
- **D-19:** Markdown chunking: heading-aware split on `##`/`###` boundaries; apply `chunk_text()` on oversized sections
- **D-20:** Strip YAML frontmatter before chunking; store frontmatter key/values as Qdrant payload metadata
- **D-21:** Sidecar `file_type: markdown` for embedded `.md` files
- **D-22:** Add `.md` to `_SUPPORTED_EXTENSIONS` in `carta/embed/pipeline.py`; dispatch to markdown parser based on file extension

### Claude's Discretion

- Implementation location of markdown heading parser (recommend `carta/embed/parse.py`, same module as `extract_pdf_text`)
- Whether query extraction (D-06) uses same Ollama endpoint as judge or a shared `_call_ollama()` helper — factoring out is recommended
- Exact excerpt length cap for judge prompt chunks (recommend ~200 chars)

### Deferred Ideas (OUT OF SCOPE)

- Three-state judge (yes/no/maybe): surface "maybe" matches as lower-priority async suggestion. Adds complexity; small models unreliable at 3-state output.
</user_constraints>

---

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|------------------|
| HOOK-01 | Hook registered as UserPromptSubmit handler; extracts semantic query from prompt and queries Qdrant via shared search service | `run_search()` in pipeline.py verified as direct reuse; bash stub exists at `carta/hooks/carta-prompt-hook.sh` |
| HOOK-02 | Fast path: score >0.85 → inject without calling Ollama | Score from `run_search()` return dict `{"score": float, ...}`; threshold comparison is straightforward |
| HOOK-03 | Noise gate: score <0.60 → discard without injection | Exit 0 with no stdout or empty stdout; hook protocol verified |
| HOOK-04 | Gray zone (0.60–0.85): `hooks/judge.py` calls Ollama (qwen2.5:0.5b default) for binary judgment; inject only on "yes" | Ollama `/api/chat` POST verified; MEDIUM risk: no small judge model currently installed |
| HOOK-05 | Hard 3-second wall-clock timeout on all Ollama judge calls; fail-open (no injection, prompt unblocked) | Pattern: `concurrent.futures.ThreadPoolExecutor` + `future.result(timeout=3)` — directly mirrors FILE_TIMEOUT_S pattern in pipeline.py |
| HOOK-06 | Max 5 injected chunks per prompt | Simple slice: `hits[:5]`; max_results config default updated to 5 |
| HOOK-07 | Thresholds (high/low bounds) and judge model configurable in `.carta/config.yaml` | Requires `DEFAULTS` dict update in `carta/config.py`; `load_config()` deep-merges user values |
| EMBED-01 | Markdown files (`.md`) processed by embed pipeline with `file_type: markdown` in sidecar | `_SUPPORTED_EXTENSIONS`, `_embed_one_file()` dispatch, and `generate_sidecar_stub()` all identified for extension |
</phase_requirements>

---

## Summary

Phase 3 delivers two independent but related capabilities: a smart hook that injects relevant documentation into Claude's context on prompt submission, and markdown file support in the embed pipeline. The codebase is well-positioned — `run_search()`, `chunk_text()`, `find_config()`/`load_config()`, and the `concurrent.futures` timeout pattern are all directly reusable. The new `carta/hook/` module follows the exact same structure as `carta/mcp/`.

The three-tier score band (fast-inject / noise-gate / Ollama-judge) is architecturally clean. The main implementation risk is the Claude Code hook stdout protocol: research found that `hookSpecificOutput.additionalContext` is the documented JSON format, while D-01 specifies `{"context": "..."}`. There is also a known bug (#17550) with hookSpecificOutput on the first message of new sessions — plain text stdout is the most reliable fallback. The planner must address this discrepancy.

The second risk is the Ollama judge model: `phi3.5-mini` and `qwen2.5:0.5b` are NOT installed on this machine. The plan must include a Wave 0 step to pull a small judge model, or default to the largest available model that meets the ≤2B constraint.

**Primary recommendation:** Implement `carta/hook/hook.py` reusing `run_search()` + the `concurrent.futures` timeout pattern from pipeline.py. The bash stub becomes a one-liner. Use `carta/embed/parse.py` for the markdown extractor. Resolve the hook stdout format question in Wave 0 before wiring the injection path.

---

## Standard Stack

### Core (no new dependencies)
| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| qdrant-client | 1.17.1 (installed) | Qdrant search via `run_search()` | Already in project; `run_search()` is direct reuse |
| requests | 2.32.5 (installed) | Ollama HTTP API calls | Already in project; used by existing embed code |
| PyYAML | 6.0+ (installed) | Config loading, frontmatter strip | Already in project |
| concurrent.futures | stdlib | 3s judge timeout (ThreadPoolExecutor) | Established pattern in pipeline.py FILE_TIMEOUT_S |
| re | stdlib | Markdown heading split, frontmatter strip | No new dependency |

### No New Dependencies Required
All stack needs are met by existing project dependencies. The hook module is pure Python using stdlib + existing project libraries.

**Installation:** No new packages. Verified installed versions above.

---

## Architecture Patterns

### Recommended Project Structure
```
carta/
├── hook/
│   ├── __init__.py          # empty
│   ├── hook.py              # main() entry point; stdin read, score routing, stdout write
│   └── tests/
│       └── test_hook.py     # unit tests with mocked run_search and _call_ollama
├── embed/
│   ├── parse.py             # ADD: extract_markdown_text(), chunk_markdown_sections()
│   └── pipeline.py          # MODIFY: _SUPPORTED_EXTENSIONS, _embed_one_file dispatch
└── config.py                # MODIFY: DEFAULTS proactive_recall keys
```

### Pattern 1: Hook Entry Point (mirrors MCP server pattern)
**What:** `hook.py:main()` reads stdin JSON, calls `run_search()`, routes by score band, writes stdout.
**When to use:** This is THE hook architecture — one module, one entry point.
```python
# Pattern from carta/mcp/server.py — hook.py follows same import structure
from carta.config import find_config, load_config
from carta.embed.pipeline import run_search

def main():
    data = json.loads(sys.stdin.read())
    prompt = data.get("prompt", "")
    # ... score routing, inject or exit 0
```

### Pattern 2: Timeout via ThreadPoolExecutor (from pipeline.py)
**What:** Wrap Ollama call in executor, use `future.result(timeout=N)`.
**When to use:** All Ollama judge calls (D-09, HOOK-05).
```python
# Source: carta/embed/pipeline.py lines 287-294 — exact same pattern
with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
    future = executor.submit(_call_ollama_judge, prompt, chunks, cfg)
    try:
        result = future.result(timeout=judge_timeout_s)
    except concurrent.futures.TimeoutError:
        return  # fail-open: no injection
```

### Pattern 3: Markdown Extractor (mirrors extract_pdf_text)
**What:** `extract_markdown_text()` returns `list[dict]` with same shape as PDF pages.
**When to use:** Called by `_embed_one_file()` when `file_path.suffix == ".md"`.
```python
# Mirrors extract_pdf_text return shape: [{"page": int, "text": str, "headings": list[str]}]
# For markdown: split on ## / ### headings, each section = one "page" dict
def extract_markdown_text(md_path: Path) -> list[dict]:
    text = md_path.read_text(encoding="utf-8")
    text = _strip_frontmatter(text)  # remove leading ---...--- block
    sections = _split_on_headings(text)  # re.split on ^##+ pattern
    return [{"page": i+1, "text": s["text"], "headings": [s["heading"]]}
            for i, s in enumerate(sections)]
```

### Pattern 4: Hook stdout protocol
**What:** Write JSON to stdout on exit 0 to inject context.

**CRITICAL DISCREPANCY — MUST RESOLVE IN WAVE 0:**

D-01 specifies `{"context": "..."}` as the hook output format. Research found the documented Claude Code format is:
```json
{
  "hookSpecificOutput": {
    "hookEventName": "UserPromptSubmit",
    "additionalContext": "## Relevant documentation\n\n..."
  }
}
```
There is also a known bug (GitHub #17550) where `hookSpecificOutput` fails on the first message of a new session. Plain text stdout (non-JSON) is the most reliable current approach.

**Resolution options for planner:**
1. Use `hookSpecificOutput.additionalContext` per official docs (known first-session bug risk)
2. Use plain text stdout (most reliable, avoids JSON parsing bugs)
3. Use `{"context": "..."}` as D-01 specifies (verify this is a valid key by testing)

**Recommendation:** Wave 0 should include a manual smoke test of the hook stdout format before implementing the injection path.

### Anti-Patterns to Avoid
- **Writing to stdout outside JSON:** Hook shares stdout with Claude Code's JSON channel. Any stray print() breaks the protocol. All logging MUST go to stderr (precedent set by MCP server).
- **Blocking on Ollama without timeout:** Ollama cold-start can take 10–30s for first inference. The 3s timeout is non-negotiable (HOOK-05).
- **Raising exceptions from hook main():** Hook must exit 0 on all paths (fail-open). Exception → non-zero exit → prompt blocked.
- **Reusing chunk_text() directly with markdown string:** `chunk_text()` takes `list[dict]` (page dicts), not raw string. The markdown extractor must return the same shape.

---

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Semantic search | Custom Qdrant query | `run_search(query, cfg)` in pipeline.py | Already returns `{"score", "source", "excerpt"}`; tested |
| Per-operation timeout | `signal.alarm()` or `threading.Timer` | `concurrent.futures.ThreadPoolExecutor + future.result(timeout=N)` | Exact pattern from pipeline.py; cross-platform; tested |
| Config loading | Manual YAML parse | `find_config() + load_config()` | Deep-merge of DEFAULTS already handles missing keys |
| YAML frontmatter parsing | Custom parser | `re` strip of `^---\n.*?\n---\n` with `re.DOTALL` | Simple regex; PyYAML for the key/value extraction |
| Overlap chunking of large markdown sections | New chunker | `chunk_text(pages, max_tokens, overlap_fraction)` | Already handles oversized sections with overlap |

**Key insight:** The hook is thin orchestration over existing services. 80% of the logic already exists in `pipeline.py` and `parse.py`.

---

## Common Pitfalls

### Pitfall 1: Hook stdout pollution
**What goes wrong:** Any non-JSON bytes written to stdout corrupt the hook protocol. Claude Code receives garbled output and raises a hook error.
**Why it happens:** Python's `print()` goes to stdout by default. Import-time side effects, startup messages, or exception tracebacks can pollute.
**How to avoid:** At top of `hook.py:main()`, redirect all output: `sys.stdout = open(os.devnull, 'w')` then only write the final JSON. Or consistently use `print(..., file=sys.stderr)` throughout.
**Warning signs:** Hook "error" displayed in Claude Code UI; hook works when run manually but not in Claude Code.

### Pitfall 2: No judge model installed
**What goes wrong:** Gray-zone path calls Ollama with `phi3.5-mini` or `qwen2.5:0.5b` — neither is installed. Ollama returns 404 or error. Hook fails or falls through without injection on every gray-zone prompt.
**Why it happens:** Config default (`phi3.5-mini`) doesn't match installed models. Installed models: `nomic-embed-text`, `qwen3.5:27b/9b`, `llama3`, `qwen2.5-coder:32b`.
**How to avoid:** Wave 0 must pull a small judge model. Options: `ollama pull qwen2.5:0.5b` (smallest, ~400MB) or `ollama pull phi3.5-mini`. Update config default to match what's actually pulled.
**Warning signs:** Gray-zone always falls through to no-injection; Ollama error in hook stderr.

### Pitfall 3: chunk_text() shape mismatch for markdown
**What goes wrong:** `chunk_text()` expects `list[dict]` with `{"page": int, "text": str, "headings": list}` — the PDF page format. Passing a raw string or wrong-shaped dict causes KeyError at runtime.
**Why it happens:** Markdown extractor is new code; easy to get shape wrong.
**How to avoid:** `extract_markdown_text()` MUST return identical shape to `extract_pdf_text()`. Write a unit test that asserts shape before wiring into pipeline.

### Pitfall 4: Config DEFAULTS not updated — missing keys cause KeyError
**What goes wrong:** Hook reads `cfg["proactive_recall"]["high_threshold"]` but DEFAULTS still has `similarity_threshold`. `load_config()` deep-merges DEFAULTS — if key is missing from DEFAULTS, missing from all configs without explicit setting.
**Why it happens:** D-11 through D-14 require DEFAULTS dict changes in `config.py`. Easy to implement hook before fixing config.
**How to avoid:** Update `config.py` DEFAULTS first (Wave 1 task 1). All other tasks depend on it.

### Pitfall 5: Hook subdirectory trigger bug
**What goes wrong:** Claude Code has known issues (#8810, #17277) where `UserPromptSubmit` hooks registered in project-level `.claude.json` may not fire when Claude is working in a subdirectory.
**Why it happens:** Claude Code hook path resolution bug.
**How to avoid:** Document in BOOT-03 (Phase 4). For Phase 3, test hook from repo root only. MCP pull path (`carta_search`) is reliable fallback for subdirectory contexts.

### Pitfall 6: `run_search()` raises on Qdrant unreachable
**What goes wrong:** If Qdrant is not running, `run_search()` raises `RuntimeError`. Hook crashes with non-zero exit, potentially blocking prompts.
**Why it happens:** `run_search()` does not fail-open — it raises on connection failure.
**How to avoid:** Wrap `run_search()` call in try/except in hook. On any exception from search, exit 0 silently (fail-open).

---

## Code Examples

### Hook main() skeleton
```python
# carta/hook/hook.py
import concurrent.futures
import json
import sys
from pathlib import Path

def main():
    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
        prompt = data.get("prompt", "")
    except Exception:
        sys.exit(0)  # fail-open: can't parse stdin

    try:
        from carta.config import find_config, load_config
        cfg_path = find_config(Path.cwd())
        if cfg_path is None:
            sys.exit(0)
        cfg = load_config(cfg_path)
    except Exception:
        sys.exit(0)  # carta not configured for this project

    if not cfg.get("modules", {}).get("proactive_recall", False):
        sys.exit(0)

    pr = cfg.get("proactive_recall", {})
    high_threshold = pr.get("high_threshold", 0.85)
    low_threshold = pr.get("low_threshold", 0.60)
    max_results = pr.get("max_results", 5)
    judge_timeout_s = pr.get("judge_timeout_s", 3)

    try:
        from carta.embed.pipeline import run_search
        query = _extract_query(prompt, cfg, judge_timeout_s)
        hits = run_search(query, cfg)
    except Exception:
        sys.exit(0)  # fail-open on search failure

    hits = hits[:max_results]
    if not hits or hits[0]["score"] < low_threshold:
        sys.exit(0)  # noise gate

    if hits[0]["score"] > high_threshold:
        _inject(hits)
        return

    # gray zone: call Ollama judge
    verdict = _judge_with_timeout(prompt, hits, cfg, judge_timeout_s)
    if verdict:
        _inject(hits)
    # else: exit 0, no injection
```

### _inject() output (pending stdout format resolution)
```python
def _inject(hits: list[dict]) -> None:
    lines = ["## Relevant documentation\n"]
    for h in hits:
        lines.append(f"**Source: {h['source']} (score: {h['score']:.2f})**\n")
        lines.append(f"> {h['excerpt'][:200]}\n\n")
    context_text = "\n".join(lines)
    # NOTE: Verify correct key — D-01 says "context", docs say hookSpecificOutput.additionalContext
    output = {"context": context_text}
    print(json.dumps(output))
```

### _embed_one_file markdown dispatch
```python
# In pipeline.py _embed_one_file():
if file_path.suffix == ".pdf":
    pages = extract_pdf_text(file_path)
elif file_path.suffix == ".md":
    pages = extract_markdown_text(file_path)  # same return shape
else:
    raise ValueError(f"Unsupported extension: {file_path.suffix}")
raw_chunks = chunk_text(pages, max_tokens=max_tokens, overlap_fraction=overlap_fraction)
```

### generate_sidecar_stub for markdown (file_type field)
```python
# In induct.py generate_sidecar_stub(): add file_type based on extension
file_type = "pdf" if file_path.suffix == ".pdf" else "markdown"
stub = {
    ...existing fields...,
    "file_type": file_type,
}
```

### Ollama /api/chat call pattern
```python
import requests

def _call_ollama_judge(prompt: str, hits: list[dict], cfg: dict) -> bool:
    ollama_url = cfg["embed"]["ollama_url"]
    model = cfg["proactive_recall"]["ollama_model"]
    excerpts = "\n---\n".join(h["excerpt"][:200] for h in hits)
    user_msg = (
        f"Prompt: {prompt[:300]}\n\n"
        f"Documentation candidates:\n{excerpts}\n\n"
        f"Are any of these relevant?"
    )
    resp = requests.post(
        f"{ollama_url}/api/chat",
        json={
            "model": model,
            "messages": [
                {"role": "system", "content": "You decide if documentation is relevant to a coding prompt. Answer only 'yes' or 'no'."},
                {"role": "user", "content": user_msg},
            ],
            "stream": False,
        },
        timeout=4,  # slightly > judge_timeout_s for executor timeout to fire first
    )
    resp.raise_for_status()
    answer = resp.json()["message"]["content"]
    return answer.strip().lower().startswith("yes")
```

---

## Environment Availability

| Dependency | Required By | Available | Version/Detail | Fallback |
|------------|------------|-----------|----------------|----------|
| Python 3.10+ | All hook code | YES | 3.14.3 | — |
| qdrant-client | `run_search()` | YES | 1.17.1 | — |
| requests | Ollama HTTP calls | YES | 2.32.5 | — |
| Ollama (service) | Judge calls, embed | YES (binary) | `/usr/local/bin/ollama` | Fail-open on timeout |
| `nomic-embed-text` | Embeddings | YES | Installed | — |
| `phi3.5-mini` (judge) | HOOK-04 default | **NO** | Not pulled | Pull `qwen2.5:0.5b` |
| `qwen2.5:0.5b` (judge) | REQUIREMENTS.md default | **NO** | Not pulled | Use llama3 (4B, too large) |
| Qdrant service | Search | Unknown (not tested) | Docker required | Fail-open in hook |

**Missing dependencies with no fallback:**
- Small judge model (`phi3.5-mini` or `qwen2.5:0.5b`): Wave 0 MUST include `ollama pull qwen2.5:0.5b` (preferred — ~400MB, ≤2B constraint). Config default `ollama_model` should be updated to `qwen2.5:0.5b` matching REQUIREMENTS.md (D-14 says `phi3.5-mini` but REQUIREMENTS.md says `qwen2.5:0.5b`; these conflict — planner must pick one and note).

**Missing dependencies with fallback:**
- Qdrant service: hook wraps search in try/except and fails open if Qdrant is unreachable.

---

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| `similarity_threshold: 0.78` (single value) | `high_threshold: 0.85` + `low_threshold: 0.60` (two thresholds) | Phase 3 | Config DEFAULTS must be updated; old config.yaml files will get new keys via deep-merge defaults |
| `max_results: 3` | `max_results: 5` | Phase 3 | Affects `run_search()` `top_n` usage |
| Bash stub does nothing | Bash stub calls `carta-hook` (Python entry point) | Phase 3 | No logic in bash; Python handles all routing |

**Deprecated/outdated:**
- `proactive_recall.similarity_threshold`: replaced by `high_threshold` + `low_threshold` in D-11
- `proactive_recall.ollama_judge: False` boolean flag: replaced by implicit behavior (always use judge in gray zone when enabled)

---

## Validation Architecture

### Test Framework
| Property | Value |
|----------|-------|
| Framework | pytest 7.0+ |
| Config file | `pyproject.toml` (pytest section) or none — run as `pytest carta/` |
| Quick run command | `pytest carta/hook/tests/test_hook.py -x -q` |
| Full suite command | `pytest carta/ -x -q` |

### Phase Requirements → Test Map

| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| HOOK-01 | Hook reads stdin JSON, extracts prompt, calls run_search | unit | `pytest carta/hook/tests/test_hook.py::test_hook_reads_stdin -x` | ❌ Wave 0 |
| HOOK-02 | Score >0.85 → inject, no Ollama call | unit | `pytest carta/hook/tests/test_hook.py::test_fast_path_injects -x` | ❌ Wave 0 |
| HOOK-03 | Score <0.60 → no injection, exit 0 | unit | `pytest carta/hook/tests/test_hook.py::test_noise_gate_silent -x` | ❌ Wave 0 |
| HOOK-04 | Gray zone → Ollama judge called; "yes" → inject | unit | `pytest carta/hook/tests/test_hook.py::test_gray_zone_yes_injects -x` | ❌ Wave 0 |
| HOOK-04 | Gray zone → Ollama judge called; "no" → no inject | unit | `pytest carta/hook/tests/test_hook.py::test_gray_zone_no_discards -x` | ❌ Wave 0 |
| HOOK-05 | Judge timeout (>3s) → no injection, exit 0 | unit | `pytest carta/hook/tests/test_hook.py::test_judge_timeout_fails_open -x` | ❌ Wave 0 |
| HOOK-06 | Max 5 chunks even if search returns more | unit | `pytest carta/hook/tests/test_hook.py::test_chunk_cap_five -x` | ❌ Wave 0 |
| HOOK-07 | Thresholds read from config; non-default values respected | unit | `pytest carta/hook/tests/test_hook.py::test_config_thresholds -x` | ❌ Wave 0 |
| EMBED-01 | .md file embedded with file_type: markdown in sidecar | unit | `pytest carta/embed/tests/test_embed.py::test_markdown_embed_sidecar -x` | ❌ Wave 0 |
| EMBED-01 | Markdown heading-aware chunking produces correct sections | unit | `pytest carta/embed/tests/test_embed.py::test_markdown_chunking -x` | ❌ Wave 0 |
| HOOK-01/02 | End-to-end: hook script injects on high-score prompt | manual | Manual smoke test with real Qdrant + Ollama | — |

### Key Invariants to Verify
1. **Score band routing is exclusive:** A hit cannot be both fast-path AND gray-zone. Test with score exactly at boundary (0.85, 0.60).
2. **Chunk cap is enforced across all paths:** Even fast-path injects at most 5 chunks regardless of `top_n` setting.
3. **Timeout fails open:** Judge timeout must NOT block the prompt. Test with mock that sleeps >3s.
4. **Stdout is clean:** Hook must write NOTHING to stdout except the final JSON (or nothing at all). Use subprocess capture in integration test.
5. **No injection on Qdrant failure:** If `run_search()` raises, hook exits 0 silently.

### Sampling Rate
- **Per task commit:** `pytest carta/hook/tests/ -x -q`
- **Per wave merge:** `pytest carta/ -x -q`
- **Phase gate:** Full suite green before `/gsd:verify-work`

### Wave 0 Gaps
- [ ] `carta/hook/__init__.py` — empty module init
- [ ] `carta/hook/tests/__init__.py` — empty test init
- [ ] `carta/hook/tests/test_hook.py` — covers HOOK-01 through HOOK-07
- [ ] `carta/embed/tests/test_embed.py` — add markdown test cases (covers EMBED-01)
- [ ] `ollama pull qwen2.5:0.5b` — required for gray-zone path to function

---

## Open Questions

1. **Hook stdout format: `{"context": "..."}` vs `hookSpecificOutput.additionalContext`**
   - What we know: D-01 locks `{"context": "..."}`. Official Claude Code docs say `hookSpecificOutput.additionalContext`. Known bug #17550 with hookSpecificOutput on first session message.
   - What's unclear: Whether `{"context": "..."}` is a valid shorthand or entirely unsupported.
   - Recommendation: Wave 0 smoke test — run a minimal hook that writes `{"context": "test"}` and verify Claude receives it. If not, switch to `hookSpecificOutput.additionalContext` or plain text stdout.

2. **Judge model: D-14 says `phi3.5-mini`, REQUIREMENTS.md says `qwen2.5:0.5b`**
   - What we know: Neither is installed. `qwen2.5:0.5b` is smaller (~400MB vs ~2GB for phi3.5). Both satisfy ≤2B constraint.
   - Recommendation: Default to `qwen2.5:0.5b` (matches REQUIREMENTS.md; smaller download).

3. **`modules.proactive_recall` flag vs always-on**
   - What we know: Current bash stub checks `modules.proactive_recall` boolean. D-04 says hook exits silently if not enabled.
   - What's unclear: Whether the new Python hook should also gate on this flag.
   - Recommendation: Yes — maintain the existing guard. Hook exits 0 if `modules.proactive_recall` is False.

---

## Sources

### Primary (HIGH confidence)
- Codebase direct read: `carta/embed/pipeline.py` — `run_search()`, `FILE_TIMEOUT_S`, `_embed_one_file()`, `_SUPPORTED_EXTENSIONS`
- Codebase direct read: `carta/embed/parse.py` — `extract_pdf_text()`, `chunk_text()` signatures
- Codebase direct read: `carta/embed/induct.py` — `generate_sidecar_stub()`, `write_sidecar()`
- Codebase direct read: `carta/config.py` — `DEFAULTS.proactive_recall`, `load_config()`, `find_config()`
- Codebase direct read: `carta/hooks/carta-prompt-hook.sh` — existing stub structure
- Codebase direct read: `pyproject.toml` — `[project.scripts]` pattern for `carta-mcp`
- `ollama list` — confirmed installed models

### Secondary (MEDIUM confidence)
- WebSearch: Claude Code hooks reference — `hookSpecificOutput.additionalContext` format, exit code behavior
- WebSearch: Ollama `/api/chat` endpoint — `{"model", "messages", "stream": false}` JSON format

### Tertiary (LOW confidence — flag for validation)
- GitHub issue #17550: hookSpecificOutput fails on first message of new session — single source, needs direct testing
- GitHub issue #13912: stdout causes error in UserPromptSubmit hooks — may be resolved in current Claude Code version
- D-01 `{"context": "..."}` format: not verified against official docs; may be outdated or project-specific convention

---

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH — all libraries verified installed; no new dependencies
- Architecture: HIGH — patterns directly from verified codebase
- Hook stdout format: LOW-MEDIUM — documented discrepancy between CONTEXT.md D-01 and official docs; needs Wave 0 validation
- Pitfalls: HIGH — timeout pattern, stdout pollution, missing model all verified from codebase + environment check
- Environment: HIGH — directly verified via `ollama list` and `pip show`

**Research date:** 2026-03-26
**Valid until:** 2026-04-25 (Claude Code hook protocol may change; re-verify if Claude Code updated)
