import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import requests

from carta import __version__

PYPI_URL = "https://pypi.org/pypi/carta-cc/json"
CACHE_FILENAME = "update-check.json"
CHECK_INTERVAL_HOURS = 24


def _installed_version() -> str:
    return __version__


def _fetch_latest(timeout: float = 2.0) -> Optional[str]:
    """Fetch latest carta-cc version from PyPI. Returns None on any failure."""
    try:
        resp = requests.get(PYPI_URL, timeout=timeout)
        resp.raise_for_status()
        return resp.json()["info"]["version"]
    except Exception:
        return None


def _read_cache(carta_dir: Path) -> dict:
    try:
        return json.loads((carta_dir / CACHE_FILENAME).read_text())
    except OSError:
        return {}
    except json.JSONDecodeError:
        print(f"Warning: corrupt update cache at {carta_dir / CACHE_FILENAME}, ignoring.", file=sys.stderr)
        return {}


def _write_cache(carta_dir: Path, latest: str, notified: str) -> None:
    """Write update check result to cache file. Silently ignores OSError (e.g. read-only fs)."""
    data = {
        "checked_at": datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
        "latest": latest,
        "notified": notified,
    }
    try:
        (carta_dir / CACHE_FILENAME).write_text(json.dumps(data, indent=2))
    except OSError:
        pass


def _is_cache_stale(cache: dict) -> bool:
    checked_at = cache.get("checked_at")
    if not checked_at:
        return True
    try:
        return datetime.now(timezone.utc).replace(tzinfo=None) - datetime.fromisoformat(checked_at) > timedelta(hours=CHECK_INTERVAL_HOURS)
    except (ValueError, TypeError):
        return True


def _version_tuple(v: str) -> tuple:
    try:
        return tuple(int(re.match(r"(\d+)", part).group(1)) for part in v.split(".") if re.match(r"\d", part))
    except (ValueError, AttributeError):
        return (0,)


def check_for_update(carta_dir: Optional[Path]) -> Optional[str]:
    """Return a notification string if an unnotified newer version is available, else None.

    Reads cache from carta_dir if provided. Re-fetches PyPI if cache is stale (>24h).
    Returns None if already up-to-date, PyPI unreachable, or this version was already notified.

    When carta_dir is None there is no project directory available, so no cache is read or
    written. This means the "already notified" guard cannot persist between calls — every
    call with carta_dir=None that finds a newer version will return a notification string.
    This is intentional: the caller has no project dir, so persistent suppression is not
    possible.
    """
    installed = _installed_version()
    cache = _read_cache(carta_dir) if carta_dir else {}

    if _is_cache_stale(cache):
        latest = _fetch_latest()
        if latest is None:
            return None
        notified = cache.get("notified", "")
        if carta_dir:
            _write_cache(carta_dir, latest, notified)
    else:
        latest = cache.get("latest", installed)

    notified = cache.get("notified", "")

    if _version_tuple(latest) <= _version_tuple(installed):
        return None
    if notified == latest:
        return None

    # Mark this version as notified so we don't repeat it
    if carta_dir:
        _write_cache(carta_dir, latest, latest)

    return (
        f"carta {latest} is available (you have {installed}). "
        f"Run `carta update` to upgrade."
    )


def maybe_notify(carta_dir: Optional[Path], cfg: dict) -> None:
    """Print an update notification if a newer version is available.

    Respects update_check config key. Silently swallows all errors.
    """
    if not cfg.get("update_check", True):
        return
    try:
        msg = check_for_update(carta_dir)
        if msg:
            sep = "─" * 51
            print(f"\n{sep}\n{msg}\n{sep}")
    except Exception:
        pass
