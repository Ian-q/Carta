# Technology Stack

**Project:** Carta v0.2 — MCP server + smart hook milestone
**Researched:** 2026-03-25
**Confidence:** MEDIUM-HIGH (SDK version from PyPI; patterns from official docs + community sources)

---

## Existing Stack (Do Not Change)

| Technology | Version | Purpose |
|------------|---------|---------|
| Python | 3.10+ | Runtime |
| qdrant-client | >=1.7 | Vector DB client |
| requests | >=2.31 | HTTP (Ollama API calls) |
| PyMuPDF | >=1.23 | PDF parsing |
| PyYAML | >=6.0 | Config/sidecar files |
| setuptools | >=61.0 | Build backend |
| pipx | system | User install mechanism |

---

## New Dependencies for v0.2

### Core Addition: MCP Python SDK

| Technology | Version | Purpose | Why |
|------------|---------|---------|-----|
| mcp | >=1.7.1 | MCP server + stdio transport | Official Anthropic/MCP SDK. FastMCP is now bundled inside `mcp` (merged in 2024). One package gives you `FastMCP`, `@tool` decorator, and `mcp.run(transport="stdio")`. Do NOT add `fastmcp` as a separate dependency — the standalone `fastmcp` PyPI package (v2.x/3.x by jlowin) is a community fork; use the official `mcp` package instead. |

**Confidence:** HIGH — v1.7.1 confirmed on PyPI (https://pypi.org/project/mcp/1.7.1/); FastMCP bundled confirmed via official GitHub README.

**Key APIs from `mcp`:**
```python
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("carta")

@mcp.tool()
def carta_search(query: str, top_k: int = 5) -> list[dict]:
    """Search embedded project docs by semantic similarity."""
    ...

@mcp.tool()
def carta_embed(path: str) -> dict:
    """Embed a file into the vector store."""
    ...

if __name__ == "__main__":
    mcp.run(transport="stdio")
```

Type hints + docstrings auto-generate the MCP JSON schema. No manual schema required.

### No New Dependencies for Ollama Judge

The Ollama judge reuses the existing `requests` library. No `ollama` PyPI package needed.

```python
import requests

def ollama_judge(query: str, chunk: str, model: str = "qwen2.5:0.5b") -> bool:
    """Returns True if chunk is relevant to query."""
    resp = requests.post(
        "http://localhost:11434/api/generate",
        json={
            "model": model,
            "prompt": f"Is this text relevant to: '{query}'?\n\nText: {chunk}\n\nAnswer yes or no only.",
            "stream": False,
        },
        timeout=10,
    )
    resp.raise_for_status()
    answer = resp.json()["response"].strip().lower()
    return answer.startswith("yes")
```

**Confidence:** HIGH — Ollama `/api/generate` endpoint is stable and already used in codebase for embeddings via the same pattern.

---

## Updated pyproject.toml

Add `mcp>=1.7.1` to `[project.dependencies]` and add a second script entrypoint:

```toml
[project.dependencies]
# ... existing deps ...
"mcp>=1.7.1",

[project.scripts]
carta = "carta.cli:main"
carta-mcp = "carta.mcp_server:main"
```

`carta-mcp` becomes a pipx-installed binary. Claude Code's `.mcp.json` references it directly:

```json
{
  "mcpServers": {
    "carta": {
      "type": "stdio",
      "command": "carta-mcp",
      "args": []
    }
  }
}
```

**Why a separate entrypoint over `carta serve`?** Claude Code launches MCP servers as long-running subprocesses via the binary path. A dedicated `carta-mcp` binary in `$PATH` (installed by pipx) means `.mcp.json` needs no path discovery logic and survives project directory changes. A subcommand (`carta serve`) would also work but requires knowing the full path to `carta` — the dedicated binary is cleaner.

---

## Module Architecture: Shared vs Separate

**Recommendation: shared modules, separate entrypoint file.**

```
carta/
  cli.py              # existing — carta entrypoint
  mcp_server.py       # NEW — carta-mcp entrypoint
  embed/
    pipeline.py       # shared by both CLI and MCP server
  scanner/
    scanner.py        # shared by both CLI and MCP server
  search.py           # shared — Qdrant query logic
  hook/
    inject.py         # NEW — hook logic (similarity threshold + Ollama judge)
```

`mcp_server.py` imports from `carta.embed.pipeline`, `carta.search`, etc. — the same modules the CLI uses. No subprocess boundary between MCP server and business logic. This means:
- Embed pipeline reliability fixes (batch upsert, timeout) benefit both CLI and MCP automatically.
- Single Python process per `carta-mcp` invocation — no inter-process communication complexity.
- Shared config loading from `.carta/config.yaml`.

**What NOT to do:** Do not run the MCP server as a wrapper that shells out to `carta embed` — this doubles process overhead and loses the ability to return structured tool results.

---

## stdio vs SSE Transport

| | stdio | SSE |
|--|-------|-----|
| Claude Code support | Yes (primary) | Yes (remote servers) |
| Use case | Local subprocess | Remote/networked server |
| Security | Process isolation by OS | Requires auth/firewall |
| Latency | ~0ms transport overhead | Network round-trip |
| Complexity | Zero config | Requires HTTP server |

**Use stdio.** Carta is local-only by design. SSE adds HTTP server complexity with no benefit.

---

## Ollama Model Selection for Judge

| Model | Size | Latency (est.) | Recommended? |
|-------|------|---------------|--------------|
| qwen2.5:0.5b | 0.5B | ~200-400ms | YES — first choice |
| qwen2.5:1.5b | 1.5B | ~400-700ms | Fallback if 0.5b quality poor |
| llama3.2:1b | 1B | ~300-500ms | Alternative |
| mistral:7b | 7B | ~2-4s | NO — too slow for hook |
| llama3.1:8b | 8B | ~3-6s | NO — blocks prompt submission |

**Constraint from PROJECT.md:** Hook runs on `UserPromptSubmit` and blocks prompt submission. Budget is ~500ms for the Ollama judge call. Stick to <=1.5B parameter models.

Make model name configurable in `.carta/config.yaml` so users can tune it without code changes.

---

## Alternatives Considered

| Category | Recommended | Alternative | Why Not |
|----------|-------------|-------------|---------|
| MCP library | `mcp>=1.7.1` (official) | `fastmcp` standalone (jlowin) | Community fork, not official; FastMCP already bundled in official `mcp` package |
| MCP transport | stdio | SSE | Local-only tool; SSE adds complexity with no benefit |
| Ollama client | `requests` (existing) | `ollama` PyPI package | Adds a dependency for functionality already covered; `requests` is already present |
| Server architecture | Shared modules, separate entrypoint | Separate process / subprocess wrapper | Subprocess wrapper loses structured return values and doubles overhead |
| Build backend | setuptools (existing) | uv / hatch | No reason to change; setuptools + pipx install already validated |

---

## Installation

No changes to install flow. After adding `mcp>=1.7.1` to deps and `carta-mcp` to scripts:

```bash
pipx install --editable .   # dev
# or
pipx install carta-cc       # production (after publish)
```

Both `carta` and `carta-mcp` land in pipx's bin directory.

---

## Sources

- MCP Python SDK (PyPI): https://pypi.org/project/mcp/1.7.1/ — MEDIUM confidence (version confirmed; internal API from README)
- MCP Python SDK (GitHub): https://github.com/modelcontextprotocol/python-sdk — HIGH confidence
- MCP build-server docs: https://modelcontextprotocol.io/docs/develop/build-server — HIGH confidence
- FastMCP (official, bundled): https://gofastmcp.com/servers/tools — MEDIUM confidence (community docs for official FastMCP)
- Claude Code MCP config: https://code.claude.com/docs/en/mcp — HIGH confidence
- Ollama API: https://github.com/ollama/ollama/blob/main/docs/api.md — HIGH confidence
