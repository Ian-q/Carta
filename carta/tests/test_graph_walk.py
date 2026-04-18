"""Tests for carta.search.graph — graph-walk hop expansion."""

from pathlib import Path

import pytest

from carta.search.graph import build_related_graph, walk_hops


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_doc(path: Path, related: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["---", "related:"]
    for r in related:
        lines.append(f"  - {r}")
    lines += ["last_reviewed: 2026-03-18", "---", "# Doc\n"]
    path.write_text("\n".join(lines))


# ---------------------------------------------------------------------------
# build_related_graph
# ---------------------------------------------------------------------------

def test_build_related_graph_basic(tmp_path):
    docs = tmp_path / "docs" / "CAN"
    docs.mkdir(parents=True)
    _write_doc(docs / "MESSAGE_FLOW.md", ["docs/CAN/TOPOLOGY.md", "docs/CAN/SAFETY.md"])
    _write_doc(docs / "TOPOLOGY.md", ["docs/CAN/MESSAGE_FLOW.md"])
    _write_doc(docs / "SAFETY.md", [])

    graph = build_related_graph(tmp_path)

    assert "docs/CAN/MESSAGE_FLOW.md" in graph
    assert "docs/CAN/TOPOLOGY.md" in graph["docs/CAN/MESSAGE_FLOW.md"]
    assert "docs/CAN/SAFETY.md" in graph["docs/CAN/MESSAGE_FLOW.md"]
    assert "docs/CAN/MESSAGE_FLOW.md" in graph["docs/CAN/TOPOLOGY.md"]
    assert graph["docs/CAN/SAFETY.md"] == []


def test_build_related_graph_no_frontmatter(tmp_path):
    """Docs without frontmatter should appear in graph with empty adjacency."""
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "BARE.md").write_text("# Bare doc — no frontmatter\n")

    graph = build_related_graph(tmp_path)
    assert "docs/BARE.md" in graph
    assert graph["docs/BARE.md"] == []


def test_build_related_graph_missing_docs_root(tmp_path):
    """Returns empty graph when docs_root doesn't exist."""
    graph = build_related_graph(tmp_path, tmp_path / "nonexistent")
    assert graph == {}


# ---------------------------------------------------------------------------
# walk_hops
# ---------------------------------------------------------------------------

def test_walk_hops_zero(tmp_path):
    """hops=0 should always return an empty list."""
    graph = {"docs/A.md": ["docs/B.md"], "docs/B.md": []}
    assert walk_hops(["docs/A.md"], graph, hops=0) == []


def test_walk_hops_one(tmp_path):
    """hops=1 should return direct neighbours not already in seeds."""
    graph = {
        "docs/A.md": ["docs/B.md", "docs/C.md"],
        "docs/B.md": ["docs/D.md"],
        "docs/C.md": [],
        "docs/D.md": [],
    }
    results = walk_hops(["docs/A.md"], graph, hops=1)
    docs = [r["doc"] for r in results]
    assert "docs/B.md" in docs
    assert "docs/C.md" in docs
    assert "docs/D.md" not in docs


def test_walk_hops_two(tmp_path):
    """hops=2 should reach docs/D.md via A → B → D."""
    graph = {
        "docs/A.md": ["docs/B.md"],
        "docs/B.md": ["docs/D.md"],
        "docs/D.md": [],
    }
    results = walk_hops(["docs/A.md"], graph, hops=2)
    docs = [r["doc"] for r in results]
    assert "docs/B.md" in docs
    assert "docs/D.md" in docs

    d_entry = next(r for r in results if r["doc"] == "docs/D.md")
    assert d_entry["hop"] == 2
    assert d_entry["via"] == "docs/B.md"


def test_walk_hops_no_duplicates(tmp_path):
    """A doc reachable via multiple paths should appear only once."""
    graph = {
        "docs/A.md": ["docs/B.md", "docs/C.md"],
        "docs/B.md": ["docs/D.md"],
        "docs/C.md": ["docs/D.md"],
        "docs/D.md": [],
    }
    results = walk_hops(["docs/A.md"], graph, hops=2)
    docs = [r["doc"] for r in results]
    assert docs.count("docs/D.md") == 1


def test_walk_hops_seeds_excluded_from_results(tmp_path):
    """Seed documents should not appear in the hop results."""
    graph = {
        "docs/A.md": ["docs/B.md"],
        "docs/B.md": ["docs/A.md"],
    }
    results = walk_hops(["docs/A.md"], graph, hops=1)
    docs = [r["doc"] for r in results]
    assert "docs/A.md" not in docs
    assert "docs/B.md" in docs


def test_walk_hops_multiple_seeds(tmp_path):
    """Multiple seeds should expand their neighbourhoods correctly."""
    graph = {
        "docs/A.md": ["docs/C.md"],
        "docs/B.md": ["docs/D.md"],
        "docs/C.md": [],
        "docs/D.md": [],
    }
    results = walk_hops(["docs/A.md", "docs/B.md"], graph, hops=1)
    docs = [r["doc"] for r in results]
    assert "docs/C.md" in docs
    assert "docs/D.md" in docs


def test_walk_hops_cycle_safe(tmp_path):
    """Cyclic related: graphs should not cause infinite loops."""
    graph = {
        "docs/A.md": ["docs/B.md"],
        "docs/B.md": ["docs/C.md"],
        "docs/C.md": ["docs/A.md"],
    }
    results = walk_hops(["docs/A.md"], graph, hops=5)
    docs = [r["doc"] for r in results]
    assert "docs/B.md" in docs
    assert "docs/C.md" in docs
    assert docs.count("docs/A.md") == 0


def test_walk_hops_result_structure(tmp_path):
    """Each result dict must have doc, hop, and via keys."""
    graph = {"docs/A.md": ["docs/B.md"], "docs/B.md": []}
    results = walk_hops(["docs/A.md"], graph, hops=1)
    assert len(results) == 1
    r = results[0]
    assert set(r.keys()) >= {"doc", "hop", "via"}
    assert r["hop"] == 1
    assert r["via"] == "docs/A.md"


def test_walk_hops_end_to_end(tmp_path):
    """Integration: build graph from real files then walk hops."""
    docs = tmp_path / "docs" / "CAN"
    docs.mkdir(parents=True)
    _write_doc(docs / "MESSAGE_FLOW.md", ["docs/CAN/TOPOLOGY.md"])
    _write_doc(docs / "TOPOLOGY.md", ["docs/CAN/SAFETY.md"])
    _write_doc(docs / "SAFETY.md", [])

    graph = build_related_graph(tmp_path)
    results = walk_hops(["docs/CAN/MESSAGE_FLOW.md"], graph, hops=2)
    docs_reached = [r["doc"] for r in results]

    assert "docs/CAN/TOPOLOGY.md" in docs_reached
    assert "docs/CAN/SAFETY.md" in docs_reached
