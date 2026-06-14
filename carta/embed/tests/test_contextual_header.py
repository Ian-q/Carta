"""Tests for contextual chunk headers (issue #19)."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

from carta.config import DEFAULTS


def test_default_enables_contextual_header():
    assert DEFAULTS["embed"]["chunking"]["contextual_header"] is True
