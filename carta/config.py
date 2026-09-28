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
        "dedupe_results": True,
        "hybrid": {
            "enabled": True,
            "bm25_model": "Qdrant/bm25",
            "prefetch_limit": 40,
            # Qdrant's server-side RRF used k=2; kept as the default so client-side
            # fusion is behaviour-identical. Flath's course and most literature use
            # 60. Changing it shifts ordering — do it against an eval, not by feel.
            "rrf_k": 2,
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
        "visual_triage_paths": [],  # repo-relative prefixes prioritized in visual drain; [] = no triage
        "vision_call_timeout_s": 300,  # seconds per Ollama vision/OCR call (was hardcoded 120)
        "vision_render_dpi": 150,  # full-page pixmap render DPI (structured/text-with-images-fallback/flattened+vector-drawing routes)
        "deep_scan": {  # NEW: vector-CAD detection thresholds (+ future tiled-render config)
            "dpi": 300,
            "tile_px": 1280,
            "tile_overlap": 0.15,
            "vector_min_paths": 50,       # >= this many page.get_drawings() paths -> candidate vector-CAD page
            "vector_text_max_chars": 1000,  # below this text_length -> vector-CAD wins over FLATTENED/PURE_TEXT
        },
        "two_pass_visual": True,    # pass-1 marks image-heavy pages; pass-2 (--visual) drains them
        "visual_timeout_s": 3600,   # generous per-file timeout for the slow visual pass (0 = unbounded)
        "enrichment": {"repo_visible": False, "suffix": ".extraction.md"},
    },
    "proactive_recall": {
        "high_threshold": 0.85,
        # Dense-cosine floor for "measurably irrelevant" (hybrid path) — CALIBRATED, see
        # agree_rank below and carta/hook/eval/calibrate.py. 0.65 sits just under the
        # weakest relevant top hit observed (0.651) and above the no-answer band
        # (0.600-0.659): it silences 9 of 10 prompts nothing answers at a cost of zero
        # relevant hits. 0.68 starts costing recall, 0.70 costs a lot of it.
        "low_threshold": 0.65,
        "max_results": 5,
        "judge_timeout_s": 3,
        "search_timeout_s": 3,      # wall-clock budget for the recall search (#106)
        "ollama_model": "qwen3.5:0.8b",   # used when judge_backend == "ollama"
        # Gray-zone judge. "crossenc" (default) scores query-vs-passage with a local
        # fastembed cross-encoder: no Ollama, no reasoning model, ~10ms warm and ~0.5s
        # cold in a fresh process — which is what the hook is, once per prompt. On 229
        # labelled pairs from the 84-query eval corpus it reaches AUC 0.915 against
        # random chunks where the binary qwen3.5:2b judge reaches 0.78, and admits ~5%
        # of random chunks at the threshold below (the 0.8b judge admitted 59%).
        # "ollama" keeps the yes/no LLM judge (needs its model pulled). "auto" (default)
        # prefers the cross-encoder and falls back to the Ollama judge when fastembed is
        # not installed — it ships in the optional carta-cc[hybrid] extra, and a plain
        # install hard-defaulted to crossenc would leave the gray zone silent for good.
        "judge_backend": "auto",
        "judge_model": "Xenova/ms-marco-MiniLM-L-6-v2",
        # Raw cross-encoder logit, model-specific and NOT a probability. Calibrated with
        # carta/hook/eval/calibrate_gate.py: the point where ~5% of random chunks pass
        # while 80% of true passages do. Re-derive it if judge_model changes.
        "judge_threshold": -10.4,
        # Measurably-low dense cosine (< low_threshold) -> silent; else top-N in
        # BOTH lanes -> inject; otherwise -> judge.
        # CALIBRATED (#118) with `python -m carta.hook.eval.calibrate gate` over an
        # 84-query eval corpus + 15 off-corpus prompts. 1 means "rank 0 in both lanes",
        # the only bypass strong enough to skip the judge: at 1, zero unrelated and one
        # plausible-but-wrong top hit inject unvetted, against six and four at the old
        # placeholder of 3 — while 12 of 40 relevant hits still bypass and the rest go to
        # a judge that now costs ~10ms (it used to be an LLM call that always timed out,
        # which is why bypassing it liberally once made sense).
        "agree_rank": 1,
        # Append one JSONL record per hook invocation to
        # ~/.carta/traces/<project>/hook-YYYY-MM.jsonl (never inside the repo).
        # The record holds the derived query, which for prompts <=500 chars is
        # the prompt verbatim — set false to switch that off.
        "trace": True,
    },
    "hooks": {
        "stale_scan": {
            "enabled": True,
            "block_on_stale": False,
            # DEPRECATED and ignored since the RRF-scale fix (#121). It gated
            # run_search's fused RRF output — sum of 1/(k+rank), k=2 — as though it
            # were a cosine, so a hit ranked FIRST in one lane maxed at 0.5 and was
            # dropped before the judge. Left accepted-but-inert so existing configs
            # do not silently keep a mis-scaled value; use candidate_dense_threshold.
            "candidate_threshold": 0.65,
            # Relevance floor on the DENSE lane's raw cosine — an absolute measure,
            # unlike a rank statistic. 0.55 is deliberately below the recall hook's
            # 0.60 for the identical measurement: a missed supersession is invisible
            # and this feature is warn-only, so it errs toward reaching the judge.
            # Uncalibrated, like the hook's own values; max_judge_calls is the real
            # cost bound.
            "candidate_dense_threshold": 0.55,
            # Fused-result depth for the supersession search. Explicit and deeper
            # than search.top_n (5): a superseding doc fused at rank 6-29 was
            # retrieved and then truncated away before any threshold could see it,
            # so the gate could be tuned perfectly and still recover nothing.
            # Cost is unchanged — one judge call per chunk regardless of depth.
            "candidate_depth": 30,
            "judge_timeout_s": 60,
            "ollama_model": "qwen3.5:9b",
            "max_judge_calls": 30,
            "claude_md_nudge": True,
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
