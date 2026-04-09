# Qdrant & Ollama Setup UX — Design Spec

**Date:** 2026-04-08
**Status:** Draft

---

## Problem

Carta's current prerequisite guidance is minimal: a single Docker one-liner buried in the README and a link to Qdrant's quickstart. `carta doctor` detects missing services but hides fix instructions behind `--verbose`, and the Phase 3 model check misses the judge model entirely. New users have no clear path from zero to working.

## Goals

1. Docs give a complete, copy-pasteable local setup path for Qdrant and Ollama.
2. `carta doctor` is self-contained — a failing check always prints the fix command, no flags needed.
3. Phase 3 checks all required models including the hook judge.
4. `carta init` inherits the improvements automatically (it already calls preflight).

## Out of Scope

- `mcp-server-qdrant` integration — carta-mcp already provides the semantic search layer; adding the official Qdrant MCP would be redundant and bypass carta's context abstraction.
- Cloud Qdrant or remote Ollama — local-only setup.
- Auto-launching Docker containers from `carta init`.

---

## Design

### 1. Documentation (`docs/install.md` + `README.md`)

**`docs/install.md`** gets a new top-level **Prerequisites** section before the `carta install` steps:

#### Qdrant (vector store)

```bash
docker run -d \
  -p 6333:6333 \
  -v ~/.carta/qdrant_storage:/qdrant/storage \
  --name qdrant \
  qdrant/qdrant
```

Notes:
- `-d` runs detached; Qdrant starts automatically on Docker restart.
- The `-v` flag persists your collections across container restarts and upgrades. Without it, all embedded documents are lost when the container stops.
- For anything beyond basic setup (TLS, resource limits, upgrades), see [Qdrant quickstart](https://qdrant.tech/documentation/quickstart/).

#### Ollama (embeddings + vision + hook judge)

Install: https://ollama.ai/download

Pull the required models:

```bash
# Required — text embeddings
ollama pull nomic-embed-text

# Required — hook relevance judge (small, fast)
# Can be swapped for a larger model if latency is not a concern
ollama pull qwen3.5:0.8b

# Optional — vision embedding and visual search
ollama pull llava
```

Model roles:
- `nomic-embed-text` — generates 768-dim vectors for all document embedding and search
- `qwen3.5:0.8b` — judges whether retrieved context is relevant before injecting into the prompt (hook judge). Default is 0.8B to keep hook latency low (the hook blocks prompt submission). Users can set `proactive_recall.ollama_model` in `.carta/config.yaml` to a larger model if they prefer higher recall accuracy over speed.
- `llava` — required only if using visual embedding for PDF pages (`carta embed --visual`)

#### Verify

```bash
carta doctor
```

Run this after starting Qdrant and Ollama. All Phase 2 and Phase 3 checks should pass before running `carta init`.

**`README.md`** gets a trimmed version — the two Docker/Ollama command blocks and a "→ see docs/install.md for the full setup" pointer. The existing one-liner is replaced.

---

### 2. `carta doctor` improvements (`carta/install/preflight.py`)

#### Always show suggestions for failures and warnings

`_print_check` currently gates suggestions behind `verbose=True`. Change: always print `suggestion` when status is `fail` or `warn`, regardless of verbose flag. Verbose mode retains the additional `details` dict output.

```python
# Before
if verbose and check.suggestion:
    print(f"     → {check.suggestion}")

# After
if check.suggestion and check.status in ("fail", "warn"):
    print(f"     → {check.suggestion}")
if verbose and check.details:
    ...
```

#### Add judge model check

`_phase3_models` currently checks `nomic-embed-text` and `llava`. Add `qwen3.5:0.8b` from `proactive_recall.ollama_model` in config defaults.

The check reads the configured model name from config (falling back to the default) so it stays in sync if the user has customised the judge model.

#### Update Qdrant fix suggestion

`_check_qdrant_running` suggestion updated to include the persistence volume:

```
Start with: docker run -d -p 6333:6333 -v ~/.carta/qdrant_storage:/qdrant/storage --name qdrant qdrant/qdrant
```

#### Add actionable fix footer to summary

`_print_summary` currently prints "must be resolved manually" with no commands. When there are failures or fixable issues, append a numbered list of all `suggestion` values from failed/warned checks:

```
🔴 2 issue(s) to resolve:

  1. Qdrant not running
     → docker run -d -p 6333:6333 -v ~/.carta/qdrant_storage:/qdrant/storage --name qdrant qdrant/qdrant

  2. Model 'qwen3.5:0.8b' not pulled
     → ollama pull qwen3.5:0.8b
```

---

## Config change

`carta/config.py` DEFAULTS: update `proactive_recall.ollama_model` from `"qwen2.5:0.5b"` to `"qwen3.5:0.8b"`.

---

## Files changed

| File | Change |
|------|--------|
| `docs/install.md` | New Prerequisites section (Qdrant + Ollama + verify) |
| `README.md` | Trimmed prerequisite block + pointer to install.md |
| `carta/install/preflight.py` | Always-on suggestions, judge model check, updated Qdrant suggestion, fix footer |
| `carta/config.py` | Update default judge model to `qwen3.5:0.8b` |

---

## Testing

- `carta doctor` with Qdrant stopped → fix suggestion printed without `--verbose`
- `carta doctor` with judge model not pulled → warning + pull command shown
- `carta doctor` with all services up → clean pass, no suggestions cluttering output
- Existing preflight unit tests pass
