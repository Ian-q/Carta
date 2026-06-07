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
