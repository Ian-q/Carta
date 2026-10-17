"""Stage 2 and the composed pure extractor. One test per normalisation rule."""
from __future__ import annotations

import builtins
import json
import os
import subprocess

import pytest

from carta.graph.markdown import scan_text
from carta.graph.mentions import extract_mentions, normalise_candidate, resolve_candidates
from carta.graph.paths import PathIndex
from carta.graph.settings import DEFAULT_SETTINGS, graph_settings

PATHS = [
    "README.md", "docs/design/parser.md", "docs/design/store.md", "docs/guide/setup.md",
    "docs/guide/My Notes.md", "src/parser/frame.py", "src/parser/checksum.py",
    "src/parser/tests/test_frame.py", "src/store/db.py", "src/store/frame.py",
    "tools/gen_tables.py", "tools/build/gen.py", "Makefile", "assets/logo.png",
    "examples/demo/run.py", "packages/a/README.md", "packages/a/src/index.ts",
    "packages/b/src/index.ts", "packages/a/lib/util.ts",
]
DOC = "docs/design/parser.md"


def edges(text, doc=DOC, settings=DEFAULT_SETTINGS, paths=PATHS):
    return extract_mentions(doc, text, paths, settings)


def paths_of(text, doc=DOC, **kw):
    return [(e.dst, e.dst_type) for e in edges(text, doc, **kw) if e.kind == "mention"]


def meta(edge):
    return json.loads(edge.meta)


# -- N1 / N2 -----------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "`src/parser/frame.py.`", "`src/parser/frame.py,`", "`src/parser/frame.py:`",
    "`src/parser/frame.py:42`", "`src/parser/frame.py:42:7`", "`src/parser/frame.py:10-20`",
    "`src/parser/frame.py#L10-L20`", "`'src/parser/frame.py'`", "`<src/parser/frame.py>`",
    "`(src/parser/frame.py)`", "`src/parser/frame.py::test_reads_header`",
    "`src/parser/frame.py::TestFrame::test_x[case-1]`", "`src/parser/frame.py:parse_frame`",
    "[x](../../src/parser/frame.py?raw=1)", "[x](../../src/parser/frame.py#L3)",
])
def test_n1_n2_suffixes_and_punctuation_are_cut(text):
    assert paths_of(text) == [("src/parser/frame.py", "file")]


def test_n2_fragment_only_gives_nothing():
    assert edges("[top](#section) and `#anchor`") == []


def test_n1_link_target_is_percent_decoded():
    assert paths_of("[notes](../guide/My%20Notes.md)") == [("docs/guide/My Notes.md", "doc")]
    assert paths_of("[notes](<../guide/My Notes.md>)") == [("docs/guide/My Notes.md", "doc")]


# -- N3 / N4 -----------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "`https://example.org/src/parser/frame.py`", "[m](mailto:someone@example.org)",
    "[c](//cdn.example.org/src/x.js)", "`www.example.org/src/x.py`", "`~/src/parser/frame.py`",
    "`@scope/pkg/index.js`", "`someone@example.org`", "`src\\parser\\frame.py`",
    "`$ROOT/src/parser/frame.py`", "`<dir>/src/frame.py`", "`src/.../frame.py`",
    "`src/…/frame.py`", "`-o/src/x.py`", "`src/parser/\"frame\".py`", "`" + "a/" * 200 + "b.py`",
    "`git@host.example:team/repo.git`",
])
def test_n3_n4_non_paths_give_no_edge(text):
    assert paths_of(text) == []


# -- N5 ----------------------------------------------------------------------

def test_n5_leading_slash_is_repo_root_only_in_links():
    assert paths_of("[x](/src/parser/frame.py)") == [("src/parser/frame.py", "file")]
    assert paths_of("`/usr/lib/libx.so` and `/src/parser/frame.py`") == []


# -- N6 ----------------------------------------------------------------------

def test_n6_brace_expansion():
    assert paths_of("`src/parser/{frame,checksum}.py`") == [
        ("src/parser/checksum.py", "file"), ("src/parser/frame.py", "file")]
    assert paths_of("`src/{name}.py` and `src/{a,{b,c}}.py`") == []


# -- N7 ----------------------------------------------------------------------

def test_n7_globs():
    (e,) = edges("`src/parser/*.py`")
    assert (e.dst, e.dst_type, meta(e)["matches"]) == ("src/parser/*.py", "glob", 2)
    (deep,) = edges("`src/**/*.py`")
    assert meta(deep)["matches"] == 5
    assert edges("`nope/*.py` and `src/parser/*.rs`") == []          # no match: no edge at all
    assert [e.dst for e in edges("`*.md`")] == ["*.md"]              # root-anchored
    assert edges("`*` and `**` and `a?` and `x[0]`") == []
    # not globs once normalised: bare wildcards, and a pasted regex
    assert edges("`*/` `**/` `*//` `[^/]*` `*/*`") == []


def test_n1_bold_markers_inside_a_span_are_not_a_glob():
    assert paths_of("`**src/store/db.py**` and `**src/parser/frame.py`") == [
        ("src/parser/frame.py", "file"), ("src/store/db.py", "file")]
    assert [e.dst for e in edges("`**/*.md`")] == ["**/*.md"]
    assert [e.dst for e in edges("`src/**`")] == ["src/**"]
    (cls,) = edges("`src/store/d[ab].py`")
    assert cls.dst_type == "glob" and meta(cls)["matches"] == 1


def test_n7_glob_relative_to_doc_directory():
    (e,) = edges("`./*.md`")
    assert (e.dst, meta(e)["matches"]) == ("docs/design/*.md", 2)
    assert edges("`../../../*.md`") == []


# -- N8 ----------------------------------------------------------------------

def test_n8_links_resolve_from_doc_directory_then_root():
    assert paths_of("[a](../../src/parser/frame.py)") == [("src/parser/frame.py", "file")]
    assert paths_of("[b](store.md)") == [("docs/design/store.md", "doc")]
    assert paths_of("[c](src/store/db.py)") == [("src/store/db.py", "file")]


def test_n8_dot_relative_code_resolves_from_doc_directory_first():
    assert paths_of("`./store.md`") == [("docs/design/store.md", "doc")]
    assert paths_of("`../guide/setup.md`") == [("docs/guide/setup.md", "doc")]
    # "./" is tried beside the doc first, then from the repo root
    assert paths_of("`./src/store/db.py`") == [("src/store/db.py", "file")]


def test_n8_outside_the_repo_gives_nothing():
    assert edges("`../../../etc/passwd` [x](../../../other/repo.md) `../../../x/y.py`") == []


def test_n8_nested_readme_resolves_relative_to_itself():
    doc = "packages/a/README.md"
    (e,) = edges("`src/index.ts`", doc=doc)
    assert (e.dst, e.dst_type, meta(e)["via"]) == ("packages/a/src/index.ts", "file", "relative")
    (d,) = edges("`lib/`", doc=doc)
    assert (d.dst, d.dst_type) == ("packages/a/lib/", "dir")
    # the repo root still wins when it has the path
    (root,) = edges("`src/store/db.py`", doc=doc)
    assert root.dst == "src/store/db.py" and "via" not in meta(root)


def test_n8_ancestor_directory_is_tried():
    (e,) = edges("`lib/util.ts`", doc="packages/a/src/notes.md",
                 paths=PATHS + ["packages/a/src/notes.md"])
    assert (e.dst, meta(e)["via"]) == ("packages/a/lib/util.ts", "relative")


# -- N9 ----------------------------------------------------------------------

def test_n9_doc_targets_are_distinguishable():
    got = dict(paths_of("`docs/design/store.md` `src/parser/frame.py` `assets/logo.png`"))
    assert got == {"docs/design/store.md": "doc", "src/parser/frame.py": "file",
                   "assets/logo.png": "file"}


# -- N10 ---------------------------------------------------------------------

def test_n10_directories():
    assert paths_of("`src/parser/`") == [("src/parser/", "dir")]
    assert paths_of("`src/parser`") == [("src/parser/", "dir")]
    assert paths_of("`docs/`") == [("docs/", "dir")]
    assert paths_of("`docs` and `tests` and `src`") == []
    assert paths_of("`src/parser/frame.py/`") == []


# -- N11 ---------------------------------------------------------------------

def test_n11_unique_suffix():
    (e,) = edges("`parser/checksum.py`")
    assert (e.dst, meta(e)["via"], meta(e)["raw"]) == (
        "src/parser/checksum.py", "suffix", "parser/checksum.py")
    assert paths_of("`tests/test_frame.py`") == [("src/parser/tests/test_frame.py", "file")]
    assert paths_of("`parser/tests/`") == [("src/parser/tests/", "dir")]
    assert paths_of("`src/index.ts`") == []            # two packages have it: ambiguous
    assert paths_of("`xparser/frame.py`") == [("xparser/frame.py", "missing")]


# -- N12 ---------------------------------------------------------------------

def test_n12_bare_filenames():
    (e,) = edges("`checksum.py`")
    assert (e.dst, meta(e)["via"]) == ("src/parser/checksum.py", "basename")
    assert paths_of("`frame.py`") == []                            # two files
    assert paths_of("`Makefile`") == []                            # no extension: a word
    assert paths_of("`README.md`") == [("README.md", "doc")]       # the root file wins
    assert paths_of("`store.md`") == [("docs/design/store.md", "doc")]
    assert paths_of("`1.5` `v2.0` `3.10`") == []
    assert paths_of("Run `python gen_tables.py --all`") == []      # multi-token span
    assert paths_of("```\ngen_tables.py\n```") == []               # fence
    assert paths_of("`README.md`", doc="packages/a/README.md") == [("README.md", "doc")]


def test_n12_sibling_is_marked_relative():
    (e,) = edges("`store.md`")
    assert meta(e)["via"] == "relative"


def test_n12_links_do_not_search_by_basename():
    assert paths_of("[x](checksum.py)") == [
        ("checksum.py", "missing"), ("docs/design/checksum.py", "missing")]
    assert paths_of("[x](../../Makefile)") == [("Makefile", "file")]


# -- N13 ---------------------------------------------------------------------

def test_n13_missing_paths():
    assert paths_of("`src/parser/old_frame.py`") == [("src/parser/old_frame.py", "missing")]
    assert paths_of("`removed_pkg/core/x.py`") == [("removed_pkg/core/x.py", "missing")]
    assert paths_of("`src/gone/`") == [("src/gone/", "missing")]
    assert paths_of("[x](gone.md)") == [("docs/design/gone.md", "missing"), ("gone.md", "missing")]
    assert paths_of("[x](../guide/gone/)") == [("docs/guide/gone/", "missing")]


@pytest.mark.parametrize("text", [
    "`path/to/file.py`", "`src/path/to/file.py`", "`foo/bar.py`", "`src/foo.py`",
    "`your-project/main.py`", "`my_app/x.py`", "`src/example.py`", "`xxx/y.py`",
    "`src/parser/thing`", "`nope/dir/`", "`read/write`", "`and/or`", "`application/json`",
    "`origin/main`", "`v1.2/v1.3`", "`.carta/graph/edges.db`", "`.git/hooks/pre-commit.sample`",
    "```\nsrc/parser/old_frame.py\n```", "`gone.py`",
])
def test_n13_not_missing(text):
    assert paths_of(text) == []


def test_n13_placeholder_never_shadows_a_real_directory():
    assert paths_of("`examples/demo/old.py`") == [("examples/demo/old.py", "missing")]
    assert paths_of("`tools/build/old.py`") == [("tools/build/old.py", "missing")]
    extra = PATHS + ["bazel/rules/x.bzl", "foo/real.py"]
    assert paths_of("`bazel/rules/gone.bzl`", paths=extra) == [("bazel/rules/gone.bzl", "missing")]
    assert paths_of("`foo/gone.py`", paths=extra) == [("foo/gone.py", "missing")]
    assert paths_of("`bazelisk/gone.bzl`") == [("bazelisk/gone.bzl", "missing")]


# -- N14 ---------------------------------------------------------------------

def test_n14_self_mention_is_dropped():
    assert edges("[me](parser.md) `docs/design/parser.md` [me](#top)") == []


# -- weights, de-duplication, ordering ---------------------------------------

def test_fence_mentions_have_half_weight():
    (e,) = edges("```\ncat src/store/db.py\n```")
    assert (e.weight, meta(e)["ctx"]) == (0.5, "fence")
    (c,) = edges("`src/store/db.py`")
    assert c.weight == 1.0


def test_same_path_twice_on_one_line_is_one_edge():
    assert len(edges("`src/store/db.py` and again `src/store/db.py`")) == 1
    assert len(edges("[`src/store/db.py`](../../src/store/db.py)")) == 1


def test_same_path_on_two_lines_is_two_edges():
    assert [e.line for e in edges("`src/store/db.py`\n\n`src/store/db.py`")] == [1, 3]


def test_evidence_is_the_doc_line():
    (e,) = edges("#  Title\n\nThe   rules live in `src/store/db.py`.\n")
    assert (e.line, e.evidence) == (3, "The rules live in `src/store/db.py`.")


def test_edge_defaults_for_a_mention():
    (e,) = edges("`src/store/db.py`")
    assert (e.kind, e.src, e.src_type, e.key, e.extractor) == ("mention", DOC, "doc", "", "mention")
    assert meta(e) == {"ctx": "code"}


def test_frontmatter_mentions():
    text = "---\nrelated:\n  - store.md\ncovers: [src/parser/*.py, src/store/]\n---\n"
    assert paths_of(text) == [("docs/design/store.md", "doc"), ("src/parser/*.py", "glob"),
                              ("src/store/", "dir")]


# -- identifiers and patterns -------------------------------------------------

def test_unresolved_single_token_falls_through_to_identifier():
    (e,) = edges("Call `parse_frame()` first.")
    assert (e.kind, e.dst, e.dst_type, meta(e)) == ("ident", "parse_frame", "token", {"call": True})
    (plain,) = edges("`MAX_RETRIES`")
    assert meta(plain) == {}
    assert [x.dst for x in edges("`parse_frame(buf, n)`")] == ["parse_frame"]
    off = graph_settings({"graph": {"identifiers": {"enabled": False}}})
    assert edges("Call `parse_frame()` first.", settings=off) == []


def test_identifier_filter_is_configurable_and_narrow():
    assert edges("`git` `--force` `HTTP` `RuntimeError` `git commit -m`") == []
    stop = graph_settings({"graph": {"identifiers": {"stoplist": ["parse_frame"]}}})
    assert edges("`parse_frame`", settings=stop) == []
    assert edges("```\nparse_frame()\n```") == []                 # never from fences
    assert edges("[parse_frame](parse_frame)") == []               # never from links
    # a span that became a path edge is not also an identifier
    assert [e.kind for e in edges("`src/store/db.py`")] == ["mention"]


def test_pattern_edges():
    s = graph_settings({"graph": {"patterns": [{"name": "message", "regex": r"\bMSG_[A-Z]+\b"}]}})
    got = [(e.kind, e.dst, e.dst_type, e.line) for e in edges("Send MSG_START.\n", settings=s)]
    assert got == [("pattern", "message:MSG_START", "pattern", 1)]
    both = edges("`MSG_START`", settings=s)
    assert sorted(e.kind for e in both) == ["ident", "pattern"]


def test_target_under_excluded_path_is_still_an_edge():
    s = graph_settings({"excluded_paths": ["src/store/", "docs/guide/"]})
    assert paths_of("`src/store/db.py` `docs/guide/setup.md`", settings=s) == [
        ("docs/guide/setup.md", "file"), ("src/store/db.py", "file")]
    assert paths_of("`tools/build/gen.py`") == [("tools/build/gen.py", "file")]


def test_output_sorted_and_deduplicated():
    out = edges("`tools/gen_tables.py` `src/store/db.py`\n`parse_frame` `src/store/db.py`\n")
    assert out == sorted(out)
    assert len({e.identity for e in out}) == len(out)


def test_extract_is_pure(monkeypatch):
    text = "`src/store/db.py` [s](store.md) `parse_frame()`\n```\ncat src/parser/frame.py\n```\n"
    expected = edges(text)

    def boom(*a, **k):
        raise AssertionError("the extractor touched the outside world")

    monkeypatch.setattr(builtins, "open", boom)
    monkeypatch.setattr(os, "stat", boom)
    monkeypatch.setattr(os, "listdir", boom)
    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(subprocess, "Popen", boom)
    for repo_paths in (list(PATHS), set(PATHS), tuple(reversed(PATHS)), PathIndex(PATHS)):
        assert extract_mentions(DOC, text, repo_paths) == expected
    assert extract_mentions(DOC, text, PATHS, DEFAULT_SETTINGS) == expected


def test_composition_equals_stages():
    text = "`src/store/db.py` and [x](store.md)\n"
    index = PathIndex(PATHS)
    assert extract_mentions(DOC, text, index) == resolve_candidates(
        DOC, scan_text(text), index, DEFAULT_SETTINGS)


def test_normalise_candidate_is_pure_string_work():
    assert normalise_candidate("src/a.py:42.", "code") == ["src/a.py"]
    assert normalise_candidate("a/{b,c}.py", "code") == ["a/b.py", "a/c.py"]
    assert normalise_candidate("/abs/x.py", "code") is None
    assert normalise_candidate("/abs/x.py", "link") == ["abs/x.py"]
    assert normalise_candidate("https://example.org/x", "link") is None
    assert normalise_candidate("", "code") is None


# -- review round 2: literal names, explicit code paths, alternates ---------------

WEB = PATHS + ["src/app/[id]/page.tsx", "src/C#/x.cs", "src/app/main.py", "src/app/data.json",
               "scripts/run.sh"]


@pytest.mark.parametrize("text", [
    "`src/app/[id]/page.tsx`", "[n](../../src/app/[id]/page.tsx)", "[n](/src/app/[id]/page.tsx)",
])
def test_a_real_file_with_glob_characters_in_its_name_is_a_literal(text):
    assert paths_of(text, paths=WEB) == [("src/app/[id]/page.tsx", "file")]


def test_a_bracket_pattern_that_names_no_file_is_still_a_glob():
    assert paths_of("`src/parser/[cf]*.py`", paths=WEB) == [("src/parser/[cf]*.py", "glob")]


def test_hash_inside_a_real_path_is_not_a_fragment():
    assert paths_of("`src/C#/x.cs`", paths=WEB) == [("src/C#/x.cs", "file")]
    # and a fragment is still cut when the full text names nothing
    assert paths_of("`src/app/main.py#L3/L4`", paths=WEB) == [("src/app/main.py", "file")]
    assert paths_of("`src/C#/gone.cs`", paths=WEB) == []


@pytest.mark.parametrize("text", [
    "`src/app/main.py:L10-L20`", "`src/app/main.py:L10`", "`src/app/main.py:L10-20`",
])
def test_l_prefixed_line_suffix_is_cut(text):
    assert paths_of(text, paths=WEB) == [("src/app/main.py", "file")]


@pytest.mark.parametrize("text", [
    "`--config=src/app/data.json`", "`run --config=src/app/data.json --fast`",
    "`CONFIG=src/app/data.json`",
])
def test_code_tokens_are_split_on_equals(text):
    assert paths_of(text, paths=WEB) == [("src/app/data.json", "file")]


def test_a_url_with_a_query_is_not_split_on_equals():
    assert paths_of("`https://example.invalid/x?next=src/app/main.py`", paths=WEB) == []


def test_dot_slash_in_code_falls_back_to_the_repo_root():
    assert paths_of("`./src/app/main.py`", doc="docs/guide/setup.md", paths=WEB) == [
        ("src/app/main.py", "file")]
    assert paths_of("Run `./scripts/run.sh --fast`.", doc="docs/guide/setup.md", paths=WEB) == [
        ("scripts/run.sh", "file")]
    # the doc's own directory still wins
    assert paths_of("`./setup.md`", doc="docs/guide/other.md", paths=WEB) == [
        ("docs/guide/setup.md", "doc")]


def test_dot_slash_in_code_that_resolves_nowhere_is_not_missing():
    assert paths_of("`./src/app/gone.py`", doc="docs/guide/setup.md", paths=WEB) == []
    # at the repo root there is one reading, so it is recorded
    assert paths_of("`./src/app/gone.py`", doc="README.md", paths=WEB) == [
        ("src/app/gone.py", "missing")]
    # "../" has one reading wherever the doc is
    assert paths_of("`../../src/app/gone.py`", doc="docs/guide/setup.md", paths=WEB) == [
        ("src/app/gone.py", "missing")]


def test_missing_link_records_the_root_reading_as_an_alternate():
    got = edges("[gen](tools/gone.py)", doc="docs/guide/setup.md", paths=WEB)
    assert [(e.dst, e.dst_type) for e in got] == [
        ("docs/guide/tools/gone.py", "missing"), ("tools/gone.py", "missing")]
    assert "alt" not in meta(got[0]) and meta(got[1])["alt"] is True
    # explicit, rooted and root-level links have one reading only
    assert paths_of("[gen](./tools/gone.py)", doc="docs/guide/setup.md", paths=WEB) == [
        ("docs/guide/tools/gone.py", "missing")]
    assert paths_of("[gen](/tools/gone.py)", doc="docs/guide/setup.md", paths=WEB) == [
        ("tools/gone.py", "missing")]
    assert paths_of("[gen](tools/gone.py)", doc="README.md", paths=WEB) == [
        ("tools/gone.py", "missing")]
    # a bare filename too
    got = edges("![alt](gone.png)", doc="docs/guide/setup.md", paths=WEB)
    assert [(e.dst, meta(e).get("alt", False)) for e in got] == [
        ("docs/guide/gone.png", False), ("gone.png", True)]
