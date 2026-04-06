import pytest
import yaml
from pathlib import Path
from carta.config import load_config, ConfigError

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
    assert pr["ollama_model"] == "qwen2.5:0.5b"
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
