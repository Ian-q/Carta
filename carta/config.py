from pathlib import Path
from typing import Optional
import yaml

REQUIRED_FIELDS = ["project_name", "qdrant_url"]

DEFAULTS = {
    "docs_root": "docs/",
    "stale_threshold_days": 30,
    "needs_input_at_audit_count": 3,
    "anchor_doc": "CLAUDE.md",
    "excluded_paths": [
        "node_modules/", ".venv/", "*.tmp",
        ".planning/", ".worktrees/", ".carta/", ".pio/",
    ],
    "contradiction_types": [
        "version numbers",
        "API endpoints",
        "configuration values",
        "environment variable names",
    ],
    "search": {"top_n": 5},
    "embed": {
        "reference_docs_path": "docs/reference/",
        "audio_path": "docs/audio/",
        "ollama_url": "http://localhost:11434",
        "ollama_model": "nomic-embed-text:latest",
        "ollama_vision_model": "llava:latest",
        "ocr_model": "glm-ocr:latest",  # NEW: for text/table extraction
        "classification": {  # NEW: content classification thresholds
            "text_threshold": 0.70,
            "visual_threshold": 0.40,
        },
        "vision_routing": "auto",  # NEW: auto | ocr | vision | both
        "chunking": {
            "max_tokens": 800,
            "overlap_fraction": 0.15,
            "preserve_tables": True,  # NEW: keep markdown tables whole
        },
    },
    "proactive_recall": {
        "high_threshold": 0.85,
        "low_threshold": 0.60,
        "max_results": 5,
        "judge_timeout_s": 3,
        "ollama_model": "qwen2.5:0.5b",
    },
    "cross_project_recall": {
        "enabled": False,
        "scope": ["quirk"],
        "require_ollama_judge": True,
        "project_filter": {"mode": "all", "projects": []},
        "default_search_scope": "repo",  # "repo" | "shared" | "global"
        "global_pool": {
            "enabled": True,
            "auto_promote": False,
        },
    },
    "modules": {
        "doc_audit": True,
        "doc_embed": True,
        "doc_search": True,
        "session_memory": True,
        "proactive_recall": True,
    },
}


class ConfigError(Exception):
    pass


def load_config(path: Path) -> dict:
    if not path.exists():
        raise ConfigError(f"Config not found: {path}")
    with open(path, encoding="utf-8") as f:
        try:
            raw = yaml.safe_load(f) or {}
        except yaml.YAMLError as e:
            raise ConfigError(f"Invalid YAML in {path}: {e}") from e
    for field in REQUIRED_FIELDS:
        if field not in raw:
            raise ConfigError(f"Missing required field: {field}")
    for field in REQUIRED_FIELDS:
        if not isinstance(raw[field], str) or not raw[field].strip():
            raise ConfigError(f"Field '{field}' must be a non-empty string")
    for key in ("embed", "modules", "search"):
        if key in raw and not isinstance(raw[key], dict):
            raise ConfigError(f"Field '{key}' must be a mapping, got {type(raw[key]).__name__}")
    merged = _deep_merge(DEFAULTS, raw)
    return merged


def collection_name(cfg: dict, type_: str) -> str:
    return f"{cfg['project_name']}_{type_}"


def collection_for_doc_type(cfg: dict, doc_type: str) -> str:
    """Return the collection name for a given doc_type (Plan 999.1-02).

    Maps protected types (quirk, bug-note, helpful-note) to a dedicated _notes collection.
    Maps session type to _session collection.
    Maps all other types (including unknown) to _doc collection.

    Args:
        cfg: carta config dict (must contain project_name).
        doc_type: document type string.

    Returns:
        Collection name (e.g., "myproject_doc", "myproject_notes", "myproject_session").
    """
    if doc_type in ("quirk", "bug-note", "helpful-note"):
        return collection_name(cfg, "notes")
    elif doc_type == "session":
        return collection_name(cfg, "session")
    else:
        return collection_name(cfg, "doc")


def find_config(start: Path = None) -> Path:
    """Walk up from start (or cwd) looking for .carta/config.yaml.

    Args:
        start: directory to begin the search (defaults to cwd).

    Returns:
        Path to the config file.

    Raises:
        FileNotFoundError: if no .carta/config.yaml found up to filesystem root.
    """
    current = (start or Path.cwd()).resolve()
    while True:
        candidate = current / ".carta" / "config.yaml"
        if candidate.exists():
            return candidate
        parent = current.parent
        if parent == current:
            break
        current = parent
    raise FileNotFoundError(
        ".carta/config.yaml not found (searched up to filesystem root). "
        "Run `carta init` first."
    )


def _deep_merge(base: dict, override: dict) -> dict:
    import copy

    result = copy.deepcopy(base)
    for k, v in override.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result


def get_search_scope(cfg: dict) -> str:
    """Get the default search scope from config.
    
    Args:
        cfg: Carta config dict
    
    Returns:
        'repo', 'shared', or 'global'
    """
    return cfg.get("cross_project_recall", {}).get("default_search_scope", "repo")
