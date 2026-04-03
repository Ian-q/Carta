---
gsd_backlog_version: 1.0
milestone: v0.2
last_updated: "2026-04-03"
---

# Carta Backlog — Post ColPali Implementation

**Context:** Issue #1 ColPali/ColQwen2 multimodal embedding is feature-complete with core functionality. This backlog tracks remaining refinements identified during code review.

---

## Priority: Medium

### Issue #1-Followup-01: Page Classifier Integration for Selective Visual Embedding

**Status:** ⏸️ TODO  
**Added:** 2026-04-03  
**Source:** Code Review Issue M2  

**Problem:** Currently all PDF pages are embedded via ColPali when enabled, ignoring content classification. This wastes compute on text-heavy pages that don't benefit from visual embedding.

**Current behavior:**
```python
# pipeline.py:_embed_visual_pages_colpali()
# TODO: Use page classifier to select only visual-rich pages
page_results = embedder.embed_pdf_pages(file_path, page_nums=None)  # All pages!
```

**Expected behavior:**
- Use `carta/vision/classifier.py` to classify each page
- Only embed pages with `content_type == ContentType.VISUAL` or `ContentType.MIXED`
- Respect visual_score threshold from config (`embed.classification.visual_threshold: 0.40`)
- Return count of skipped pages in sidecar metadata

**Acceptance Criteria:**
- [ ] Integrate `ContentClassifier` into `_embed_visual_pages_colpali()`
- [ ] Only pages with `visual_score >= 0.40` get ColPali embedded
- [ ] Text-heavy pages fall back to standard text extraction only
- [ ] Sidecar metadata includes: `visual_pages_embedded`, `visual_pages_skipped`
- [ ] Tests for selective embedding logic

**Files affected:**
- `carta/embed/pipeline.py`
- `carta/vision/classifier.py` (may need export)
- `carta/embed/tests/test_pipeline.py`

---

### Issue #1-Followup-02: Collection Cleanup on Re-embed

**Status:** ⏸️ TODO  
**Added:** 2026-04-03  
**Source:** Code Review Issue M3  

**Problem:** When a PDF is re-embedded (new generation), old visual page points in `_visual` collection are not marked stale or deleted. This causes accumulation of orphaned visual vectors and potential duplicate results on search.

**Current behavior:**
- Text collections use `mark_sidecar_stale()` for lifecycle management
- Visual collections have no equivalent cleanup mechanism
- Re-embedding a PDF creates new points without invalidating old ones

**Expected behavior:**
- Add `mark_visual_stale()` function to `carta/embed/embed.py`
- Call it in `run_embed_file()` when re-embedding PDFs
- Consider TTL or periodic cleanup strategy for orphaned vectors
- Sidecar metadata tracks generation for visual pages too

**Acceptance Criteria:**
- [ ] Create `mark_visual_stale()` function (similar to `mark_sidecar_stale()`)
- [ ] Integrate into `run_embed_file()` lifecycle
- [ ] Add generation-aware point IDs for visual pages
- [ ] Test that re-embedding doesn't create duplicates
- [ ] Document cleanup strategy

**Files affected:**
- `carta/embed/embed.py`
- `carta/embed/lifecycle.py` (may need visual variant)
- `carta/embed/pipeline.py`
- `carta/embed/tests/test_lifecycle.py`

---

### Issue #1-Followup-03: Comprehensive Embedding Tests

**Status:** ⏸️ TODO  
**Added:** 2026-04-03  
**Source:** Code Review Issue M1  

**Problem:** Current tests only cover initialization and configuration. Core embedding functionality (`embed_page()`, `embed_pdf_page()`) is not directly tested. Refactors could break embedding without detection.

**Current coverage:**
- ✅ Config/init tests
- ✅ Error handling tests  
- ❌ Actual embedding output shape
- ❌ PDF page extraction
- ❌ Batch processing
- ❌ Model caching behavior
- ❌ Query encoding results

**Test additions needed:**

```python
# test_colpali.py additions needed:

def test_embed_page_returns_correct_shape():
    """Mocked test for embed_page returning (num_patches, 128) array."""
    
def test_embed_pdf_page_handles_missing_page():
    """Test error handling for out-of-range page number."""
    
def test_embed_pdf_pages_batch_processing():
    """Test that batch_size is respected across pages."""
    
def test_embed_query_returns_query_vectors():
    """Test query encoding produces proper token vectors."""
    
def test_model_caching_prevents_reload():
    """Verify class-level cache prevents duplicate model loads."""
```

**Acceptance Criteria:**
- [ ] Add integration-style tests with mocked ColPali model
- [ ] Test actual vector shapes from embedding methods
- [ ] Test PDF page extraction and error handling
- [ ] Test batch processing with various batch sizes
- [ ] Test query encoding path
- [ ] Verify model caching behavior
- [ ] Target: 80%+ coverage for `carta/embed/colpali.py`

**Files affected:**
- `carta/embed/tests/test_colpali.py`
- May need test fixtures/mocks for ColPali models

---

## Notes

**Why these aren't blockers for Issue #1:**

1. **Page classifier:** The feature works correctly without it — just less efficiently. It's an optimization, not core functionality.

2. **Collection cleanup:** Visual search works fine; this is data hygiene for long-running deployments. Workaround: manual collection deletion if needed.

3. **Tests:** Core functionality is tested indirectly through integration. These are unit test gaps, not functional gaps.

**When to address:**
- These can be tackled in v0.2.x patch releases
- Or bundled into v0.3 milestone
- Priority increases if users report performance/storage issues

**Related work:**
- May overlap with future "intelligent embedding" enhancements
- Could combine with page classifier work for smart PDF processing

---

*Backlog initialized: 2026-04-03*
