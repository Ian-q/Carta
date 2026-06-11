"""Ollama embedding and Qdrant upsert for carta embed."""

import concurrent.futures
import hashlib
import uuid

import requests
from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance,
    HnswConfigDiff,
    Modifier,
    MultiVectorConfig,
    MultiVectorComparator,
    PointStruct,
    SparseVector,
    SparseVectorParams,
    VectorParams,
)

from carta.config import collection_name, collection_for_doc_type

# nomic-embed-text produces 768-dimensional vectors
VECTOR_DIM = 768

# Named vector keys for hybrid collections
DENSE_VECTOR_NAME = "dense"
SPARSE_VECTOR_NAME = "bm25"

# ColPali produces 128-dimensional patch vectors
COLPALI_VECTOR_DIM = 128

# Maximum number of PointStructs sent per client.upsert() call
BATCH_SIZE = 32

# Visual embeddings have multi-vector patches (1024 x 128 dims per page)
# Reduce batch size to stay under Qdrant's 32MB payload limit
VISUAL_BATCH_SIZE = 4


_CONTEXT_OVERFLOW = "the input length exceeds the context length"


def _fastembed_available() -> bool:
    try:
        import fastembed  # noqa: F401
        return True
    except ImportError:
        return False


def get_embedding(
    text: str,
    ollama_url: str = "http://localhost:11434",
    model: str = "nomic-embed-text:latest",
    prefix: str = "search_document: ",
) -> list[float]:
    """Get embedding vector from Ollama API.

    If Ollama reports the input exceeds the model's context length, truncates
    the text by 25% per attempt and retries up to 3 times before raising.
    """
    attempt_text = text
    for attempt in range(4):
        resp = requests.post(
            f"{ollama_url}/api/embeddings",
            json={"model": model, "prompt": f"{prefix}{attempt_text}"},
            timeout=60,
        )
        if resp.status_code == 200:
            if attempt > 0:
                print(
                    f"  (truncated to {len(attempt_text.split())} words after {attempt} attempt(s))",
                    flush=True,
                )
            return resp.json()["embedding"]
        body = resp.text
        if resp.status_code == 500 and _CONTEXT_OVERFLOW in body:
            words = attempt_text.split()
            keep = int(len(words) * 0.75)
            if keep < 10:
                raise RuntimeError(f"Ollama embedding failed — input too long even after truncation: {body[:200]}")
            attempt_text = " ".join(words[:keep])
            continue
        raise RuntimeError(f"Ollama embedding failed ({resp.status_code}): {body[:200]}")
    raise RuntimeError(f"Ollama embedding failed — input too long after 3 truncation attempts")


def ensure_collection(client: QdrantClient, coll_name: str) -> None:
    """Create Qdrant collection if it doesn't exist.

    Schema selection is driven by fastembed availability so that the collection
    schema always matches what upsert_chunks can actually write:

    - fastembed present: named-vector hybrid schema (dense + bm25 sparse) for
      BM25+RRF retrieval.
    - fastembed absent: legacy unnamed-dense schema, consistent with the legacy
      upsert path so batches are never silently dropped.

    Existing collections (any schema) are left untouched; collection_is_hybrid()
    distinguishes them at upsert and query time.
    """
    if client.collection_exists(coll_name):
        return
    if _fastembed_available():
        client.create_collection(
            collection_name=coll_name,
            vectors_config={
                DENSE_VECTOR_NAME: VectorParams(size=VECTOR_DIM, distance=Distance.COSINE),
            },
            sparse_vectors_config={
                SPARSE_VECTOR_NAME: SparseVectorParams(modifier=Modifier.IDF),
            },
        )
    else:
        # fastembed absent: legacy unnamed-dense schema, consistent with legacy upsert
        client.create_collection(
            collection_name=coll_name,
            vectors_config=VectorParams(size=VECTOR_DIM, distance=Distance.COSINE),
        )


def collection_is_hybrid(client: QdrantClient, coll_name: str) -> bool:
    """Return True if the collection has the named dense+sparse hybrid schema.

    Used to gate hybrid query logic: legacy unnamed-dense collections return
    False and fall through to the classic single-vector query path.

    Only a 404 / collection-not-found response is silently treated as False.
    Any other error (connectivity, SDK, etc.) is re-raised so callers see real
    failures instead of silently getting the wrong schema.
    """
    from qdrant_client.http.exceptions import UnexpectedResponse

    try:
        info = client.get_collection(coll_name)
    except UnexpectedResponse as e:
        if getattr(e, "status_code", None) == 404:
            return False
        raise
    except Exception:
        # Some qdrant-client builds wrap 404 in a plain ValueError/RuntimeError;
        # treat those as "not found" only when the message looks like a missing
        # collection.  All other exceptions propagate.
        raise

    vectors = info.config.params.vectors
    has_named_dense = isinstance(vectors, dict) and DENSE_VECTOR_NAME in vectors
    sparse = getattr(info.config.params, "sparse_vectors", None)
    has_sparse = bool(sparse) and SPARSE_VECTOR_NAME in sparse
    return has_named_dense and has_sparse


def _point_id(slug: str, chunk_index: int) -> str:
    """Deterministic UUID from slug + chunk_index for idempotent upserts."""
    raw = f"{slug}:{chunk_index}"
    return str(uuid.UUID(hashlib.md5(raw.encode()).hexdigest()))


def _point_id_versioned(slug: str, chunk_index: int, generation: int) -> str:
    """Deterministic UUID from slug + chunk_index + generation for generation-aware upserts.

    Used when chunks carry doc_generation metadata (Plan 999.1-02+).
    Different generations produce different UUIDs, enabling retries without collisions.
    """
    raw = f"{slug}:{chunk_index}:g{generation}"
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
    # Route by the batch's doc_type (batches come from a single file, so they are
    # doc_type-homogeneous). Note types (quirk/bug-note/helpful-note) land in
    # {project}_notes; everything else (incl. image/visual chunk types) stays in _doc.
    batch_doc_type = chunks[0].get("doc_type", "unknown") if chunks else "unknown"
    coll_name = collection_for_doc_type(cfg, batch_doc_type)
    embed_cfg = cfg["embed"]
    ollama_url = embed_cfg["ollama_url"]
    model = embed_cfg["ollama_model"]
    workers = max(1, int(embed_cfg.get("embedding_workers", 8)))

    if client is None:
        qdrant_url = cfg["qdrant_url"]
        client = QdrantClient(url=qdrant_url, timeout=5)
    ensure_collection(client, coll_name)

    # Determine schema ONCE before the chunk loop so every point in this batch
    # is written in the format that matches the collection.  No per-point guessing.
    is_hybrid = collection_is_hybrid(client, coll_name)

    def build_point(chunk: dict, vec: list[float]) -> PointStruct:
        payload = {k: v for k, v in chunk.items() if k != "text"}
        payload["text"] = chunk["text"]
        payload["doc_generation"] = chunk.get("doc_generation", 1)
        payload["stale_as_of"] = None
        payload["superseded_at"] = None
        payload["orphaned_at"] = None
        payload["sidecar_id"] = chunk.get("sidecar_id", "")
        payload["chunk_source_hash"] = chunk.get("chunk_source_hash", "")

        if chunk.get("doc_generation") is not None:
            point_id = _point_id_versioned(
                chunk["slug"], chunk["chunk_index"], chunk["doc_generation"]
            )
        else:
            point_id = _point_id(chunk["slug"], chunk["chunk_index"])

        if is_hybrid:
            from carta.embed.sparse import embed_sparse_document
            sv = embed_sparse_document(chunk["text"])
            vector: dict | list = {
                DENSE_VECTOR_NAME: vec,
                SPARSE_VECTOR_NAME: SparseVector(indices=sv.indices, values=sv.values),
            }
        else:
            # Legacy unnamed-dense collection — write matching format
            vector = vec

        return PointStruct(id=point_id, vector=vector, payload=payload)

    def flush(batch: list[PointStruct]) -> int:
        if not batch:
            return 0
        try:
            client.upsert(collection_name=coll_name, points=batch)
            return len(batch)
        except Exception as e:
            print(f"Warning: batch upsert failed — {e}", flush=True)
            return 0

    upserted = 0
    batch: list[PointStruct] = []

    if workers == 1:
        for chunk in chunks:
            chunk_id = f"{chunk.get('slug', '?')}[{chunk.get('chunk_index', '?')}]"
            try:
                vec = get_embedding(chunk["text"], ollama_url=ollama_url, model=model)
                batch.append(build_point(chunk, vec))
            except Exception as e:
                print(f"Warning: skipping chunk {chunk_id} — {e}", flush=True)
                continue
            if len(batch) >= BATCH_SIZE:
                upserted += flush(batch)
                batch = []
    else:
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=workers, thread_name_prefix="carta-embed"
        ) as ex:
            future_to_chunk = {
                ex.submit(get_embedding, c["text"], ollama_url=ollama_url, model=model): c
                for c in chunks
            }
            for fut in concurrent.futures.as_completed(future_to_chunk):
                chunk = future_to_chunk[fut]
                chunk_id = f"{chunk.get('slug', '?')}[{chunk.get('chunk_index', '?')}]"
                try:
                    vec = fut.result()
                    batch.append(build_point(chunk, vec))
                except Exception as e:
                    print(f"Warning: skipping chunk {chunk_id} — {e}", flush=True)
                    continue
                if len(batch) >= BATCH_SIZE:
                    upserted += flush(batch)
                    batch = []

    upserted += flush(batch)
    return upserted


def ensure_visual_collection(client: QdrantClient, coll_name: str) -> None:
    """Create Qdrant multi-vector collection for ColPali visual embeddings.

    Creates a collection configured for late-interaction MaxSim retrieval
    using 128-dimensional patch vectors.

    Args:
        client: QdrantClient instance.
        coll_name: Collection name (typically "{project}_visual").

    Raises:
        RuntimeError: If collection creation fails.
    """
    if not client.collection_exists(coll_name):
        client.create_collection(
            collection_name=coll_name,
            vectors_config={
                "colpali": VectorParams(
                    size=COLPALI_VECTOR_DIM,
                    distance=Distance.COSINE,
                    multivector_config=MultiVectorConfig(
                        comparator=MultiVectorComparator.MAX_SIM
                    ),
                    hnsw_config=HnswConfigDiff(m=0),  # brute-force for MaxSim
                )
            },
        )


def _visual_point_id(slug: str, page_num: int) -> str:
    """Deterministic UUID for visual page embeddings.

    Args:
        slug: Document slug identifier.
        page_num: 1-indexed page number.

    Returns:
        UUID string for the point ID.
    """
    raw = f"{slug}:visual:{page_num}"
    return str(uuid.UUID(hashlib.md5(raw.encode()).hexdigest()))


def upsert_visual_pages(
    pages: list[dict],
    cfg: dict,
    client: QdrantClient = None,
) -> int:
    """Upsert ColPali visual embeddings to Qdrant.

    Args:
        pages: List of page dicts with keys:
            - "slug": Document identifier
            - "page_num": 1-indexed page number
            - "vectors": numpy array of shape (num_patches, 128)
            - "file_path": Relative path to source PDF
            - "png_path": Relative path to cached PNG
            - Optional: "doc_type", "extraction_model", etc.
        cfg: carta config dict (must contain qdrant_url, project_name).
        client: Optional QdrantClient instance.

    Returns:
        Number of points upserted.

    Raises:
        RuntimeError: If upsert fails.
    """
    from carta.config import collection_name

    coll_name = f"{cfg['project_name']}_visual"

    if client is None:
        qdrant_url = cfg["qdrant_url"]
        client = QdrantClient(url=qdrant_url, timeout=5)
    ensure_visual_collection(client, coll_name)

    upserted = 0
    batch: list[PointStruct] = []

    for page in pages:
        page_id = f"{page.get('slug', '?')}:p{page.get('page_num', '?')}"
        try:
            vectors = page["vectors"]
            # Convert numpy array to list of lists for Qdrant multi-vector
            if hasattr(vectors, "tolist"):
                vector_list = vectors.tolist()
            else:
                vector_list = list(vectors)

            # Build payload with page metadata
            payload = {
                k: v for k, v in page.items()
                if k not in ("vectors", "png_bytes")
            }
            payload["doc_type"] = page.get("doc_type", "visual_page")

            point_id = _visual_point_id(page["slug"], page["page_num"])

            point = PointStruct(
                id=point_id,
                vector={"colpali": vector_list},
                payload=payload,
            )
            batch.append(point)

        except Exception as e:
            print(f"Warning: skipping visual page {page_id} — {e}", flush=True)
            continue

        if len(batch) >= VISUAL_BATCH_SIZE:
            try:
                client.upsert(collection_name=coll_name, points=batch)
                upserted += len(batch)
            except Exception as e:
                print(f"Warning: visual batch upsert failed — {e}", flush=True)
            batch = []

    # Flush remaining points
    if batch:
        try:
            client.upsert(collection_name=coll_name, points=batch)
            upserted += len(batch)
        except Exception as e:
            print(f"Warning: visual batch upsert failed — {e}", flush=True)

    return upserted
