"""Regression tests for embed-pipeline robustness fixes (#103).

Every fixture here is invented; nothing is copied from a real project.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

CFG = {
    "project_name": "p",
    "qdrant_url": "http://localhost:6333",
    "embed": {"ollama_url": "http://x", "ollama_model": "m"},
}


def _setup_repo(tmp_path: Path) -> Path:
    cfg_path = tmp_path / ".carta" / "config.yaml"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.touch()
    return cfg_path


def _write_sidecar(tmp_path: Path, rel: str, data: dict) -> Path:
    from carta.embed.induct import sidecar_path
    sc = sidecar_path(tmp_path / rel, tmp_path)
    sc.parent.mkdir(parents=True, exist_ok=True)
    sc.write_text(yaml.safe_dump(data))
    return sc


# ---------------------------------------------------------------------------
# #103 — token-dense chunk overflow
# ---------------------------------------------------------------------------

def _overflow_then_ok(max_chars: int):
    """Fake Ollama: overflows while the prompt is longer than max_chars."""
    from carta.embed.embed import _CONTEXT_OVERFLOW
    sent = []

    def post(url, json, timeout):
        sent.append(json["prompt"])
        resp = MagicMock()
        if len(json["prompt"]) > max_chars:
            resp.status_code = 500
            resp.text = f'{{"error":"{_CONTEXT_OVERFLOW}"}}'
        else:
            resp.status_code = 200
            resp.json.return_value = {"embedding": [0.1] * 768}
        return resp
    return post, sent


class TestTokenDenseTruncation:
    def test_few_words_many_tokens_still_embeds(self):
        """A chunk of a handful of very long 'words' (URLs/ids) must be reducible
        to something embeddable — word-count truncation alone cannot do it."""
        from carta.embed.embed import get_embedding
        long_id = "https://example.invalid/d/" + "x" * 1200
        text = " ".join([long_id] * 12)  # 12 words, ~15k chars
        post, sent = _overflow_then_ok(max_chars=2000)
        with patch("carta.embed.embed.requests.post", side_effect=post):
            vec = get_embedding(text)
        assert len(vec) == 768
        assert len(sent[-1]) <= 2000

    def test_single_unbroken_token_run_still_embeds(self):
        from carta.embed.embed import get_embedding
        text = "z" * 40000  # one 'word'
        post, _sent = _overflow_then_ok(max_chars=500)
        with patch("carta.embed.embed.requests.post", side_effect=post):
            assert len(get_embedding(text)) == 768

    def test_never_fitting_input_raises_typed_error(self):
        from carta.embed.embed import EmbedInputTooLong, get_embedding
        post, _sent = _overflow_then_ok(max_chars=0)
        with patch("carta.embed.embed.requests.post", side_effect=post):
            with pytest.raises(EmbedInputTooLong):
                get_embedding("a reasonably sized chunk of ordinary text " * 20)


class TestUnembeddableChunkDoesNotPoisonDoc:
    def test_doc_marked_done_and_gap_recorded(self, tmp_path, capsys):
        """One chunk Ollama can never embed must not leave the doc 'partial'
        forever: the rest is kept, the gap recorded in the sidecar."""
        from carta.embed.embed import EmbedInputTooLong
        from carta.embed.pipeline import _embed_one_file

        doc = tmp_path / "docs" / "a.md"
        doc.parent.mkdir(parents=True)
        doc.write_text("\n\n".join(
            f"# Section {i}\n\n" + ("alpha beta gamma delta " * 120) for i in range(4)))

        def fake_get_embedding(text, **kw):
            if "Section 2" in text:
                raise EmbedInputTooLong("input too long even after truncation")
            return [0.1] * 768

        client = MagicMock()
        info = {"slug": "a", "doc_type": "unknown", "generation": 3}
        with patch("carta.embed.embed.get_embedding", side_effect=fake_get_embedding), \
             patch("carta.embed.embed.ensure_collection"), \
             patch("carta.embed.embed.collection_is_hybrid", return_value=False), \
             patch("carta.embed.pipeline.delete_other_points") as dop:
            count, updates = _embed_one_file(
                doc, info, {**CFG, "embed": {**CFG["embed"], "embedding_workers": 1}},
                client, tmp_path, 200, 0.0)

        assert updates["status"] == "embedded", updates
        skipped = updates.get("unembeddable_chunks")
        assert skipped and len(skipped) >= 1
        assert count > 0
        dop.assert_called_once()  # complete-minus-gap counts as complete: cleanup runs
        assert "unembeddable" in capsys.readouterr().out.lower()

    def test_transient_failure_still_partial(self, tmp_path):
        """Only the typed too-long error is forgiven; a transient error keeps the
        doc re-pickable."""
        from carta.embed.pipeline import _embed_one_file

        doc = tmp_path / "docs" / "a.md"
        doc.parent.mkdir(parents=True)
        doc.write_text("\n\n".join(
            f"# Section {i}\n\n" + ("alpha beta gamma delta " * 120) for i in range(4)))

        def fake_get_embedding(text, **kw):
            if "Section 2" in text:
                raise RuntimeError("connection reset")
            return [0.1] * 768

        info = {"slug": "a", "doc_type": "unknown", "generation": 3}
        with patch("carta.embed.embed.get_embedding", side_effect=fake_get_embedding), \
             patch("carta.embed.embed.ensure_collection"), \
             patch("carta.embed.embed.collection_is_hybrid", return_value=False), \
             patch("carta.embed.pipeline.delete_other_points"):
            _count, updates = _embed_one_file(
                doc, info, {**CFG, "embed": {**CFG["embed"], "embedding_workers": 1}},
                MagicMock(), tmp_path, 200, 0.0)
        assert updates["status"] == "partial"
