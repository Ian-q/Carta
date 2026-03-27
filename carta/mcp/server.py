"""Carta MCP server — stdio JSON-RPC transport.

Wire-protocol discipline:
- stdout is RESERVED for JSON-RPC framing. Never call print() in this module.
- All log output goes to stderr via the logging module.
- Never call sys.exit() — return structured errors instead.
- Tool handlers (Phase 2) must catch all exceptions and return error dicts.
"""
import concurrent.futures
import logging
import sys
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from carta.config import find_config, load_config, ConfigError
from carta.embed.pipeline import run_search, run_embed_file, FILE_TIMEOUT_S
from carta.scanner.scanner import check_embed_induction_needed, check_embed_drift

# Direct ALL log output to stderr — stdout is reserved for JSON-RPC framing
logging.basicConfig(
    stream=sys.stderr,
    level=logging.WARNING,
    format="%(levelname)s [carta-mcp] %(message)s",
)

_logger = logging.getLogger(__name__)

mcp_server = FastMCP("carta")


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def _load_cfg() -> dict:
    """Load carta config from nearest .carta/config.yaml ancestor.

    Raises ConfigError or FileNotFoundError if not found.
    """
    return load_config(find_config())


def _repo_root_from_cfg() -> Path:
    """Derive repo root from config file location (.carta is one level deep)."""
    return find_config().parent.parent


# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------

@mcp_server.tool()
def carta_search(query: str, top_k: int = 5) -> list[dict] | dict:
    """Search embedded project documentation for chunks relevant to query.

    Args:
        query: Natural language search query.
        top_k: Maximum number of results to return (default 5).

    Returns:
        List of result dicts with score, source path, and excerpt.
        On failure, returns {"error": "<type>", "detail": "<message>"}.
    """
    try:
        cfg = _load_cfg()
    except (ConfigError, FileNotFoundError) as e:
        return {"error": "service_unavailable", "detail": str(e)}
    try:
        results = run_search(query, cfg, verbose=False)
    except RuntimeError as e:
        detail = str(e)
        if "collection" in detail.lower() and "not found" in detail.lower():
            return {"error": "collection_not_found", "detail": detail}
        return {"error": "service_unavailable", "detail": detail}
    except Exception as e:
        _logger.warning("carta_search unexpected error: %s", e)
        return {"error": "service_unavailable", "detail": str(e)}
    return [
        {"score": round(r["score"], 4), "source": r["source"], "excerpt": r["excerpt"][:300]}
        for r in results[:top_k]
    ]


@mcp_server.tool()
def carta_embed(path: str, force: bool = False) -> dict:
    """Embed a single file into the project's vector store.

    Args:
        path: Path to the file to embed (absolute or relative to project root).
        force: If True, re-embed even if file has not changed since last embed.

    Returns:
        {"status": "ok", "chunks": N} on success.
        {"status": "skipped", "reason": "..."} if already current.
        {"error": "<type>", "detail": "..."} on failure.
    """
    try:
        cfg = _load_cfg()
    except (ConfigError, FileNotFoundError) as e:
        return {"error": "service_unavailable", "detail": str(e)}
    file_path = Path(path)
    if not file_path.is_absolute():
        try:
            file_path = _repo_root_from_cfg() / file_path
        except (FileNotFoundError, ConfigError) as e:
            return {"error": "service_unavailable", "detail": str(e)}
    try:
        result = run_embed_file(file_path, cfg, force=force, verbose=False)
        return result
    except FileNotFoundError as e:
        return {"error": "file_not_found", "detail": str(e)}
    except concurrent.futures.TimeoutError:
        return {"error": "timeout", "detail": f"Embed exceeded {FILE_TIMEOUT_S}s timeout for {path}"}
    except RuntimeError as e:
        detail = str(e)
        if "collection" in detail.lower() and "not found" in detail.lower():
            return {"error": "collection_not_found", "detail": detail}
        return {"error": "service_unavailable", "detail": detail}
    except Exception as e:
        _logger.warning("carta_embed unexpected error: %s", e)
        return {"error": "service_unavailable", "detail": str(e)}


@mcp_server.tool()
def carta_scan() -> dict:
    """Scan project for files pending embed or drifted since last embed.

    Returns:
        {"pending": ["path/a.pdf", ...], "drift": ["path/b.pdf", ...]}
        On failure: {"error": "<type>", "detail": "..."}.
    """
    try:
        cfg = _load_cfg()
        repo_root = _repo_root_from_cfg()
    except (ConfigError, FileNotFoundError) as e:
        return {"error": "service_unavailable", "detail": str(e)}
    try:
        pending_issues = check_embed_induction_needed(repo_root, cfg)
        pending = [issue["doc"] for issue in pending_issues]
        drift_issues = check_embed_drift(repo_root, cfg)
        drift = [issue["doc"] for issue in drift_issues]
        return {"pending": pending, "drift": drift}
    except Exception as e:
        _logger.warning("carta_scan unexpected error: %s", e)
        return {"error": "service_unavailable", "detail": str(e)}


def main() -> None:
    """Entry point for carta-mcp command."""
    mcp_server.run()


if __name__ == "__main__":
    main()
