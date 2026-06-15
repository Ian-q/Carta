import json
import shutil
from carta import registry


def _mk_project(tmp_path, name="proj"):
    root = tmp_path / name
    (root / ".carta").mkdir(parents=True)
    (root / ".carta" / "config.yaml").write_text(
        f"project_name: {name}\nqdrant_url: http://localhost:6333\n"
    )
    return root


def test_register_creates_file(tmp_path, monkeypatch):
    monkeypatch.setenv("CARTA_HOME", str(tmp_path / "home"))
    root = _mk_project(tmp_path)
    registry.register_project(root, "proj", "http://localhost:6333", now=100.0)
    data = json.loads((tmp_path / "home" / "registry.json").read_text())
    key = str(root.resolve())
    assert data["projects"][key]["name"] == "proj"
    assert data["projects"][key]["qdrant_url"] == "http://localhost:6333"
    assert data["projects"][key]["last_seen"] == 100.0


def test_register_updates_in_place(tmp_path, monkeypatch):
    monkeypatch.setenv("CARTA_HOME", str(tmp_path / "home"))
    root = _mk_project(tmp_path)
    registry.register_project(root, "proj", "u1", now=1.0)
    registry.register_project(root, "proj", "u2", now=2.0)
    entries = registry.load_registry()
    assert len(entries) == 1
    assert entries[0]["qdrant_url"] == "u2"
    assert entries[0]["last_seen"] == 2.0


def test_load_prunes_deleted_projects(tmp_path, monkeypatch):
    monkeypatch.setenv("CARTA_HOME", str(tmp_path / "home"))
    root = _mk_project(tmp_path, "gone")
    registry.register_project(root, "gone", "u", now=1.0)
    shutil.rmtree(root)
    assert registry.load_registry() == []


def test_load_missing_file_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("CARTA_HOME", str(tmp_path / "home"))
    assert registry.load_registry() == []


def test_register_recovers_from_corrupt_file(tmp_path, monkeypatch):
    monkeypatch.setenv("CARTA_HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    (tmp_path / "home" / "registry.json").write_text("{ not json")
    root = _mk_project(tmp_path)
    registry.register_project(root, "proj", "u", now=1.0)
    entries = registry.load_registry()
    assert len(entries) == 1
    assert entries[0]["name"] == "proj"
