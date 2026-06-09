"""Tests for the macOS single-threaded-BLAS workaround (0.7.1).

torch-CPU on Apple Silicon segfaults in Accelerate's multithreaded cblas_sgemm
during ColPali matmuls. Pinning BLAS to one thread BEFORE torch imports avoids it.
"""
import os
import pytest
from carta._compat import apply_macos_blas_workaround, _BLAS_THREAD_VARS


@pytest.fixture(autouse=True)
def _clean_blas_env(monkeypatch):
    for v in _BLAS_THREAD_VARS:
        monkeypatch.delenv(v, raising=False)
    yield


def test_applies_on_darwin(monkeypatch):
    applied = apply_macos_blas_workaround(system="Darwin")
    assert applied is True
    for v in _BLAS_THREAD_VARS:
        assert os.environ[v] == "1"


def test_noop_off_darwin(monkeypatch):
    applied = apply_macos_blas_workaround(system="Linux")
    assert applied is False
    for v in _BLAS_THREAD_VARS:
        assert v not in os.environ


def test_respects_user_override(monkeypatch):
    monkeypatch.setenv("OMP_NUM_THREADS", "8")
    apply_macos_blas_workaround(system="Darwin")
    assert os.environ["OMP_NUM_THREADS"] == "8"  # setdefault — not clobbered
    assert os.environ["VECLIB_MAXIMUM_THREADS"] == "1"  # others still pinned
