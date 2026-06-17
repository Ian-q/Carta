from pathlib import Path
from typing import Optional
import os
import yaml


def ollama_keep_alive() -> str:
    """How long Ollama keeps a model resident after a request (its ``keep_alive``).

    Default ``"10m"`` (Ollama's own default is 5m), overridable via
    ``CARTA_OLLAMA_KEEP_ALIVE``. ``"-1"`` keeps models loaded indefinitely; ``"0"``
    unloads immediately. Applied to every carta Ollama request (embed, rerank, hook
    judge) so a model doesn't reload across idle gaps — notably the prompt-submit
    hook, whose calls can be minutes apart.
    """
    return os.environ.get("CARTA_OLLAMA_KEEP_ALIVE", "10m")

REQUIRED_FIELDS = ["project_name", "qdrant_url"]

# Curated note types — routed to {project}_notes and labeled in search output.
# Keep in sync with PROTECTED_DOC_TYPES in carta/embed/lifecycle.py (orphan-cleanup guard).
NOTE_DOC_TYPES = ("quirk", "bug-note", "helpful-note")

DEFAULTS = {
    "docs_root": "docs/",
    "stale_threshold_days": 30,
    "needs_input_at_audit_count": 3,
    "anchor_doc": "CLAUDE.md",
    "excluded_paths": [
        "node_modules/", ".venv/", "*.tmp",
        ".planning/", ".worktrees/", ".claude/worktrees/", ".carta/", ".pio/",
        "build/", "temp/",
    ],
    "contradiction_types": [
        "version numbers",
        "API endpoints",
        "configuration values",
        "environment variable names",
    ],
    "memory": {
        "quirks_dir": "docs/quirks",     # note_type: quirk
        "notes_dir": "docs/notes",       # note_type: bug-note, helpful-note
    },
    "search": {
        "top_n": 5,
        "hybrid": {
            "enabled": True,
            "bm25_model": "Qdrant/bm25",
            "prefetch_limit": 40,
        },
        "rerank": {
            "enabled": False,
            "backend": "cross-encoder",   # cross-encoder | llm
            "model": "BAAI/bge-reranker-base",   # used when backend=cross-encoder
            "llm_model": "qwen3.5:0.8b",  # used when backend=llm (local Ollama)
            "llm_timeout_s": 20,
            "candidate_pool": 30,
        },
        "graph": {
            # Opt-in: undirected 1-hop related: expansion that promotes graph-adjacent
            # deep docs into the rerank pool. Measured neutral on a dense-reranker corpus
            # (a strong reranker already floats in-pool docs); may help sparser-ranking
            # corpora with rich related: graphs. Enable per-project.
            "enabled": False,
            "hops": 1,              # related: traversal depth
            "seed_count": 10,       # how many top fused hits seed the walk
            "candidate_depth": 50,  # deep-fetch size when graph expansion is enabled
        },
        "fusion": {
            # Ceiling on the visual (_visual/ColPali) collection's share of the fused
            # candidate pool, as a fraction of pool size (cap = round(ratio * pool)).
            # RRF interleaves text and visual ~1:1 by rank, which halves text depth on
            # every query once a _visual collection exists; this bounds visual so text
            # questions keep their depth. 1.0 disables the cap (legacy behaviour). No
            # effect on pure-text corpora. Eval-swept optimum (ET-embed 62q hybrid
            # 0.839->0.887, visual 14q held at 0.857, reranked neutral at 0.935) —
            # see RESULTS.md 2026-06-13.
            "visual_max_ratio": 0.2,
        },
    },
    "embed": {
        "reference_docs_path": "docs/reference/",
        "audio_path": "docs/audio/",
        "ollama_url": "http://localhost:11434",
        "ollama_model": "nomic-embed-text:latest",
        "ollama_vision_model": "qwen3-vl:8b",  # requires Ollama >=0.12.7
        "ocr_model": "glm-ocr:latest",  # NEW: for text/table extraction
        "classification": {  # NEW: content classification thresholds
            "text_threshold": 0.70,
            "visual_threshold": 0.40,
        },
        "vision_routing": "auto",  # NEW: auto | ocr | vision | both
        "vision_text_min_chars": 150,      # below → FLATTENED
        "vision_text_max_chars": 600,      # above → captions are cross-refs, skip
        "vision_flattened_min_yield": 50,  # GLM-OCR chars below this → LLaVA fallback
        "vision_max_images_per_page": 4,   # cap LLaVA calls per page (largest first)
        "vision_image_min_area_fraction": 0.05,  # images smaller than 5% of page area are decorative
        "vision_workers": 4,               # parallel vision/OCR HTTP calls per PDF (1 = serial)
        "embedding_workers": 8,            # parallel text-embedding HTTP calls (1 = serial)
        "file_timeout_s": 600,  # seconds allowed per file; raise for large/dense PDFs
        "status_file": True,  # write .carta/embed-status.json for the status-line widget
        "chunking": {
            "max_tokens": 800,
            "overlap_fraction": 0.15,
            "preserve_tables": True,  # NEW: keep markdown tables whole
            # Prepend "{doc_title} > {section_heading}" to each chunk's EMBEDDING
            # input (not the stored excerpt) so vectors carry doc identity. Re-embed
            # required to take effect. Set false to opt out. (issue #19)
            "contextual_header": True,
            # Title-only header by default: the per-section heading added dilution
            # that cancelled the gain on the ET-embed eval (recall@5 0.887 flat with
            # section vs 0.903 title-only). Set true to also include the section. (#19)
            "contextual_header_section": False,
        },
        # ColPali/ColQwen2 multimodal embedding (Issue #1)
        # Uses native transformers API (no colpali-engine) — requires transformers>=4.49
        # Checkpoints must use the HF-native variants (no PEFT adapters):
        #   ColQwen2:  vidore/colqwen2-v1.0-hf  (default, ~5GB, lower VRAM)
        #   ColPali:   vidore/colpali-v1.3-hf   (~7GB)
        # Tri-state: None = auto (search the _visual collection when it exists and
        # is non-empty, so two-pass output is visible by default); True = force on;
        # False = hard opt-out. Auto never loads ColPali unless there's something to search.
        "colpali_enabled": None,
        "colpali_model": "vidore/colqwen2-v1.0-hf",  # or vidore/colpali-v1.3-hf
        "colpali_device": "auto",  # "auto" (MPS>CUDA>CPU), "cpu", "cuda", "mps"; CARTA_COLPALI_DEVICE env overrides
        "colpali_batch_size": 1,  # pages per batch (1 for CPU)
        "colpali_sidecar_path": ".carta/visual_cache/",  # where to store page PNGs
        "colpali_scoped_paths": [],  # restrict ColPali to these repo-relative globs/dirs; [] = all PDFs
        "vision_call_timeout_s": 300,  # seconds per Ollama vision/OCR call (was hardcoded 120)
        "two_pass_visual": True,    # pass-1 marks image-heavy pages; pass-2 (--visual) drains them
        "visual_timeout_s": 3600,   # generous per-file timeout for the slow visual pass (0 = unbounded)
    },
    "proactive_recall": {
        "high_threshold": 0.85,
        "low_threshold": 0.60,
        "max_results": 5,
        "judge_timeout_s": 3,
        "ollama_model": "qwen3.5:0.8b",
    },
    "hooks": {
        "stale_scan": {
            "enabled": True,
            "block_on_stale": False,
            "candidate_threshold": 0.65,
            "judge_timeout_s": 5,
            "ollama_model": "qwen3.5:0.8b",
            "max_judge_calls": 30,
        },
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
    "update_check": True,
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
    if doc_type in NOTE_DOC_TYPES:
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
