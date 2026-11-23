"""Split-host deployment (#106, #116): separate query and ingest Ollama endpoints,
remote-aware doctor checks.

Every hostname here is invented (`*.example`, `*.invalid`, RFC 5737 addresses).
"""
import ast
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests
import yaml

from carta.config import (
    DEFAULTS, ConfigError, ingest_ollama_url, is_loopback_url, is_split_ollama,
    load_config, query_ollama_url,
)

QUERY = "http://localhost:11434"
INGEST = "http://gpu-box.example:11434"


def _cfg(ingest=None, query=QUERY, qdrant="http://localhost:6333", **embed):
    e = {"ollama_url": query, "ollama_model": "embedder:latest", **embed}
    if ingest is not None:
        e["ingest_ollama_url"] = ingest
    return {"project_name": "demo", "qdrant_url": qdrant, "embed": e,
            "proactive_recall": {"search_timeout_s": 3}}


# ---------------------------------------------------------------------------
# Config resolution and back-compat
# ---------------------------------------------------------------------------

def test_single_host_config_resolves_both_paths_to_ollama_url():
    cfg = _cfg()
    assert query_ollama_url(cfg) == QUERY
    assert ingest_ollama_url(cfg) == QUERY
    assert not is_split_ollama(cfg)


def test_ingest_url_routes_ingest_only():
    cfg = _cfg(ingest=INGEST)
    assert query_ollama_url(cfg) == QUERY
    assert ingest_ollama_url(cfg) == INGEST
    assert is_split_ollama(cfg)


@pytest.mark.parametrize("blank", ["", "   ", None])
def test_blank_ingest_url_means_unset(blank):
    cfg = _cfg()
    cfg["embed"]["ingest_ollama_url"] = blank
    assert ingest_ollama_url(cfg) == QUERY


def test_trailing_slashes_are_normalised():
    cfg = _cfg(ingest=INGEST + "/", query=QUERY + "/")
    assert query_ollama_url(cfg) == QUERY
    assert ingest_ollama_url(cfg) == INGEST


def test_missing_embed_section_falls_back_to_default():
    assert query_ollama_url({}) == "http://localhost:11434"
    assert ingest_ollama_url({"embed": None}) == "http://localhost:11434"


def test_defaults_do_not_add_ingest_key():
    """A generated config must look exactly as before: no new key, no null."""
    assert "ingest_ollama_url" not in DEFAULTS["embed"]


def test_load_config_without_ingest_key_is_single_host(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump({"project_name": "demo", "qdrant_url": "http://localhost:6333",
                                 "embed": {"ollama_url": "http://other.example:11434"}}))
    cfg = load_config(p)
    assert ingest_ollama_url(cfg) == query_ollama_url(cfg) == "http://other.example:11434"


def test_load_config_keeps_ingest_key(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump({"project_name": "demo", "qdrant_url": "http://localhost:6333",
                                 "embed": {"ingest_ollama_url": INGEST}}))
    cfg = load_config(p)
    assert ingest_ollama_url(cfg) == INGEST
    assert query_ollama_url(cfg) == "http://localhost:11434"


def test_load_config_rejects_non_string_ingest_url(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump({"project_name": "demo", "qdrant_url": "http://localhost:6333",
                                 "embed": {"ingest_ollama_url": 11434}}))
    with pytest.raises(ConfigError, match="ingest_ollama_url"):
        load_config(p)


@pytest.mark.parametrize("url,expected", [
    ("http://localhost:11434", True),
    ("http://127.0.0.1:6333", True),
    ("http://127.5.5.5:6333", True),
    ("http://[::1]:6333", True),
    ("http://app.localhost:1", True),
    ("http://gpu-box.example:11434", False),
    ("http://192.0.2.10:6333", False),
    ("https://vectors.example", False),
    ("localhost:6333", True),
])
def test_is_loopback_url(url, expected):
    assert is_loopback_url(url) is expected


# ---------------------------------------------------------------------------
# Call sites: every config read goes through the two helpers
# ---------------------------------------------------------------------------

_PKG = Path(__file__).resolve().parents[1]


def test_no_module_reads_ollama_url_from_config_directly():
    """Regression guard for #116: a new call site that reads embed.ollama_url
    itself would silently ignore ingest_ollama_url (or send a query to the GPU
    host). Only carta/config.py may touch the key."""
    offenders = []
    for path in _PKG.rglob("*.py"):
        rel = path.relative_to(_PKG)
        if "tests" in rel.parts or rel == Path("config.py"):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            # cfg["embed"]["ollama_url"], embed_cfg["ollama_url"]
            if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant) \
                    and node.slice.value in ("ollama_url", "ingest_ollama_url"):
                offenders.append(f"{rel}:{node.lineno}")
            # x.get("ollama_url", ...)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                    and node.func.attr == "get" and node.args \
                    and isinstance(node.args[0], ast.Constant) \
                    and node.args[0].value in ("ollama_url", "ingest_ollama_url"):
                offenders.append(f"{rel}:{node.lineno}")
    assert offenders == [], f"read embed.ollama_url via carta.config helpers: {offenders}"


@patch("carta.embed.embed.QdrantClient")
def test_document_embeds_use_ingest_url(_client_cls):
    from carta.embed import embed as embed_mod
    seen = []

    def fake_embedding(text, ollama_url, model, **kw):
        seen.append(ollama_url)
        return [0.1] * embed_mod.VECTOR_DIM

    client = MagicMock()
    client.collection_exists.return_value = True
    with patch.object(embed_mod, "get_embedding", side_effect=fake_embedding), \
         patch.object(embed_mod, "collection_is_hybrid", return_value=False):
        embed_mod.upsert_chunks(
            [{"slug": "a", "file_path": "docs/a.md", "chunk_index": 0, "text": "invented"}],
            _cfg(ingest=INGEST), client=client)
    assert seen == [INGEST]


@patch("carta.embed.embed.QdrantClient")
def test_document_embeds_single_host_unchanged(_client_cls):
    from carta.embed import embed as embed_mod
    seen = []
    client = MagicMock()
    client.collection_exists.return_value = True
    with patch.object(embed_mod, "get_embedding",
                      side_effect=lambda t, ollama_url, model, **kw: seen.append(ollama_url)
                      or [0.1] * embed_mod.VECTOR_DIM), \
         patch.object(embed_mod, "collection_is_hybrid", return_value=False):
        embed_mod.upsert_chunks(
            [{"slug": "a", "file_path": "docs/a.md", "chunk_index": 0, "text": "invented"}],
            _cfg(), client=client)
    assert seen == [QUERY]


def test_ocr_and_vision_router_use_ingest_url():
    from carta.vision.router import SmartRouter
    assert SmartRouter(_cfg(ingest=INGEST)).ollama_url == INGEST
    assert SmartRouter(_cfg()).ollama_url == QUERY


def test_image_descriptions_use_ingest_url(tmp_path):
    import pymupdf
    pdf = tmp_path / "invented.pdf"
    doc = pymupdf.open()
    doc.new_page()
    doc.save(str(pdf))
    doc.close()
    from carta.embed import vision
    seen = []
    with patch.object(vision, "_extract_embedded_images", return_value=[b"png"]), \
         patch.object(vision, "_call_vision_model",
                      side_effect=lambda png, url, model: seen.append(url) or "a caption"):
        out = vision.extract_image_descriptions(pdf, _cfg(ingest=INGEST))
    assert seen == [INGEST] and out[0]["text"] == "a caption"


def test_query_embed_uses_query_url():
    from carta.embed import pipeline
    seen = []
    with patch.object(pipeline, "get_embedding",
                      side_effect=lambda q, **kw: seen.append(kw["ollama_url"]) or [0.0]):
        pipeline._embed_query_or_raise("invented query", _cfg(ingest=INGEST), ["demo_doc"])
    assert seen == [QUERY]


def test_query_embed_error_names_query_host():
    from carta.embed import pipeline
    with patch.object(pipeline, "get_embedding", side_effect=requests.ConnectionError("x")):
        with pytest.raises(RuntimeError, match="localhost:11434"):
            pipeline._embed_query_or_raise("q", _cfg(ingest=INGEST), ["demo_doc"])


def test_hook_query_extractor_and_judge_use_query_url():
    from carta.hook import hook
    cfg = _cfg(ingest=INGEST)
    cfg["proactive_recall"]["ollama_model"] = "small-judge:1b"
    posted = []

    def fake_post(url, **kw):
        posted.append(url)
        r = MagicMock()
        r.json.return_value = {"message": {"content": "yes"}}
        return r

    with patch("requests.post", side_effect=fake_post):
        hook._extract_query("x" * 600, cfg)
        hook._call_ollama_judge("p", [{"excerpt": "e"}], cfg, timeout_s=1)
    assert posted and all(u.startswith(QUERY) for u in posted), posted


def test_stale_scan_judge_uses_query_url():
    from carta.hook import stale_scan
    seen = []
    with patch("carta.hook.stale_scan.ollama_json",
               side_effect=lambda url, *a, **k: seen.append(url) or None):
        stale_scan._stale_judge("section", {"excerpt": "e", "source": "docs/b.md"},
                                _cfg(ingest=INGEST))
    assert seen == [QUERY]


# ---------------------------------------------------------------------------
# Doctor: endpoint checks
# ---------------------------------------------------------------------------

def _tags(*models, digest="sha-a"):
    r = MagicMock()
    r.status_code = 200
    r.raise_for_status.return_value = None
    r.json.return_value = {"models": [
        {"name": m, "digest": (digest if m.startswith("embedder") else f"d-{m}")}
        for m in models]}
    return r


def _fake_get(routes):
    """routes: {url prefix: response or exception}."""
    def get(url, **kw):
        for prefix, resp in routes.items():
            if url.startswith(prefix):
                if isinstance(resp, Exception):
                    raise resp
                return resp
        raise requests.ConnectionError(f"no route for {url}")
    return get


def _ok():
    r = MagicMock()
    r.status_code = 200
    return r


def _by_name(items):
    return {i.name: i for i in items}


def test_endpoints_single_host_checks_one_ollama():
    from carta.hook import health
    with patch("carta.hook.health.requests.get", side_effect=_fake_get({
        "http://localhost:6333": _ok(),
        QUERY: _tags("embedder:latest"),
    })):
        items = _by_name(health.check_endpoints(_cfg()))
    assert items["endpoint_qdrant"].status == "pass"
    assert items["endpoint_ollama_query"].status == "pass"
    assert "query + ingest" in items["endpoint_ollama_query"].message.lower()
    assert items["endpoint_query_embed_model"].status == "pass"
    assert "endpoint_ollama_ingest" not in items


def test_endpoints_split_host_all_good():
    from carta.hook import health
    with patch("carta.hook.health.requests.get", side_effect=_fake_get({
        "http://localhost:6333": _ok(),
        QUERY: _tags("embedder:latest"),
        INGEST: _tags("embedder:latest", "glm-ocr:latest", "qwen3-vl:8b"),
    })):
        items = _by_name(health.check_endpoints(_cfg(ingest=INGEST)))
    for name in ("endpoint_ollama_query", "endpoint_ollama_ingest",
                 "endpoint_query_embed_model", "endpoint_ingest_embed_model",
                 "endpoint_ingest_ocr_model", "endpoint_ingest_ollama_vision_model",
                 "endpoint_embed_model_match"):
        assert items[name].status == "pass", (name, items[name].message)
    assert "remote" in items["endpoint_ollama_ingest"].message


def test_endpoints_ingest_host_missing_models():
    from carta.hook import health
    with patch("carta.hook.health.requests.get", side_effect=_fake_get({
        "http://localhost:6333": _ok(),
        QUERY: _tags("embedder:latest"),
        INGEST: _tags("something-else:1b"),
    })):
        items = _by_name(health.check_endpoints(_cfg(ingest=INGEST)))
    assert items["endpoint_ingest_embed_model"].status == "fail"
    assert items["endpoint_ingest_ocr_model"].status == "warn"
    assert "ollama pull glm-ocr:latest" in items["endpoint_ingest_ocr_model"].suggestion


def test_endpoints_embed_model_build_mismatch_is_an_error():
    from carta.hook import health
    with patch("carta.hook.health.requests.get", side_effect=_fake_get({
        "http://localhost:6333": _ok(),
        QUERY: _tags("embedder:latest", digest="sha-local"),
        INGEST: _tags("embedder:latest", "glm-ocr:latest", "qwen3-vl:8b", digest="sha-gpu"),
    })):
        items = _by_name(health.check_endpoints(_cfg(ingest=INGEST)))
    assert items["endpoint_embed_model_match"].status == "fail"


def test_endpoints_ingest_host_down_is_a_warning_query_down_is_an_error():
    from carta.hook import health
    with patch("carta.hook.health.requests.get", side_effect=_fake_get({
        "http://localhost:6333": _ok(),
        QUERY: requests.ConnectionError("refused"),
        INGEST: requests.ConnectTimeout("timed out"),
    })):
        items = _by_name(health.check_endpoints(_cfg(ingest=INGEST)))
    assert items["endpoint_ollama_query"].status == "fail"
    assert items["endpoint_ollama_ingest"].status == "warn"
    assert "network path" in items["endpoint_ollama_ingest"].suggestion
    assert "endpoint_embed_model_match" not in items


def test_endpoints_remote_qdrant_down_points_at_the_network():
    from carta.hook import health
    q = "http://vectors.example:6333"
    with patch("carta.hook.health.requests.get", side_effect=_fake_get({
        q: requests.ConnectTimeout("timed out"),
        QUERY: _tags("embedder:latest"),
    })):
        items = _by_name(health.check_endpoints(_cfg(qdrant=q)))
    assert items["endpoint_qdrant"].status == "fail"
    assert "remote" in items["endpoint_qdrant"].message
    assert "network" in items["endpoint_qdrant"].suggestion.lower()


def test_endpoints_slow_remote_round_trip_warns():
    from carta.hook import health
    q = "http://vectors.example:6333"
    clock = iter([0.0, 2.5] + [10.0] * 20)
    with patch("carta.hook.health.time.monotonic", side_effect=lambda: next(clock)), \
         patch("carta.hook.health.requests.get", side_effect=_fake_get({
             q: _ok(), QUERY: _tags("embedder:latest")})):
        items = _by_name(health.check_endpoints(_cfg(qdrant=q)))
    assert items["endpoint_qdrant"].status == "warn"
    assert "search_timeout_s" in items["endpoint_qdrant"].message


# ---------------------------------------------------------------------------
# Doctor: preflight phases 2-3 follow the project's config
# ---------------------------------------------------------------------------

def _project(tmp_path, cfg):
    (tmp_path / ".carta").mkdir()
    (tmp_path / ".carta" / "config.yaml").write_text(yaml.safe_dump(cfg))
    return tmp_path


def test_doctor_checks_the_configured_remote_qdrant_not_localhost(tmp_path):
    from carta.install.preflight import PreflightChecker
    q = "http://vectors.example:6333"
    root = _project(tmp_path, _cfg(qdrant=q, query="http://ollama-box.example:11434"))
    checker = PreflightChecker(interactive=False, project_root=root, include_hooks=True)
    urls = []

    def get(url, **kw):
        urls.append(url)
        raise requests.ConnectTimeout("timed out")

    fail_docker = MagicMock(returncode=1, stdout="", stderr="")
    with patch("carta.install.preflight.requests.get", side_effect=get), \
         patch("carta.install.preflight.shutil.which", return_value=None), \
         patch("carta.install.preflight.subprocess.run", return_value=fail_docker):
        checker._phase2_infrastructure()
    by = _by_name(checker.checks)
    assert any(u.startswith(q) for u in urls)
    assert not any("localhost" in u for u in urls)
    assert by["qdrant_running"].status == "fail"
    assert by["qdrant_running"].fixable is False
    assert by["qdrant_running"].details["remote"] is True
    assert by["docker_installed"].status == "skip"
    assert by["ollama_installed"].status == "skip"
    assert by["ports_available"].status == "skip"

    from carta.install.auto_fix import AutoInstaller
    assert AutoInstaller(interactive=False).can_fix(by["qdrant_running"]) is False


def test_init_preflight_still_uses_env_seed_not_project_config(tmp_path, monkeypatch):
    from carta.install.preflight import PreflightChecker
    root = _project(tmp_path, _cfg(qdrant="http://vectors.example:6333"))
    monkeypatch.delenv("CARTA_QDRANT_URL", raising=False)
    checker = PreflightChecker(interactive=False, project_root=root)
    assert checker._endpoint_urls()[0] == "http://localhost:6333"


def test_doctor_phase3_defers_model_checks_to_phase5(tmp_path):
    from carta.install.preflight import PreflightChecker
    root = _project(tmp_path, _cfg(ingest=INGEST))
    checker = PreflightChecker(interactive=False, project_root=root, include_hooks=True)
    checker.checks = []
    with patch.object(PreflightChecker, "_check_colpali_available",
                      return_value=MagicMock(name="colpali")):
        checker._phase3_models()
    models = [c for c in checker.checks if getattr(c, "name", "") == "ollama_models"]
    assert models and models[0].status == "skip"
