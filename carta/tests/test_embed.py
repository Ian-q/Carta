"""Tests for carta embed (Qdrant upsert and payload schema)."""

import pytest
from unittest.mock import MagicMock, patch
from carta.embed.embed import _point_id, _point_id_versioned, upsert_chunks
from carta.config import collection_for_doc_type


class TestPointIdVersioned:
    """Test PAYLOAD-01: _point_id_versioned generates generation-aware UUIDs."""

    def test_point_id_versioned_differs_from_point_id(self):
        """_point_id_versioned produces different UUID than _point_id for same slug/chunk_index."""
        slug = "test-doc"
        chunk_index = 0

        legacy_id = _point_id(slug, chunk_index)
        versioned_id = _point_id_versioned(slug, chunk_index, 1)

        assert legacy_id != versioned_id

    def test_point_id_versioned_differs_per_generation(self):
        """Different generations produce different UUIDs."""
        slug = "x"
        chunk_index = 0

        id_gen0 = _point_id_versioned(slug, chunk_index, 0)
        id_gen1 = _point_id_versioned(slug, chunk_index, 1)
        id_gen2 = _point_id_versioned(slug, chunk_index, 2)

        assert id_gen0 != id_gen1
        assert id_gen1 != id_gen2
        assert id_gen0 != id_gen2


class TestUpsertChunksPayload:
    """Test PAYLOAD-01: upsert_chunks includes lifecycle fields in payload."""

    @patch("carta.embed.embed.requests.post")
    @patch("carta.embed.embed.QdrantClient")
    def test_upsert_chunks_with_doc_generation_uses_versioned_id(self, mock_client_class, mock_post):
        """When chunk contains doc_generation, upsert_chunks uses _point_id_versioned."""
        mock_client = MagicMock()
        mock_client_class.return_value = mock_client
        mock_client.collection_exists.return_value = True

        # Mock Ollama embedding response
        mock_post.return_value.status_code = 200
        mock_post.return_value.json.return_value = {"embedding": [0.1] * 768}

        cfg = {
            "project_name": "test",
            "qdrant_url": "http://localhost:6333",
            "embed": {
                "ollama_url": "http://localhost:11434",
                "ollama_model": "nomic-embed-text:latest",
            },
        }

        chunks = [
            {
                "slug": "doc1",
                "chunk_index": 0,
                "text": "chunk text",
                "doc_generation": 2,
                "sidecar_id": "sid-123",
            }
        ]

        upsert_chunks(chunks, cfg, client=mock_client)

        # Verify upsert was called with versioned ID
        mock_client.upsert.assert_called_once()
        points = mock_client.upsert.call_args[1]["points"]
        assert len(points) == 1
        point = points[0]

        # Versioned ID should differ from legacy ID
        legacy_id = _point_id("doc1", 0)
        assert str(point.id) != legacy_id

    @patch("carta.embed.embed.requests.post")
    @patch("carta.embed.embed.QdrantClient")
    def test_upsert_chunks_without_doc_generation_uses_legacy_id(self, mock_client_class, mock_post):
        """When chunk lacks doc_generation, upsert_chunks uses _point_id (backward compat)."""
        mock_client = MagicMock()
        mock_client_class.return_value = mock_client
        mock_client.collection_exists.return_value = True

        mock_post.return_value.status_code = 200
        mock_post.return_value.json.return_value = {"embedding": [0.1] * 768}

        cfg = {
            "project_name": "test",
            "qdrant_url": "http://localhost:6333",
            "embed": {
                "ollama_url": "http://localhost:11434",
                "ollama_model": "nomic-embed-text:latest",
            },
        }

        chunks = [
            {
                "slug": "doc1",
                "chunk_index": 0,
                "text": "chunk text",
                # No doc_generation key
            }
        ]

        upsert_chunks(chunks, cfg, client=mock_client)

        mock_client.upsert.assert_called_once()
        points = mock_client.upsert.call_args[1]["points"]
        point = points[0]

        # Should use legacy ID
        expected_id = _point_id("doc1", 0)
        assert str(point.id) == expected_id

    @patch("carta.embed.embed.requests.post")
    @patch("carta.embed.embed.QdrantClient")
    def test_upsert_chunks_payload_includes_lifecycle_fields(self, mock_client_class, mock_post):
        """PointStruct payload includes all six new lifecycle fields."""
        mock_client = MagicMock()
        mock_client_class.return_value = mock_client
        mock_client.collection_exists.return_value = True

        mock_post.return_value.status_code = 200
        mock_post.return_value.json.return_value = {"embedding": [0.1] * 768}

        cfg = {
            "project_name": "test",
            "qdrant_url": "http://localhost:6333",
            "embed": {
                "ollama_url": "http://localhost:11434",
                "ollama_model": "nomic-embed-text:latest",
            },
        }

        chunks = [
            {
                "slug": "doc1",
                "chunk_index": 0,
                "text": "chunk text",
                "doc_type": "doc",
                "doc_generation": 1,
                "sidecar_id": "sid-456",
                "chunk_source_hash": "hash123",
            }
        ]

        upsert_chunks(chunks, cfg, client=mock_client)

        mock_client.upsert.assert_called_once()
        points = mock_client.upsert.call_args[1]["points"]
        point = points[0]
        payload = point.payload

        # Check new lifecycle fields are present
        assert "doc_generation" in payload
        assert payload["doc_generation"] == 1
        assert "stale_as_of" in payload
        assert payload["stale_as_of"] is None
        assert "superseded_at" in payload
        assert payload["superseded_at"] is None
        assert "orphaned_at" in payload
        assert payload["orphaned_at"] is None
        assert "sidecar_id" in payload
        assert payload["sidecar_id"] == "sid-456"
        assert "chunk_source_hash" in payload
        assert payload["chunk_source_hash"] == "hash123"

    @patch("carta.embed.embed.requests.post")
    @patch("carta.embed.embed.QdrantClient")
    def test_upsert_chunks_payload_defaults_for_missing_fields(self, mock_client_class, mock_post):
        """Lifecycle fields get defaults when absent from chunk dict."""
        mock_client = MagicMock()
        mock_client_class.return_value = mock_client
        mock_client.collection_exists.return_value = True

        mock_post.return_value.status_code = 200
        mock_post.return_value.json.return_value = {"embedding": [0.1] * 768}

        cfg = {
            "project_name": "test",
            "qdrant_url": "http://localhost:6333",
            "embed": {
                "ollama_url": "http://localhost:11434",
                "ollama_model": "nomic-embed-text:latest",
            },
        }

        # Chunk with minimal fields (no doc_generation, sidecar_id, chunk_source_hash)
        chunks = [
            {
                "slug": "doc1",
                "chunk_index": 0,
                "text": "chunk text",
            }
        ]

        upsert_chunks(chunks, cfg, client=mock_client)

        mock_client.upsert.assert_called_once()
        points = mock_client.upsert.call_args[1]["points"]
        point = points[0]
        payload = point.payload

        # Check defaults
        assert payload.get("doc_generation") == 1
        assert payload.get("sidecar_id") == ""
        assert payload.get("chunk_source_hash") == ""


class TestCollectionForDocType:
    """Test PAYLOAD-01: collection_for_doc_type maps doc types to collections."""

    def test_protected_types_map_to_notes_collection(self):
        """Protected doc types (quirk, bug-note, helpful-note) map to notes collection."""
        cfg = {"project_name": "myproject", "qdrant_url": "http://localhost:6333"}

        assert collection_for_doc_type(cfg, "quirk") == "myproject_notes"
        assert collection_for_doc_type(cfg, "bug-note") == "myproject_notes"
        assert collection_for_doc_type(cfg, "helpful-note") == "myproject_notes"

    def test_regular_types_map_to_doc_collection(self):
        """Regular doc types map to doc collection."""
        cfg = {"project_name": "myproject", "qdrant_url": "http://localhost:6333"}

        assert collection_for_doc_type(cfg, "doc") == "myproject_doc"
        assert collection_for_doc_type(cfg, "datasheet") == "myproject_doc"
        assert collection_for_doc_type(cfg, "manual") == "myproject_doc"

    def test_session_type_maps_to_session_collection(self):
        """Session doc type maps to session collection."""
        cfg = {"project_name": "myproject", "qdrant_url": "http://localhost:6333"}

        assert collection_for_doc_type(cfg, "session") == "myproject_session"

    def test_unknown_type_defaults_to_doc_collection(self):
        """Unknown doc type defaults to doc collection (safe default)."""
        cfg = {"project_name": "myproject", "qdrant_url": "http://localhost:6333"}

        assert collection_for_doc_type(cfg, "unknown_type") == "myproject_doc"
        assert collection_for_doc_type(cfg, "random-string") == "myproject_doc"
