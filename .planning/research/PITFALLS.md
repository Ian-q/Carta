# Pitfalls Research

**Domain:** Python stdio MCP server + UserPromptSubmit hook + Ollama judge (Carta v0.2)
**Researched:** 2026-03-25
**Confidence:** MEDIUM-HIGH (confirmed via Claude Code issue tracker, MCP official docs, Ollama community)

---

## Critical Pitfalls

### Pitfall 1: stdout Pollution Breaks the MCP JSON-RPC Stream

**What goes wrong:**
Any `print()` call, startup banner, progress indicator, or uncaught exception traceback written to stdout corrupts the JSON-RPC framing. Claude Code receives malformed bytes and the connection drops with an opaque error (`-32000: Connection Closed`). The server appears to start but every tool call fails immediately.

**Why it happens:**
Developers add debug prints during development and forget they are writing to the MCP wire format itself. Python's logging defaults to stdout. Third-party libraries (Qdrant client, httpx) may emit startup messages to stdout.

**How to avoid:**
- Configure `logging.basicConfig(stream=sys.stderr)` before any import that might log.
- Replace all `print(...)` with `print(..., file=sys.stderr)` or a logger directed at stderr.
- In `carta/mcp_server.py`, add an explicit guard at module top: `sys.stdout = open(os.devnull, 'w')` is too aggressive — instead ensure no library calls `print()` by testing with `MCP_INSPECTOR=1` and watching the raw protocol stream.
- Send user-facing progress via MCP `notifications/message` not print.

**Warning signs:**
- Claude Code shows "MCP server disconnected" immediately after first tool call.
- `claude mcp logs carta` shows garbled JSON or plain text lines mixed into the stream.

**Phase to address:** Phase 1 (MCP server scaffolding) — set up logging discipline before writing any tool handler.

---

### Pitfall 2: Ollama Cold-Start Blocks the UserPromptSubmit Hook

**What goes wrong:**
The hook fires synchronously on `UserPromptSubmit` and blocks prompt submission until it exits. If the Ollama judge model is not loaded in memory (cold start after idle), the first `POST /api/generate` call takes 13–46 seconds for a 0.5B–2B model to load from disk into RAM/VRAM. Claude Code appears frozen. If the hook exceeds its implicit timeout, it may be killed mid-response with no clear error.

**Why it happens:**
Ollama unloads models from memory after `OLLAMA_KEEP_ALIVE` seconds (default: 5 minutes). The hook assumes Ollama is hot. On low-RAM machines the model must also evict whatever was previously loaded.

**How to avoid:**
- Set `OLLAMA_KEEP_ALIVE=-1` (never unload) in the Ollama service configuration so the judge model stays resident.
- Add a liveness pre-check in the hook: call `GET /api/tags` first; if the judge model is absent or Ollama is unreachable, skip to fast-path behavior (inject if >0.85, discard if <0.6, pass through unchanged for gray zone) rather than blocking.
- Keep a warm-up call at hook start that sends a trivially small prompt to force the model into memory, but accept that the first session after system boot may be slow.
- Implement an explicit wall-clock timeout (e.g., 3s) around the Ollama judge call. If it times out, fail open: pass the prompt through without injection rather than blocking indefinitely.

**Warning signs:**
- First prompt after idle takes >10s with no visible activity.
- `htop` shows Ollama process reading from disk during hook execution.
- Users report "Claude Code feels frozen" on the first message of a session.

**Phase to address:** Phase 2 (smart hook implementation) — build the timeout/fallback path before any Ollama integration; never wire the blocking path without it.

---

### Pitfall 3: UserPromptSubmit Hook Not Triggering in Subdirectories or Early in Session

**What goes wrong:**
There are confirmed bugs in Claude Code where `UserPromptSubmit` hooks defined in `~/.claude/settings.json` do not fire when Claude Code is launched from a subdirectory. Additionally, hooks may not trigger for the very first prompt immediately after session start or immediately after context compaction.

**Why it happens:**
Claude Code's hook dispatcher uses the working directory at launch to resolve hook matchers. There is also a race between session initialization and hook registration. These are known bugs in the Claude Code issue tracker (issues #8810, #17277).

**How to avoid:**
- Test hook firing explicitly: launch Claude Code from a project subdirectory and verify injection occurs on the first prompt.
- Add a `carta status` invocation as a fallback — if hook fails silently, the user can still invoke `carta_search` via MCP directly.
- Use `matcher` patterns that are broad enough to not accidentally exclude the project root.
- Do not rely on hook injection being 100% reliable; the MCP pull path (`carta_search`) is the reliable fallback for all cases.

**Warning signs:**
- Logs show hook was registered but context injection never appears in responses.
- Hook works when testing from `~` but not from `~/dev/projectname`.

**Phase to address:** Phase 2 (hook implementation) — write integration tests that launch from a subdirectory path.

---

### Pitfall 4: MCP Server Crash Silently Leaves Tool Calls Failing

**What goes wrong:**
If the Carta MCP server process exits (unhandled exception, OOM, Qdrant connection failure), Claude Code does not automatically restart it. Tool calls return errors but Claude may not surface this clearly to the user. Sessions that were running continue to show `carta_search` in the tool list but every invocation fails. There are no automatic reconnect or restart mechanisms as of early 2025.

**Why it happens:**
Claude Code spawns the stdio process once at session start. If the process dies, the pipe closes. Claude Code has no built-in watchdog for stdio MCP servers.

**How to avoid:**
- Wrap all tool handlers in `try/except` that return a structured error via MCP rather than raising, which would crash the server.
- Handle Qdrant and Ollama connection failures gracefully: return `{"error": "Qdrant unavailable"}` as a tool result, not an unhandled exception.
- Add a top-level exception handler in the server's main loop to log to stderr and attempt graceful shutdown rather than a hard crash.
- Implement `carta status` as a health-check tool — a crash will make even `carta_status` fail, which gives users a clear signal to restart.

**Warning signs:**
- `carta_search` returns errors on every call after working earlier in the session.
- `claude mcp list` shows carta as registered but `carta_search` fails with "process exited."

**Phase to address:** Phase 1 (MCP server scaffolding) — error handling discipline must be established before any tool handler is written.

---

### Pitfall 5: Context Window Pollution From Hook Over-Injection

**What goes wrong:**
Every injected document chunk consumes context window tokens. A hook that injects on every gray-zone prompt (0.6–0.85) with multiple high-scoring chunks can consume 10,000–30,000 tokens before Claude's response, degrading long-session quality and eventually causing context compaction to evict the injected knowledge anyway.

**Why it happens:**
The threshold logic is clear in design but easy to miscalibrate in practice. Gray-zone injection that skips the Ollama judge (e.g., as a temporary simplification) degrades to near-blind injection. Chunk count limits are easy to forget.

**How to avoid:**
- Hard cap injected chunks per prompt: 3–5 chunks maximum regardless of how many exceed the threshold.
- Log injected token count to stderr on every hook invocation during development so you can observe real-world consumption.
- The Ollama judge path (0.6–0.85) should be the noise filter, not an escalation path — if the judge says "not relevant," discard even if similarity is 0.80.
- Measure context window usage in integration tests: inject a representative corpus, run 10 simulated prompts, assert total injected tokens stay below a budget (e.g., 8,000 tokens per session).

**Warning signs:**
- Claude responses reference irrelevant documents that were injected.
- Session context compaction triggers earlier than expected.
- Hook logs show >5 chunks injected per prompt routinely.

**Phase to address:** Phase 2 (smart hook) — threshold calibration and chunk cap must be validated with real documents before shipping.

---

### Pitfall 6: Plugin Cache Residue Conflicts With MCP Registration

**What goes wrong:**
After migrating from plugin-cache skills to MCP tools, stale entries in `~/.claude/plugins/` or leftover `~/.claude/settings.json` skill registrations create a two-registry situation. The old `carta_search` skill (pointing to v0.1.x code) and the new `carta_search` MCP tool coexist. Lexicographic ordering may cause the old skill to win. Users see the old behavior and assume the migration failed.

**Why it happens:**
This is the root cause of Issue #7 in the existing codebase — the plugin cache removal logic is not reliable. Migration leaves orphaned cache entries that Claude Code picks up.

**How to avoid:**
- During migration phase, explicitly verify and remove stale plugin cache entries as part of the `carta init` migration path.
- Add a `carta doctor` check (or extend `carta status`) that detects both a plugin-cache skill registration and an MCP registration for the same tool name, and warns loudly.
- Test migration on a clean profile AND on a profile with v0.1.x previously installed — the latter is where residue appears.
- Document the manual cleanup steps in the migration guide so users with v0.1.x can self-remediate.

**Warning signs:**
- `carta_search` is available but returns v0.1.x behavior (no threshold logic, no judge).
- `~/.claude/plugins/carta/` directory still exists after migration.
- Claude Code shows duplicate tool registrations with the same name.

**Phase to address:** Phase 1 (migration bootstrap) — cache cleanup must be automated, not documented-only.

---

## Technical Debt Patterns

| Shortcut | Immediate Benefit | Long-term Cost | When Acceptable |
|----------|-------------------|----------------|-----------------|
| Skip Ollama judge timeout, block indefinitely | Simpler hook code | First cold-start freezes Claude Code for 30–60s | Never — always implement the timeout |
| Inject all gray-zone results without judge | Avoids Ollama latency | Context pollution degrades long sessions | Never — defeats the noise-gate design |
| print() for hook debug output | Fast debugging | Corrupts MCP wire protocol if hook shares process with server | Never in server process; OK in standalone hook script |
| Single Qdrant query per hook call | Simple | Adds 100–300ms per prompt even for short prompts | OK initially; optimize with embedding cache if needed |
| Re-use v0.1.x embed pipeline without batch upsert | Less initial work | MCP `carta_embed` hangs on dense PDFs (known bug) | Never — reliability fixes are stated prerequisites |

---

## Integration Gotchas

| Integration | Common Mistake | Correct Approach |
|-------------|----------------|------------------|
| Ollama HTTP API | Calling `/api/embeddings` with a batch array | Ollama does not support batch input natively; call per-text sequentially |
| Ollama HTTP API | Using default timeout in `httpx` or `requests` | Ollama cold-start can exceed 30s; set explicit `timeout=60` on first call, `timeout=10` on warm calls |
| Qdrant client | Not setting `prefer_grpc=False` in Docker setup | gRPC is not enabled by default in the standard Qdrant Docker image; use HTTP |
| MCP stdio | Running the server with `uvicorn` or similar ASGI runner | stdio MCP servers must run as plain Python processes (`python -m carta.mcp_server`), not via HTTP servers |
| Claude Code hooks | Hardcoding absolute paths to the hook script | Use `$HOME` or project-relative paths; absolute paths break when project moves |

---

## Performance Traps

| Trap | Symptoms | Prevention | When It Breaks |
|------|----------|------------|----------------|
| Embedding query on every prompt unconditionally | Hook adds 200–500ms per prompt even for trivial inputs ("yes", "ok", "thanks") | Add a minimum prompt length gate (e.g., skip if <20 chars) before calling Qdrant | Immediately — noticeable on every short response |
| Qdrant sequential upsert in `carta_embed` via MCP | `carta_embed` times out on large PDFs, leaving collection partially updated | Batch upsert (32/batch) with per-file timeout — prerequisite fix before MCP exposure | PDFs >50 pages, dense text |
| Loading judge model per hook invocation | Ollama reloads model each time if keep-alive expired | Set `OLLAMA_KEEP_ALIVE=-1` in systemd/launchd service | After any idle period >5 min |

---

## "Looks Done But Isn't" Checklist

- [ ] **MCP server:** Tool handlers return structured errors on Qdrant/Ollama failure — verify by killing Qdrant and calling `carta_search`.
- [ ] **Hook:** Fires correctly when Claude Code is launched from a project subdirectory — verify with `cd src/ && claude`.
- [ ] **Hook:** Falls back gracefully when Ollama is not running — verify by stopping Ollama and submitting a gray-zone prompt.
- [ ] **Migration:** `~/.claude/plugins/carta/` is absent after `carta init` migration — verify on a system with v0.1.x installed.
- [ ] **MCP registration:** `.mcp.json` is present and `claude mcp list` shows `carta` — verify on fresh checkout.
- [ ] **Stdout discipline:** No text reaches stdout during normal MCP operation — verify via `carta/mcp_server.py | cat` and confirm only JSON-RPC frames appear.
- [ ] **Context cap:** Hook injects no more than N chunks per prompt — verify by loading a large corpus and checking hook logs.

---

## Recovery Strategies

| Pitfall | Recovery Cost | Recovery Steps |
|---------|---------------|----------------|
| stdout pollution / broken MCP stream | LOW | Restart Claude Code; add `file=sys.stderr` to all print calls; re-test |
| Ollama cold-start freeze | LOW | Kill hook process; restart Claude Code; set `OLLAMA_KEEP_ALIVE=-1` |
| Plugin cache residue conflict | MEDIUM | Manually delete `~/.claude/plugins/carta/`; restart Claude Code; verify `carta_search` uses MCP version |
| MCP server crash loop (unhandled exception) | MEDIUM | Add top-level try/except in tool handler; check stderr logs for root cause |
| Over-injection / context exhaustion | MEDIUM | Lower chunk cap; re-tune threshold; start new session to clear context |
| Incomplete embed migration (partial upsert) | HIGH | Re-run `carta embed` with batch upsert fix; verify `.embed-meta.yaml` checksums match Qdrant collection |

---

## Pitfall-to-Phase Mapping

| Pitfall | Prevention Phase | Verification |
|---------|------------------|--------------|
| stdout pollution | Phase 1 (MCP scaffolding) | `python -m carta.mcp_server | python -c "import sys; [print(l) for l in sys.stdin]"` — all lines are valid JSON |
| Ollama cold-start block | Phase 2 (smart hook) | Time hook execution with Ollama cold; assert <3s with fallback path |
| Hook subdirectory bug | Phase 2 (smart hook) | Integration test launching from subdirectory |
| MCP server crash recovery | Phase 1 (MCP scaffolding) | Kill Qdrant mid-call; assert structured error returned, not crash |
| Context over-injection | Phase 2 (smart hook) | Load 100-doc corpus; assert <8K tokens injected across 10 prompts |
| Plugin cache residue | Phase 1 (migration bootstrap) | Install v0.1.x; run migration; assert plugins directory absent |
| Ollama keep-alive misconfiguration | Phase 2 (smart hook) | Document required env vars in `carta doctor` output |

---

## Sources

- [Claude Code Hooks Reference](https://code.claude.ai/docs/en/hooks)
- [UserPromptSubmit subdirectory bug — Issue #8810](https://github.com/anthropics/claude-code/issues/8810)
- [UserPromptSubmit not triggering consistently — Issue #17277](https://github.com/anthropics/claude-code/issues/17277)
- [Worker startup blocks Claude Code 15s — claude-mem Issue #729](https://github.com/thedotmack/claude-mem/issues/729)
- [MCP server 16+ hour hang, no timeout detection — Issue #15945](https://github.com/anthropics/claude-code/issues/15945)
- [MCP stdio stdout pollution — modelcontextprotocol.io build-server](https://modelcontextprotocol.io/docs/develop/build-server)
- [Understanding MCP stdio transport — Medium](https://medium.com/@laurentkubaski/understanding-mcp-stdio-transport-protocol-ae3d5daf64db)
- [Ollama cold start latency — acecloud.ai](https://acecloud.ai/blog/cold-start-latency-llm-inference/)
- [Ollama keep-alive preload guide — Medium](https://medium.com/@rafal.kedziorski/speed-up-ollama-how-i-preload-local-llms-into-ram-for-lightning-fast-ai-experiments-291a832edd48)
- [MCP context window tool overhead — morphllm.com](https://www.morphllm.com/claude-code-skills-mcp-plugins)
- Project knowledge: `.planning/PROJECT.md` (v0.1.x plugin cache issue #7, known bugs)

---
*Pitfalls research for: Carta v0.2 — MCP server + smart hook + Ollama judge*
*Researched: 2026-03-25*
