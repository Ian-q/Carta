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


# ---------------------------------------------------------------------------
# updater tests
# ---------------------------------------------------------------------------

def test_detect_install_method_returns_pipx_when_available():
    from carta.update.updater import _detect_install_method
    mock_result = MagicMock()
    mock_result.stdout = "carta-cc 0.3.5\n"
    with patch("carta.update.updater.shutil.which", return_value="/usr/bin/pipx"), \
         patch("carta.update.updater.subprocess.run", return_value=mock_result):
        assert _detect_install_method() == "pipx"


def test_detect_install_method_returns_pip_when_no_pipx():
    from carta.update.updater import _detect_install_method
    with patch("carta.update.updater.shutil.which", return_value=None):
        assert _detect_install_method() == "pip"


def test_detect_install_method_returns_pip_when_carta_not_in_pipx():
    from carta.update.updater import _detect_install_method
    mock_result = MagicMock()
    mock_result.stdout = "some-other-package 1.0\n"
    with patch("carta.update.updater.shutil.which", return_value="/usr/bin/pipx"), \
         patch("carta.update.updater.subprocess.run", return_value=mock_result):
        assert _detect_install_method() == "pip"


def test_run_update_returns_0_when_already_current(capsys):
    from carta.update.updater import run_update
    with patch("carta.update.updater._fetch_latest", return_value="0.3.5"), \
         patch("carta.update.updater._installed_version", return_value="0.3.5"):
        code = run_update(yes=True)
    assert code == 0
    assert "up to date" in capsys.readouterr().out


def test_run_update_returns_1_when_pypi_unreachable(capsys):
    from carta.update.updater import run_update
    with patch("carta.update.updater._fetch_latest", return_value=None):
        code = run_update(yes=True)
    assert code == 1
    assert "PyPI" in capsys.readouterr().err


def test_run_update_yes_runs_pipx_upgrade():
    from carta.update.updater import run_update
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = "carta-cc 0.3.6\n"
    with patch("carta.update.updater._fetch_latest", return_value="0.3.6"), \
         patch("carta.update.updater._installed_version", return_value="0.3.5"), \
         patch("carta.update.updater._detect_install_method", return_value="pipx"), \
         patch("carta.update.updater.subprocess.run", return_value=mock_result) as mock_run:
        code = run_update(yes=True)
    assert code == 0
    call_args = mock_run.call_args[0][0]
    assert call_args == ["pipx", "upgrade", "carta-cc"]


def test_run_update_yes_runs_pip_upgrade():
    from carta.update.updater import run_update
    import sys
    mock_result = MagicMock()
    mock_result.returncode = 0
    with patch("carta.update.updater._fetch_latest", return_value="0.3.6"), \
         patch("carta.update.updater._installed_version", return_value="0.3.5"), \
         patch("carta.update.updater._detect_install_method", return_value="pip"), \
         patch("carta.update.updater.subprocess.run", return_value=mock_result) as mock_run:
        code = run_update(yes=True)
    assert code == 0
    call_args = mock_run.call_args[0][0]
    assert call_args == [sys.executable, "-m", "pip", "install", "--upgrade", "carta-cc"]


def test_print_check_shows_available(capsys):
    from carta.update.updater import print_check
    with patch("carta.update.updater._fetch_latest", return_value="0.4.0"), \
         patch("carta.update.updater._installed_version", return_value="0.3.5"):
        print_check()
    out = capsys.readouterr().out
    assert "0.3.5" in out
    assert "0.4.0" in out
    assert "carta update" in out


def test_print_check_shows_up_to_date(capsys):
    from carta.update.updater import print_check
    with patch("carta.update.updater._fetch_latest", return_value="0.3.5"), \
         patch("carta.update.updater._installed_version", return_value="0.3.5"):
        print_check()
    assert "up to date" in capsys.readouterr().out
