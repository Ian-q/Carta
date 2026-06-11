"""Tests for carta/memory/capture.py — note capture core."""

from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from carta.memory.capture import capture_note


def _cfg():
    return {
        "project_name": "p",
        "qdrant_url": "http://localhost:6333",
        "memory": {"quirks_dir": "docs/quirks", "notes_dir": "docs/notes"},
        "embed": {"ollama_url": "http://localhost:11434", "ollama_model": "m"},
    }


def _capture(tmp_path, text="The bench PSU must be on for CAN tests", **kw):
    kw.setdefault("note_type", "quirk")
    with patch("carta.embed.pipeline.run_embed_file",
               return_value={"status": "ok", "chunks": 2}) as emb:
        out = capture_note(_cfg(), tmp_path, text, **kw)
    return out, emb


class TestCaptureNote:
    def test_quirk_file_written_with_frontmatter(self, tmp_path):
        out, emb = _capture(tmp_path, title="Bench PSU quirk", tags=["bench", "can"])
        p = tmp_path / out["path"]
        assert p.parent == tmp_path / "docs" / "quirks"
        content = p.read_text()
        assert content.startswith("---\n")
        fm = yaml.safe_load(content.split("---")[1])
        assert fm["doc_type"] == "quirk"
        assert fm["title"] == "Bench PSU quirk"
        assert fm["tags"] == ["bench", "can"]
        assert "created" in fm
        assert "The bench PSU must be on" in content
        assert out["collection"] == "p_notes"
        assert out["chunks"] == 2
        emb.assert_called_once()

    def test_bug_note_and_helpful_note_go_to_notes_dir(self, tmp_path):
        for nt in ("bug-note", "helpful-note"):
            out, _ = _capture(tmp_path, note_type=nt)
            assert (tmp_path / out["path"]).parent == tmp_path / "docs" / "notes"

    def test_title_drives_slug_else_first_words(self, tmp_path):
        out, _ = _capture(tmp_path, title="EZKontrol CAN Handshake!")
        assert "ezkontrol-can-handshake" in out["path"]
        out2, _ = _capture(tmp_path, text="Always check the shunt resistor first", title="")
        assert "always-check-the-shunt" in out2["path"]

    def test_filename_collision_appends_suffix(self, tmp_path):
        out1, _ = _capture(tmp_path, title="Same Title")
        out2, _ = _capture(tmp_path, title="Same Title")
        assert out1["path"] != out2["path"]
        assert out2["path"].endswith("-2.md")

    def test_invalid_note_type_raises(self, tmp_path):
        with pytest.raises(ValueError, match="note_type"):
            capture_note(_cfg(), tmp_path, "x", note_type="session")

    def test_empty_text_raises(self, tmp_path):
        with pytest.raises(ValueError, match="empty"):
            capture_note(_cfg(), tmp_path, "   ", note_type="quirk")

    def test_embed_failure_keeps_file_and_raises(self, tmp_path):
        with patch("carta.embed.pipeline.run_embed_file",
                   side_effect=RuntimeError("qdrant down")):
            with pytest.raises(RuntimeError, match="carta embed"):
                capture_note(_cfg(), tmp_path, "important fact", note_type="quirk")
        written = list((tmp_path / "docs" / "quirks").glob("*.md"))
        assert len(written) == 1, "the note file must survive an embed failure"

    def test_custom_dirs_from_config(self, tmp_path):
        cfg = _cfg()
        cfg["memory"]["quirks_dir"] = "docs/carta/quirks"
        with patch("carta.embed.pipeline.run_embed_file",
                   return_value={"status": "ok", "chunks": 1}):
            out = capture_note(cfg, tmp_path, "fact", note_type="quirk")
        assert out["path"].startswith("docs/carta/quirks/")
