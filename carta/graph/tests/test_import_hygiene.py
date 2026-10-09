"""The graph arm must never pull in the vector stack, even transitively."""
from __future__ import annotations

import json
import subprocess
import sys

from carta.graph.model import (
    DST_TYPES, KINDS, MENTION_EXTRACTOR_VERSION, SCHEMA_VERSION, Candidate, Edge, canonical_meta,
)
from carta.graph.tests.conftest import requires_git

FORBIDDEN = ("qdrant_client", "fastembed", "torch", "requests", "carta.embed", "carta.search",
             "carta.scanner")

_IMPORT_ALL = r"""
import importlib, json, pkgutil, sys
import carta.graph
names = ["carta.graph"]
for info in pkgutil.walk_packages(carta.graph.__path__, "carta.graph."):
    if ".tests" in info.name:
        continue
    names.append(info.name)
for name in sorted(names):
    importlib.import_module(name)
"""

_REPORT = r"""
forbidden = %r
bad = sorted(m for m in sys.modules
             if any(m == f or m.startswith(f + ".") for f in forbidden))
print(json.dumps({"imported": sorted(names), "bad": bad}))
""" % (FORBIDDEN,)

_BUILD = r"""
import tempfile
from pathlib import Path
from carta.graph.build import build_graph
from carta.graph.store import EdgeStore, store_path
root = Path(tempfile.mkdtemp())
(root / "docs").mkdir()
(root / "src").mkdir()
(root / "src" / "frame.py").write_text("x = 1\n")
(root / "docs" / "parser.md").write_text("See `src/frame.py` and `parse_frame()`.\n")
report = build_graph(root, {"qdrant_url": "http://127.0.0.1:9", "embed": {"ollama_url": "http://127.0.0.1:9"}})
assert report["edges"] == 2, report
with EdgeStore(store_path(root)) as store:
    assert [e.dst for e in store.iter_edges(kinds=("mention",))] == ["src/frame.py"]
"""


def _run(code: str) -> dict:
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_importing_graph_package_pulls_no_vector_stack():
    out = _run(_IMPORT_ALL + _REPORT)
    assert "carta.graph.model" in out["imported"]
    assert out["bad"] == []


def test_running_a_build_pulls_no_vector_stack():
    out = _run(_IMPORT_ALL + _BUILD + _REPORT)
    assert {"carta.graph.build", "carta.graph.store", "carta.graph.mentions"} <= set(out["imported"])
    assert out["bad"] == []


def test_edge_is_ordered_and_hashable():
    a = Edge("mention", "docs/a.md", "src/a.py", "file", 3)
    b = Edge("mention", "docs/a.md", "src/b.py", "file", 1)
    assert sorted([b, a]) == [a, b]
    assert len({a, Edge("mention", "docs/a.md", "src/a.py", "file", 3)}) == 1
    assert a.key == "" and a.src_type == "doc" and a.extractor == "mention"
    assert a.extractor_version == MENTION_EXTRACTOR_VERSION


def test_canonical_meta_is_sorted_and_compact():
    assert canonical_meta({}) == "{}"
    assert canonical_meta({"b": 1, "a": "é"}) == '{"a":"é","b":1}'


def test_constants():
    assert SCHEMA_VERSION == 1
    assert {"mention", "ident", "pattern", "cochange", "symbol", "related"} <= set(KINDS)
    assert {"file", "doc", "dir", "glob", "missing", "token", "pattern"} <= set(DST_TYPES)
    assert Candidate("x", "code", 1, "x").single is True


@requires_git
def test_git_repo_fixture_is_reproducible(make_git_repo):
    shas = []
    for name in ("one", "two"):
        repo = make_git_repo(name)
        repo.write("a.md", "hello\n")
        repo.commit("first", 0)
        repo.write("b.md", "world\n")
        shas.append(repo.commit("second", 1))
    assert shas[0] == shas[1]
