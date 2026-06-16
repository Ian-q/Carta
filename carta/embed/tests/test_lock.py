"""Tests for the shared single-writer embed lock (audit Group F: CA-2/5/12)."""

import os

import pytest

from carta.embed.lock import acquire, embed_lock, EmbedLockHeld


def test_acquire_creates_lock_with_our_pid(tmp_path):
    lp = tmp_path / "embed.lock"
    acquire(lp)
    assert lp.exists()
    assert lp.read_text().strip() == str(os.getpid())


def test_acquire_raises_when_held_by_live_pid(tmp_path):
    """A second acquirer must not silently take a lock another live process holds —
    that is exactly how concurrent embeds delete each other's points."""
    lp = tmp_path / "embed.lock"
    lp.write_text(str(os.getpid()))  # this (alive) process "holds" it
    with pytest.raises(EmbedLockHeld) as exc:
        acquire(lp)
    assert exc.value.pid == os.getpid()


def test_acquire_reclaims_stale_lock_from_dead_pid(tmp_path):
    lp = tmp_path / "embed.lock"
    lp.write_text("999999999")  # not a live PID — stale lock
    acquire(lp)
    assert lp.read_text().strip() == str(os.getpid())


def test_embed_lock_releases_on_exit(tmp_path):
    lp = tmp_path / "embed.lock"
    with embed_lock(lp):
        assert lp.exists()
    assert not lp.exists()


def test_embed_lock_releases_on_exception(tmp_path):
    lp = tmp_path / "embed.lock"
    with pytest.raises(ValueError):
        with embed_lock(lp):
            raise ValueError("boom")
    assert not lp.exists()


def test_embed_lock_raises_when_held(tmp_path):
    lp = tmp_path / "embed.lock"
    lp.write_text(str(os.getpid()))
    with pytest.raises(EmbedLockHeld):
        with embed_lock(lp):
            pass
