"""Tests for carta/share.py — export/import of a project's embeddings.

The Qdrant client and the HTTP snapshot download/upload (via `requests`) are
mocked; the tar round-trip and manifest read/write run for real against tmp_path.
"""

import json
import tarfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
import yaml

from carta import share


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _collections_response(*names):
    """Build a fake CollectionsResponse with the given collection names."""
    return SimpleNamespace(collections=[SimpleNamespace(name=n) for n in names])


def _make_carta_dir(tmp_path, project_name="myproj", with_sidecars=True):
    """Create a .carta dir with config.yaml (and optionally sidecars) under tmp_path."""
    carta_dir = tmp_path / ".carta"
    (carta_dir).mkdir(parents=True)
    cfg = {"project_name": project_name, "qdrant_url": "http://localhost:6333"}
    (carta_dir / "config.yaml").write_text(yaml.safe_dump(cfg))
    if with_sidecars:
        sc = carta_dir / "sidecars" / "docs"
        sc.mkdir(parents=True)
        (sc / "guide.embed-meta.yaml").write_text("slug: guide\nstatus: indexed\n")
    return carta_dir


# ---------------------------------------------------------------------------
# _discover_collections
# ---------------------------------------------------------------------------

class TestDiscoverCollections:
    def test_filters_by_project_prefix_and_sorts(self):
        client = Mock()
        client.get_collections.return_value = _collections_response(
            "myproj_session", "myproj_doc", "other_doc", "myproj_notes"
        )
        result = share._discover_collections(client, "myproj", include_visual=True)
        assert result == ["myproj_doc", "myproj_notes", "myproj_session"]

    def test_excludes_visual_when_not_included(self):
        client = Mock()
        client.get_collections.return_value = _collections_response(
            "myproj_doc", "myproj_visual"
        )
        result = share._discover_collections(client, "myproj", include_visual=False)
        assert result == ["myproj_doc"]

    def test_includes_visual_when_requested(self):
        client = Mock()
        client.get_collections.return_value = _collections_response(
            "myproj_doc", "myproj_visual"
        )
        result = share._discover_collections(client, "myproj", include_visual=True)
        assert result == ["myproj_doc", "myproj_visual"]

    def test_does_not_match_prefix_of_another_project(self):
        # "myproj2_doc" must NOT be picked up for project "myproj".
        client = Mock()
        client.get_collections.return_value = _collections_response(
            "myproj_doc", "myproj2_doc"
        )
        result = share._discover_collections(client, "myproj", include_visual=True)
        assert result == ["myproj_doc"]


# ---------------------------------------------------------------------------
# manifest build / read
# ---------------------------------------------------------------------------

class TestManifest:
    def test_build_manifest_shape(self):
        collections = [{"name": "myproj_doc", "snapshot": "snapshots/myproj_doc.snapshot", "points": 12}]
        m = share._build_manifest(
            project_name="myproj",
            qdrant_version="1.17.1",
            include_visual=True,
            collections=collections,
            created_at="2026-06-08T00:00:00Z",
        )
        assert m["project_name"] == "myproj"
        assert m["qdrant_version"] == "1.17.1"
        assert m["include_visual"] is True
        assert m["collections"] == collections
        assert m["created_at"] == "2026-06-08T00:00:00Z"
        assert "carta_version" in m

    def test_read_manifest_round_trip(self, tmp_path):
        m = share._build_manifest(
            project_name="myproj", qdrant_version="1.17.1", include_visual=False,
            collections=[{"name": "myproj_doc", "snapshot": "snapshots/myproj_doc.snapshot", "points": 1}],
            created_at="2026-06-08T00:00:00Z",
        )
        (tmp_path / "manifest.json").write_text(json.dumps(m))
        assert share._read_manifest(tmp_path) == m

    def test_read_manifest_missing_raises(self, tmp_path):
        with pytest.raises(ValueError, match="manifest.json"):
            share._read_manifest(tmp_path)

    def test_read_manifest_corrupt_raises(self, tmp_path):
        (tmp_path / "manifest.json").write_text("{not json")
        with pytest.raises(ValueError):
            share._read_manifest(tmp_path)

    def test_read_manifest_missing_fields_raises(self, tmp_path):
        (tmp_path / "manifest.json").write_text(json.dumps({"project_name": "x"}))
        with pytest.raises(ValueError):
            share._read_manifest(tmp_path)


# ---------------------------------------------------------------------------
# _rewrite_collection_name
# ---------------------------------------------------------------------------

class TestRewriteCollectionName:
    def test_rewrites_matching_prefix(self):
        assert share._rewrite_collection_name("myproj_doc", "myproj", "newproj") == "newproj_doc"

    def test_rewrites_visual(self):
        assert share._rewrite_collection_name("myproj_visual", "myproj", "newproj") == "newproj_visual"

    def test_leaves_non_matching_unchanged(self):
        assert share._rewrite_collection_name("other_doc", "myproj", "newproj") == "other_doc"


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

def _export_mock_client(collection_names, points=7):
    """Mock Qdrant client for export: discovery, snapshot create/delete, count."""
    client = Mock()
    client.get_collections.return_value = _collections_response(*collection_names)
    client.create_snapshot.side_effect = (
        lambda collection_name, **kw: SimpleNamespace(name=f"{collection_name}-snap")
    )
    client.count.side_effect = lambda collection_name, **kw: SimpleNamespace(count=points)
    client.delete_snapshot.return_value = True
    return client


def _fake_requests_get(url, **kwargs):
    """Mock requests.get: server-version root vs snapshot download."""
    if url.rstrip("/").endswith("6333"):  # root version probe
        return SimpleNamespace(
            status_code=200, json=lambda: {"version": "1.17.1"}, raise_for_status=lambda: None
        )
    # snapshot download
    return SimpleNamespace(
        status_code=200, content=b"SNAPSHOT-BYTES", raise_for_status=lambda: None
    )


def _extract_names(tar_path):
    with tarfile.open(tar_path, "r:gz") as tf:
        return set(tf.getnames())


class TestRunExport:
    def test_happy_path_bundles_everything(self, tmp_path):
        carta_dir = _make_carta_dir(tmp_path, project_name="myproj")
        cfg = {"project_name": "myproj", "qdrant_url": "http://localhost:6333"}
        client = _export_mock_client(["myproj_doc", "myproj_visual"])

        with patch("carta.share.QdrantClient", return_value=client), \
             patch("carta.share.requests.get", side_effect=_fake_requests_get), \
             patch("carta.share._now_iso", return_value="2026-06-08T00:00:00Z"):
            out = share.run_export(cfg, carta_dir, output_path=tmp_path / "bundle.tar.gz",
                                   include_visual=True, verbose=False)

        assert out == tmp_path / "bundle.tar.gz"
        assert out.exists()
        names = _extract_names(out)
        assert "manifest.json" in names
        assert "config.yaml" in names
        assert "snapshots/myproj_doc.snapshot" in names
        assert "snapshots/myproj_visual.snapshot" in names
        assert any(n.startswith("sidecars/") and n.endswith(".embed-meta.yaml") for n in names)

    def test_manifest_records_collections_and_points(self, tmp_path):
        carta_dir = _make_carta_dir(tmp_path, project_name="myproj")
        cfg = {"project_name": "myproj", "qdrant_url": "http://localhost:6333"}
        client = _export_mock_client(["myproj_doc"], points=42)

        with patch("carta.share.QdrantClient", return_value=client), \
             patch("carta.share.requests.get", side_effect=_fake_requests_get), \
             patch("carta.share._now_iso", return_value="2026-06-08T00:00:00Z"):
            out = share.run_export(cfg, carta_dir, output_path=tmp_path / "b.tar.gz",
                                   include_visual=True, verbose=False)

        with tarfile.open(out, "r:gz") as tf:
            manifest = json.loads(tf.extractfile("manifest.json").read())
        assert manifest["project_name"] == "myproj"
        assert manifest["qdrant_version"] == "1.17.1"
        assert manifest["collections"] == [
            {"name": "myproj_doc", "snapshot": "snapshots/myproj_doc.snapshot", "points": 42}
        ]

    def test_no_visual_excludes_visual_collection(self, tmp_path):
        carta_dir = _make_carta_dir(tmp_path, project_name="myproj")
        cfg = {"project_name": "myproj", "qdrant_url": "http://localhost:6333"}
        client = _export_mock_client(["myproj_doc", "myproj_visual"])

        with patch("carta.share.QdrantClient", return_value=client), \
             patch("carta.share.requests.get", side_effect=_fake_requests_get), \
             patch("carta.share._now_iso", return_value="2026-06-08T00:00:00Z"):
            out = share.run_export(cfg, carta_dir, output_path=tmp_path / "b.tar.gz",
                                   include_visual=False, verbose=False)

        names = _extract_names(out)
        assert "snapshots/myproj_doc.snapshot" in names
        assert "snapshots/myproj_visual.snapshot" not in names

    def test_zero_collections_raises(self, tmp_path):
        carta_dir = _make_carta_dir(tmp_path, project_name="myproj")
        cfg = {"project_name": "myproj", "qdrant_url": "http://localhost:6333"}
        client = _export_mock_client([])  # nothing embedded

        with patch("carta.share.QdrantClient", return_value=client), \
             patch("carta.share.requests.get", side_effect=_fake_requests_get):
            with pytest.raises(RuntimeError, match="[Nn]othing"):
                share.run_export(cfg, carta_dir, output_path=tmp_path / "b.tar.gz",
                                 include_visual=True, verbose=False)

    def test_deletes_server_side_snapshots(self, tmp_path):
        carta_dir = _make_carta_dir(tmp_path, project_name="myproj")
        cfg = {"project_name": "myproj", "qdrant_url": "http://localhost:6333"}
        client = _export_mock_client(["myproj_doc"])

        with patch("carta.share.QdrantClient", return_value=client), \
             patch("carta.share.requests.get", side_effect=_fake_requests_get), \
             patch("carta.share._now_iso", return_value="2026-06-08T00:00:00Z"):
            share.run_export(cfg, carta_dir, output_path=tmp_path / "b.tar.gz",
                             include_visual=True, verbose=False)

        client.delete_snapshot.assert_called_once_with(
            collection_name="myproj_doc", snapshot_name="myproj_doc-snap"
        )

    def test_default_output_path_uses_project_name(self, tmp_path, monkeypatch):
        carta_dir = _make_carta_dir(tmp_path, project_name="myproj")
        cfg = {"project_name": "myproj", "qdrant_url": "http://localhost:6333"}
        client = _export_mock_client(["myproj_doc"])
        monkeypatch.chdir(tmp_path)

        with patch("carta.share.QdrantClient", return_value=client), \
             patch("carta.share.requests.get", side_effect=_fake_requests_get), \
             patch("carta.share._now_iso", return_value="2026-06-08T00:00:00Z"):
            out = share.run_export(cfg, carta_dir, output_path=None,
                                   include_visual=True, verbose=False)

        assert out.exists()
        assert "myproj" in out.name
        assert out.name.endswith(".tar.gz")


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------

def _write_bundle(path, *, project="myproj", collection_points=None,
                  qdrant_url="http://localhost:6333", qdrant_version="1.17.1",
                  with_sidecars=True, include_visual=True):
    """Create a real bundle .tar.gz on disk (as run_export would)."""
    if collection_points is None:
        collection_points = {"myproj_doc": 5}
    import tempfile as _tf
    staging = Path(_tf.mkdtemp())
    (staging / "snapshots").mkdir()
    collections = []
    for name, pts in collection_points.items():
        (staging / "snapshots" / f"{name}.snapshot").write_bytes(b"SNAP:" + name.encode())
        collections.append({"name": name, "snapshot": f"snapshots/{name}.snapshot", "points": pts})
    manifest = {
        "carta_version": "0.6.0", "qdrant_version": qdrant_version,
        "project_name": project, "created_at": "2026-06-08T00:00:00Z",
        "include_visual": include_visual, "collections": collections,
    }
    (staging / "manifest.json").write_text(json.dumps(manifest))
    (staging / "config.yaml").write_text(
        yaml.safe_dump({"project_name": project, "qdrant_url": qdrant_url})
    )
    if with_sidecars:
        sc = staging / "sidecars" / "docs"
        sc.mkdir(parents=True)
        (sc / "guide.embed-meta.yaml").write_text("slug: guide\nstatus: indexed\n")
    with tarfile.open(path, "w:gz") as tf:
        for item in sorted(staging.iterdir()):
            tf.add(item, arcname=item.name)
    return path


def _import_mock_client(existing=()):
    client = Mock()
    existing = set(existing)
    client.collection_exists.side_effect = lambda collection_name, **kw: collection_name in existing
    client.delete_collection.return_value = True
    return client


def _fake_get_version(url, **kwargs):
    return SimpleNamespace(status_code=200, json=lambda: {"version": "1.17.1"},
                           raise_for_status=lambda: None)


class TestRunImport:
    def test_happy_path_restores_and_wires_carta(self, tmp_path):
        bundle = _write_bundle(tmp_path / "b.tar.gz", collection_points={"myproj_doc": 5, "myproj_visual": 9})
        target_carta = tmp_path / "dest" / ".carta"
        target_carta.mkdir(parents=True)
        client = _import_mock_client()

        with patch("carta.share.QdrantClient", return_value=client), \
             patch("carta.share.requests.get", side_effect=_fake_get_version), \
             patch("carta.share._upload_snapshot") as up:
            summary = share.run_import(bundle, target_carta, verbose=False)

        uploaded = {call.args[1] for call in up.call_args_list}  # (qdrant_url, collection, file)
        assert uploaded == {"myproj_doc", "myproj_visual"}
        assert summary["project"] == "myproj"
        assert {r["name"] for r in summary["restored"]} == {"myproj_doc", "myproj_visual"}
        # fresh dest: bundled config + sidecars copied in
        assert (target_carta / "config.yaml").exists()
        assert (target_carta / "sidecars" / "docs" / "guide.embed-meta.yaml").exists()

    def test_existing_collection_without_force_raises(self, tmp_path):
        bundle = _write_bundle(tmp_path / "b.tar.gz", collection_points={"myproj_doc": 5})
        target_carta = tmp_path / ".carta"; target_carta.mkdir()
        client = _import_mock_client(existing=["myproj_doc"])

        with patch("carta.share.QdrantClient", return_value=client), \
             patch("carta.share.requests.get", side_effect=_fake_get_version), \
             patch("carta.share._upload_snapshot") as up:
            with pytest.raises(RuntimeError, match="already exist"):
                share.run_import(bundle, target_carta, force=False, verbose=False)
        up.assert_not_called()  # nothing restored when preflight fails

    def test_existing_collection_with_force_deletes_then_restores(self, tmp_path):
        bundle = _write_bundle(tmp_path / "b.tar.gz", collection_points={"myproj_doc": 5})
        target_carta = tmp_path / ".carta"; target_carta.mkdir()
        client = _import_mock_client(existing=["myproj_doc"])

        with patch("carta.share.QdrantClient", return_value=client), \
             patch("carta.share.requests.get", side_effect=_fake_get_version), \
             patch("carta.share._upload_snapshot") as up:
            share.run_import(bundle, target_carta, force=True, verbose=False)

        client.delete_collection.assert_called_once_with(collection_name="myproj_doc")
        assert up.call_count == 1

    def test_project_rename_rewrites_target_collections(self, tmp_path):
        bundle = _write_bundle(tmp_path / "b.tar.gz", collection_points={"myproj_doc": 5})
        target_carta = tmp_path / ".carta"; target_carta.mkdir()
        client = _import_mock_client()

        with patch("carta.share.QdrantClient", return_value=client), \
             patch("carta.share.requests.get", side_effect=_fake_get_version), \
             patch("carta.share._upload_snapshot") as up:
            summary = share.run_import(bundle, target_carta, project="newproj", verbose=False)

        assert up.call_args_list[0].args[1] == "newproj_doc"
        assert summary["project"] == "newproj"
        written_cfg = yaml.safe_load((target_carta / "config.yaml").read_text())
        assert written_cfg["project_name"] == "newproj"

    def test_version_mismatch_warns_but_proceeds(self, tmp_path, capsys):
        bundle = _write_bundle(tmp_path / "b.tar.gz", qdrant_version="1.10.0")
        target_carta = tmp_path / ".carta"; target_carta.mkdir()
        client = _import_mock_client()

        with patch("carta.share.QdrantClient", return_value=client), \
             patch("carta.share.requests.get", side_effect=_fake_get_version), \
             patch("carta.share._upload_snapshot"):
            share.run_import(bundle, target_carta, verbose=True)

        err = capsys.readouterr().err.lower()
        assert "version" in err and ("warn" in err or "mismatch" in err or "differ" in err)

    def test_sidecars_non_destructive(self, tmp_path):
        bundle = _write_bundle(tmp_path / "b.tar.gz")
        target_carta = tmp_path / ".carta"
        existing_sc = target_carta / "sidecars" / "docs"
        existing_sc.mkdir(parents=True)
        (existing_sc / "guide.embed-meta.yaml").write_text("LOCAL-NEWER\n")
        client = _import_mock_client()

        with patch("carta.share.QdrantClient", return_value=client), \
             patch("carta.share.requests.get", side_effect=_fake_get_version), \
             patch("carta.share._upload_snapshot"):
            share.run_import(bundle, target_carta, verbose=False)

        # local sidecar must be left untouched
        assert (existing_sc / "guide.embed-meta.yaml").read_text() == "LOCAL-NEWER\n"

    def test_qdrant_url_falls_back_to_bundled_config(self, tmp_path):
        bundle = _write_bundle(tmp_path / "b.tar.gz", qdrant_url="http://example:6333")
        target_carta = tmp_path / ".carta"; target_carta.mkdir()
        client = _import_mock_client()

        with patch("carta.share.QdrantClient", return_value=client) as qc, \
             patch("carta.share.requests.get", side_effect=_fake_get_version), \
             patch("carta.share._upload_snapshot"):
            share.run_import(bundle, target_carta, qdrant_url=None, verbose=False)

        qc.assert_called_once()
        assert qc.call_args.kwargs.get("url") == "http://example:6333"

    def test_bad_bundle_missing_manifest_raises(self, tmp_path):
        bad = tmp_path / "bad.tar.gz"
        junk = tmp_path / "junk.txt"; junk.write_text("hi")
        with tarfile.open(bad, "w:gz") as tf:
            tf.add(junk, arcname="junk.txt")
        target_carta = tmp_path / ".carta"; target_carta.mkdir()

        with patch("carta.share.QdrantClient"), \
             patch("carta.share.requests.get", side_effect=_fake_get_version):
            with pytest.raises(ValueError, match="manifest"):
                share.run_import(bad, target_carta, verbose=False)


# ---------------------------------------------------------------------------
# HTTP snapshot helpers (direct)
# ---------------------------------------------------------------------------

class TestSnapshotHttpHelpers:
    def test_upload_snapshot_posts_to_target_endpoint(self, tmp_path):
        snap = tmp_path / "myproj_doc.snapshot"; snap.write_bytes(b"x")
        posted = {}

        def fake_post(url, files=None, timeout=None):
            posted["url"] = url
            return SimpleNamespace(raise_for_status=lambda: None)

        with patch("carta.share.requests.post", side_effect=fake_post):
            share._upload_snapshot("http://h:6333", "newproj_doc", snap)
        assert posted["url"].startswith("http://h:6333/collections/newproj_doc/snapshots/upload")

    def test_create_and_download_happy_path(self, tmp_path):
        client = Mock()
        client.create_snapshot.return_value = SimpleNamespace(name="snap1")
        got = {}

        def fake_get(url, **kw):
            got["url"] = url
            return SimpleNamespace(content=b"DATA", raise_for_status=lambda: None)

        with patch("carta.share.requests.get", side_effect=fake_get):
            dest = share._create_and_download_snapshot(client, "http://h:6333", "myproj_doc", tmp_path)

        assert got["url"] == "http://h:6333/collections/myproj_doc/snapshots/snap1"
        assert dest.read_bytes() == b"DATA"
        client.delete_snapshot.assert_called_once_with(collection_name="myproj_doc", snapshot_name="snap1")

    def test_create_and_download_deletes_snapshot_on_download_failure(self, tmp_path):
        # Even if the HTTP download fails, the server-side snapshot must be cleaned up.
        client = Mock()
        client.create_snapshot.return_value = SimpleNamespace(name="snap1")

        def boom(url, **kw):
            raise RuntimeError("download exploded")

        with patch("carta.share.requests.get", side_effect=boom):
            with pytest.raises(RuntimeError):
                share._create_and_download_snapshot(client, "http://h:6333", "myproj_doc", tmp_path)

        client.delete_snapshot.assert_called_once_with(collection_name="myproj_doc", snapshot_name="snap1")


# ---------------------------------------------------------------------------
# Bundle extraction safety (path traversal + version portability)
# ---------------------------------------------------------------------------

def _force_no_filter_kwarg():
    """Patch TarFile.extractall to reject the `filter=` kwarg (simulates Python < 3.10.12)."""
    real = tarfile.TarFile.extractall

    def fake(self, path=".", members=None, **kw):
        if "filter" in kw:
            raise TypeError("extractall() got an unexpected keyword argument 'filter'")
        return real(self, path, members)

    return patch.object(tarfile.TarFile, "extractall", fake)


def _traversal_bundle(path, tmp_path):
    payload = tmp_path / "payload"
    payload.write_text("pwned")
    with tarfile.open(path, "w:gz") as tf:
        tf.add(payload, arcname="../escape.txt")
    return path


class TestUnpackSafety:
    def test_rejects_path_traversal(self, tmp_path):
        evil = _traversal_bundle(tmp_path / "evil.tar.gz", tmp_path)
        dest = tmp_path / "dest"; dest.mkdir()
        with pytest.raises((ValueError, tarfile.TarError, Exception)):
            share._unpack_bundle(evil, dest)
        assert not (tmp_path / "escape.txt").exists()

    def test_fallback_extracts_normally_without_filter_kwarg(self, tmp_path):
        bundle = _write_bundle(tmp_path / "b.tar.gz")
        dest = tmp_path / "dest"; dest.mkdir()
        with _force_no_filter_kwarg():
            share._unpack_bundle(bundle, dest)
        assert (dest / "manifest.json").exists()
        assert (dest / "snapshots" / "myproj_doc.snapshot").exists()

    def test_fallback_rejects_path_traversal_without_filter_kwarg(self, tmp_path):
        evil = _traversal_bundle(tmp_path / "evil.tar.gz", tmp_path)
        dest = tmp_path / "dest"; dest.mkdir()
        with _force_no_filter_kwarg():
            with pytest.raises(ValueError, match="[Uu]nsafe"):
                share._unpack_bundle(evil, dest)
        assert not (tmp_path / "escape.txt").exists()


class TestImportPartialFailureReporting:
    def test_reports_restored_collections_before_failure(self, tmp_path, capsys):
        bundle = _write_bundle(
            tmp_path / "b.tar.gz",
            collection_points={"myproj_doc": 1, "myproj_visual": 2},
        )
        target_carta = tmp_path / ".carta"; target_carta.mkdir()
        client = _import_mock_client()

        # First upload succeeds, second blows up.
        calls = {"n": 0}

        def flaky_upload(url, collection, snap):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("upload failed")

        with patch("carta.share.QdrantClient", return_value=client), \
             patch("carta.share.requests.get", side_effect=_fake_get_version), \
             patch("carta.share._upload_snapshot", side_effect=flaky_upload):
            with pytest.raises(RuntimeError):
                share.run_import(bundle, target_carta, verbose=True)

        err = capsys.readouterr().err
        assert "myproj_doc" in err  # the one that did restore is reported


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------

class TestCliWiring:
    def test_cmd_export_dispatches_with_include_visual_true(self, tmp_path, monkeypatch):
        from carta import cli
        carta_dir = _make_carta_dir(tmp_path, project_name="myproj")
        monkeypatch.chdir(tmp_path)
        args = SimpleNamespace(no_visual=False, output=str(tmp_path / "out.tar.gz"))

        with patch("carta.share.run_export", return_value=Path("out.tar.gz")) as rx:
            cli.cmd_export(args)

        rx.assert_called_once()
        assert rx.call_args.args[1] == carta_dir
        assert rx.call_args.kwargs["include_visual"] is True
        assert rx.call_args.kwargs["output_path"] == str(tmp_path / "out.tar.gz")

    def test_cmd_export_no_visual_flag(self, tmp_path, monkeypatch):
        from carta import cli
        _make_carta_dir(tmp_path, project_name="myproj")
        monkeypatch.chdir(tmp_path)
        args = SimpleNamespace(no_visual=True, output=None)

        with patch("carta.share.run_export", return_value=Path("x")) as rx:
            cli.cmd_export(args)

        assert rx.call_args.kwargs["include_visual"] is False

    def test_cmd_import_dispatches_with_local_config(self, tmp_path, monkeypatch):
        from carta import cli
        carta_dir = _make_carta_dir(tmp_path, project_name="myproj")
        monkeypatch.chdir(tmp_path)
        args = SimpleNamespace(bundle="b.tar.gz", project=None, force=False)

        with patch("carta.share.run_import", return_value={"restored": []}) as ri:
            cli.cmd_import(args)

        ri.assert_called_once()
        assert ri.call_args.args[0] == "b.tar.gz"
        assert ri.call_args.args[1] == carta_dir
        assert ri.call_args.kwargs["qdrant_url"] == "http://localhost:6333"
        assert ri.call_args.kwargs["force"] is False

    def test_cmd_import_without_config_uses_cwd_and_no_url(self, tmp_path, monkeypatch):
        from carta import cli
        monkeypatch.chdir(tmp_path)  # no .carta here
        args = SimpleNamespace(bundle="b.tar.gz", project="newproj", force=True)

        with patch("carta.share.run_import", return_value={"restored": []}) as ri:
            cli.cmd_import(args)

        assert ri.call_args.args[1] == tmp_path / ".carta"
        assert ri.call_args.kwargs["qdrant_url"] is None
        assert ri.call_args.kwargs["project"] == "newproj"
        assert ri.call_args.kwargs["force"] is True
