"""Regression tests for embed-pipeline robustness fixes (#96).

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
# #96 — run_embed_file failure window
# ---------------------------------------------------------------------------

class TestRunEmbedFileFailureWindow:
    def _prior_sidecar(self, tmp_path, doc, status="embedded"):
        import os
        from carta.embed.lifecycle import compute_file_hash
        return _write_sidecar(tmp_path, "docs/a.md", {
            "slug": "a",
            "doc_type": "unknown",
            "current_path": "docs/a.md",
            "status": status,
            "generation": 1,
            "sidecar_id": "sc-123",
            "file_hash": "stale-hash-from-an-older-version",
            "file_mtime": os.path.getmtime(str(doc)) - 100,
        })

    def test_embed_exception_does_not_stale_flag_previous_points(self, tmp_path):
        """A failure inside the embed step must leave the previous generation's
        points untouched — no stale flag without a replacement written."""
        from carta.embed.pipeline import run_embed_file
        doc = tmp_path / "docs" / "a.md"
        doc.parent.mkdir(parents=True)
        doc.write_text("# Title\n\nbody text for the widget manual\n")
        cfg_path = _setup_repo(tmp_path)
        self._prior_sidecar(tmp_path, doc)

        with patch("carta.embed.pipeline.find_config", return_value=cfg_path), \
             patch("carta.embed.pipeline.QdrantClient"), \
             patch("carta.embed.pipeline.ensure_collection"), \
             patch("carta.embed.pipeline.mark_sidecar_stale") as mark, \
             patch("carta.embed.pipeline._embed_one_file",
                   side_effect=RuntimeError("ollama down")):
            with pytest.raises(RuntimeError):
                run_embed_file(doc, CFG)
        mark.assert_not_called()

    def test_extraction_failed_does_not_stale_flag_previous_points(self, tmp_path):
        from carta.embed.pipeline import run_embed_file
        doc = tmp_path / "docs" / "a.md"
        doc.parent.mkdir(parents=True)
        doc.write_text("")
        cfg_path = _setup_repo(tmp_path)
        self._prior_sidecar(tmp_path, doc)

        with patch("carta.embed.pipeline.find_config", return_value=cfg_path), \
             patch("carta.embed.pipeline.QdrantClient"), \
             patch("carta.embed.pipeline.ensure_collection"), \
             patch("carta.embed.pipeline.mark_sidecar_stale") as mark:
            run_embed_file(doc, CFG)
        mark.assert_not_called()

    def test_successful_embed_stale_flags_only_older_generations(self, tmp_path):
        """Once replacement points exist, the older generation is flagged — and the
        flag must exclude the generation just written."""
        from carta.embed.pipeline import run_embed_file
        doc = tmp_path / "docs" / "a.md"
        doc.parent.mkdir(parents=True)
        doc.write_text("# Title\n\nbody text for the widget manual\n")
        cfg_path = _setup_repo(tmp_path)
        self._prior_sidecar(tmp_path, doc)

        with patch("carta.embed.pipeline.find_config", return_value=cfg_path), \
             patch("carta.embed.pipeline.QdrantClient"), \
             patch("carta.embed.pipeline.ensure_collection"), \
             patch("carta.embed.pipeline.delete_other_points"), \
             patch("carta.embed.pipeline.upsert_chunks", return_value=1), \
             patch("carta.embed.pipeline.mark_sidecar_stale") as mark:
            result = run_embed_file(doc, CFG)
        assert result["status"] == "ok"
        mark.assert_called_once()
        assert mark.call_args.kwargs.get("exclude_generation") == 2

    @pytest.mark.parametrize("status", ["pending", "embed_failed", "partial"])
    def test_repickable_status_defeats_mtime_fast_path(self, tmp_path, status):
        """A previously-embedded file whose sidecar is re-pickable must not be
        skipped as 'already embedded' just because its old stamps still match."""
        import os
        from carta.embed.lifecycle import compute_file_hash
        from carta.embed.pipeline import run_embed_file
        doc = tmp_path / "docs" / "a.md"
        doc.parent.mkdir(parents=True)
        doc.write_text("# Title\n\nbody text for the widget manual\n")
        cfg_path = _setup_repo(tmp_path)
        _write_sidecar(tmp_path, "docs/a.md", {
            "slug": "a",
            "doc_type": "unknown",
            "current_path": "docs/a.md",
            "status": status,
            "generation": 1,
            # stamps left from an earlier successful embed — they match the disk
            "file_hash": compute_file_hash(doc),
            "file_mtime": os.path.getmtime(str(doc)),
        })

        with patch("carta.embed.pipeline.find_config", return_value=cfg_path), \
             patch("carta.embed.pipeline.QdrantClient"), \
             patch("carta.embed.pipeline.ensure_collection"), \
             patch("carta.embed.pipeline.delete_other_points"), \
             patch("carta.embed.pipeline.upsert_chunks", return_value=1) as up:
            result = run_embed_file(doc, CFG)
        assert result["status"] == "ok", result
        assert up.called

    def test_embedded_status_still_takes_fast_path(self, tmp_path):
        import os
        from carta.embed.lifecycle import compute_file_hash
        from carta.embed.pipeline import run_embed_file
        doc = tmp_path / "docs" / "a.md"
        doc.parent.mkdir(parents=True)
        doc.write_text("# Title\n\nbody\n")
        cfg_path = _setup_repo(tmp_path)
        _write_sidecar(tmp_path, "docs/a.md", {
            "slug": "a", "doc_type": "unknown", "current_path": "docs/a.md",
            "status": "embedded", "generation": 1,
            "file_hash": compute_file_hash(doc),
            "file_mtime": os.path.getmtime(str(doc)),
        })
        with patch("carta.embed.pipeline.find_config", return_value=cfg_path), \
             patch("carta.embed.pipeline.upsert_chunks") as up:
            result = run_embed_file(doc, CFG)
        assert result["status"] == "skipped"
        up.assert_not_called()


def test_mark_sidecar_stale_excludes_generation_in_memory_qdrant():
    """exclude_generation keeps the freshly written generation un-flagged."""
    from datetime import datetime, timezone
    from qdrant_client import QdrantClient
    from qdrant_client.models import Distance, PointStruct, VectorParams
    from carta.embed.lifecycle import mark_sidecar_stale

    client = QdrantClient(":memory:")
    client.create_collection("c", vectors_config=VectorParams(size=2, distance=Distance.COSINE))
    client.upsert("c", points=[
        PointStruct(id=1, vector=[1.0, 0.0], payload={"sidecar_id": "s", "doc_generation": 1}),
        PointStruct(id=2, vector=[0.0, 1.0], payload={"sidecar_id": "s", "doc_generation": 2}),
    ])
    mark_sidecar_stale(client, "c", "s", datetime.now(timezone.utc), exclude_generation=2)
    pts = {p.id: p.payload for p in client.retrieve("c", ids=[1, 2], with_payload=True)}
    assert pts[1].get("status") == "stale"
    assert pts[2].get("status") != "stale"
