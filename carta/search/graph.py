"""Graph-walk utilities for hop-based related: document traversal.

Provides BFS over the ``related:`` frontmatter graph so that ``carta search``
can surface contextually adjacent documents within N hops of the initial semantic
search results.
"""

import re
from pathlib import Path
from typing import Optional

from carta.scanner.scanner import parse_frontmatter


def _slugify(s: str) -> str:
    """Lowercase kebab-case slug: '_'/' ' -> '-', drop other punctuation."""
    s = s.replace("_", "-").replace(" ", "-")
    s = re.sub(r"[^A-Za-z0-9-]", "", s)
    s = re.sub(r"-+", "-", s).strip("-")
    return s.lower()


def _bare_stem(entry: str) -> str:
    """Slug of an entry's filename stem, tolerating a .embed-meta.yaml suffix."""
    name = entry
    if name.endswith(".embed-meta.yaml"):
        name = name[: -len(".embed-meta.yaml")]
    return _slugify(Path(name).stem)


def _iter_md_paths(repo_root: Path, docs_root: Path) -> list[Path]:
    """All markdown docs: root-level *.md plus everything under docs_root."""
    paths = [p for p in repo_root.glob("*.md")]
    if docs_root.exists():
        paths += [p for p in docs_root.rglob("*.md") if ".git" not in p.parts]
    return paths


def build_doc_index(repo_root: Path, docs_root: Optional[Path] = None) -> dict[str, str]:
    """Map each doc's frontmatter ``id:`` slug and kebab-cased filename stem to its
    canonical repo-root POSIX path. Only unambiguous keys are kept — a slug claimed by
    two docs is omitted so resolution never silently picks the wrong target.
    """
    if docs_root is None:
        docs_root = repo_root / "docs"
    candidates: dict[str, set[str]] = {}

    def add(key: str, canon: str) -> None:
        if key:
            candidates.setdefault(key, set()).add(canon)

    for p in _iter_md_paths(repo_root, docs_root):
        canon = p.relative_to(repo_root).as_posix()
        add(_slugify(p.stem), canon)
        fm = parse_frontmatter(p)
        if fm and fm.get("id"):
            add(_slugify(str(fm["id"])), canon)
    return {k: next(iter(v)) for k, v in candidates.items() if len(v) == 1}


def resolve_entry(entry: object, doc_index: dict[str, str], repo_root: Path) -> Optional[str]:
    """Resolve a single ``related:`` entry to a canonical repo-root POSIX path, or None.

    Tiers: (1) exact repo-root path → (2) docs/-prefixed path → (3) bare id/stem lookup
    (also handles .embed-meta.yaml drift) → else None.
    """
    if not isinstance(entry, str) or not entry.strip():
        return None
    e = entry.strip()
    if ".." in Path(e).parts:
        return None
    if (repo_root / e).exists():
        return Path(e).as_posix()
    if (repo_root / "docs" / e).exists():
        return (Path("docs") / e).as_posix()
    key = _bare_stem(e)
    return doc_index.get(key)


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
