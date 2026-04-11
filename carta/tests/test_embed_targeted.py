"""Tests for carta embed <files> targeted path."""
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch, call
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def _make_args(files):
    args = MagicMock()
    args.files = files
    return args


@patch("carta.embed.pipeline.run_embed_file")
@patch("carta.config.load_config")
@patch("carta.config.find_config")
def test_targeted_calls_run_embed_file(mock_find_config, mock_load_config, mock_run_embed_file, tmp_path):
    """When files are passed, run_embed_file is called for each, lock is skipped."""
    from carta.cli import cmd_embed

    cfg_path = tmp_path / ".carta" / "config.yaml"
    cfg_path.parent.mkdir(parents=True)
    cfg_path.touch()
    mock_find_config.return_value = cfg_path
    mock_load_config.return_value = {
        "modules": {"doc_embed": True},
        "qdrant_url": "http://localhost:6333",
        "embed": {"ollama_url": "http://localhost:11434", "ollama_model": "nomic-embed-text"},
    }
    mock_run_embed_file.return_value = {"status": "ok", "chunks": 42}

    pdf = tmp_path / "test.pdf"
    pdf.touch()

    with patch("carta.cli._acquire_embed_lock") as mock_lock, \
         patch("carta.ui.Progress") as MockProgress:
        mock_progress = MagicMock()
        mock_progress.__enter__ = MagicMock(return_value=mock_progress)
        mock_progress.__exit__ = MagicMock(return_value=False)
        MockProgress.return_value = mock_progress

        with pytest.raises(SystemExit) as exc_info:
            cmd_embed(_make_args([str(pdf)]))

        assert exc_info.value.code == 0
        # Lock must NOT be acquired for targeted embed
        mock_lock.assert_not_called()
        # run_embed_file called with force=True
        mock_run_embed_file.assert_called_once_with(
            Path(str(pdf)), mock_load_config.return_value, force=True, progress=mock_progress
        )


@patch("carta.embed.pipeline.run_embed_file")
@patch("carta.config.load_config")
@patch("carta.config.find_config")
def test_targeted_missing_file_exits_1(mock_find_config, mock_load_config, mock_run_embed_file, tmp_path):
    """FileNotFoundError from run_embed_file causes exit(1)."""
    from carta.cli import cmd_embed

    cfg_path = tmp_path / ".carta" / "config.yaml"
    cfg_path.parent.mkdir(parents=True)
    cfg_path.touch()
    mock_find_config.return_value = cfg_path
    mock_load_config.return_value = {
        "modules": {"doc_embed": True},
        "qdrant_url": "http://localhost:6333",
        "embed": {},
    }
    mock_run_embed_file.side_effect = FileNotFoundError("no such file: ghost.pdf")

    with patch("carta.ui.Progress") as MockProgress:
        mock_progress = MagicMock()
        mock_progress.__enter__ = MagicMock(return_value=mock_progress)
        mock_progress.__exit__ = MagicMock(return_value=False)
        MockProgress.return_value = mock_progress

        with pytest.raises(SystemExit) as exc_info:
            cmd_embed(_make_args(["ghost.pdf"]))

        assert exc_info.value.code == 1


@patch("carta.embed.pipeline.run_embed_file")
@patch("carta.config.load_config")
@patch("carta.config.find_config")
def test_targeted_multiple_files_all_processed(mock_find_config, mock_load_config, mock_run_embed_file, tmp_path):
    """All files are processed even if one errors; exit 1 if any errors."""
    from carta.cli import cmd_embed

    cfg_path = tmp_path / ".carta" / "config.yaml"
    cfg_path.parent.mkdir(parents=True)
    cfg_path.touch()
    mock_find_config.return_value = cfg_path
    mock_load_config.return_value = {
        "modules": {"doc_embed": True},
        "qdrant_url": "http://localhost:6333",
        "embed": {},
    }
    mock_run_embed_file.side_effect = [
        {"status": "ok", "chunks": 10},
        FileNotFoundError("missing.pdf not found"),
        {"status": "ok", "chunks": 5},
    ]

    with patch("carta.ui.Progress") as MockProgress:
        mock_progress = MagicMock()
        mock_progress.__enter__ = MagicMock(return_value=mock_progress)
        mock_progress.__exit__ = MagicMock(return_value=False)
        MockProgress.return_value = mock_progress

        with pytest.raises(SystemExit) as exc_info:
            cmd_embed(_make_args(["a.pdf", "missing.pdf", "b.pdf"]))

        assert exc_info.value.code == 1
        assert mock_run_embed_file.call_count == 3
