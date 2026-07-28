# Bounded search budget for the proactive-recall hook

**Date:** 2026-07-28
**Status:** Design approved, pending implementation plan
**Issue:** [#106](https://github.com/Ian-q/Carta/issues/106) (split-host deployment)

## Goal

Give `carta-hook` a wall-clock budget for its Qdrant search so that an unreachable
or slow backend delays prompt submission by a bounded ~3 s instead of up to ~80 s.

## Background: why this only becomes dangerous with a remote backend

The hook already fails open correctly. `run_search` is wrapped in `try/except`,
every path exits 0, and the prompt always proceeds. **The defect is duration, not
failure handling.**

Two unbounded-in-practice timeouts sit on the hook's path:

| Call | Timeout | Location |
|---|---|---|
| Ollama query embed | **60 s** | `carta/embed/embed.py:95` |
| Qdrant search client | **10 s** | `carta/embed/pipeline.py:2228` |

The client's timeout applies per request and `run_search` queries once per
collection, so the worst case is `60 + 10 × n_collections` — about **80 s with two
collections**, on *every* prompt.

That ceiling has never bound before because the backend was on localhost: a dead
local service returns `ECONNREFUSED` immediately, so failure is instant and the
60 s is academic. Pointing `qdrant_url` at a **remote host over the tailnet**
changes the failure mode, not the code — a peer that is down silently drops
packets with no RST, so `requests` waits the full connect timeout. The same
configuration that was harmless locally becomes an 80-second stall per prompt.

This is the blocker on repointing the Mac at homelab (Phase 0a of the homelab
brain deliberately stopped short of it for this reason).

### Why an outer wrapper is not the fix

The obvious approach — wrap `run_search` in a `ThreadPoolExecutor` with a timeout
— does not work here, and the codebase already knows why. From
`_call_ollama_judge`'s docstring:

> An inner > outer let the hook block past `judge_timeout_s`, since
> `ThreadPoolExecutor.__exit__` waits for the abandoned thread to finish.

Abandoning a blocked thread does not free the process. **Inner timeouts are the
only real fix**, which means threading a budget down into `run_search`.

## Design

### Config

One new key, beside the existing `judge_timeout_s`:

```yaml
proactive_recall:
  judge_timeout_s: 3
  search_timeout_s: 3    # new
```

Registered in `carta/config.py` `DEFAULTS` under the existing `proactive_recall`
block.

### Signatures

```python
def run_search(query: str, cfg: dict, verbose: bool = False,
               stats: dict | None = None,
               timeout_s: float | None = None) -> list[dict]

def get_embedding(text: str, ollama_url: str = ..., model: str = ...,
                  prefix: str = "search_document: ",
                  timeout: float = 60) -> list[float]
```

**`timeout_s=None` means today's behaviour, exactly** — 60 s embed, 10 s Qdrant,
no deadline. CLI, MCP and eval pass nothing and are unaffected. Only the hook
opts in. This keeps the blast radius on a 1205-test suite to the hook path.

### Budget semantics: one deadline, not a per-call timeout

A per-call timeout is not a bound — 3 s across one embed and two collections is
still 9 s. Since the entire point is a limit the user can reason about, the budget
is a **single wall-clock deadline**:

1. Stamp `deadline = time.monotonic() + timeout_s` on entry.
2. Clamp the query embed to the remaining time.
3. Construct `QdrantClient` with the remaining time **after** the embed.
4. Before each per-collection query, check the deadline; if exhausted, stop
   querying further collections.

Step 3 means moving the client construction below `_embed_query_or_raise`. This is
low risk: `QdrantClient()` does not connect on construction, so the existing
`try/except` around it is nearly vacuous and no connection error ordering changes
in practice.

### Exhaustion degrades to silence, not error

When the deadline expires mid-loop, `run_search` returns the results it has
(possibly none) rather than raising. The hook's existing noise gate already exits
silently on an empty or low-scoring result set, so a budget-exhausted search
produces no injection and no stderr noise — the prompt simply proceeds. Raising
would also be caught by the hook's fail-open handler, but it would print an error
on every prompt during an outage, which is its own kind of noise.

### Why 3 s is generous rather than tight

After the repoint, only `qdrant_url` becomes remote. `ollama_url` **stays on
localhost** because the split-model design keeps `nomic-embed-text` on the laptop
for query-time embedding. A warm local embed measures ~22 ms, so 3 s covers the
embed plus a tailnet round trip with two orders of magnitude of headroom.

Worth recording the trap this avoids: had the embedder moved to the GPU host, a
hook embed arriving while a large model was resident would trigger a measured
**17 s model swap** under `OLLAMA_MAX_LOADED_MODELS=1` — silently exceeding any
sane budget and disabling proactive recall on every prompt.

## Testing

Written first, TDD.

| # | Test |
|---|---|
| 1 | `run_search` without `timeout_s` still uses 60 s embed / 10 s Qdrant (regression guard) |
| 2 | `run_search(timeout_s=X)` passes a clamped timeout to `get_embedding` |
| 3 | `run_search(timeout_s=X)` constructs `QdrantClient` with timeout ≤ X |
| 4 | Deadline exhausted mid-loop → later collections skipped, partial results returned, no raise |
| 5 | Hook reads `search_timeout_s` from config and passes it to `run_search` |
| 6 | Hook defaults to 3 when the key is absent |
| 7 | Hook still exits 0 when `run_search` raises (fail-open preserved) |
| 8 | A backend that blocks past the budget returns within ~budget, not ~80 s |

Test 1 is the important one: it pins the promise that non-hook callers are
untouched.

## Out of scope

- **A global search timeout.** Considered and rejected: a slow `carta search` over
  a wide candidate pool is legitimate, and a tight global default would break real
  usage. Only the hook has a hard latency contract.
- **`carta doctor` remote reachability checks**, and documented remote-endpoint
  configuration. Both are part of #106 but independent of this fix.
- **Repointing the Mac at homelab.** Follows this, once merged and released.
