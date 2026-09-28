"""The recall hook's cross-encoder judge backend.

A cross-encoder is purpose-built for "does this passage answer this query", needs no
Ollama, and costs ~10ms warm / ~0.5s cold — which fits a hook that starts a fresh
process per prompt. Measured on 229 labelled pairs from the 84-query eval corpus
(400-char excerpts): AUC 0.915 against random chunks vs 0.78 for the binary
qwen3.5:2b judge it replaces.
"""
from unittest.mock import patch

import carta.hook.hook as hook


def _cfg(**pr):
    base = {"embed": {"ollama_url": "http://o"},
            "proactive_recall": {"ollama_model": "m", "judge_threshold": -10.4,
                                 "judge_model": "Xenova/ms-marco-MiniLM-L-6-v2"}}
    base["proactive_recall"].update(pr)
    return base


def _hits(*excerpts):
    return [{"excerpt": e, "source": f"docs/{i}.md"} for i, e in enumerate(excerpts)]


def test_crossenc_judge_true_at_threshold():
    """>= threshold, not > : the calibrated value is an inclusive floor."""
    with patch("carta.search.rerank.score_pairs", return_value=[-10.4]):
        assert hook._call_crossenc_judge("p", _hits("x"), _cfg()) is True


def test_crossenc_judge_false_below_threshold():
    with patch("carta.search.rerank.score_pairs", return_value=[-10.41]):
        assert hook._call_crossenc_judge("p", _hits("x"), _cfg()) is False


def test_crossenc_judge_takes_the_best_candidate():
    """The gate question is 'are ANY of these relevant', so the max score decides."""
    with patch("carta.search.rerank.score_pairs", return_value=[-30.0, -1.0, -25.0]):
        assert hook._call_crossenc_judge("p", _hits("a", "b", "c"), _cfg()) is True


def test_crossenc_judge_truncates_prompt_and_excerpts():
    with patch("carta.search.rerank.score_pairs", return_value=[-1.0]) as sp:
        hook._call_crossenc_judge("P" * 900, _hits("E" * 900), _cfg())
    query, texts, model = sp.call_args[0]
    assert len(query) == 300                      # prompt[:300], as the Ollama judge does
    assert len(texts[0]) == 400                   # 400 beats 200: AUC 0.915 vs 0.877
    assert model == "Xenova/ms-marco-MiniLM-L-6-v2"


def test_crossenc_judge_no_hits_is_false():
    assert hook._call_crossenc_judge("p", [], _cfg()) is False


def _fake_find_spec(present: bool):
    """Pretend fastembed is/isn't installed, so resolution tests don't depend on the env."""
    import importlib.util
    real = importlib.util.find_spec
    return lambda name, *a, **k: (object() if present else None) if name == "fastembed" \
        else real(name, *a, **k)


def test_auto_resolves_to_crossenc_when_fastembed_is_installed(monkeypatch):
    import importlib.util
    monkeypatch.setattr(importlib.util, "find_spec", _fake_find_spec(True))
    assert hook._resolve_judge_backend(_cfg()) == "crossenc"


def test_auto_falls_back_to_ollama_without_fastembed(monkeypatch):
    """fastembed ships in the optional [hybrid] extra — a plain install must still judge,
    not go permanently silent."""
    import importlib.util
    monkeypatch.setattr(importlib.util, "find_spec", _fake_find_spec(False))
    assert hook._resolve_judge_backend(_cfg()) == "ollama"


def test_auto_treats_a_raising_find_spec_as_absent(monkeypatch):
    """find_spec can raise rather than return None; that must not break prompt submission."""
    import importlib.util

    def boom(name, *a, **k):
        raise ImportError("broken import machinery")

    monkeypatch.setattr(importlib.util, "find_spec", boom)
    assert hook._resolve_judge_backend(_cfg()) == "ollama"


def test_explicit_crossenc_is_honoured_even_without_fastembed(monkeypatch):
    import importlib.util
    monkeypatch.setattr(importlib.util, "find_spec", _fake_find_spec(False))
    assert hook._resolve_judge_backend(_cfg(judge_backend="crossenc")) == "crossenc"


def test_dispatch_uses_crossenc_when_resolution_says_so(monkeypatch):
    import importlib.util
    monkeypatch.setattr(importlib.util, "find_spec", _fake_find_spec(True))
    with patch("carta.hook.hook._call_crossenc_judge", return_value=True) as ce, \
         patch("carta.hook.hook._call_ollama_judge", return_value=False) as ol:
        assert hook._judge_with_timeout("p", _hits("x"), _cfg(), 3) is True
    assert ce.called and not ol.called


def test_dispatch_honours_ollama_backend():
    with patch("carta.hook.hook._call_crossenc_judge", return_value=False) as ce, \
         patch("carta.hook.hook._call_ollama_judge", return_value=True) as ol:
        assert hook._judge_with_timeout("p", _hits("x"), _cfg(judge_backend="ollama"), 3) is True
    assert ol.called and not ce.called


def test_unknown_backend_warns_and_resolves_as_auto(capsys, monkeypatch):
    import importlib.util
    monkeypatch.setattr(importlib.util, "find_spec", _fake_find_spec(True))
    assert hook._resolve_judge_backend(_cfg(judge_backend="nope")) == "crossenc"
    assert "nope" in capsys.readouterr().err


def test_dispatch_records_backend_and_score_for_calibration():
    """The trace is the calibration data (#118); a verdict with no score can't be recalibrated."""
    out = {}
    with patch("carta.search.rerank.score_pairs", return_value=[-9.0]):
        hook._judge_with_timeout("p", _hits("x"), _cfg(judge_backend="crossenc"), 3, out=out)
    assert out["backend"] == "crossenc"
    assert out["score"] == -9.0


def test_crossenc_failure_fails_open_without_injecting():
    """A missing/undownloadable model must not block the prompt, and must not inject unvetted."""
    with patch("carta.search.rerank.score_pairs", side_effect=RuntimeError("no model")):
        assert hook._judge_with_timeout("p", _hits("x"), _cfg(judge_backend="crossenc"), 3) is False


def test_gray_zone_with_crossenc_default_injects_end_to_end(tmp_path):
    """The default judge path, end to end: gray-zone hit + a score above threshold injects.
    score_pairs is patched — a test must never download a model."""
    import json
    cfg = {
        "project_name": "test-proj", "qdrant_url": "http://localhost:6333",
        "modules": {"proactive_recall": True},
        "proactive_recall": {"high_threshold": 0.85, "low_threshold": 0.60, "max_results": 5,
                             "judge_timeout_s": 3, "ollama_model": "m", "trace": False,
                             "judge_backend": "crossenc"},
        "embed": {"ollama_url": "http://o", "ollama_model": "nomic-embed-text:latest"},
        "search": {"top_n": 5},
    }
    hits = [{"score": 0.75, "source": "docs/a.md", "excerpt": "relevant text"}]
    import io
    buf = io.StringIO()   # _inject writes to sys.__stdout__, which capsys does not capture
    with patch("sys.stdin", io.StringIO(json.dumps({"prompt": "query"}))), \
         patch("sys.stdout", buf), patch("sys.__stdout__", buf), \
         patch("carta.hook.hook.find_config", return_value=tmp_path / ".carta" / "config.yaml"), \
         patch("carta.hook.hook.load_config", return_value=cfg), \
         patch("carta.hook.hook.run_search", return_value=hits), \
         patch("carta.search.rerank.score_pairs", return_value=[-2.0]) as sp:
        try:
            hook.main()
        except SystemExit as e:
            assert e.code == 0
    out = buf.getvalue()
    assert sp.called, "the cross-encoder judge must be the one consulted"
    assert "docs/a.md" in out


def test_config_defaults_name_the_judge_backend_model_and_threshold():
    from carta.config import DEFAULTS
    pr = DEFAULTS["proactive_recall"]
    assert pr["judge_backend"] == "auto"
    assert pr["judge_model"] == "Xenova/ms-marco-MiniLM-L-6-v2"
    assert pr["judge_threshold"] == -10.4
