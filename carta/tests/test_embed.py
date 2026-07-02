"""Tests for carta embed (Qdrant upsert and payload schema)."""

import pytest
from unittest.mock import MagicMock, patch
from carta.embed.embed import _point_id_versioned, _visual_point_id, upsert_chunks
from carta.config import collection_for_doc_type


class TestPointIdVersioned:
    """Test PAYLOAD-01: _point_id_versioned generates generation-aware UUIDs."""

    def test_point_id_versioned_differs_per_key(self):
        """Different keys produce different UUIDs."""
        id_a = _point_id_versioned("docs/a/test-doc.md", 0, 1)
        id_b = _point_id_versioned("docs/b/test-doc.md", 0, 1)
        assert id_a != id_b

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
                "file_path": "docs/sub/doc1.md",
                "chunk_index": 0,
                "text": "chunk text",
                "doc_generation": 2,
                "sidecar_id": "sid-123",
            }
        ]

        upsert_chunks(chunks, cfg, client=mock_client)

        # Verify upsert was called with versioned ID derived from file_path
        mock_client.upsert.assert_called_once()
        points = mock_client.upsert.call_args[1]["points"]
        assert len(points) == 1
        point = points[0]

        expected_id = _point_id_versioned("docs/sub/doc1.md", 0, 2)
        assert str(point.id) == expected_id

    @patch("carta.embed.embed.requests.post")
    @patch("carta.embed.embed.QdrantClient")
    def test_upsert_chunks_without_doc_generation_uses_file_path_id(self, mock_client_class, mock_post):
        """When chunk lacks doc_generation, upsert_chunks uses _point_id_versioned with generation=1."""
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
                "file_path": "docs/sub/doc1.md",
                "chunk_index": 0,
                "text": "chunk text",
                # No doc_generation key — defaults to generation=1
            }
        ]

        upsert_chunks(chunks, cfg, client=mock_client)

        mock_client.upsert.assert_called_once()
        points = mock_client.upsert.call_args[1]["points"]
        point = points[0]

        # Should use path-based versioned ID with generation=1
        expected_id = _point_id_versioned("docs/sub/doc1.md", 0, 1)
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


class TestUpsertChunksRouting:
    """upsert_chunks must route by the chunks' doc_type via collection_for_doc_type —
    it previously hardcoded {project}_doc, making note types unreachable."""

    def _run(self, doc_type):
        from unittest.mock import patch, MagicMock
        from carta.embed.embed import upsert_chunks
        chunks = [{"slug": "s", "text": "hello world", "chunk_index": 0,
                   "doc_type": doc_type}]
        cfg = {"project_name": "p", "qdrant_url": "http://localhost:6333",
               "embed": {"ollama_url": "http://localhost:11434",
                          "ollama_model": "m", "embedding_workers": 1}}
        client = MagicMock()
        with patch("carta.embed.embed.get_embedding", return_value=[0.0] * 768), \
             patch("carta.embed.embed.collection_is_hybrid", return_value=False), \
             patch("carta.embed.embed.ensure_collection") as ens:
            upsert_chunks(chunks, cfg, client=client)
        ensured = ens.call_args[0][1]
        # the same name must be used for the actual upsert call
        assert ensured in str(client.upsert.call_args)
        return ensured

    def test_quirk_routes_to_notes(self):
        assert self._run("quirk") == "p_notes"

    def test_bug_note_routes_to_notes(self):
        assert self._run("bug-note") == "p_notes"

    def test_helpful_note_routes_to_notes(self):
        assert self._run("helpful-note") == "p_notes"

    def test_plain_doc_type_still_routes_to_doc(self):
        assert self._run("datasheet") == "p_doc"

    def test_image_description_still_routes_to_doc(self):
        assert self._run("image_description") == "p_doc"


class TestPathBasedPointIds:
    """Same-stem files must never share point IDs (the README-collision bug)."""

    def test_same_stem_different_paths_get_distinct_ids(self):
        id_a = _point_id_versioned("docs/ci/README.md", 0, 1)
        id_b = _point_id_versioned("docs/diagrams/README.md", 0, 1)
        assert id_a != id_b

    def test_id_is_deterministic(self):
        assert (_point_id_versioned("docs/ci/README.md", 3, 2)
                == _point_id_versioned("docs/ci/README.md", 3, 2))

    def test_visual_same_stem_different_paths_distinct(self):
        id_a = _visual_point_id("docs/a/spec.pdf", 1)
        id_b = _visual_point_id("docs/b/spec.pdf", 1)
        assert id_a != id_b

    @patch("carta.embed.embed.requests.post")
    @patch("carta.embed.embed.collection_is_hybrid", return_value=False)
    @patch("carta.embed.embed.ensure_collection")
    def test_upsert_uses_file_path_for_point_id(self, mock_ensure, mock_hybrid, mock_post):
        """build_point derives the ID from chunk['file_path'], not slug."""
        mock_post.return_value.status_code = 200
        mock_post.return_value.json.return_value = {"embedding": [0.1] * 768}
        mock_client = MagicMock()

        cfg = {
            "project_name": "test",
            "qdrant_url": "http://localhost:6333",
            "embed": {
                "ollama_url": "http://localhost:11434",
                "ollama_model": "nomic-embed-text:latest",
                "embedding_workers": 1,
            },
        }
        chunk = {"slug": "readme", "file_path": "docs/ci/README.md",
                 "chunk_index": 0, "text": "hello world", "doc_type": "unknown"}
        upsert_chunks([chunk], cfg, client=mock_client)

        points = mock_client.upsert.call_args.kwargs["points"]
        expected = _point_id_versioned("docs/ci/README.md", 0, 1)
        assert points[0].id == expected


class TestEmptyChunkGuard:
    """upsert_chunks must drop empty/whitespace-only chunks before embedding."""

    @patch("carta.embed.embed.requests.post")
    @patch("carta.embed.embed.collection_is_hybrid", return_value=False)
    @patch("carta.embed.embed.ensure_collection")
    def test_empty_chunks_are_not_upserted(self, mock_ensure, mock_hybrid, mock_post):
        """Empty and whitespace-only chunks are dropped; only real content is upserted."""
        mock_post.return_value.status_code = 200
        mock_post.return_value.json.return_value = {"embedding": [0.1] * 768}
        mock_client = MagicMock()
        mock_client.collection_exists.return_value = True
        info = MagicMock()
        info.config.params.vectors = None
        info.config.params.sparse_vectors = None
        mock_client.get_collection.return_value = info
        cfg = {
            "project_name": "test", "qdrant_url": "http://localhost:6333",
            "embed": {"ollama_url": "http://x", "ollama_model": "m",
                      "embedding_workers": 1},
        }
        chunks = [
            {"slug": "d", "file_path": "d.pdf", "chunk_index": 0, "text": ""},
            {"slug": "d", "file_path": "d.pdf", "chunk_index": 1, "text": "   \n"},
            {"slug": "d", "file_path": "d.pdf", "chunk_index": 2, "text": "real content"},
        ]
        count = upsert_chunks(chunks, cfg, client=mock_client)
        assert count == 1
        points = mock_client.upsert.call_args.kwargs["points"]
        assert len(points) == 1
        assert points[0].payload["text"] == "real content"

    def test_all_empty_returns_zero_without_upsert(self):
        """When all chunks are empty, upsert is never called and 0 is returned."""
        mock_client = MagicMock()
        mock_client.collection_exists.return_value = True
        cfg = {
            "project_name": "test", "qdrant_url": "http://localhost:6333",
            "embed": {"ollama_url": "http://x", "ollama_model": "m"},
        }
        chunks = [{"slug": "d", "file_path": "d.pdf", "chunk_index": 0, "text": ""}]
        count = upsert_chunks(chunks, cfg, client=mock_client)
        assert count == 0
        mock_client.upsert.assert_not_called()


class TestGetEmbeddingValidation:
    """get_embedding must reject empty/invalid embedding payloads loudly (#79).

    A 200 response with no usable vector (empty list or missing key) otherwise
    flows downstream as a zero/None query vector and is later swallowed by the
    search path as 'no results / nothing embedded'.
    """

    @patch("carta.embed.embed.requests.post")
    def test_raises_on_empty_embedding_vector(self, mock_post):
        from carta.embed.embed import get_embedding
        mock_post.return_value = MagicMock(status_code=200, json=lambda: {"embedding": []})
        with pytest.raises(RuntimeError, match="empty|invalid"):
            get_embedding("CAN bus")

    @patch("carta.embed.embed.requests.post")
    def test_raises_on_missing_embedding_key(self, mock_post):
        from carta.embed.embed import get_embedding
        mock_post.return_value = MagicMock(status_code=200, json=lambda: {})
        with pytest.raises(RuntimeError, match="empty|invalid"):
            get_embedding("CAN bus")
