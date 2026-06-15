from carta.install.auto_fix import AutoInstaller


def test_suggest_model_pulls_uses_current_vision_default():
    """The `carta doctor --fix` pull map must offer the current vision default
    (qwen3-vl:8b) so it matches what preflight checks — not the retired
    qwen2.5vl:7b (#45)."""
    pulls = AutoInstaller(interactive=False).suggest_model_pulls()
    assert pulls.get("qwen3-vl:8b") == "ollama pull qwen3-vl:8b"
    assert "qwen2.5vl:7b" not in pulls


def test_suggest_model_pulls_judge_matches_preflight():
    """The pull map's judge entry must match what preflight checks
    (qwen3.5:0.8b) — not the retired qwen2.5:0.5b judge default (#56)."""
    pulls = AutoInstaller(interactive=False).suggest_model_pulls()
    assert pulls.get("qwen3.5:0.8b") == "ollama pull qwen3.5:0.8b"
    assert "qwen2.5:0.5b" not in pulls
