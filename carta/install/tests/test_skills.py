"""Tests for Carta Claude skill installation during bootstrap."""
from pathlib import Path

from carta.install.bootstrap import (
    _install_skills,
    _skills_destination_root,
    _skills_source_dir,
)


def test_skills_source_dir_layout(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "skills").mkdir()
    d = _skills_source_dir(root)
    assert d == root / "skills"


def test_skills_destination_global(monkeypatch, tmp_path):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr("carta.install.bootstrap.Path.home", lambda: fake_home)
    p = _skills_destination_root("G", Path("/tmp/unused-for-global"))
    assert p == fake_home / ".claude" / "skills"


def test_skills_destination_project(tmp_path):
    p = _skills_destination_root("P", tmp_path)
    assert p == tmp_path / ".claude" / "skills"


def test_install_skills_copies_to_global(tmp_path, monkeypatch):
    """Copies each {skill}/SKILL.md into ~/.claude/skills/{skill}/SKILL.md when choice is G."""
    home = tmp_path / "home"
    home.mkdir()
    project = tmp_path / "proj"
    src = project / "skills" / "audit-embed"
    src.mkdir(parents=True)
    (src / "SKILL.md").write_text("---\nname: audit-embed\n---\nbody\n")

    monkeypatch.setattr("carta.install.bootstrap.Path.home", lambda: home)

    copied, already, display = _install_skills("G", project)
    assert display == "~/.claude/skills"
    dest = home / ".claude" / "skills" / "audit-embed" / "SKILL.md"
    assert dest.is_file()
    assert "body" in dest.read_text()
    assert copied == 1 and already == 0


def test_install_skills_project_scope(tmp_path):
    project = tmp_path / "proj"
    src = project / "skills" / "carta-workflow"
    src.mkdir(parents=True)
    (src / "SKILL.md").write_text("---\nname: carta-workflow\n---\n")

    copied, already, display = _install_skills("P", project)
    assert display == ".claude/skills"
    dest = project / ".claude" / "skills" / "carta-workflow" / "SKILL.md"
    assert dest.is_file()
    assert copied == 1 and already == 0


def test_install_skills_idempotent_skip_existing(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    project = tmp_path / "proj"
    src = project / "skills" / "audit-embed"
    src.mkdir(parents=True)
    (src / "SKILL.md").write_text("new")
    dest_dir = home / ".claude" / "skills" / "audit-embed"
    dest_dir.mkdir(parents=True)
    existing = dest_dir / "SKILL.md"
    existing.write_text("old")

    monkeypatch.setattr("carta.install.bootstrap.Path.home", lambda: home)

    copied, already, _ = _install_skills("G", project)
    assert existing.read_text() == "old"
    assert copied == 0 and already == 1


def test_install_skills_missing_source_warns(tmp_path, capsys, monkeypatch):
    project = tmp_path / "proj"
    project.mkdir()
    monkeypatch.setattr("carta.install.bootstrap._skills_source_dir", lambda _p: tmp_path / "missing")
    copied, already, display = _install_skills("G", project)
    assert copied == 0 and already == 0
    assert display == ""
    err = capsys.readouterr().err
    assert "skill sources not found" in err


def test_install_skills_empty_dir_warns(tmp_path, capsys):
    project = tmp_path / "proj"
    src = project / "skills"
    src.mkdir(parents=True)
    copied, already, display = _install_skills("G", project)
    assert copied == 0 and already == 0
    assert display == ""
    assert "no skill dirs" in capsys.readouterr().err
