from pathlib import Path
import yaml

REQUIRED_FIELDS = ["project_name", "qdrant_url"]

DEFAULTS = {
    "docs_root": "docs/",
    "stale_threshold_days": 30,
    "needs_input_at_audit_count": 3,
    "anchor_doc": "CLAUDE.md",
    "excluded_paths": ["node_modules/", ".venv/", "*.tmp"],
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
        "chunking": {"max_tokens": 800, "overlap_fraction": 0.15},
    },
    "proactive_recall": {
        "similarity_threshold": 0.78,
        "max_results": 3,
        "ollama_judge": False,
        "ollama_model": "phi3.5-mini",
    },
    "cross_project_recall": {
        "enabled": False,
        "scope": ["quirk"],
        "require_ollama_judge": True,
        "project_filter": {"mode": "all", "projects": []},
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
    return f"{cfg['project_name']}:{type_}"


def _deep_merge(base: dict, override: dict) -> dict:
    import copy

    result = copy.deepcopy(base)
    for k, v in override.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result
