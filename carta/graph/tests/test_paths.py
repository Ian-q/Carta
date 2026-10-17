"""Glob translation, brace expansion, exclusion parity and the path index."""
from __future__ import annotations

from pathlib import Path

import pytest

from carta.graph.paths import (
    ALWAYS_EXCLUDED_DIRS, PathIndex, expand_braces, glob_to_regex, in_path_set, is_doc_path,
    is_excluded_rel,
)
from carta.graph.settings import DEFAULT_SETTINGS, graph_settings


@pytest.mark.parametrize("glob, path, expected", [
    ("src/*.py", "src/a.py", True),
    ("src/*.py", "src/x/a.py", False),
    ("src/**/*.py", "src/a.py", True),
    ("src/**/*.py", "src/x/y/a.py", True),
    ("src/**", "src/x/y/a.py", True),
    ("**/*.md", "a.md", True),
    ("**/*.md", "x/y/a.md", True),
    ("**/*.md", "x/y/a.mdx", False),
    ("*.md", "x/a.md", False),
    ("src/fsm.[ch]", "src/fsm.c", True),
    ("src/fsm.[ch]", "src/fsm.h", True),
    ("src/fsm.[ch]", "src/fsm.o", False),
    ("src/fsm.[!ch]", "src/fsm.o", True),
    ("src/a?c.py", "src/abc.py", True),
    ("src/a?c.py", "src/a/c.py", False),
    ("lib/c++/(x).txt", "lib/c++/(x).txt", True),
    ("lib/a.b", "lib/aXb", False),
    ("src/[unclosed.py", "src/[unclosed.py", True),
])
def test_glob_to_regex(glob, path, expected):
    assert bool(glob_to_regex(glob).match(path)) is expected


@pytest.mark.parametrize("text, expected", [
    ("a.{c,h}", ["a.c", "a.h"]),
    ("src/{x,y,z}/m.py", ["src/x/m.py", "src/y/m.py", "src/z/m.py"]),
    ("plain/path.py", ["plain/path.py"]),
    ("a.{c,{h,o}}", None),
    ("a.{c,h", None),
    ("a.c,h}", None),
    ("src/{name}.py", None),
    ("{a,b}/{c,d}", None),
    ("x{" + ",".join(str(i) for i in range(17)) + "}", None),
])
def test_expand_braces(text, expected):
    assert expand_braces(text) == expected


EXCLUSION_CASES = [
    ("docs/a.md", []),
    ("node_modules/x/readme.md", []),
    ("pkg/node_modules/x.md", []),
    ("pkg/.venv/lib/site-packages/a.md", []),
    ("tools/build/gen.md", []),
    ("builder/a.md", []),
    ("docs/a.md", ["docs/"]),
    ("x/docs/a.md", ["docs/"]),
    ("xdocs/a.md", ["docs/"]),
    ("notes/a.tmp", ["*.tmp"]),
    ("notes/a.md", ["notes/*.md"]),
    ("notes/deep/a.md", ["notes/*.md"]),
    ("vendor/lib/a.md", ["vendor/lib/"]),
    ("a/vendor/lib/b.md", ["vendor/lib/"]),
    (".claude/worktrees/w/README.md", []),
    ("README.md", ["README.md"]),
]


@pytest.mark.parametrize("rel, patterns", EXCLUSION_CASES)
def test_is_excluded_rel_matches_scanner_semantics(rel, patterns, tmp_path):
    """Parity with the scanner's rule. The graph package may not import the
    scanner (it loads the vector stack); a test may."""
    from carta.scanner import scanner
    assert tuple(scanner._ALWAYS_EXCLUDED_DIRS) == ALWAYS_EXCLUDED_DIRS
    expected = scanner.is_excluded(tmp_path / Path(rel), {"excluded_paths": patterns}, tmp_path)
    assert is_excluded_rel(rel, patterns) is expected


def test_exclusion_cases_cover_both_outcomes(tmp_path):
    outcomes = {is_excluded_rel(rel, patterns) for rel, patterns in EXCLUSION_CASES}
    assert outcomes == {True, False}


@pytest.mark.parametrize("rel, expected", [
    ("README.md", True), ("docs/a/b.md", True), ("src/a.py", False), (".venv/x.md", False),
    ("pkg/node_modules/y/README.md", False), ("docs/a.MD", False),
])
def test_is_doc_path_defaults(rel, expected):
    assert is_doc_path(rel, DEFAULT_SETTINGS) is expected


def test_is_doc_path_honours_config():
    s = graph_settings({"excluded_paths": ["drafts/"], "graph": {"doc_globs": ["docs/**/*.md", "*.rst"]}})
    assert is_doc_path("docs/x.md", s) and is_doc_path("index.rst", s)
    assert not is_doc_path("README.md", s)
    assert not is_doc_path("docs/drafts/x.md", s)


@pytest.mark.parametrize("rel, expected", [
    ("src/a.py", True), (".carta/config.yaml", False), (".carta/graph/edges.db", False),
    (".git/HEAD", False), ("sub/inner/", False), ("x/.carta/y", True), ("", False),
])
def test_in_path_set(rel, expected):
    assert in_path_set(rel) is expected


PATHS = ["README.md", "docs/design/parser.md", "src/parser/frame.py", "src/parser/checksum.py",
         "src/parser/tests/test_frame.py", "src/store/frame.py", "src/xparser/frame.py", "Makefile"]


def test_path_index_dirs_and_top_level():
    idx = PathIndex(PATHS)
    assert idx.has_file("src/parser/frame.py") and not idx.has_file("src/parser")
    assert idx.has_dir("src/parser") and idx.has_dir("src/parser/") and idx.has_dir("src")
    assert not idx.has_dir("src/parser/frame.py") and not idx.has_dir("nope")
    assert idx.top_level_dirs == frozenset({"docs", "src"})
    assert len(idx) == len(PATHS) and "Makefile" in idx
    assert idx.paths() == sorted(PATHS)
    assert idx.files == frozenset(PATHS)


def test_by_basename_sorted_and_complete():
    idx = PathIndex(PATHS)
    assert idx.by_basename("frame.py") == (
        "src/parser/frame.py", "src/store/frame.py", "src/xparser/frame.py")
    assert idx.by_basename("checksum.py") == ("src/parser/checksum.py",)
    assert idx.by_basename("nope.py") == ()


def test_suffix_matches_respects_segment_boundary():
    idx = PathIndex(PATHS)
    assert idx.suffix_matches("parser/frame.py") == ("src/parser/frame.py",)
    assert idx.suffix_matches("tests/test_frame.py") == ("src/parser/tests/test_frame.py",)
    assert idx.suffix_matches("parser/tests") == ("src/parser/tests/",)
    assert idx.suffix_matches("parser/tests/") == ("src/parser/tests/",)
    assert idx.suffix_matches("frame.py") == ()            # one segment: not a suffix match
    assert idx.suffix_matches("src/parser/frame.py") == ()  # the full path is not a suffix
    assert idx.suffix_matches("nope/frame.py") == ()


def test_glob_matches_counts():
    idx = PathIndex(PATHS)
    assert idx.glob_matches("src/parser/*.py") == 2
    assert idx.glob_matches("src/**/*.py") == 5
    assert idx.glob_matches("nope/*.py") == 0


def test_fingerprint_independent_of_input_order():
    a, b = PathIndex(PATHS), PathIndex(reversed(PATHS))
    assert a.fingerprint() == b.fingerprint()
    assert PathIndex(PATHS[:-1]).fingerprint() != a.fingerprint()
    assert PathIndex(PATHS + PATHS).fingerprint() == a.fingerprint()


def test_add_and_remove_equal_a_fresh_index():
    idx = PathIndex(PATHS)
    idx.glob_matches("src/**/*.py")                       # prime the cache
    idx.remove("src/store/frame.py")
    idx.remove("src/parser/tests/test_frame.py")
    idx.add("lib/new/thing.py")
    idx.add("lib/new/thing.py")                           # idempotent
    idx.remove("never/there.py")                          # no error
    want = [p for p in PATHS if p not in ("src/store/frame.py", "src/parser/tests/test_frame.py")]
    want.append("lib/new/thing.py")
    fresh = PathIndex(want)
    assert idx.fingerprint() == fresh.fingerprint()
    assert idx.top_level_dirs == fresh.top_level_dirs == frozenset({"docs", "src", "lib"})
    assert not idx.has_dir("src/store") and not idx.has_dir("src/parser/tests")
    assert idx.has_dir("lib/new")
    assert idx.by_basename("frame.py") == fresh.by_basename("frame.py")
    assert idx.suffix_matches("parser/tests") == ()
    assert idx.suffix_matches("new/thing.py") == ("lib/new/thing.py",)
    assert idx.glob_matches("src/**/*.py") == fresh.glob_matches("src/**/*.py") == 3
