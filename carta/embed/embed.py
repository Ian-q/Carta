"""Ollama embedding and Qdrant upsert for carta embed."""

import hashlib
import uuid

import requests
from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance,
    PointStruct,
    VectorParams,
)

from carta.config import collection_name

# nomic-embed-text produces 768-dimensional vectors
VECTOR_DIM = 768


def get_embedding(
    text: str,
    ollama_url: str = "http://localhost:11434",
    model: str = "nomic-embed-text:latest",
    prefix: str = "search_document: ",
) -> list[float]:
    """Get embedding vector from Ollama API."""
    resp = requests.post(
        f"{ollama_url}/api/embeddings",
        json={"model": model, "prompt": f"{prefix}{text}"},
        timeout=60,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Ollama embedding failed ({resp.status_code}): {resp.text[:200]}")
    return resp.json()["embedding"]


def ensure_collection(client: QdrantClient, coll_name: str) -> None:
    """Create Qdrant collection if it doesn't exist."""
    if not client.collection_exists(coll_name):
        client.create_collection(
            collection_name=coll_name,
            vectors_config=VectorParams(size=VECTOR_DIM, distance=Distance.COSINE),
        )


def _point_id(slug: str, chunk_index: int) -> str:
    """Deterministic UUID from slug + chunk_index for idempotent upserts."""
    raw = f"{slug}:{chunk_index}"
    return str(uuid.UUID(hashlib.md5(raw.encode()).hexdigest()))


def upsert_chunks(chunks: list[dict], cfg: dict, client: QdrantClient = None) -> int:
    """Embed and upsert chunks to Qdrant using settings from cfg.

    Args:
        chunks: list of chunk dicts with at minimum keys:
            "slug", "text", "chunk_index".
            Any additional keys are stored as Qdrant payload.
        cfg: carta config dict (must contain qdrant_url, embed.ollama_url,
             embed.ollama_model, and project_name).
        client: optional QdrantClient instance. If None, a new client is created.

    Returns:
        Number of points upserted.
    """
    coll_name = collection_name(cfg, "doc")
    ollama_url = cfg["embed"]["ollama_url"]
    model = cfg["embed"]["ollama_model"]

    if client is None:
        qdrant_url = cfg["qdrant_url"]
        client = QdrantClient(url=qdrant_url)
    ensure_collection(client, coll_name)

    upserted = 0
    for chunk in chunks:
        chunk_id = f"{chunk.get('slug', '?')}[{chunk.get('chunk_index', '?')}]"
        try:
            vec = get_embedding(chunk["text"], ollama_url=ollama_url, model=model)
            payload = {k: v for k, v in chunk.items() if k != "text"}
            payload["text"] = chunk["text"]
            point = PointStruct(
                id=_point_id(chunk["slug"], chunk["chunk_index"]),
                vector=vec,
                payload=payload,
            )
            client.upsert(collection_name=coll_name, points=[point])
            upserted += 1
        except Exception as e:
            print(f"Warning: skipping chunk {chunk_id} — {e}")

    return upserted
