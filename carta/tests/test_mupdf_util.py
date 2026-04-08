"""Tests for MuPDF stderr noise control."""

import pytest


def test_mupdf_quiet_runs_when_fitz_available():
    pytest.importorskip("fitz", reason="PyMuPDF not installed")
    from carta.mupdf_util import mupdf_quiet

    with mupdf_quiet():
        pass
