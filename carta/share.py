"""Share a project's embeddings between machines.

`carta export` bundles this project's Qdrant collections — plus a copy of the
config, the sidecar metadata, and a manifest — into a single portable `.tar.gz`.
`carta import` restores that bundle into another machine's Qdrant and wires up
`.carta/` so `carta search` works immediately, with no re-embedding.

The mechanism is Qdrant's native snapshot API (via `qdrant-client` for
create/discover and `requests` for the file download/upload the Python client
does not wrap). Snapshots round-trip the `_visual` collection's ColPali
multi-vectors natively, which is why they are preferred over a scroll-and-reupsert
format. Snapshots are coupled to the Qdrant server version, so export records the
version and import warns on a mismatch.
"""

import json
import shutil
import sys
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import requests
import yaml
from qdrant_client import QdrantClient

from carta import __version__

VISUAL_SUFFIX = "_visual"


def _now_iso() -> str:
    """UTC timestamp, second precision, e.g. 2026-06-08T12:00:00Z."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _discover_collections(client, project_name: str, include_visual: bool) -> list[str]:
    """Return this project's Qdrant collection names, sorted.

    Collections are named `{project_name}_{type}`; discovery is prefix-based so it
    picks up whatever types exist (doc, notes, session, visual). The trailing
    underscore in the prefix prevents `myproj` from matching `myproj2_doc`.
    """
    prefix = f"{project_name}_"
    names = [
        c.name
        for c in client.get_collections().collections
        if c.name.startswith(prefix)
    ]
    if not include_visual:
        names = [n for n in names if not n.endswith(VISUAL_SUFFIX)]
    return sorted(names)


def _build_manifest(
    *,
    project_name: str,
    qdrant_version,
    include_visual: bool,
    collections: list[dict],
    created_at: str,
) -> dict:
    """Build the bundle manifest dict.

    `collections` is a list of {"name", "snapshot", "points"} entries.
    """
    return {
        "carta_version": __version__,
        "qdrant_version": qdrant_version,
        "project_name": project_name,
        "created_at": created_at,
        "include_visual": include_visual,
        "collections": collections,
    }


def _read_manifest(bundle_dir) -> dict:
    """Read and validate manifest.json from an unpacked bundle directory."""
    path = Path(bundle_dir) / "manifest.json"
    if not path.exists():
        raise ValueError("Not a Carta bundle: manifest.json is missing")
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        raise ValueError(f"Corrupt manifest.json: {e}")
    for field in ("project_name", "collections"):
        if field not in data:
            raise ValueError(f"Invalid manifest.json: missing '{field}'")
    return data


def _rewrite_collection_name(name: str, old_project: str, new_project: str) -> str:
    """Rewrite a `{old_project}_*` collection name to use `new_project`."""
    prefix = f"{old_project}_"
    if name.startswith(prefix):
        return f"{new_project}_{name[len(prefix):]}"
    return name


def _qdrant_server_version(qdrant_url: str):
    """Best-effort Qdrant server version from the REST root; None on failure."""
    try:
        resp = requests.get(qdrant_url.rstrip("/") + "/", timeout=5)
        resp.raise_for_status()
        return resp.json().get("version")
    except Exception:
        return None


def _create_and_download_snapshot(client, qdrant_url: str, collection: str, dest_dir: Path) -> Path:
    """Snapshot `collection`, download it to dest_dir, delete the server-side copy.

    The server-side snapshot is always cleaned up — even if the download fails —
    so a failed export doesn't leave snapshots piling up in Qdrant's storage dir.

    Returns the path to the downloaded `.snapshot` file.
    """
    snap = client.create_snapshot(collection_name=collection)
    name = snap.name
    dest = Path(dest_dir) / f"{collection}.snapshot"
    try:
        url = f"{qdrant_url.rstrip('/')}/collections/{collection}/snapshots/{name}"
        resp = requests.get(url, timeout=600)
        resp.raise_for_status()
        dest.write_bytes(resp.content)
    finally:
        try:
            client.delete_snapshot(collection_name=collection, snapshot_name=name)
        except Exception:
            # Best-effort cleanup; never mask the original error.
            pass
    return dest


def _upload_snapshot(qdrant_url: str, collection: str, snapshot_file: Path) -> None:
    """Restore `collection` on the target Qdrant from a local snapshot file."""
    url = f"{qdrant_url.rstrip('/')}/collections/{collection}/snapshots/upload?priority=snapshot"
    with open(snapshot_file, "rb") as fh:
        resp = requests.post(url, files={"snapshot": (Path(snapshot_file).name, fh)}, timeout=1800)
    resp.raise_for_status()


def run_export(cfg: dict, carta_dir: Path, *, output_path=None,
               include_visual: bool = True, verbose: bool = True) -> Path:
    """Bundle this project's embeddings into a portable `.tar.gz`.

    Args:
        cfg: loaded carta config (needs project_name, qdrant_url).
        carta_dir: the project's `.carta/` directory (holds config.yaml, sidecars/).
        output_path: destination file; defaults to ./carta-<project>-<date>.tar.gz.
        include_visual: include the `_visual` ColPali collection (default True).
        verbose: print progress.

    Returns:
        Path to the written bundle.

    Raises:
        RuntimeError: if Qdrant is unreachable or no collections exist.
    """
    project_name = cfg["project_name"]
    qdrant_url = cfg["qdrant_url"]
    carta_dir = Path(carta_dir)

    try:
        client = QdrantClient(url=qdrant_url, timeout=30)
        collections = _discover_collections(client, project_name, include_visual)
    except Exception as e:
        raise RuntimeError(f"Could not reach Qdrant at {qdrant_url}: {e}")

    if not collections:
        raise RuntimeError(
            f"Nothing to export: no collections found for project '{project_name}'. "
            "Run `carta embed` first."
        )

    created_at = _now_iso()
    if output_path is None:
        date = created_at[:10].replace("-", "")
        output_path = Path.cwd() / f"carta-{project_name}-{date}.tar.gz"
    output_path = Path(output_path)

    qdrant_version = _qdrant_server_version(qdrant_url)
    staging = Path(tempfile.mkdtemp(prefix="carta-export-"))
    try:
        snap_dir = staging / "snapshots"
        snap_dir.mkdir()
        manifest_collections = []
        for collection in collections:
            if verbose:
                print(f"Snapshotting {collection} ...", flush=True)
            _create_and_download_snapshot(client, qdrant_url, collection, snap_dir)
            points = client.count(collection_name=collection).count
            manifest_collections.append({
                "name": collection,
                "snapshot": f"snapshots/{collection}.snapshot",
                "points": points,
            })

        # Copy config + sidecars so the receiver is fully wired.
        config_src = carta_dir / "config.yaml"
        if config_src.exists():
            shutil.copy2(config_src, staging / "config.yaml")
        sidecars_src = carta_dir / "sidecars"
        if sidecars_src.exists():
            shutil.copytree(sidecars_src, staging / "sidecars")

        manifest = _build_manifest(
            project_name=project_name,
            qdrant_version=qdrant_version,
            include_visual=include_visual,
            collections=manifest_collections,
            created_at=created_at,
        )
        (staging / "manifest.json").write_text(json.dumps(manifest, indent=2))

        output_path.parent.mkdir(parents=True, exist_ok=True)
        with tarfile.open(output_path, "w:gz") as tf:
            for item in sorted(staging.iterdir()):
                tf.add(item, arcname=item.name)
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    if verbose:
        size_mb = output_path.stat().st_size / (1024 * 1024)
        total_points = sum(c["points"] for c in manifest_collections)
        print(
            f"Exported {len(collections)} collection(s), {total_points} points "
            f"-> {output_path} ({size_mb:.1f} MB)"
        )
        print("Send this file to your collaborator and have them run `carta import`.")
    return output_path


def _is_within(directory: Path, target: Path) -> bool:
    """True if `target` resolves to a path inside `directory`."""
    try:
        target.resolve().relative_to(directory.resolve())
        return True
    except ValueError:
        return False


def _unpack_bundle(bundle_path: Path, dest_dir: Path) -> None:
    """Extract a bundle tarball into dest_dir, safe against path traversal.

    Prefers tarfile's `data` filter (Python 3.12+, backported to 3.10.12/3.11.4).
    On older interpreters that lack the `filter=` kwarg, falls back to a manual
    member-vetting pass so a malicious bundle can't escape dest_dir via `..` or
    absolute paths or symlinks.
    """
    dest_dir = Path(dest_dir)
    with tarfile.open(bundle_path, "r:gz") as tf:
        try:
            tf.extractall(dest_dir, filter="data")
            return
        except TypeError:
            pass  # Python < 3.10.12 / < 3.11.4: no `data` filter available.
        for member in tf.getmembers():
            if member.issym() or member.islnk():
                raise ValueError(f"Unsafe link in bundle: {member.name}")
            if not _is_within(dest_dir, dest_dir / member.name):
                raise ValueError(f"Unsafe path in bundle: {member.name}")
        tf.extractall(dest_dir)


def _copy_sidecars_non_destructive(src_dir: Path, dst_dir: Path) -> int:
    """Copy sidecar files from src to dst, skipping any that already exist locally.

    Returns the number of files written.
    """
    if not src_dir.exists():
        return 0
    written = 0
    for src in src_dir.rglob("*"):
        if not src.is_file():
            continue
        rel = src.relative_to(src_dir)
        dst = dst_dir / rel
        if dst.exists():
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        written += 1
    return written


def run_import(bundle_path, carta_dir: Path, *, qdrant_url=None, project=None,
               force: bool = False, verbose: bool = True) -> dict:
    """Restore a bundle into the local Qdrant and wire up `.carta/`.

    Args:
        bundle_path: path to a `carta export` .tar.gz.
        carta_dir: target `.carta/` directory (created if missing).
        qdrant_url: target Qdrant; falls back to the bundled config's url, then
            the default localhost.
        project: rename the project on import (rewrites collection names + config).
        force: delete any pre-existing target collections before restoring.
        verbose: print progress.

    Returns:
        Summary dict: {"project", "restored": [{"name","points"}], "sidecars_written",
        "qdrant_url"}.

    Raises:
        ValueError: bundle is not a valid Carta bundle.
        RuntimeError: Qdrant unreachable, or target collections exist without force.
    """
    carta_dir = Path(carta_dir)
    staging = Path(tempfile.mkdtemp(prefix="carta-import-"))
    try:
        _unpack_bundle(Path(bundle_path), staging)
        manifest = _read_manifest(staging)

        bundled_cfg = {}
        bundled_cfg_path = staging / "config.yaml"
        if bundled_cfg_path.exists():
            bundled_cfg = yaml.safe_load(bundled_cfg_path.read_text()) or {}

        effective_url = qdrant_url or bundled_cfg.get("qdrant_url") or "http://localhost:6333"
        source_project = manifest["project_name"]
        target_project = project or source_project

        # Map each bundled collection to its target name + snapshot file.
        targets = []
        for entry in manifest["collections"]:
            src_name = entry["name"]
            tgt_name = _rewrite_collection_name(src_name, source_project, target_project)
            targets.append({
                "name": tgt_name,
                "points": entry.get("points"),
                "snapshot": staging / entry["snapshot"],
            })

        try:
            client = QdrantClient(url=effective_url, timeout=30)
            # Version preflight (warn-only).
            server_version = _qdrant_server_version(effective_url)
            bundle_version = manifest.get("qdrant_version")
            if server_version and bundle_version and server_version != bundle_version:
                print(
                    f"Warning: Qdrant version mismatch — bundle made with "
                    f"{bundle_version}, this server is {server_version}. "
                    "Snapshots are usually compatible across close versions; "
                    "restore may fail if they diverge.",
                    file=sys.stderr,
                )
            # Conflict preflight (before any upload).
            conflicts = [t["name"] for t in targets if client.collection_exists(collection_name=t["name"])]
        except Exception as e:
            raise RuntimeError(f"Could not reach Qdrant at {effective_url}: {e}")

        if conflicts and not force:
            raise RuntimeError(
                "These collections already exist: " + ", ".join(sorted(conflicts)) +
                ". Re-run with --force to overwrite them."
            )
        if conflicts and force:
            for name in conflicts:
                client.delete_collection(collection_name=name)

        restored = []
        try:
            for t in targets:
                if verbose:
                    print(f"Restoring {t['name']} ...", flush=True)
                _upload_snapshot(effective_url, t["name"], t["snapshot"])
                restored.append({"name": t["name"], "points": t["points"]})
        except Exception:
            if restored:
                names = ", ".join(r["name"] for r in restored)
                print(
                    f"Import failed partway. Already restored: {names}. "
                    "Re-run with --force to retry cleanly.",
                    file=sys.stderr,
                )
            raise

        # Wire up .carta/: config + sidecars.
        carta_dir.mkdir(parents=True, exist_ok=True)
        config_dst = carta_dir / "config.yaml"
        if not config_dst.exists() and bundled_cfg:
            out_cfg = dict(bundled_cfg)
            out_cfg["project_name"] = target_project
            config_dst.write_text(yaml.safe_dump(out_cfg, sort_keys=False))
        elif config_dst.exists():
            local_cfg = yaml.safe_load(config_dst.read_text()) or {}
            if local_cfg.get("project_name") != target_project:
                print(
                    f"Warning: local config project_name is "
                    f"'{local_cfg.get('project_name')}' but restored collections use "
                    f"'{target_project}'. Set project_name to '{target_project}' in "
                    f"{config_dst} for `carta search` to find them.",
                    file=sys.stderr,
                )

        sidecars_written = _copy_sidecars_non_destructive(
            staging / "sidecars", carta_dir / "sidecars"
        )
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    # Stamp a terminal embed-status.json so `carta status` reflects the imported
    # corpus instead of reporting the project as 'embed never run' (#80). Import
    # restores vectors + sidecars but never runs the embed pipeline, so nothing
    # else writes this file. Best-effort: never fail the import over a status write.
    try:
        from carta.embed.status import StatusWriter
        sidecars_dir = carta_dir / "sidecars"
        n_docs = (sum(1 for _ in sidecars_dir.rglob("*.embed-meta.yaml"))
                  if sidecars_dir.is_dir() else 0)
        sw = StatusWriter(carta_dir.parent, enabled=True)
        sw.start(total=n_docs)
        if n_docs:
            sw.file_done(embedded=n_docs)
        sw.finish("done")
    except Exception:
        pass

    summary = {
        "project": target_project,
        "restored": restored,
        "sidecars_written": sidecars_written,
        "qdrant_url": effective_url,
    }
    if verbose:
        total = sum(r["points"] or 0 for r in restored)
        print(
            f"Imported {len(restored)} collection(s), {total} points into {effective_url}."
        )
        if sidecars_written:
            print(f"Wrote {sidecars_written} new sidecar file(s).")
        print(f'Try: carta search "your query"')
    return summary
