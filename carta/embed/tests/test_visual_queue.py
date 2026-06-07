from carta.embed.visual_queue import (
    add_pending_pages, move_to_done, queue_summary, format_summary_line,
    VISUAL_PENDING_KEY, VISUAL_DONE_KEY,
)


def test_add_pending_pages_dedupes_and_sorts():
    sc = {}
    add_pending_pages(sc, [3, 1, 1])
    assert sc[VISUAL_PENDING_KEY] == [1, 3]
    add_pending_pages(sc, [2, 3])
    assert sc[VISUAL_PENDING_KEY] == [1, 2, 3]


def test_add_pending_pages_excludes_already_done():
    sc = {VISUAL_DONE_KEY: [2]}
    add_pending_pages(sc, [1, 2, 3])
    assert sc[VISUAL_PENDING_KEY] == [1, 3]  # 2 already done, not re-queued


def test_move_to_done_transfers_page():
    sc = {VISUAL_PENDING_KEY: [1, 2, 3], VISUAL_DONE_KEY: []}
    move_to_done(sc, 2)
    assert sc[VISUAL_PENDING_KEY] == [1, 3]
    assert sc[VISUAL_DONE_KEY] == [2]
    move_to_done(sc, 2)  # idempotent
    assert sc[VISUAL_PENDING_KEY] == [1, 3]
    assert sc[VISUAL_DONE_KEY] == [2]


def test_queue_summary_counts_files_and_pages():
    sidecars = [
        {VISUAL_PENDING_KEY: [1, 2]},
        {VISUAL_PENDING_KEY: [5]},
        {VISUAL_PENDING_KEY: []},
        {},
    ]
    assert queue_summary(sidecars) == {"files": 2, "pages": 3}


def test_format_summary_line():
    assert format_summary_line({"files": 18, "pages": 42}) == (
        "Visual queue: 42 page(s) across 18 file(s) await visual embedding. "
        "Run `carta embed --visual` to process them."
    )
    assert format_summary_line({"files": 0, "pages": 0}) == ""
