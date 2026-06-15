"""Global registry of carta projects on this machine.

Records each project carta runs in so `carta status` can list projects beyond
the current directory — without crawling the filesystem. Lives at
``~/.carta/registry.json`` (override the home dir with ``CARTA_HOME``).

All functions are best-effort and never raise: a registry failure must never
affect the command that triggered it.
"""

import json
import os
import time
from pathlib import Path

SCHEMA = 1  # on-disk registry shape; bump when the structure changes


def _carta_home() -> Path:
    override = os.environ.get("CARTA_HOME")
    return Path(override) if override else Path.home() / ".carta"


def registry_path() -> Path:
    """Return the path to the registry JSON file."""
    return _carta_home() / "registry.json"


def _read_raw() -> dict:
    """Return a well-formed {schema, projects} dict; recover from missing/corrupt."""
    try:
        data = json.loads(registry_path().read_text())
        if isinstance(data, dict) and isinstance(data.get("projects"), dict):
            return data
    except Exception:
        pass
    return {"schema": SCHEMA, "projects": {}}


def _config_exists(path: str) -> bool:
    return (Path(path) / ".carta" / "config.yaml").exists()


def register_project(
    repo_root: str | Path, name: str, qdrant_url: str, *, now: float | None = None
) -> None:
    """Upsert a project entry, keyed by absolute root path.

    Best-effort: any error is swallowed. Dead entries are pruned at read
    time by load_registry (not here), so a project on a temporarily
    unavailable path is never evicted by an unrelated write.
    """
    try:
        key = str(Path(repo_root).resolve())
        data = _read_raw()
        projects = data["projects"]
        projects[key] = {
            "name": name,
            "qdrant_url": qdrant_url,
            "last_seen": time.time() if now is None else now,
        }
        data["projects"] = projects
        data["schema"] = SCHEMA
        path = registry_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        try:
            tmp.write_text(json.dumps(data, indent=2))
            os.replace(tmp, path)
        except Exception:
            tmp.unlink(missing_ok=True)
            raise
    except Exception:
        pass


def load_registry() -> list[dict]:
    """Return live registry entries (pruning projects whose config is gone).

    Each entry: {"path", "name", "qdrant_url", "last_seen"}. Never raises.
    """
    data = _read_raw()
    out = []
    for path, entry in data["projects"].items():
        if not _config_exists(path):
            continue
        out.append({
            "path": path,
            "name": entry.get("name") or Path(path).name,
            "qdrant_url": entry.get("qdrant_url"),
            "last_seen": entry.get("last_seen") or 0,
        })
    return out
