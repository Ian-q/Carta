import pytest


@pytest.fixture
def minimal_cfg():
    """Canonical minimal config dict for tests across the carta package."""
    return {
        "project_name": "test-project",
        "qdrant_url": "http://localhost:6333",
        "docs_root": "docs/",
        "stale_threshold_days": 30,
        "needs_input_at_audit_count": 3,
        "anchor_doc": "CLAUDE.md",
        "excluded_paths": ["node_modules/", ".venv/", "*.tmp"],
        "contradiction_types": ["version numbers"],
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


@pytest.fixture
def minimal_config_yaml():
    """Canonical minimal config as a YAML string for tests that need file content."""
    return (
        "project_name: test-project\n"
        "qdrant_url: http://localhost:6333\n"
    )
