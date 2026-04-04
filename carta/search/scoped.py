"""Collection scoping logic for multi-project search.

Determines which Qdrant collections to search based on scope (repo/shared/global)
and cross-project recall configuration.
"""
import re
from typing import Optional
import requests

# Pattern: {project_name}_{type} or carta_global_{type}
CARTA_COLLECTION_PATTERN = re.compile(
    r"^(?:(\w+)_(doc|notes|session|visual)|carta_global_(doc|notes|session|visual))$"
)

# Valid collection types
COLLECTION_TYPES = ["doc", "notes", "session", "visual"]


def get_search_collections(cfg: dict, scope: str = "repo") -> list[str]:
    """Return list of collection names to search based on scope.
    
    Args:
        cfg: Carta config dict with project_name and cross_project_recall
        scope: Search scope - "repo" (default), "shared", or "global"
    
    Returns:
        List of collection names to query
    
    Raises:
        ValueError: If scope is not one of the valid values
    """
    if scope not in ("repo", "shared", "global"):
        raise ValueError(f"Invalid scope: {scope}. Must be 'repo', 'shared', or 'global'.")
    
    project_name = cfg.get("project_name", "carta-project")
    
    # Check if ColPali visual embedding is enabled
    colpali_enabled = cfg.get("embed", {}).get("colpali_enabled", False)
    
    # Filter collection types based on config
    if scope == "global":
        # Global scope: only carta_global_* collections
        types = COLLECTION_TYPES.copy()
        if not colpali_enabled:
            types.remove("visual")  # Skip visual if ColPali not enabled
        return [f"carta_global_{t}" for t in types]
    
    if scope == "repo":
        # Repo scope: only current project collections
        types = COLLECTION_TYPES.copy()
        if not colpali_enabled:
            types.remove("visual")  # Skip visual if ColPali not enabled
        return [f"{project_name}_{t}" for t in types]
    
    # scope == "shared"
    cross_project = cfg.get("cross_project_recall", {})
    
    if not cross_project.get("enabled", False):
        # Cross-project disabled: behave like repo scope
        return [f"{project_name}_{t}" for t in COLLECTION_TYPES]
    
    # Cross-project enabled: discover and filter
    qdrant_url = cfg.get("qdrant_url", "http://localhost:6333")
    all_collections = discover_collections(qdrant_url)
    
    filter_config = cross_project.get("project_filter", {"mode": "all", "projects": []})
    permitted = filter_by_permission(all_collections, project_name, filter_config)
    
    return permitted


def discover_collections(qdrant_url: str, timeout: int = 5) -> list[str]:
    """Discover all Carta collections from Qdrant.
    
    Queries the /collections endpoint and filters to only Carta-related
    collections (matching project_* or carta_global_* patterns).
    
    Args:
        qdrant_url: Base URL of Qdrant instance
        timeout: Request timeout in seconds
    
    Returns:
        List of Carta collection names
    """
    try:
        response = requests.get(
            f"{qdrant_url}/collections",
            timeout=timeout
        )
        response.raise_for_status()
        data = response.json()
        
        collections = data.get("result", {}).get("collections", [])
        names = [c.get("name") for c in collections if c.get("name")]
        
        # Filter to only Carta collections
        return [n for n in names if _is_carta_collection(n)]
        
    except Exception:
        # Fail gracefully: return empty list on any error
        return []


def filter_by_permission(
    all_collections: list[str],
    current_project: str,
    filter_config: dict
) -> list[str]:
    """Filter collections based on cross_project_recall configuration.
    
    Args:
        all_collections: All discovered Carta collection names
        current_project: Current project name (always included)
        filter_config: {"mode": "all"|"include"|"exclude", "projects": []}
    
    Returns:
        Filtered list of collection names
    """
    mode = filter_config.get("mode", "all")
    projects = filter_config.get("projects", [])
    
    # Extract project names from collection names
    def get_project(collection_name: str) -> Optional[str]:
        match = CARTA_COLLECTION_PATTERN.match(collection_name)
        if match:
            # Group 1 is project name for project_* pattern, None for carta_global_*
            return match.group(1) or "carta_global"
        return None
    
    # Always include current project and global collections
    current_project_collections = [
        c for c in all_collections 
        if get_project(c) == current_project or get_project(c) == "carta_global"
    ]
    
    if mode == "all":
        # Include all discovered Carta collections
        return all_collections
    
    if mode == "include":
        # Include only specified projects + current + global
        permitted_projects = set(projects) | {current_project, "carta_global"}
        return [
            c for c in all_collections
            if get_project(c) in permitted_projects
        ]
    
    if mode == "exclude":
        # Exclude specified projects, but always keep current + global
        excluded_projects = set(projects)
        return [
            c for c in all_collections
            if get_project(c) == current_project 
            or get_project(c) == "carta_global"
            or get_project(c) not in excluded_projects
        ]
    
    # Unknown mode: default to all
    return all_collections


def _is_carta_collection(name: str) -> bool:
    """Check if a collection name follows Carta naming conventions.
    
    Args:
        name: Collection name to check
    
    Returns:
        True if name matches project_* or carta_global_* patterns
    """
    if not name:
        return False
    return CARTA_COLLECTION_PATTERN.match(name) is not None


def get_global_collections() -> list[str]:
    """Return the list of global collection names.
    
    Returns:
        List of carta_global_* collection names
    """
    return [f"carta_global_{t}" for t in COLLECTION_TYPES]
