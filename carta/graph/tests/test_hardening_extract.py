"""Untrusted doc text, file names and config values: bounded, and never an exception.

Each "bounded" test runs its input in a child process with a kill timer. On
the code these tests were written against, every such input either ran for
minutes or never finished; fixed, each takes milliseconds. The timer is a
hang guard with a margin of several thousand, not a performance budget.
"""
from __future__ import annotations

import os
import random
import re
import subprocess
import sys
import warnings
from pathlib import Path

import pytest

from carta.graph import limits
from carta.graph.markdown import collapse_evidence, scan_text
from carta.graph.mentions import MAX_CANDIDATE_LENGTH, extract_mentions, normalise_candidate
from carta.graph.paths import PathIndex, glob_matcher, glob_problem, glob_to_regex
from carta.graph.settings import GraphConfigError, graph_settings

REPO_ROOT = Path(__file__).resolve().parents[3]
GUARD_SECONDS = float(os.environ.get("CARTA_HANG_GUARD_SECONDS", "120"))


def run_bounded(code: str) -> str:
    """Run ``code`` in a child interpreter; fail if it has not finished by the guard."""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    try:
        proc = subprocess.run([sys.executable, "-W", "error", "-c", code], cwd=str(REPO_ROOT),
                              env=env, capture_output=True, text=True, timeout=GUARD_SECONDS)
    except subprocess.TimeoutExpired:
        pytest.fail(f"did not finish within {GUARD_SECONDS:g} s: unbounded work on hostile input")
    assert proc.returncode == 0, proc.stderr[-2000:]
    return proc.stdout


# -- globs ------------------------------------------------------------------------

def _old_regex(glob: str) -> "re.Pattern[str]":
    """The translation ``glob_to_regex`` used to be: the reference for what a glob means."""
    out = []
    i, n = 0, len(glob)
    while i < n:
        c = glob[i]
        if c == "*":
            if glob.startswith("**/", i):
                out.append("(?:.*/)?")
                i += 3
            elif glob.startswith("**", i):
                out.append(".*")
                i += 2
            else:
                out.append("[^/]*")
                i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        elif c == "[":
            j = glob.find("]", i + 2)
            if j == -1:
                out.append(re.escape(c))
                i += 1
            else:
                body = glob[i + 1:j]
                negate = body[:1] in ("!", "^")
                if negate:
                    body = body[1:]
                body = body.replace("\\", "\\\\").replace("[", "\\[").replace("^", "\\^")
                out.append("[" + ("^/" if negate else "") + body + "]")
                i = j + 1
        else:
            out.append(re.escape(c))
            i += 1
    return re.compile("".join(out) + r"\Z")


def test_glob_matcher_means_what_the_regex_translation_meant():
    rng = random.Random(20261009)
    glob_alphabet = "**?[]!^-/ab.\\c"
    path_alphabet = "/ab.c-^]![\n\\"
    compared = 0
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        for _ in range(20000):
            glob = "".join(rng.choice(glob_alphabet) for _ in range(rng.randint(0, 9)))
            if "[!-" in glob or "[^-" in glob:
                continue        # the old translation read "[!-x]" as the range "/-x": a bug
            try:
                reference = _old_regex(glob)
            except re.error:
                continue        # the old translation raised; see the next test
            for _ in range(4):
                if rng.random() < 0.5:
                    path = "".join(rng.choice(path_alphabet) for _ in range(rng.randint(0, 8)))
                else:           # shaped like the glob, so that matches are common
                    path = "".join(rng.choice("ab/") if ch in "*?" else ch
                                   for ch in glob if ch not in "[]!^")
                compared += 1
                assert glob_matcher(glob).match(path) is bool(reference.match(path)), (glob, path)
    assert compared > 50000


@pytest.mark.parametrize("glob", [
    "src/[z-a]/x.py", "src/[z-a]*.py", "src/[a--b].py", "src/[a-\\].py", "[!z-a]", "[b-a]",
])
def test_reversed_class_range_matches_nothing_and_never_raises(glob):
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        matcher = glob_to_regex(glob)
        for path in ("src/a/x.py", "src/a.py", "src/-.py", "a", "z", glob):
            assert not matcher.match(path)
    assert glob_problem(glob) is not None


@pytest.mark.parametrize("glob, path, expected", [
    ("a[!]b", "axb", True), ("a[!]b", "a/b", False), ("a[^]b", "a-b", True),
    ("a[]]b", "a]b", True), ("a[]b", "a[]b", True), ("[!-z]", "a", True),
    ("[!-z]", "-", False), ("[!-z]", "z", False), ("[--a]", "5", True), ("[a-]", "-", True),
    ("**/", "x/", True), ("**/", "x", False), ("a**/b", "ab", True), ("a**/b", "ax/b", True),
])
def test_glob_edge_cases(glob, path, expected):
    assert glob_matcher(glob).match(path) is expected


def test_glob_over_the_length_bound_matches_nothing():
    glob = "a/" * (limits.MAX_GLOB_LENGTH // 2) + "*.py"
    assert len(glob) > limits.MAX_GLOB_LENGTH
    assert not glob_matcher(glob).match("a/" * (limits.MAX_GLOB_LENGTH // 2) + "x.py")
    assert "longer" in glob_problem(glob)
    assert glob_problem("src/**/*.py") is None


def test_stacked_wildcards_are_bounded():
    out = run_bounded("""
from carta.graph.paths import glob_matcher, PathIndex
deep = "a/" * 40 + "y"
assert not glob_matcher("**/" * 99 + "x").match(deep)
assert glob_matcher("**/" * 99 + "y").match(deep)
assert not glob_matcher("a/" + "*a" * 60 + "x").match("a/" + "a" * 600)
assert glob_matcher("a/" + "*a" * 60).match("a/" + "a" * 600)
assert not glob_matcher("a/" + "**x" * 40 + "**y").match("a/" + "x" * 800)
index = PathIndex("docs/" + "a/" * 30 + "f%d.md" % i for i in range(200))
assert index.glob_matches("docs/" + "**/" * 9 + "q.md") == 0
assert index.glob_matches("docs/" + "**/" * 9 + "f7.md") == 1
print("ok")
""")
    assert out.strip() == "ok"


def test_a_doc_cannot_stall_extraction_with_a_glob():
    out = run_bounded("""
from carta.graph.mentions import extract_mentions
paths = ["docs/" + "a/" * 40 + "y.md", "src/app.py"]
edges = extract_mentions("README.md", "`docs/" + "**/" * 8 + "q.md` and `src/[z-a]*.py`\\n", paths)
assert edges == [], edges
edges = extract_mentions("README.md", "`docs/" + "**/" * 8 + "y.md`\\n", paths)
assert [(e.dst_type, e.meta) for e in edges] == [("glob", '{"ctx":"code","matches":1}')], edges
print("ok")
""")
    assert out.strip() == "ok"


def test_distinct_globs_per_doc_are_capped():
    paths = [f"src/m{i}.py" for i in range(limits.MAX_GLOBS_PER_DOC + 40)]
    text = "".join(f"`src/m{i}.*`\n" for i in range(limits.MAX_GLOBS_PER_DOC + 40))
    edges = extract_mentions("README.md", text, paths)
    assert len(edges) == limits.MAX_GLOBS_PER_DOC
    assert edges == extract_mentions("README.md", text, paths)        # and deterministic
    # the same glob named again is not a new one
    again = extract_mentions("README.md", "`src/m1.*`\n" * 600, paths)
    assert len(again) == 600


# -- the path index -----------------------------------------------------------------

def test_a_deep_path_is_indexed_without_suffixes():
    shallow = "/".join(f"d{i}" for i in range(limits.MAX_SUFFIX_DEPTH - 1)) + "/f.py"
    index = PathIndex([shallow, "x/" * limits.MAX_SUFFIX_DEPTH + "f.py"])
    assert shallow.count("/") + 1 == limits.MAX_SUFFIX_DEPTH
    assert index.suffix_matches("/".join(shallow.split("/")[3:])) == (shallow,)
    assert index.suffix_matches("x/x/f.py") == ()            # too deep to be found by suffix
    assert index.has_file("x/" * limits.MAX_SUFFIX_DEPTH + "f.py")     # but still a path
    assert index.by_basename("f.py") == tuple(sorted(index.paths()))
    index.remove("x/" * limits.MAX_SUFFIX_DEPTH + "f.py")
    assert index.paths() == [shallow] and not index.has_dir("x")


def test_very_deep_paths_are_bounded():
    out = run_bounded("""
from carta.graph.paths import PathIndex
from carta.graph.eval.incremental import MentionState
from carta.graph.settings import DEFAULT_SETTINGS
deep = ["a/" * 3000 + "f", "b/" * 3000 + "g"]
index = PathIndex(deep)
assert len(index) == 2
state = MentionState(DEFAULT_SETTINGS)
state.seed_paths(deep[:1])
state.paths_changed(added=deep[1:], deleted=deep[:1])
print("ok")
""")
    assert out.strip() == "ok"


# -- stage 1 ------------------------------------------------------------------------

def test_hostile_lines_are_bounded():
    out = run_bounded("""
from carta.graph.markdown import scan_text
from carta.graph.limits import MAX_LINE_LENGTH
for unit in ("](x", "](<", "`a` ](x) ", '](x "', "](x (", "`", "`` ` "):
    for size in (MAX_LINE_LENGTH, 300_000):
        scan_text(unit * (size // len(unit)))                     # one line
    scan_text("\\n".join([unit * (MAX_LINE_LENGTH // len(unit))] * 60))
print("ok")
""")
    assert out.strip() == "ok"


def test_a_line_over_the_bound_is_not_scanned_but_still_counts():
    long_line = "see `src/app.py` " + "x" * limits.MAX_LINE_LENGTH
    found = scan_text("`a/first.py`\n" + long_line + "\n`a/third.py`\n")
    assert [(c.raw, c.line) for c in found] == [("a/first.py", 1), ("a/third.py", 3)]
    at_bound = "`src/app.py` " + "x" * (limits.MAX_LINE_LENGTH - 13)
    assert len(at_bound) == limits.MAX_LINE_LENGTH
    assert [c.raw for c in scan_text(at_bound)] == ["src/app.py"]


def test_a_text_over_the_doc_bound_yields_nothing(monkeypatch):
    monkeypatch.setattr("carta.graph.markdown.MAX_DOC_BYTES", 64)
    assert scan_text("`src/app.py`\n" * 4) != []
    assert scan_text("`src/app.py`\n" * 5) == []


def test_candidates_per_doc_are_capped(monkeypatch):
    monkeypatch.setattr("carta.graph.markdown.MAX_CANDIDATES_PER_DOC", 7)
    found = scan_text("".join(f"`a/f{i}.py` [l](b/g{i}.md)\n" for i in range(50)))
    assert len(found) == 7
    assert [c.line for c in found] == [1, 1, 2, 2, 3, 3, 4]


def test_a_link_target_over_the_bound_is_not_a_link():
    ok = "d/" + "x" * (limits.MAX_LINK_TARGET_LENGTH - 5) + ".md"
    assert len(ok) == limits.MAX_LINK_TARGET_LENGTH
    assert [c.raw for c in scan_text(f"[a]({ok}) [b]({ok}x)")] == [ok]


def test_evidence_holds_no_terminal_control_characters():
    line = "`src/app.py` \x1b]0;title\x07\x1b[2J\x1b[31mFAKE\x9b0m ‮evil⁦ end\x7f"
    assert collapse_evidence(line) == "`src/app.py` ]0;title[2J[31mFAKE0m evil end"
    assert scan_text(line)[0].evidence == collapse_evidence(line)


# -- stage 2 ------------------------------------------------------------------------

def test_an_oversized_candidate_is_rejected_whatever_it_wraps():
    for left, right in (("(", ")"), ("<", ">"), ("`", "`")):
        raw = left * 2000 + "a/b.py" + right * 2000
        assert normalise_candidate(raw, "code") is None
        assert extract_mentions("README.md", "`" + raw.replace("`", "'") + "`\n", ["a/b.py"]) == []
    assert normalise_candidate("a/" * 600 + "#frag", "link") is None
    assert MAX_CANDIDATE_LENGTH < limits.MAX_RAW_CANDIDATE_LENGTH


def test_wrapper_stripping_is_bounded_and_still_strips():
    assert normalise_candidate('(<"a/b.py">)', "code") == ["a/b.py"]
    layers = limits.MAX_WRAPPER_LAYERS
    assert normalise_candidate("(" * layers + "a/b.py" + ")" * layers, "code") == ["a/b.py"]
    assert normalise_candidate("(" * (layers + 2) + "a/b.py" + ")" * (layers + 2), "code") != \
        ["a/b.py"]


def test_oversized_candidates_are_bounded():
    out = run_bounded("""
from carta.graph.mentions import extract_mentions, normalise_candidate
for left, right in (("(", ")"), ("<", ">")):
    assert normalise_candidate(left * 400_000 + "a/b.py" + right * 400_000, "code") is None
text = "---\\nsee: " + "<" * 300_000 + "a/b.py" + ">" * 300_000 + "\\n---\\n"
assert extract_mentions("README.md", text, ["a/b.py"]) == []
print("ok")
""")
    assert out.strip() == "ok"


# -- config -------------------------------------------------------------------------

@pytest.mark.parametrize("cfg, needle", [
    ({"graph": {"identifiers": {"min_length": float("inf")}}}, "min_length"),
    ({"graph": {"identifiers": {"min_length": float("nan")}}}, "min_length"),
    ({"graph": {"scan": {"history_limit": float("nan")}}}, "history_limit"),
    ({"graph": {"replay": {"hub_share": float("nan")}}}, "hub_share"),
    ({"graph": {"replay": {"max_files": float("-inf")}}}, "max_files"),
    ({"graph": {"impact": {"weights": {"mention": float("inf")}}}}, "weights"),
    ({"graph": {"doc_globs": ["docs/[z-a]*.md"]}}, "doc_globs"),
    ({"graph": {"doc_globs": ["**/" * 400 + "x.md"]}}, "doc_globs"),
    ({"graph": {"doc_globs": [f"d{i}/*.md" for i in range(limits.MAX_CONFIG_GLOBS + 1)]}},
     "doc_globs"),
    ({"graph": {"scan": {"ignore_docs": ["[b-a]"]}}}, "ignore_docs"),
    ({"excluded_paths": ["*" * (limits.MAX_GLOB_LENGTH + 1)]}, "excluded_paths"),
    ({"excluded_paths": [f"x{i}/" for i in range(limits.MAX_CONFIG_GLOBS + 1)]},
     "excluded_paths"),
    ({"graph": {"patterns": [{"name": f"p{i}", "regex": "x"}
                             for i in range(limits.MAX_PATTERNS + 1)]}}, "patterns"),
    ({"graph": {"patterns": [{"name": "p", "regex": "a" * (limits.MAX_PATTERN_REGEX_LENGTH + 1)}]}},
     "regex"),
])
def test_out_of_bounds_config_is_a_config_error(cfg, needle):
    with pytest.raises(GraphConfigError) as raised:
        graph_settings(cfg)
    assert needle in str(raised.value)


def test_config_at_the_bounds_is_accepted():
    settings = graph_settings({
        "graph": {"doc_globs": [f"d{i}/*.md" for i in range(limits.MAX_CONFIG_GLOBS)],
                  "patterns": [{"name": "p", "regex": "a" * limits.MAX_PATTERN_REGEX_LENGTH}]},
        "excluded_paths": ["[z-a]/", "*.tmp"],      # fnmatch patterns: any class is allowed
    })
    assert len(settings.doc_globs) == limits.MAX_CONFIG_GLOBS
