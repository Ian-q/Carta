import yaml
from pathlib import Path
import pytest

from carta.embed.flags import flag_file, clear_flag, list_flagged, FLAG_FIELDS
from carta.embed.induct import sidecar_path, read_sidecar

CFG = {"project_name": "t", "qdrant_url": "http://localhost:6333", "embed": {}}


@pytest.fixture
def repo(tmp_path):
    (tmp_path / ".carta").mkdir()
    d = tmp_path / "docs" / "reference"
    d.mkdir(parents=True)
    (d / "note.md").write_text("# hello\nbody\n")
    return tmp_path


def test_flag_sets_fields_and_stub_for_unembedded(repo):
    sc = flag_file(repo, CFG, Path("docs/reference/note.md"), "MSD miss 2026-07-29")
    assert sc["priority"] == "high"
    assert sc["deep_scan"] == "requested"
    assert sc["deep_scan_reason"] == "MSD miss 2026-07-29"
    assert sc["deep_scan_requested_at"]
    on_disk = read_sidecar(sidecar_path(repo / "docs/reference/note.md", repo))
    assert on_disk["priority"] == "high"


def test_flag_pdf_force_queues_all_pages(repo, monkeypatch):
    (repo / "docs/reference/draw.pdf").write_bytes(b"%PDF-fake")
    monkeypatch.setattr("carta.embed.flags._pdf_page_count", lambda p: 3)
    sc = flag_file(repo, CFG, Path("docs/reference/draw.pdf"), "vector CAD dark")
    assert sc["visual_pending"] == [1, 2, 3]
    assert sc["visual_done"] == []


def test_flag_unknown_path_raises(repo):
    with pytest.raises(FileNotFoundError):
        flag_file(repo, CFG, Path("docs/nope.pdf"), "x")


def test_clear_and_list(repo):
    flag_file(repo, CFG, Path("docs/reference/note.md"), "r")
    assert [Path(s["current_path"]).name for s in list_flagged(repo)] == ["note.md"]
    assert clear_flag(repo, Path("docs/reference/note.md")) is True
    assert list_flagged(repo) == []
    on_disk = read_sidecar(sidecar_path(repo / "docs/reference/note.md", repo))
    for f in FLAG_FIELDS:
        assert f not in on_disk
