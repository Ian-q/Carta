"""Stage 1: scan_text turns doc text into candidates, reading nothing but the text."""
from __future__ import annotations

from carta.graph.markdown import collapse_evidence, scan_text
from carta.graph.settings import graph_settings


def tuples(text, patterns=()):
    return [(c.raw, c.ctx, c.line, c.single) for c in scan_text(text, patterns)]


def test_inline_code_single_token():
    assert tuples("See `src/parser/frame.py` now.\n") == [("src/parser/frame.py", "code", 1, True)]


def test_inline_code_command_yields_only_slash_tokens():
    assert tuples("Run `python tools/gen.py --out build/` first.") == [
        ("tools/gen.py", "code", 1, False), ("build/", "code", 1, False)]


def test_call_with_arguments_is_one_single_candidate():
    assert tuples("Call `parse_frame(buf, n)` then `Parser::next(a, (b + c))`.") == [
        ("parse_frame(buf, n)", "code", 1, True), ("Parser::next(a, (b + c))", "code", 1, True)]
    # not a lone call: split like any command line
    assert tuples("`parse_frame(buf, n) + tools/x.py`") == [("tools/x.py", "code", 1, False)]
    assert tuples("`f(a) g(b)`") == []


def test_double_backtick_spans():
    assert tuples("Use ``src/a`b.py`` and `` `x/y.py` ``.") == [
        ("src/a`b.py", "code", 1, True), ("`x/y.py`", "code", 1, True)]


def test_unclosed_backtick_yields_nothing():
    assert tuples("An unclosed `src/a.py span\nand a second line.") == []


def test_two_spans_on_one_line_in_column_order():
    assert [t[0] for t in tuples("`b/z.py` then `a/y.py`")] == ["b/z.py", "a/y.py"]


def test_links_inline_image_and_reference_definition():
    text = "A [frame](../src/frame.py) and ![logo](assets/logo.png).\n\n[ref]: docs/guide.md\n"
    assert tuples(text) == [
        ("../src/frame.py", "link", 1, True), ("assets/logo.png", "link", 1, True),
        ("docs/guide.md", "link", 3, True)]


def test_link_with_title_and_angle_brackets():
    text = '[a](src/a.py "the title") [b](<docs/My Notes.md>) [c]( src/c.py )'
    assert [t[0] for t in tuples(text)] == ["src/a.py", "<docs/My Notes.md>", "src/c.py"]


def test_image_inside_a_link_yields_both_targets():
    assert [t[0] for t in tuples("[![badge](img/b.svg)](docs/ci.md)")] == ["img/b.svg", "docs/ci.md"]


def test_link_inside_code_span_is_code_only():
    assert tuples("Write `[x](src/a.py)` literally.") == [("[x](src/a.py)", "code", 1, True)]


def test_code_span_inside_link_text_yields_code_and_link():
    assert tuples("[`src/a.py`](src/a.py)") == [
        ("src/a.py", "code", 1, True), ("src/a.py", "link", 1, True)]


def test_footnote_definition_is_not_a_link():
    assert tuples("[^1]: see the notes\n") == []


def test_fenced_block_backticks_and_tildes():
    text = "```bash\npython tools/gen.py --out=build/x.json\n```\n~~~\ncat src/a.py\n~~~\n"
    assert tuples(text) == [
        ("tools/gen.py", "fence", 2, False), ("build/x.json", "fence", 2, False),
        ("src/a.py", "fence", 5, False)]


def test_fence_closed_only_by_long_enough_run():
    text = "````\n```\nsrc/in.py\n```\n````\n`src/out.py`\n"
    assert tuples(text) == [("src/in.py", "fence", 3, False), ("src/out.py", "code", 6, True)]


def test_fence_not_closed_by_other_character():
    text = "```\n~~~\nsrc/in.py\n```\n`src/out.py`\n"
    assert tuples(text) == [("src/in.py", "fence", 3, False), ("src/out.py", "code", 5, True)]


def test_fence_tokens_need_a_slash():
    assert tuples("```\nframe.py parse_frame\nls src/\n```\n") == [("src/", "fence", 3, False)]


def test_indented_fence_inside_a_list():
    text = "- item\n\n    ```\n    cat src/a.py\n    ```\n- `src/b.py`\n"
    assert tuples(text) == [("src/a.py", "fence", 4, False), ("src/b.py", "code", 6, True)]


def test_unterminated_fence_runs_to_eof():
    assert tuples("```\nsrc/a.py\n`src/b.py`\n") == [
        ("src/a.py", "fence", 2, False), ("src/b.py", "fence", 3, False)]


def test_frontmatter_tokens_and_line_numbers():
    text = ("---\ntitle: Parser design\nrelated:\n  - docs/store.md\n  - \"guide/setup.md\"\n"
            "covers: [src/parser/*.py, 'src/store/']\nowner: someone\n---\nBody `src/a.py`.\n")
    assert tuples(text) == [
        ("docs/store.md", "frontmatter", 4, True), ("guide/setup.md", "frontmatter", 5, True),
        ("src/parser/*.py", "frontmatter", 6, True), ("src/store/", "frontmatter", 6, True),
        ("src/a.py", "code", 9, True)]


def test_no_frontmatter_when_first_line_is_not_dashes():
    assert tuples("# Title\n---\nrelated: docs/x.md\n---\n") == []


def test_frontmatter_never_closed_is_treated_as_body():
    assert tuples("---\nrelated: docs/x.md\n`src/a.py`\n") == [("src/a.py", "code", 3, True)]


def test_line_numbers_with_crlf_and_bom():
    text = "﻿# T\r\n\r\n`src/a.py`\r\n[b](src/b.py)\r\n"
    assert tuples(text) == [("src/a.py", "code", 3, True), ("src/b.py", "link", 4, True)]


def test_bom_does_not_hide_frontmatter():
    assert tuples("﻿---\nrelated: docs/x.md\n---\n") == [("docs/x.md", "frontmatter", 2, True)]


def test_evidence_is_collapsed_and_truncated():
    assert collapse_evidence("  a \t b\n") == "a b"
    long_line = "word " * 80 + "`src/a.py`"
    (cand,) = scan_text(long_line)
    assert len(cand.evidence) == 240 and cand.evidence.endswith("…")
    assert collapse_evidence("x" * 240) == "x" * 240
    (short,) = scan_text("  The   rules live in `src/a.py`.  ")
    assert short.evidence == "The rules live in `src/a.py`."


def _patterns(where):
    return graph_settings({"graph": {"patterns": [
        {"name": "message", "regex": r"\bMSG_[A-Z]+\b", "where": where}]}}).patterns


TEXT_WITH_PATTERNS = ("---\nkey: MSG_FRONT\n---\nProse MSG_PROSE and `MSG_SPAN` here.\n"
                      "```\nsend MSG_FENCE\n```\n")


def test_patterns_scopes():
    def found(where):
        return [(c.raw, c.line, c.pattern) for c in scan_text(TEXT_WITH_PATTERNS, _patterns(where))
                if c.ctx == "pattern"]
    assert found("all") == [("MSG_FRONT", 2, "message"), ("MSG_PROSE", 4, "message"),
                            ("MSG_SPAN", 4, "message"), ("MSG_FENCE", 6, "message")]
    assert found("prose") == [("MSG_PROSE", 4, "message")]
    assert found("code") == [("MSG_SPAN", 4, "message"), ("MSG_FENCE", 6, "message")]


def test_pattern_group_one_preferred_over_whole_match():
    pats = graph_settings({"graph": {"patterns": [
        {"name": "issue", "regex": r"issue (#\d+)"}]}}).patterns
    assert [(c.raw, c.pattern) for c in scan_text("see issue #12 and issue #7", pats)] == [
        ("#12", "issue"), ("#7", "issue")]


def test_plain_prose_paths_are_not_candidates():
    assert tuples("The file src/parser/frame.py and read/write and docs/x.md are prose.") == []


def test_scan_text_is_deterministic():
    text = "`src/a.py` [b](c/d.md)\n```\nx/y\n```\n"
    assert scan_text(text) == scan_text(text)


def test_empty_and_whitespace_text():
    assert scan_text("") == [] and scan_text("   \n\n\t\n") == []
