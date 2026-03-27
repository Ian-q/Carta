"""Tests for MCP service layer primitives (Plan 02-01).

Covers:
  - find_config importable from carta.config
  - generate_sidecar_stub includes file_mtime
  - run_embed_file single-file adapter (skip, force, ok, FileNotFoundError)
  - check_embed_drift mtime-based drift detection
"""
import os
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml


# ---------------------------------------------------------------------------
# Task 1: find_config
# ---------------------------------------------------------------------------

def test_find_config_importable_from_config():
    from carta.config import find_config
    assert callable(find_config)


def test_find_config_walks_up_to_find_config_yaml(tmp_path):
    from carta.config import find_config
    # Create a .carta/config.yaml somewhere in the tree
    carta_dir = tmp_path / ".carta"
    carta_dir.mkdir()
    cfg_file = carta_dir / "config.yaml"
    cfg_file.write_text("project_name: test\nqdrant_url: http://localhost:6333\n")
    # Start from a subdirectory
    subdir = tmp_path / "a" / "b" / "c"
    subdir.mkdir(parents=True)
    result = find_config(start=subdir)
    assert result == cfg_file


def test_find_config_raises_file_not_found(tmp_path):
    from carta.config import find_config
    with pytest.raises(FileNotFoundError, match=".carta/config.yaml"):
        find_config(start=tmp_path)


# ---------------------------------------------------------------------------
# Task 1: generate_sidecar_stub includes file_mtime
# ---------------------------------------------------------------------------

def test_sidecar_stub_includes_file_mtime(tmp_path):
    from carta.embed.induct import generate_sidecar_stub
    # Create a dummy PDF file
    docs_dir = tmp_path / "docs" / "reference"
    docs_dir.mkdir(parents=True)
    pdf = docs_dir / "myfile.pdf"
    pdf.write_bytes(b"%PDF-1.4 dummy")
    cfg = {"project_name": "test", "qdrant_url": "http://localhost:6333"}
    stub = generate_sidecar_stub(pdf, tmp_path, cfg)
    assert "file_mtime" in stub
    assert stub["file_mtime"] is None  # stub value is None


# ---------------------------------------------------------------------------
# Task 1: run_embed_file
# ---------------------------------------------------------------------------

MINIMAL_CFG = {
    "project_name": "test",
    "qdrant_url": "http://localhost:6333",
    "embed": {
        "ollama_url": "http://localhost:11434",
        "ollama_model": "nomic-embed-text:latest",
        "chunking": {"max_tokens": 800, "overlap_fraction": 0.15},
    },
    "search": {"top_n": 5},
}


def test_run_embed_file_raises_for_nonexistent_path(tmp_path):
    from carta.embed.pipeline import run_embed_file
    missing = tmp_path / "no_such_file.pdf"
    with pytest.raises(FileNotFoundError):
        run_embed_file(missing, MINIMAL_CFG)


def test_run_embed_file_skips_when_mtime_matches(tmp_path):
    from carta.embed.pipeline import run_embed_file
    from carta.embed.induct import generate_sidecar_stub, write_sidecar

    # Create a PDF and its sidecar with matching mtime
    docs_dir = tmp_path / "docs" / "reference"
    docs_dir.mkdir(parents=True)
    pdf = docs_dir / "test.pdf"
    pdf.write_bytes(b"%PDF-1.4 dummy")

    cfg = {**MINIMAL_CFG, "project_name": "test"}
    stub = generate_sidecar_stub(pdf, tmp_path, cfg)
    # Set mtime in sidecar to match current file mtime
    current_mtime = os.path.getmtime(str(pdf))
    stub["file_mtime"] = current_mtime
    stub["status"] = "embedded"
    sidecar_path = write_sidecar(pdf, stub)

    # Patch find_config to return a fake config path rooted at tmp_path
    fake_cfg_path = tmp_path / ".carta" / "config.yaml"
    fake_cfg_path.parent.mkdir(parents=True, exist_ok=True)
    fake_cfg_path.write_text("project_name: test\nqdrant_url: http://localhost:6333\n")

    with patch("carta.embed.pipeline.find_config", return_value=fake_cfg_path):
        result = run_embed_file(pdf, cfg, force=False)

    assert result["status"] == "skipped"
    assert "already embedded" in result["reason"]


def test_run_embed_file_force_re_embeds(tmp_path):
    from carta.embed.pipeline import run_embed_file
    from carta.embed.induct import generate_sidecar_stub, write_sidecar

    docs_dir = tmp_path / "docs" / "reference"
    docs_dir.mkdir(parents=True)
    pdf = docs_dir / "test.pdf"
    pdf.write_bytes(b"%PDF-1.4 dummy")

    cfg = {**MINIMAL_CFG, "project_name": "test"}
    stub = generate_sidecar_stub(pdf, tmp_path, cfg)
    current_mtime = os.path.getmtime(str(pdf))
    stub["file_mtime"] = current_mtime
    stub["status"] = "embedded"
    write_sidecar(pdf, stub)

    fake_cfg_path = tmp_path / ".carta" / "config.yaml"
    fake_cfg_path.parent.mkdir(parents=True, exist_ok=True)
    fake_cfg_path.write_text("project_name: test\nqdrant_url: http://localhost:6333\n")

    mock_client = MagicMock()
    mock_result = (3, {"status": "embedded", "indexed_at": "now", "chunk_count": 3, "file_mtime": current_mtime})

    with patch("carta.embed.pipeline.find_config", return_value=fake_cfg_path), \
         patch("carta.embed.pipeline.QdrantClient", return_value=mock_client), \
         patch("carta.embed.pipeline.ensure_collection"), \
         patch("carta.embed.pipeline._embed_one_file", return_value=mock_result):
        result = run_embed_file(pdf, cfg, force=True)

    assert result["status"] == "ok"
    assert result["chunks"] == 3


def test_run_embed_file_returns_ok_on_success(tmp_path):
    from carta.embed.pipeline import run_embed_file

    docs_dir = tmp_path / "docs" / "reference"
    docs_dir.mkdir(parents=True)
    pdf = docs_dir / "test.pdf"
    pdf.write_bytes(b"%PDF-1.4 dummy")

    cfg = {**MINIMAL_CFG, "project_name": "test"}

    fake_cfg_path = tmp_path / ".carta" / "config.yaml"
    fake_cfg_path.parent.mkdir(parents=True, exist_ok=True)
    fake_cfg_path.write_text("project_name: test\nqdrant_url: http://localhost:6333\n")

    mock_client = MagicMock()
    current_mtime = os.path.getmtime(str(pdf))
    mock_result = (5, {"status": "embedded", "indexed_at": "now", "chunk_count": 5, "file_mtime": current_mtime})

    with patch("carta.embed.pipeline.find_config", return_value=fake_cfg_path), \
         patch("carta.embed.pipeline.QdrantClient", return_value=mock_client), \
         patch("carta.embed.pipeline.ensure_collection"), \
         patch("carta.embed.pipeline._embed_one_file", return_value=mock_result):
        result = run_embed_file(pdf, cfg)

    assert result["status"] == "ok"
    assert result["chunks"] == 5


# ---------------------------------------------------------------------------
# Task 2: check_embed_drift
# ---------------------------------------------------------------------------

def test_check_embed_drift_empty_when_no_drift(tmp_path):
    from carta.scanner.scanner import check_embed_drift

    cfg = {
        "embed": {
            "reference_docs_path": "docs/reference/",
            "audio_path": "docs/audio/",
        }
    }
    # No files at all
    result = check_embed_drift(tmp_path, cfg)
    assert result == []


def test_check_embed_drift_detects_modified_file(tmp_path):
    from carta.scanner.scanner import check_embed_drift

    docs_dir = tmp_path / "docs" / "reference"
    docs_dir.mkdir(parents=True)
    pdf = docs_dir / "test.pdf"
    pdf.write_bytes(b"%PDF-1.4 dummy")

    # Wait a moment so mtime is clearly different
    old_mtime = os.path.getmtime(str(pdf)) - 10  # pretend sidecar is 10s old
    sidecar = docs_dir / "test.embed-meta.yaml"
    sidecar.write_text(
        f"status: embedded\nfile_mtime: {old_mtime}\nslug: test\ndoc_type: reference\n"
    )

    cfg = {
        "embed": {
            "reference_docs_path": "docs/reference/",
            "audio_path": "docs/audio/",
        }
    }
    result = check_embed_drift(tmp_path, cfg)
    assert len(result) == 1
    assert result[0]["type"] == "embed_drift"
    assert "test.pdf" in result[0]["doc"]


def test_check_embed_drift_skips_pending_sidecars(tmp_path):
    from carta.scanner.scanner import check_embed_drift

    docs_dir = tmp_path / "docs" / "reference"
    docs_dir.mkdir(parents=True)
    pdf = docs_dir / "test.pdf"
    pdf.write_bytes(b"%PDF-1.4 dummy")

    old_mtime = os.path.getmtime(str(pdf)) - 10
    sidecar = docs_dir / "test.embed-meta.yaml"
    sidecar.write_text(
        f"status: pending\nfile_mtime: {old_mtime}\nslug: test\ndoc_type: reference\n"
    )

    cfg = {
        "embed": {
            "reference_docs_path": "docs/reference/",
            "audio_path": "docs/audio/",
        }
    }
    result = check_embed_drift(tmp_path, cfg)
    assert result == []


def test_check_embed_drift_skips_legacy_sidecars_without_mtime(tmp_path):
    from carta.scanner.scanner import check_embed_drift

    docs_dir = tmp_path / "docs" / "reference"
    docs_dir.mkdir(parents=True)
    pdf = docs_dir / "test.pdf"
    pdf.write_bytes(b"%PDF-1.4 dummy")

    sidecar = docs_dir / "test.embed-meta.yaml"
    sidecar.write_text("status: embedded\nslug: test\ndoc_type: reference\n")

    cfg = {
        "embed": {
            "reference_docs_path": "docs/reference/",
            "audio_path": "docs/audio/",
        }
    }
    result = check_embed_drift(tmp_path, cfg)
    assert result == []
