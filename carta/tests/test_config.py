import pytest
import yaml
from pathlib import Path
from carta.config import load_config, ConfigError, DEFAULTS

MINIMAL_CONFIG = {
    "project_name": "test-project",
    "qdrant_url": "http://localhost:6333",
    "modules": {
        "doc_audit": True,
        "doc_embed": True,
        "doc_search": True,
        "session_memory": False,
        "proactive_recall": False,
    },
}

def test_load_valid_config(tmp_path):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(yaml.dump(MINIMAL_CONFIG))
    cfg = load_config(cfg_path)
    assert cfg["project_name"] == "test-project"
    assert cfg["modules"]["doc_audit"] is True

def test_missing_project_name_raises(tmp_path):
    bad = {k: v for k, v in MINIMAL_CONFIG.items() if k != "project_name"}
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(yaml.dump(bad))
    with pytest.raises(ConfigError, match="project_name"):
        load_config(cfg_path)

def test_missing_qdrant_url_raises(tmp_path):
    bad = {k: v for k, v in MINIMAL_CONFIG.items() if k != "qdrant_url"}
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(yaml.dump(bad))
    with pytest.raises(ConfigError, match="qdrant_url"):
        load_config(cfg_path)

def test_missing_file_raises():
    with pytest.raises(ConfigError, match="not found"):
        load_config(Path("/nonexistent/.carta/config.yaml"))

def test_defaults_applied(tmp_path):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(yaml.dump(MINIMAL_CONFIG))
    cfg = load_config(cfg_path)
    # stale_threshold_days should default to 30 if not specified
    assert cfg["stale_threshold_days"] == 30

def test_collection_name_helper(tmp_path):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(yaml.dump(MINIMAL_CONFIG))
    cfg = load_config(cfg_path)
    from carta.config import collection_name
    assert collection_name(cfg, "doc") == "test-project_doc"
    assert collection_name(cfg, "session") == "test-project_session"
    assert collection_name(cfg, "quirk") == "test-project_quirk"


def test_proactive_recall_defaults(tmp_path):
    """proactive_recall DEFAULTS must contain three-zone threshold keys, not old keys."""
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(yaml.dump(MINIMAL_CONFIG))
    cfg = load_config(cfg_path)
    pr = cfg["proactive_recall"]
    assert pr["high_threshold"] == 0.85
    assert pr["low_threshold"] == 0.60
    assert pr["max_results"] == 5
    assert pr["judge_timeout_s"] == 3
    assert pr["ollama_model"] == "qwen3.5:0.8b"
    assert "similarity_threshold" not in pr
    assert "ollama_judge" not in pr


class TestVisionThresholdDefaults:
    def test_vision_text_min_chars_default(self):
        from carta.config import DEFAULTS
        assert DEFAULTS["embed"]["vision_text_min_chars"] == 150

    def test_vision_text_max_chars_default(self):
        from carta.config import DEFAULTS
        assert DEFAULTS["embed"]["vision_text_max_chars"] == 600

    def test_vision_flattened_min_yield_default(self):
        from carta.config import DEFAULTS
        assert DEFAULTS["embed"]["vision_flattened_min_yield"] == 50

    def test_vision_max_images_per_page_default(self):
        from carta.config import DEFAULTS
        assert DEFAULTS["embed"]["vision_max_images_per_page"] == 4


class TestDeepScanDefaults:
    """embed.deep_scan (vector-CAD detection thresholds + future tiled-render
    config) and embed.vision_render_dpi (Task 5)."""

    def test_vision_render_dpi_default(self):
        from carta.config import DEFAULTS
        assert DEFAULTS["embed"]["vision_render_dpi"] == 150

    def test_deep_scan_defaults_present(self):
        from carta.config import DEFAULTS
        deep = DEFAULTS["embed"]["deep_scan"]
        assert deep["dpi"] == 300
        assert deep["tile_px"] == 1280
        assert deep["tile_overlap"] == 0.15
        assert deep["vector_min_paths"] == 50
        assert deep["vector_text_max_chars"] == 1000

    def test_deep_scan_merges_into_loaded_config(self, tmp_path):
        cfg_path = tmp_path / "config.yaml"
        cfg_path.write_text(yaml.dump(MINIMAL_CONFIG))
        cfg = load_config(cfg_path)
        assert cfg["embed"]["deep_scan"]["vector_min_paths"] == 50
        assert cfg["embed"]["vision_render_dpi"] == 150


def test_judge_model_default_is_qwen35():
    assert DEFAULTS["proactive_recall"]["ollama_model"] == "qwen3.5:0.8b"


def test_update_check_defaults_to_true(tmp_path):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(yaml.dump(MINIMAL_CONFIG))
    cfg = load_config(cfg_path)
    assert cfg["update_check"] is True


def test_update_check_can_be_disabled(tmp_path):
    config = {**MINIMAL_CONFIG, "update_check": False}
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(yaml.dump(config))
    cfg = load_config(cfg_path)
    assert cfg["update_check"] is False


def test_two_pass_visual_defaults():
    from carta.config import DEFAULTS
    e = DEFAULTS["embed"]
    assert e["two_pass_visual"] is True
    assert e["visual_timeout_s"] == 3600


def test_status_file_default_enabled():
    from carta.config import DEFAULTS
    assert DEFAULTS["embed"]["status_file"] is True


def test_rerank_backend_defaults():
    from carta.config import DEFAULTS
    rr = DEFAULTS["search"]["rerank"]
    assert rr["backend"] == "cross-encoder"
    assert rr["llm_model"] == "qwen3.5:0.8b"
    assert rr["llm_timeout_s"] == 20


def test_graph_defaults_present():
    from carta.config import DEFAULTS
    graph = DEFAULTS["search"]["graph"]
    assert graph["enabled"] is False   # opt-in (measured neutral on a dense-reranker corpus)
    assert graph["hops"] == 1
    assert graph["seed_count"] == 10
    assert graph["candidate_depth"] == 50


def test_fusion_defaults_present():
    from carta.config import DEFAULTS
    fusion = DEFAULTS["search"]["fusion"]
    # Eval-swept optimum: maximizes ET-embed 62q text recall (0.839->0.887) while
    # holding the 14q visual eval flat at 0.857 (see RESULTS.md 2026-06-13).
    assert fusion["visual_max_ratio"] == 0.2


def test_ollama_keep_alive_default_and_env_override(monkeypatch):
    """keep_alive defaults to 10m (Ollama's own default is 5m) and is overridable
    via CARTA_OLLAMA_KEEP_ALIVE (e.g. '-1' = keep resident indefinitely)."""
    from carta.config import ollama_keep_alive
    monkeypatch.delenv("CARTA_OLLAMA_KEEP_ALIVE", raising=False)
    assert ollama_keep_alive() == "10m"
    monkeypatch.setenv("CARTA_OLLAMA_KEEP_ALIVE", "-1")
    assert ollama_keep_alive() == "-1"


def test_search_dedupe_results_default_on():
    assert DEFAULTS["search"]["dedupe_results"] is True
