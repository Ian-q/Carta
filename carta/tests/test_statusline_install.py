import json

from carta import statusline as sl

SAMPLE_SCRIPT = """#!/usr/bin/env bash
input=$(cat)
parts="user:dir"
parts="$parts │ branch"
echo -e "$parts"
"""


def _write_script(tmp_path, body=SAMPLE_SCRIPT):
    p = tmp_path / "statusline-command.sh"
    p.write_text(body)
    return p


def test_install_inserts_block_and_backup(tmp_path):
    p = _write_script(tmp_path)
    result = sl.install_into_script(p, confirm=lambda msg: True)
    assert result == "installed"
    text = p.read_text()
    assert sl.MARKER_START in text and sl.MARKER_END in text
    # block sits BEFORE the echo line
    assert text.index(sl.MARKER_START) < text.index('echo -e "$parts"')
    # backup preserved original
    assert (tmp_path / "statusline-command.sh.bak").read_text() == SAMPLE_SCRIPT


def test_install_is_idempotent(tmp_path):
    p = _write_script(tmp_path)
    sl.install_into_script(p, confirm=lambda msg: True)
    once = p.read_text()
    result = sl.install_into_script(p, confirm=lambda msg: True)
    assert result == "already"
    assert p.read_text() == once  # unchanged on second run


def test_install_declined_changes_nothing(tmp_path):
    p = _write_script(tmp_path)
    result = sl.install_into_script(p, confirm=lambda msg: False)
    assert result == "declined"
    assert p.read_text() == SAMPLE_SCRIPT
    assert not (tmp_path / "statusline-command.sh.bak").exists()


def test_install_unsupported_script_refused(tmp_path):
    # No `parts` variable / no echo of parts -> cannot safely wire
    p = _write_script(tmp_path, body="#!/usr/bin/env bash\necho hello\n")
    result = sl.install_into_script(p, confirm=lambda msg: True)
    assert result == "unsupported"
    assert "carta statusline" not in p.read_text()


def test_uninstall_removes_block(tmp_path):
    p = _write_script(tmp_path)
    sl.install_into_script(p, confirm=lambda msg: True)
    result = sl.uninstall_from_script(p)
    assert result == "removed"
    text = p.read_text()
    assert sl.MARKER_START not in text and sl.MARKER_END not in text
    assert 'echo -e "$parts"' in text  # rest intact


def test_uninstall_absent_is_noop(tmp_path):
    p = _write_script(tmp_path)
    result = sl.uninstall_from_script(p)
    assert result == "absent"
    assert p.read_text() == SAMPLE_SCRIPT


def test_find_statusline_script(tmp_path):
    script = _write_script(tmp_path)
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps(
        {"statusLine": {"type": "command", "command": f"bash {script}"}}
    ))
    assert sl.find_statusline_script(settings) == script


def test_find_statusline_script_inline_returns_none(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps(
        {"statusLine": {"type": "command", "command": "echo hi"}}
    ))
    assert sl.find_statusline_script(settings) is None


def test_find_statusline_script_missing_key_returns_none(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({}))
    assert sl.find_statusline_script(settings) is None


def test_offer_install_declined_noop(tmp_path, monkeypatch):
    script = _write_script(tmp_path)
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps(
        {"statusLine": {"type": "command", "command": f"bash {script}"}}
    ))
    # auto-decline the prompt
    monkeypatch.setattr("builtins.input", lambda *a, **k: "n")
    result = sl.offer_install(settings_path=settings, interactive=True)
    assert result == "declined"
    assert sl.MARKER_START not in script.read_text()


def test_offer_install_no_script_returns_unavailable(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({}))
    assert sl.offer_install(settings_path=settings, interactive=True) == "unavailable"


def test_offer_install_non_interactive_declines(tmp_path):
    script = _write_script(tmp_path)
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps(
        {"statusLine": {"type": "command", "command": f"bash {script}"}}
    ))
    result = sl.offer_install(settings_path=settings, interactive=False)
    assert result == "declined"
    assert sl.MARKER_START not in script.read_text()
