"""`carta sessions index` end to end through the CLI, with services faked."""
import json
import sys
from unittest.mock import patch

import pytest
import yaml
from qdrant_client import QdrantClient

from carta.sessions.project import transcript_slug
from carta.sessions.tests.synth import standard_session
from carta.sessions.tests.test_index import fake_embedding


@pytest.fixture
def project(tmp_path, monkeypatch):
    root = tmp_path / "widgets"
    (root / ".carta").mkdir(parents=True)
    tdir = tmp_path / "projects"
    (root / ".carta" / "config.yaml").write_text(yaml.safe_dump({
        "project_name": "widgets", "qdrant_url": "http://unused.invalid:6333",
        "sessions": {"transcripts_dir": str(tdir)},
        "embed": {"embedding_workers": 1},
    }))
    path = standard_session(str(root)).write(
        tdir / transcript_slug(str(root)) / "sess-alpha.jsonl")
    monkeypatch.chdir(root)
    return {"root": root, "tdir": tdir, "path": path}


def _main(argv, monkeypatch):
    from carta import cli
    monkeypatch.setattr(sys, "argv", ["carta", *argv])
    cli.main()


def test_index_dry_run_json(project, monkeypatch, capsys):
    _main(["sessions", "index", "--backfill", "--dry-run", "--json"], monkeypatch)
    out = json.loads(capsys.readouterr().out)
    assert out["indexed"] == 1
    assert out["records"][0]["session_id"] == "sess-alpha"


def test_index_writes_to_qdrant(project, monkeypatch, capsys):
    client = QdrantClient(":memory:")
    with patch("carta.sessions.index.QdrantClient", return_value=client), \
         patch("carta.embed.embed.get_embedding", side_effect=fake_embedding):
        _main(["sessions", "index", "--session", str(project["path"])], monkeypatch)
    assert "Indexed 1 session(s)" in capsys.readouterr().out
    assert client.count("widgets_sessions").count >= 1


def test_missing_session_file_errors(project, monkeypatch, capsys):
    with pytest.raises(SystemExit) as e:
        _main(["sessions", "index", "--session", "nope.jsonl"], monkeypatch)
    assert e.value.code == 1


def test_bare_sessions_prints_usage(project, monkeypatch, capsys):
    with pytest.raises(SystemExit) as e:
        _main(["sessions"], monkeypatch)
    assert e.value.code == 1
    assert "usage" in capsys.readouterr().err
