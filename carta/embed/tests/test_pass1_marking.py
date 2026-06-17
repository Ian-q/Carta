from carta.vision.classifier import PageClass
from carta.embed import pipeline
from carta.embed.visual_queue import VISUAL_PENDING_KEY


def test_mark_collects_image_heavy_pages_when_enabled():
    cfg = {"embed": {"two_pass_visual": True}}
    page_classes = [PageClass.PURE_TEXT, PageClass.TEXT_WITH_IMAGES,
                    PageClass.STRUCTURED_TEXT, PageClass.FLATTENED]
    updates = pipeline._mark_or_collect_visual_pages(page_classes, cfg)
    assert updates[VISUAL_PENDING_KEY] == [2, 4]  # 1-indexed image-heavy pages


def test_mark_noop_when_disabled():
    cfg = {"embed": {"two_pass_visual": False}}
    page_classes = [PageClass.TEXT_WITH_IMAGES, PageClass.FLATTENED]
    assert pipeline._mark_or_collect_visual_pages(page_classes, cfg) == {}


def test_mark_noop_when_no_image_heavy_pages():
    cfg = {"embed": {"two_pass_visual": True}}
    page_classes = [PageClass.PURE_TEXT, PageClass.STRUCTURED_TEXT]
    assert pipeline._mark_or_collect_visual_pages(page_classes, cfg) == {}


# Queue-side scope (bug: queueing ignored colpali_scoped_paths; only the drain
# skipped out-of-scope sources, so patents re-queued every embed and inflated the
# misleading "N pages await visual" count). Queueing must honor scope like the drain.

_SCOPED_CFG = {
    "embed": {
        "two_pass_visual": True,
        "colpali_scoped_paths": ["docs/reference/datasheets/"],
    }
}


def test_mark_skips_out_of_scope_source_when_scopes_set():
    page_classes = [PageClass.TEXT_WITH_IMAGES, PageClass.FLATTENED]
    updates = pipeline._mark_or_collect_visual_pages(
        page_classes, _SCOPED_CFG, "docs/reference/patents/US123.pdf"
    )
    assert updates == {}  # out of scope → not queued (drain would skip it anyway)


def test_mark_queues_in_scope_source_when_scopes_set():
    page_classes = [PageClass.TEXT_WITH_IMAGES, PageClass.FLATTENED]
    updates = pipeline._mark_or_collect_visual_pages(
        page_classes, _SCOPED_CFG, "docs/reference/datasheets/part.pdf"
    )
    assert updates[VISUAL_PENDING_KEY] == [1, 2]


def test_mark_no_scopes_queues_regardless_of_path():
    """Empty/absent colpali_scoped_paths = no restriction (backward compatible)."""
    cfg = {"embed": {"two_pass_visual": True}}
    page_classes = [PageClass.TEXT_WITH_IMAGES]
    updates = pipeline._mark_or_collect_visual_pages(
        page_classes, cfg, "anywhere/patents/US999.pdf"
    )
    assert updates[VISUAL_PENDING_KEY] == [1]
