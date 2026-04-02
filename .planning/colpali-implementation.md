# ColPali/ColQwen2 Multimodal Embedding Implementation Plan

Implementation of the parallel multimodal embedding pathway as specified in GitHub issue #1.

## Overview

Adds a **parallel multimodal embedding pathway** using ColPali/ColQwen2 — keeping the existing LLaVA→text pipeline intact while adding a new path for visually rich pages.

## Design Principle: Additive, Not Replacing

```
Page classifier (existing carta/vision/classifier.py)
│
├── text_score ≥ 0.70  →  existing nomic-embed-text path  (unchanged)
│
├── visual_score ≥ 0.40 AND colpali_enabled: true in config
│                        →  NEW ColPali multi-vector path
│
└── fallback              →  existing LLaVA→text path     (unchanged)
```

## Implementation Phases

### Phase 1: Foundation (Config + Dependencies)

**Files to modify:**
- `carta/config.py` — Add colpali config stanza to DEFAULTS
- `pyproject.toml` — Add `[visual]` optional dependency group

**New config keys:**
```yaml
embed:
  colpali_enabled: false          # opt-in flag
  colpali_model: "vidore/colqwen2-v1.0"   # or colpali-v1.3 for lower VRAM
  colpali_device: "cpu"           # "cpu", "cuda", "mps"
  colpali_batch_size: 1           # pages per batch (1 for CPU)
  colpali_sidecar_path: ".carta/visual_cache/"  # where to store page PNGs
```

### Phase 2: Core Embedding Module

**New file:** `carta/embed/colpali.py`

**Key classes/functions:**
- `ColPaliEmbedder` — Main embedder class
  - `__init__(model_name, device, batch_size, cache_dir)`
  - `embed_page(page_image: PIL.Image) -> np.ndarray` — Returns 1024×128 patch vectors
  - `embed_pdf_page(pdf_path, page_num) -> tuple[np.ndarray, bytes]` — Returns vectors + PNG bytes
  - `embed_pdf_pages(pdf_path, page_nums) -> list[tuple]` — Batch processing

**Implementation notes:**
- Use `colpali-engine` from PyPI
- Lazy-load model (only when first embedding call happens)
- Cache loaded model to avoid reloads
- Handle device placement (cuda, mps, cpu)
- Return numpy arrays for vector data

### Phase 3: Qdrant Multi-Vector Support

**Files to modify:**
- `carta/embed/embed.py` — Add multi-vector collection support

**New functions:**
- `ensure_visual_collection(client, coll_name)` — Create `{project}_visual` collection
- `upsert_visual_pages(pages, cfg, client)` — Upsert multi-vector points

**Qdrant collection schema:**
```python
client.create_collection(
    collection_name=f"{project_name}_visual",
    vectors_config={
        "colpali": VectorParams(
            size=128,
            distance=Distance.COSINE,
            multivector_config=MultiVectorConfig(
                comparator=MultiVectorComparator.MAX_SIM
            ),
            hnsw_config=HnswConfigDiff(m=0),  # brute-force MaxSim
        )
    }
)
```

### Phase 4: Pipeline Integration

**Files to modify:**
- `carta/embed/pipeline.py` — Route visual pages to ColPali path

**Changes needed:**
1. Check `colpali_enabled` in config
2. When processing PDF pages with high visual_score:
   - Generate page PNG to visual_cache
   - Embed via ColPaliEmbedder
   - Upsert to `_visual` collection with page metadata
3. Store visual sidecar metadata (different from text sidecars)

**Visual page metadata to store:**
- `slug`, `file_path`, `page_num`
- `doc_type`: "visual_page"
- `png_path`: relative path to cached PNG
- `extraction_model`: which ColPali variant was used

### Phase 5: MCP Tool Enhancement

**Files to modify:**
- `carta/mcp/server.py` — Extend `carta_search` to return ImageContent

**Changes needed:**
1. Search both `_doc` (text) and `_visual` (visual) collections
2. For visual hits:
   - Load the PNG from visual_cache
   - Return as FastMCP `Image` content block
3. Add score-based merging of text + visual results

**MCP return format:**
```python
# For visual results
{
    "score": 0.8234,
    "source": "datasheet.pdf (page 42)",
    "type": "visual",
    "image_b64": "...base64...",  # For clients that want raw
    # Plus ImageContent block for vision-capable clients
}
```

### Phase 6: Testing & Documentation

**New tests:**
- `carta/embed/tests/test_colpali.py` — Unit tests for ColPaliEmbedder
- Update `carta/tests/test_pipeline.py` — Test visual routing
- Update `carta/mcp/tests/test_server.py` — Test ImageContent returns

**Documentation updates:**
- README.md — Add new config keys and model selection guide
- Document the compatible models table

## Model Selection Guide

| Model | VRAM | nDCG@5 | Notes |
|-------|------|--------|-------|
| `vidore/colqwen2-v1.0` | ~8GB GPU / slow CPU | 81.4 | Best quality, recommended with GPU |
| `vidore/colpali-v1.3` | ~6GB GPU / slow CPU | ~79 | Faster inference |
| `vidore/colSmol-256M` | ~2GB / CPU-feasible | ~74 | Good for CPU-only setups |
| `vidore/colSmol-500M` | ~3GB / CPU-feasible | ~76 | Balance of speed/quality on CPU |

## Storage Layout

```
.carta/
├── config.yaml
├── embed.lock
└── visual_cache/           # NEW: Page PNG sidecars
    ├── datasheet.pdf/
    │   ├── page_001.png
    │   ├── page_042.png
    │   └── ...
    └── manual.pdf/
        ├── page_001.png
        └── ...
```

## Acceptance Criteria

- [ ] Existing text embedding pipeline passes all tests unchanged
- [ ] `colpali_enabled: false` (default) produces identical behavior to current main
- [ ] With `colpali_enabled: true`, visual pages embed to `_visual` collection
- [ ] `carta_search` returns `ImageContent` for visual hits when running in a vision-capable client
- [ ] Page PNG sidecars stored to `.carta/visual_cache/` and gitignored
- [ ] `pyproject.toml` optional `[visual]` extra installs colpali-engine deps without breaking base install
- [ ] README documents the new config keys and model selection guide

## References

- [Qdrant ColPali tutorial](https://qdrant.tech/documentation/tutorials-search-engineering/pdf-retrieval-at-scale/)
- [colpali-engine PyPI](https://pypi.org/project/colpali-engine/)
- [ViDoRe Benchmark V2](https://huggingface.co/blog/manu/vidore-v2)
