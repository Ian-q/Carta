from pathlib import Path

from carta.hook import claude_md_sidecar as sc


def test_section_hash_is_deterministic_and_changes_with_text():
    assert sc.section_hash("hello") == sc.section_hash("hello")
    assert sc.section_hash("hello") != sc.section_hash("world")


def test_sync_sidecar_path(tmp_path: Path):
    assert sc.sync_sidecar_path(tmp_path) == tmp_path / ".carta" / "sidecars" / "CLAUDE.md.sync.yaml"


def test_load_missing_returns_defaults(tmp_path: Path):
    data = sc.load_sync_sidecar(tmp_path)
    assert data == {"schema": 1, "last_synced": None, "sections": {}}


def test_load_corrupt_returns_defaults(tmp_path: Path):
    p = sc.sync_sidecar_path(tmp_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{not: valid: yaml: [", encoding="utf-8")
    data = sc.load_sync_sidecar(tmp_path)
    assert data["sections"] == {}


def test_write_then_load_round_trips(tmp_path: Path):
    sc.write_sync_sidecar(tmp_path, {
        "schema": 1,
        "last_synced": "2026-06-26T00:00:00+00:00",
        "sections": {"## A": {"hash": "abc", "pinned": True, "last_reviewed": "2026-06-26T00:00:00+00:00"}},
    })
    data = sc.load_sync_sidecar(tmp_path)
    assert data["last_synced"] == "2026-06-26T00:00:00+00:00"
    assert data["sections"]["## A"]["pinned"] is True


def _write_embed_sidecar(repo_root: Path, name: str, indexed_at):
    p = repo_root / ".carta" / "sidecars" / "docs" / f"{name}.embed-meta.yaml"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"slug: {name}\nindexed_at: {indexed_at}\n", encoding="utf-8")


def test_graph_changed_when_no_last_synced(tmp_path: Path):
    assert sc.graph_changed_since(tmp_path, None) is True


def test_graph_unchanged_when_no_embed_newer(tmp_path: Path):
    _write_embed_sidecar(tmp_path, "a", "2026-06-20T00:00:00+00:00")
    assert sc.graph_changed_since(tmp_path, "2026-06-25T00:00:00+00:00") is False


def test_graph_changed_when_embed_is_newer(tmp_path: Path):
    _write_embed_sidecar(tmp_path, "a", "2026-06-26T12:00:00+00:00")
    assert sc.graph_changed_since(tmp_path, "2026-06-25T00:00:00+00:00") is True


def test_graph_changed_handles_z_suffix(tmp_path: Path):
    _write_embed_sidecar(tmp_path, "a", "2026-06-26T12:00:00Z")
    assert sc.graph_changed_since(tmp_path, "2026-06-25T00:00:00Z") is True
