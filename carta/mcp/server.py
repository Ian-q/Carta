"""Carta MCP server — stdio JSON-RPC transport.

Wire-protocol discipline:
- stdout is RESERVED for JSON-RPC framing. Never call print() in this module.
- All log output goes to stderr via the logging module.
- Never call sys.exit() — return structured errors instead.
- Tool handlers (Phase 2) must catch all exceptions and return error dicts.
"""
import base64
import concurrent.futures
import logging
import sys
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from typing import Literal, Optional, Union

from carta.config import find_config, load_config, ConfigError
from carta.embed.pipeline import run_search, run_focus, run_embed_file, discover_stale_files, run_embed, FILE_TIMEOUT_S
from carta.embed.lock import embed_lock, EmbedLockHeld
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

    Searches both text collections (standard embedding) and visual collections
    (ColPali late-interaction retrieval when enabled). Visual results include
    image data that can be displayed by vision-capable clients.

    Args:
        query: Natural language search query.
        top_k: Maximum number of results to return (default 5).
        scope: Search scope - "repo" (current project only), "shared" (project + 
               permitted cross-project), or "global" (global collections only).

    Returns:
        List of result dicts. Text results: {score, source, excerpt}.
        Visual results: {score, source, type, image_b64, excerpt}.
        On failure, returns {"error": "<type>", "detail": "<message>"}.
    """
    try:
        cfg = _load_cfg()
        repo_root = _repo_root_from_cfg()
    except (ConfigError, FileNotFoundError) as e:
        return {"error": "service_unavailable", "detail": str(e)}
    
    try:
        # Get collections to search based on scope
        collections = get_search_collections(cfg, scope)
        
        # Search across all collections and merge results
        all_results = []
        for coll_name in collections:
            try:
                # Check if this is a visual collection
                if coll_name.endswith("_visual"):
                    # Search visual collection using ColPali late-interaction
                    results = _run_search_visual_collection(
                        query, cfg, coll_name, top_k, repo_root
                    )
                    all_results.extend(results)
                else:
                    # Search text collection using standard embedding
                    results = _run_search_collection(query, cfg, coll_name, top_k)
                    # Mark results with type for downstream processing
                    for r in results:
                        r["type"] = "text"
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
    
    # Format results for return
    formatted = []
    for r in results:
        result = {
            "score": round(r["score"], 4),
            "source": r["source"],
            "excerpt": r["excerpt"][:300],
        }
        # Include type if present
        if r.get("type") == "visual":
            result["type"] = "visual"
            if r.get("image_b64"):
                result["image_b64"] = r["image_b64"]
        formatted.append(result)
    
    return formatted


def carta_focus(source: str, query: str = "", top_k: int = 15) -> list[dict] | dict:
    """Go deep in ONE already-located file: page-anchored passages, an outline, and
    table/figure pages returned as images.

    Use AFTER carta_search has identified the relevant file — pass that result's `source`
    string here. With an EMPTY query, returns the file's section/page outline (a synthetic
    table of contents) so you can choose where to read.

    Args:
        source: Repo-relative file path (the `source` from a carta_search result; a
                trailing " (page N)" from a visual result is fine — it is stripped).
        query: Natural-language query. Empty string => outline mode.
        top_k: Maximum passages to return (default 15).

    Returns:
        List of dicts: {score, source, page, section_heading, excerpt, type, image_b64?}.
        `image_b64` is present on visual (table/figure) hits. On failure:
        {"error": "<type>", "detail": "<message>"}.
    """
    try:
        cfg = _load_cfg()
    except (ConfigError, FileNotFoundError) as e:
        return {"error": "service_unavailable", "detail": str(e)}

    try:
        results = run_focus(source, cfg, query=query, limit=top_k)
    except RuntimeError as e:
        return {"error": "service_unavailable", "detail": str(e)}
    except Exception as e:
        _logger.warning("carta_focus unexpected error: %s", e)
        return {"error": "service_unavailable", "detail": str(e)}

    formatted = []
    for r in results:
        item = {
            "score": round(r.get("score", 0.0), 4),
            "source": r.get("source", ""),
            "page": r.get("page"),
            "section_heading": r.get("section_heading", ""),
            "excerpt": (r.get("excerpt") or "")[:300],
            "type": r.get("type", "text"),
        }
        if r.get("image_b64"):
            item["image_b64"] = r["image_b64"]
        formatted.append(item)
    return formatted


# Registered via add_tool() rather than the @mcp_server.tool() decorator the sibling
# tools use. This is registration-equivalent (FastMCP's tool() decorator just calls
# add_tool(fn) then returns fn), but it keeps carta_focus a plain, callable function in
# this module's namespace. The test suite mocks out mcp.server.fastmcp, so the decorator
# would replace carta_focus with a MagicMock and make it un-unit-testable. Do not "fix"
# this back to the decorator without first making the carta_focus tests patch the mock.
mcp_server.add_tool(carta_focus)


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
    from carta.embed.embed import get_embedding
    
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


def _run_search_visual_collection(
    query: str,
    cfg: dict,
    collection_name: str,
    top_n: int,
    repo_root: Path,
) -> list[dict]:
    """Search a visual collection using ColPali late-interaction retrieval.

    Args:
        query: Natural language search query.
        cfg: Carta config dict.
        collection_name: Name of the visual Qdrant collection (e.g., "project_visual").
        top_n: Maximum number of results.
        repo_root: Repository root path for resolving image paths.

    Returns:
        List of dicts with score, source, and image data for visual results.
    """
    from qdrant_client import QdrantClient
    from qdrant_client.models import Filter
    from carta.embed.colpali import is_colpali_available, ColPaliEmbedder, ColPaliError

    if not is_colpali_available():
        _logger.warning("ColPali not available, skipping visual collection search")
        return []

    embed_cfg = cfg.get("embed", {})
    model_name = embed_cfg.get("colpali_model", "vidore/colqwen2-v1.0")
    device = embed_cfg.get("colpali_device", "cpu")

    try:
        # Initialize ColPali embedder for query encoding
        embedder = ColPaliEmbedder(
            model_name=model_name,
            device=device,
            batch_size=1,
        )

        # Encode the query text as multi-vector patches
        query_vectors = embedder.embed_query(query)
        
        # Convert to list format for Qdrant multi-vector query
        if hasattr(query_vectors, "tolist"):
            query_vector_list = query_vectors.tolist()
        else:
            query_vector_list = list(query_vectors)

        # Search the visual collection using late-interaction MaxSim
        client = QdrantClient(url=cfg["qdrant_url"], timeout=10)
        
        try:
            response = client.query_points(
                collection_name=collection_name,
                query=query_vector_list,  # Multi-vector query for MaxSim
                using="colpali",  # Specify the multi-vector field
                limit=top_n,
                with_payload=True,
            )
        except Exception as e:
            _logger.warning("Qdrant visual search failed for %s: %s", collection_name, e)
            return []

        # Format results with image data
        hits = []
        for r in response.points:
            payload = r.payload or {}
            png_path_str = payload.get("png_path", "")
            
            # Load the image if path is available
            image_b64 = ""
            if png_path_str:
                png_path = repo_root / png_path_str
                if png_path.exists():
                    image_b64 = _load_image_as_base64(png_path)
            
            hits.append({
                "score": r.score,
                "source": f"{payload.get('file_path', payload.get('slug', ''))} (page {payload.get('page_num', '?')})",
                "excerpt": f"Visual match from page {payload.get('page_num', '?')}",
                "type": "visual",
                "image_b64": image_b64,
                "page_num": payload.get("page_num"),
                "png_path": png_path_str,
            })
        
        return hits

    except ColPaliError as e:
        _logger.warning("ColPali query encoding failed: %s", e)
        return []
    except Exception as e:
        _logger.warning("Visual search failed for %s: %s", collection_name, e)
        return []


def _load_image_as_base64(png_path: Path) -> str:
    """Load a PNG file and return as base64-encoded string.

    Args:
        png_path: Path to the PNG file.

    Returns:
        Base64-encoded image data.
    """
    try:
        with open(png_path, "rb") as f:
            return base64.b64encode(f.read()).decode("utf-8")
    except Exception as e:
        _logger.warning("Failed to load image %s: %s", png_path, e)
        return ""


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

    # Single-writer lock (audit CA-5/12): carta_embed mutates the same Qdrant
    # collections as the CLI. Without the lock, an agent embedding here while the
    # maintainer runs `carta embed` (the ET-embed scenario) — or two MCP calls —
    # would race their cleanup-deletes and drop freshly-written points. Hold the
    # shared .carta/embed.lock and refuse with a busy error rather than corrupt data.
    try:
        repo_root = _repo_root_from_cfg()
    except (FileNotFoundError, ConfigError) as e:
        return {"error": "service_unavailable", "detail": str(e)}
    lock_path = repo_root / ".carta" / "embed.lock"
    try:
        with embed_lock(lock_path):
            return _carta_embed_run(scope, path, force, cfg)
    except EmbedLockHeld as e:
        return {"error": "busy", "detail": f"another embed is already running (PID {e.pid}); retry shortly"}


def _carta_embed_run(scope, path, force, cfg) -> dict:
    """Run the scope-specific embed. The caller holds the single-writer embed lock."""
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


def _remember(text: str, *, note_type: str = "helpful-note", title: str = "",
              tags: list[str] | None = None) -> dict:
    """Plain-function core for carta_remember (kept undecorated for testability)."""
    try:
        cfg = _load_cfg()
        repo_root = _repo_root_from_cfg()
    except (ConfigError, FileNotFoundError) as e:
        return {"error": "service_unavailable", "detail": str(e)}
    try:
        from carta.memory.capture import capture_note
        result = capture_note(cfg, repo_root, text, note_type=note_type,
                              title=title, tags=tags)
        return {"status": "ok", **result}
    except ValueError as e:
        return {"error": "invalid_request", "detail": str(e)}
    except Exception as e:
        _logger.warning("carta_remember error: %s", e)
        return {"error": "service_unavailable", "detail": str(e)}


@mcp_server.tool()
def carta_remember(
    text: str,
    note_type: str = "helpful-note",
    title: str = "",
    tags: list[str] | None = None,
) -> dict:
    """Save a curated project note as a repo markdown file and embed it for search.

    Use when you learn something durable about THIS project worth remembering across
    sessions: note_type="quirk" for surprising system/hardware behavior,
    "bug-note" for bug-investigation findings, "helpful-note" for other durable
    knowledge. The note lands in docs/quirks/ or docs/notes/ (git-shareable) and is
    immediately retrievable via carta_search and proactive recall.

    Returns:
        {"status": "ok", "path", "collection", "chunks"} or {"error", "detail"}.
    """
    return _remember(text, note_type=note_type, title=title, tags=tags)


def main() -> None:
    """Entry point for carta-mcp command."""
    mcp_server.run()


if __name__ == "__main__":
    main()
