"""Tests for carta.hook.git_hook install/uninstall of the managed shim."""
import pytest

from carta.hook.git_hook import SENTINEL_START, install_hook, uninstall_hook


def _hooks(tmp_path):
    d = tmp_path / ".git" / "hooks"
    d.mkdir(parents=True)
    return d


def test_install_fresh_writes_executable_shim(tmp_path):
    _hooks(tmp_path)
    status = install_hook(tmp_path, "pre-push")
    hook = tmp_path / ".git" / "hooks" / "pre-push"
    assert status == "installed"
    text = hook.read_text()
    assert SENTINEL_START in text
    assert "carta hook check --stage pre-push" in text
    assert hook.stat().st_mode & 0o100  # owner-executable


def test_install_is_idempotent(tmp_path):
    _hooks(tmp_path)
    install_hook(tmp_path, "pre-push")
    status = install_hook(tmp_path, "pre-push")
    assert status == "already-installed"
    text = (tmp_path / ".git" / "hooks" / "pre-push").read_text()
    assert text.count(SENTINEL_START) == 1


def test_install_refuses_foreign_hook(tmp_path):
    d = _hooks(tmp_path)
    (d / "pre-push").write_text("#!/bin/sh\necho mine\n")
    with pytest.raises(FileExistsError):
        install_hook(tmp_path, "pre-push")
    # foreign content untouched
    assert "echo mine" in (d / "pre-push").read_text()


def test_uninstall_removes_managed_file(tmp_path):
    _hooks(tmp_path)
    install_hook(tmp_path, "pre-push")
    status = uninstall_hook(tmp_path, "pre-push")
    assert status == "removed-file"
    assert not (tmp_path / ".git" / "hooks" / "pre-push").exists()


def test_uninstall_strips_block_from_chained_hook(tmp_path):
    d = _hooks(tmp_path)
    hook = d / "pre-push"
    hook.write_text(
        "#!/bin/sh\necho mine\n"
        f"{SENTINEL_START}\ncarta hook check --stage pre-push || exit $?\n# <<< carta managed <<<\n"
    )
    status = uninstall_hook(tmp_path, "pre-push")
    assert status == "removed-block"
    remaining = hook.read_text()
    assert "echo mine" in remaining
    assert SENTINEL_START not in remaining


def test_install_rejects_bad_stage(tmp_path):
    _hooks(tmp_path)
    with pytest.raises(ValueError):
        install_hook(tmp_path, "post-merge")
