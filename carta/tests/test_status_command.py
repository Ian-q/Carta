import json
import socket

import yaml

from carta import status


def _project(tmp_path):
    root = tmp_path / "proj"
    (root / ".carta" / "sidecars").mkdir(parents=True)
    return root


def _sidecar(sidecars_dir, stem, st):
    (sidecars_dir / f"{stem}.embed-meta.yaml").write_text(
        yaml.dump({"slug": stem, "status": st})
    )


def test_corpus_counts_by_status(tmp_path):
    root = _project(tmp_path)
    sc = root / ".carta" / "sidecars"
    _sidecar(sc, "a", "done")
    _sidecar(sc, "b", "done")
    _sidecar(sc, "c", "pending")
    _sidecar(sc, "d", "stale")
    _sidecar(sc, "e", "extraction_failed")
    _sidecar(sc, "f", "weird")
    snap = status.gather_project_status(root, name="proj", qdrant_url="u")
    assert snap["corpus"] == {
        "total": 6, "done": 2, "pending": 1, "stale": 1,
        "extraction_failed": 1, "other": 1,
    }


def test_corpus_empty_when_no_sidecars(tmp_path):
    root = _project(tmp_path)
    snap = status.gather_project_status(root, name="proj", qdrant_url="u")
    assert snap["corpus"]["total"] == 0
    assert snap["check"] is None
    assert snap["name"] == "proj"


def test_embed_state_never(tmp_path):
    root = _project(tmp_path)
    snap = status.gather_project_status(root, name="proj", qdrant_url="u")
    assert snap["embed"]["state"] == "never"


def test_embed_state_done_with_age(tmp_path):
    root = _project(tmp_path)
    (root / ".carta" / "embed-status.json").write_text(json.dumps({
        "phase": "done", "finished_at": 900.0,
        "embedded": 10, "skipped": 2, "errors": 0, "chunks": 50,
    }))
    snap = status.gather_project_status(root, name="proj", qdrant_url="u", now=1000.0)
    e = snap["embed"]
    assert e["state"] == "done"
    assert e["age_s"] == 100.0
    assert e["embedded"] == 10


def test_embed_state_interrupted_when_pid_dead(tmp_path, monkeypatch):
    root = _project(tmp_path)
    (root / ".carta" / "embed-status.json").write_text(json.dumps({
        "phase": "running", "host": "thishost", "pid": 424242,
        "current_idx": 3, "total": 9, "current_file": "x.pdf", "updated_at": 0.0,
    }))
    monkeypatch.setattr(status, "_pid_alive", lambda pid: False)
    monkeypatch.setattr(socket, "gethostname", lambda: "thishost")
    snap = status.gather_project_status(root, name="proj", qdrant_url="u", now=10_000.0)
    assert snap["embed"]["state"] == "interrupted"


def test_embed_state_running_via_live_lock(tmp_path, monkeypatch):
    root = _project(tmp_path)
    (root / ".carta" / "embed-status.json").write_text(json.dumps({
        "phase": "running", "host": "other", "pid": 1,
        "current_idx": 3, "total": 9, "current_file": "x.pdf",
        "current_file_started_at": 0.0, "updated_at": 0.0,
    }))
    (root / ".carta" / "embed.lock").write_text("4242")
    monkeypatch.setattr(status, "_pid_alive", lambda pid: True)
    snap = status.gather_project_status(root, name="proj", qdrant_url="u", now=100.0)
    assert snap["embed"]["state"] == "running"
    assert snap["embed"]["file_elapsed_s"] == 100.0


def test_embed_state_running_via_lock_no_status(tmp_path, monkeypatch):
    root = _project(tmp_path)
    (root / ".carta" / "embed.lock").write_text("4242")
    monkeypatch.setattr(status, "_pid_alive", lambda pid: True)
    snap = status.gather_project_status(root, name="proj", qdrant_url="u")
    assert snap["embed"]["state"] == "running"


def test_check_populates_qdrant_and_ollama(tmp_path, monkeypatch):
    root = _project(tmp_path)

    class _Coll:
        def __init__(self, name):
            self.name = name

    class _Colls:
        collections = [_Coll("proj_doc"), _Coll("other_doc")]

    class _Info:
        points_count = 42

    class _FakeClient:
        def __init__(self, **kwargs):
            pass

        def get_collections(self):
            return _Colls()

        def get_collection(self, name):
            return _Info()

    import qdrant_client
    monkeypatch.setattr(qdrant_client, "QdrantClient", _FakeClient)

    class _Resp:
        status_code = 200

    import requests
    monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp())

    snap = status.gather_project_status(
        root, name="proj", qdrant_url="http://q", check=True, ollama_url="http://o"
    )
    assert snap["check"]["qdrant"]["reachable"] is True
    assert snap["check"]["qdrant"]["collections"] == {"proj_doc": 42}
    assert snap["check"]["ollama"]["reachable"] is True


def test_check_handles_unreachable_services(tmp_path, monkeypatch):
    root = _project(tmp_path)

    def _boom(*a, **k):
        raise ConnectionError("down")

    import qdrant_client
    monkeypatch.setattr(qdrant_client, "QdrantClient", _boom)
    import requests
    monkeypatch.setattr(requests, "get", _boom)

    snap = status.gather_project_status(
        root, name="proj", qdrant_url="http://q", check=True, ollama_url="http://o"
    )
    assert snap["check"]["qdrant"]["reachable"] is False
    assert snap["check"]["ollama"]["reachable"] is False
    # Local data still present despite service failures.
    assert snap["corpus"]["total"] == 0
