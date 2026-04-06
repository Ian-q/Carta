import json
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# checker tests
# ---------------------------------------------------------------------------

def test_fetch_latest_returns_version_on_success():
    from carta.update.checker import _fetch_latest
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"info": {"version": "1.2.3"}}
    mock_resp.raise_for_status.return_value = None
    with patch("carta.update.checker.requests.get", return_value=mock_resp):
        assert _fetch_latest() == "1.2.3"


def test_fetch_latest_returns_none_on_network_error():
    from carta.update.checker import _fetch_latest
    with patch("carta.update.checker.requests.get", side_effect=Exception("timeout")):
        assert _fetch_latest() is None


def test_is_cache_stale_true_when_empty():
    from carta.update.checker import _is_cache_stale
    assert _is_cache_stale({}) is True


def test_is_cache_stale_true_when_old(tmp_path):
    from carta.update.checker import _is_cache_stale
    old_dt = (datetime.utcnow() - timedelta(hours=25)).isoformat()
    assert _is_cache_stale({"checked_at": old_dt}) is True


def test_is_cache_stale_false_when_fresh():
    from carta.update.checker import _is_cache_stale
    fresh_dt = datetime.utcnow().isoformat()
    assert _is_cache_stale({"checked_at": fresh_dt}) is False


def test_check_for_update_returns_message_when_newer_available(tmp_path):
    from carta.update.checker import check_for_update
    carta_dir = tmp_path / ".carta"
    carta_dir.mkdir()
    with patch("carta.update.checker._installed_version", return_value="0.3.0"), \
         patch("carta.update.checker._fetch_latest", return_value="0.4.0"):
        msg = check_for_update(carta_dir)
    assert msg is not None
    assert "0.4.0" in msg
    assert "carta update" in msg


def test_check_for_update_returns_none_when_up_to_date(tmp_path):
    from carta.update.checker import check_for_update
    carta_dir = tmp_path / ".carta"
    carta_dir.mkdir()
    with patch("carta.update.checker._installed_version", return_value="0.4.0"), \
         patch("carta.update.checker._fetch_latest", return_value="0.4.0"):
        assert check_for_update(carta_dir) is None


def test_check_for_update_returns_none_when_already_notified(tmp_path):
    from carta.update.checker import check_for_update
    carta_dir = tmp_path / ".carta"
    carta_dir.mkdir()
    cache = {
        "checked_at": datetime.utcnow().isoformat(),
        "latest": "0.4.0",
        "notified": "0.4.0",
    }
    (carta_dir / "update-check.json").write_text(json.dumps(cache))
    with patch("carta.update.checker._installed_version", return_value="0.3.0"):
        assert check_for_update(carta_dir) is None


def test_check_for_update_uses_fresh_cache(tmp_path):
    """When cache is fresh, should not call PyPI."""
    from carta.update.checker import check_for_update
    carta_dir = tmp_path / ".carta"
    carta_dir.mkdir()
    cache = {
        "checked_at": datetime.utcnow().isoformat(),
        "latest": "0.4.0",
        "notified": "",
    }
    (carta_dir / "update-check.json").write_text(json.dumps(cache))
    with patch("carta.update.checker._installed_version", return_value="0.3.0"), \
         patch("carta.update.checker._fetch_latest") as mock_fetch:
        msg = check_for_update(carta_dir)
    mock_fetch.assert_not_called()
    assert msg is not None


def test_check_for_update_works_without_carta_dir():
    from carta.update.checker import check_for_update
    with patch("carta.update.checker._installed_version", return_value="0.3.0"), \
         patch("carta.update.checker._fetch_latest", return_value="0.4.0"):
        msg = check_for_update(None)
    assert msg is not None


def test_maybe_notify_prints_when_update_available(tmp_path, capsys):
    from carta.update.checker import maybe_notify
    carta_dir = tmp_path / ".carta"
    carta_dir.mkdir()
    with patch("carta.update.checker._installed_version", return_value="0.3.0"), \
         patch("carta.update.checker._fetch_latest", return_value="0.4.0"):
        maybe_notify(carta_dir, {"update_check": True})
    captured = capsys.readouterr()
    assert "0.4.0" in captured.out


def test_maybe_notify_silent_when_disabled(tmp_path, capsys):
    from carta.update.checker import maybe_notify
    carta_dir = tmp_path / ".carta"
    carta_dir.mkdir()
    with patch("carta.update.checker._fetch_latest", return_value="0.4.0"):
        maybe_notify(carta_dir, {"update_check": False})
    captured = capsys.readouterr()
    assert captured.out == ""
