"""Graph-walk utilities for hop-based related: document traversal.

Provides BFS over the ``related:`` frontmatter graph so that ``carta search``
can surface contextually adjacent documents within N hops of the initial semantic
search results.
"""

from pathlib import Path
from typing import Optional

from carta.scanner.scanner import parse_frontmatter


def build_related_graph(repo_root: Path, docs_root: Optional[Path] = None) -> dict[str, list[str]]:
    """Parse all markdown docs under docs_root and return the related: adjacency list.

    Args:
        repo_root: Repository root path.
        docs_root: Subtree to scan.  Defaults to ``repo_root/docs``.

    Returns:
        Dict mapping ``str(relative_path)`` → list of related paths (strings,
        as they appear in frontmatter — may or may not exist on disk).
    """
    if docs_root is None:
        docs_root = repo_root / "docs"
    graph: dict[str, list[str]] = {}
    if not docs_root.exists():
        return graph
    for md_path in docs_root.rglob("*.md"):
        if ".git" in md_path.parts:
            continue
        rel = str(md_path.relative_to(repo_root))
        fm = parse_frontmatter(md_path)
        graph[rel] = list(fm.get("related") or []) if fm else []
    return graph


def walk_hops(
    seeds: list[str],
    graph: dict[str, list[str]],
    hops: int,
) -> list[dict]:
    """BFS expansion of seed documents through the related: graph.

    Starting from each document in *seeds*, expand outward up to *hops* steps
    through the ``related:`` adjacency list.  Documents already present in
    *seeds* are excluded from the results.

    Args:
        seeds: Relative paths of the initial (semantic-search) result documents.
        graph: Adjacency list from :func:`build_related_graph`.
        hops: Maximum number of traversal steps (0 = no expansion).

    Returns:
        List of dicts ordered by ascending hop distance then path::

            [{"doc": "docs/CAN/TOPOLOGY.md", "hop": 1, "via": "docs/CAN/MESSAGE_FLOW.md"}]
    """
    if hops <= 0:
        return []

    seed_set = set(seeds)
    visited: set[str] = set(seeds)
    frontier: list[tuple[str, int, str]] = []

    for seed in seeds:
        for neighbour in graph.get(seed, []):
            if neighbour not in visited:
                frontier.append((neighbour, 1, seed))
                visited.add(neighbour)

    results: list[dict] = []
    while frontier:
        doc, hop, via = frontier.pop(0)
        if doc not in seed_set:
            results.append({"doc": doc, "hop": hop, "via": via})
        if hop < hops:
            for neighbour in graph.get(doc, []):
                if neighbour not in visited:
                    frontier.append((neighbour, hop + 1, doc))
                    visited.add(neighbour)

    results.sort(key=lambda x: (x["hop"], x["doc"]))
    return results
