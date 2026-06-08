"""Tests for auto-gating the visual collection in search (BUG-2 fix).

colpali_enabled is tri-state: False = hard opt-out, True = force on,
None/unset (default) = auto — search the _visual collection when it exists
and is non-empty, so two-pass output is visible by default without forcing
every project to load ColPali on every search.
"""
from unittest.mock import MagicMock
from carta.embed.pipeline import _visual_collection_ready


def _client_with_count(count):
    client = MagicMock()
    info = MagicMock()
    info.points_count = count
    client.get_collection.return_value = info
    return client


def test_ready_true_when_collection_has_points():
    assert _visual_collection_ready(_client_with_count(10), "proj_visual") is True


def test_ready_false_when_collection_empty():
    assert _visual_collection_ready(_client_with_count(0), "proj_visual") is False


def test_ready_false_when_collection_missing():
    client = MagicMock()
    client.get_collection.side_effect = Exception("404 not found")
    assert _visual_collection_ready(client, "proj_visual") is False
