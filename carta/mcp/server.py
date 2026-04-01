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
from typing import Literal, Optional

from carta.config import find_config, load_config, ConfigError
from carta.embed.pipeline import run_search, run_embed_file, discover_stale_files, run_embed, FILE_TIMEOUT_S
from carta.scanner.scanner import check_embed_induction_needed, check_embed_drift
from carta.search.scoped import get_search_collections

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
def carta_search(
    query: str,
    top_k: int = 5,
    scope: Literal["repo", "shared", "global"] = "repo",
) -> list[dict] | dict:
    """Search embedded project documentation for chunks relevant to query.

    Args:
        query: Natural language search query.
        top_k: Maximum number of results to return (default 5).
        scope: Search scope - "repo" (current project only), "shared" (project + 
               permitted cross-project), or "global" (global collections only).

    Returns:
        List of result dicts with score, source path, and excerpt.
        On failure, returns {"error": "<type>", "detail": "<message>"}.
    """
    try:
        cfg = _load_cfg()
    except (ConfigError, FileNotFoundError) as e:
        return {"error": "service_unavailable", "detail": str(e)}
    try:
        # Get collections to search based on scope
        collections = get_search_collections(cfg, scope)
        
        # Search across all collections and merge results
        all_results = []
        for coll_name in collections:
            try:
                results = _run_search_collection(query, cfg, coll_name, top_k)
                all_results.extend(results)
            except RuntimeError:
                # Skip collections that don't exist or fail
                pass
        
        # Sort by score descending and take top_k
        all_results.sort(key=lambda x: x["score"], reverse=True)
        results = all_results[:top_k]
        
    except ValueError as e:
        # Invalid scope parameter
        return {"error": "invalid_request", "detail": str(e)}
    except Exception as e:
        _logger.warning("carta_search unexpected error: %s", e)
        return {"error": "service_unavailable", "detail": str(e)}
    return [
        {"score": round(r["score"], 4), "source": r["source"], "excerpt": r["excerpt"][:300]}
        for r in results
    ]


def _run_search_collection(query: str, cfg: dict, collection_name: str, top_n: int) -> list[dict]:
    """Search a single collection for chunks semantically similar to query.
    
    Args:
        query: Natural language search query.
        cfg: Carta config dict.
        collection_name: Name of the Qdrant collection to search.
        top_n: Maximum number of results.
    
    Returns:
        List of dicts: {"score": float, "source": str, "excerpt": str}
    """
    from qdrant_client import QdrantClient
    from carta.embed.ollama_client import get_embedding
    
    ollama_url = cfg["embed"]["ollama_url"]
    model = cfg["embed"]["ollama_model"]
    
    client = QdrantClient(url=cfg["qdrant_url"], timeout=10)
    query_vec = get_embedding(query, ollama_url=ollama_url, model=model, prefix="search_query: ")
    
    try:
        response = client.query_points(
            collection_name=collection_name,
            query=query_vec,
            limit=top_n,
            with_payload=True,
        )
    except Exception as e:
        raise RuntimeError(f"Qdrant search failed for {collection_name}: {e}") from e
    
    hits = []
    for r in response.points:
        payload = r.payload or {}
        hits.append({
            "score": r.score,
            "source": payload.get("file_path", payload.get("slug", "")),
            "excerpt": payload.get("text", ""),
        })
    return hits


@mcp_server.tool()
def carta_embed(
    scope: Literal["stale", "file", "all"] = "all",
    path: Optional[str] = None,
    force: bool = False,
) -> dict:
    """Embed files into the project's vector store with targeted scope control.

    Args:
        scope: Embedding scope — "all" (full collection), "file" (single file), or "stale" (stale files).
        path: Path to the file to embed (required when scope='file'). Relative or absolute.
        force: If True, re-embed even if file has not changed since last embed.

    Returns:
        {"status": "ok", ...} on success with scope-specific fields.
        {"error": "<type>", "detail": "..."} on failure.
    """
    try:
        cfg = _load_cfg()
    except (ConfigError, FileNotFoundError) as e:
        return {"error": "service_unavailable", "detail": str(e)}

    # Backward compat: if scope is not in valid enum and path is None, treat scope as path
    if scope not in ("stale", "file", "all") and path is None:
        path = scope
        scope = "file"

    # scope='file' path
    if scope == "file":
        if path is None:
            return {"error": "invalid_request", "detail": "path is required when scope='file'"}
        file_path = Path(path)
        if not file_path.is_absolute():
            try:
                file_path = _repo_root_from_cfg() / file_path
            except (FileNotFoundError, ConfigError) as e:
                return {"error": "service_unavailable", "detail": str(e)}
        try:
            result = run_embed_file(file_path, cfg, force=force, verbose=False)
            result["scope"] = "file"
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
            _logger.warning("carta_embed scope=file unexpected error: %s", e)
            return {"error": "service_unavailable", "detail": str(e)}

    # scope='stale' path
    if scope == "stale":
        try:
            repo_root = _repo_root_from_cfg()
            stale_files = discover_stale_files(repo_root)
            reembedded = 0
            for stale_file in stale_files:
                try:
                    result = run_embed_file(stale_file, cfg, force=force, verbose=False)
                    if result.get("status") in ("ok", "embedded"):
                        reembedded += 1
                except Exception as e:
                    _logger.warning("Error re-embedding stale file %s: %s", stale_file, e)
            return {"status": "ok", "scope": "stale", "reembedded": reembedded}
        except (ConfigError, FileNotFoundError) as e:
            return {"error": "service_unavailable", "detail": str(e)}
        except Exception as e:
            _logger.warning("carta_embed scope=stale unexpected error: %s", e)
            return {"error": "service_unavailable", "detail": str(e)}

    # scope='all' path (default)
    if scope == "all":
        try:
            repo_root = _repo_root_from_cfg()
            result = run_embed(repo_root, cfg, verbose=False)
            return result
        except (ConfigError, FileNotFoundError) as e:
            return {"error": "service_unavailable", "detail": str(e)}
        except RuntimeError as e:
            detail = str(e)
            if "collection" in detail.lower() and "not found" in detail.lower():
                return {"error": "collection_not_found", "detail": detail}
            return {"error": "service_unavailable", "detail": detail}
        except Exception as e:
            _logger.warning("carta_embed scope=all unexpected error: %s", e)
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
