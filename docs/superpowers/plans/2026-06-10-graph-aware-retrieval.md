---
id: 2026-06-10-graph-aware-retrieval
title: "Graph-aware Retrieval Implementation Plan"
status: shipped
related:
  - 2026-06-10-graph-aware-retrieval-design
date: 2026-06-10
---

# Graph-aware Retrieval Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add undirected 1-hop `related:`-graph expansion to `run_search` that promotes graph-adjacent deep documents into the rerank candidate pool, plus a search-time entry resolver (id/path normalization) and an audit check for non-canonical entries.

**Architecture:** A search-time resolver maps any `related:` entry style (exact path, missing-`docs/`-prefix, bare id/slug, extension drift) to a canonical repo-root POSIX path. `build_related_graph` uses it to build a cached, undirected adjacency over all `.md` docs (forward edges ∪ backlinks). `run_search`, when `search.graph.enabled`, deep-fetches `candidate_depth` candidates, seeds a 1-hop walk from the top `seed_count` hits, and promotes adjacent candidates to just-after the seeds so the reranker (phase 1) can float the relevant ones into the top-5. A new scanner check reports non-canonical/broken entries.

**Tech Stack:** Python 3, pytest, existing `carta/search/graph.py` (extended), `carta/embed/pipeline.py`, `carta/scanner/scanner.py`, `carta/config.py`.

**Spec:** `docs/superpowers/specs/2026-06-10-graph-aware-retrieval-design.md`

---

## File Structure

- **`carta/search/graph.py`** (extend) — resolver (`_slugify`, `_bare_stem`, `build_doc_index`, `resolve_entry`), undirected cached graph (`build_related_graph` rewrite, `_GRAPH_CACHE`, `_docs_mtime_sig`), expansion helpers (`hit_path`, `expand_seeds`, `promote_graph_neighbors`). `walk_hops` stays as-is.
- **`carta/embed/pipeline.py`** (modify) — `_apply_graph_expansion(results, cfg, repo_root)` helper; wire into `run_search` (bump `fetch_limit`, call helper after fusion / before rerank).
- **`carta/config.py`** (modify) — `DEFAULTS["search"]["graph"]` block.
- **`carta/scanner/scanner.py`** (modify) — `check_noncanonical_related(doc_path, fm, doc_index, repo_root)` + wire into the scan loop.
- **Tests** — `carta/search/tests/test_graph.py` (new), `carta/embed/tests/test_graph_expansion.py` (new), `carta/scanner/tests/test_noncanonical_related.py` (new), `carta/tests/test_config.py` (append).
- **Docs** — `CHANGELOG.md`, `README.md`, version files (`carta/__init__.py`, `.claude-plugin/plugin.json`, `marketplace.json`).

---

## Task 1: Entry resolver + doc index

**Files:**
- Modify: `carta/search/graph.py` (add functions; keep existing `build_related_graph`/`walk_hops` for now)
- Test: `carta/search/tests/test_graph.py` (new)

- [ ] **Step 1: Write the failing tests**

```python
# carta/search/tests/test_graph.py
from pathlib import Path
from carta.search import graph as g


def _make_repo(tmp_path: Path) -> Path:
    (tmp_path / "docs" / "hardware" / "vcu").mkdir(parents=True)
    (tmp_path / "docs" / "CAN").mkdir(parents=True)
    # connector-map: empty related, id = connector-map
    (tmp_path / "docs" / "hardware" / "vcu" / "connector-map.md").write_text(
        "---\nid: connector-map\nrelated: []\n---\nbody\n")
    # power-architecture links to connector-map by BARE ID (non-canonical)
    (tmp_path / "docs" / "hardware" / "vcu" / "power-architecture.md").write_text(
        "---\nid: vcu-power-architecture\nrelated:\n  - connector-map\n---\nbody\n")
    # MESSAGE_FLOW links to SAFETY by canonical path
    (tmp_path / "docs" / "CAN" / "MESSAGE_FLOW.md").write_text(
        "---\nid: can-message-flow\nrelated:\n  - docs/CAN/SAFETY-MCU-MESSAGES.md\n---\nbody\n")
    (tmp_path / "docs" / "CAN" / "SAFETY-MCU-MESSAGES.md").write_text(
        "---\nid: can-safety-mcu-messages\nrelated: []\n---\nbody\n")
    # root file, referenced missing-prefix-style elsewhere
    (tmp_path / "CLAUDE.md").write_text("---\nid: claude\nrelated: []\n---\nroot\n")
    return tmp_path


def test_doc_index_maps_id_and_stem_to_canonical_path(tmp_path):
    repo = _make_repo(tmp_path)
    idx = g.build_doc_index(repo)
    # frontmatter id
    assert idx["connector-map"] == "docs/hardware/vcu/connector-map.md"
    assert idx["can-message-flow"] == "docs/CAN/MESSAGE_FLOW.md"
    # filename stem (kebabbed; MESSAGE_FLOW -> message-flow)
    assert idx["message-flow"] == "docs/CAN/MESSAGE_FLOW.md"
    # root file indexed
    assert idx["claude"] == "CLAUDE.md"


def test_resolve_entry_tier1_exact_path(tmp_path):
    repo = _make_repo(tmp_path)
    idx = g.build_doc_index(repo)
    assert g.resolve_entry("docs/CAN/SAFETY-MCU-MESSAGES.md", idx, repo) == "docs/CAN/SAFETY-MCU-MESSAGES.md"


def test_resolve_entry_tier2_missing_docs_prefix(tmp_path):
    repo = _make_repo(tmp_path)
    idx = g.build_doc_index(repo)
    # entry omits the docs/ prefix
    assert g.resolve_entry("CAN/SAFETY-MCU-MESSAGES.md", idx, repo) == "docs/CAN/SAFETY-MCU-MESSAGES.md"


def test_resolve_entry_tier3_bare_id(tmp_path):
    repo = _make_repo(tmp_path)
    idx = g.build_doc_index(repo)
    assert g.resolve_entry("connector-map", idx, repo) == "docs/hardware/vcu/connector-map.md"


def test_resolve_entry_unresolvable_returns_none(tmp_path):
    repo = _make_repo(tmp_path)
    idx = g.build_doc_index(repo)
    assert g.resolve_entry("does-not-exist-anywhere", idx, repo) is None
    assert g.resolve_entry("", idx, repo) is None
    assert g.resolve_entry(None, idx, repo) is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/ian/dev/doc-audit-cc && python -m pytest carta/search/tests/test_graph.py -v`
Expected: FAIL with `AttributeError: module 'carta.search.graph' has no attribute 'build_doc_index'`.

- [ ] **Step 3: Implement the resolver + index**

Add to `carta/search/graph.py` (after the imports; add `import re` at top):

```python
import re


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


def resolve_entry(entry, doc_index: dict[str, str], repo_root: Path) -> Optional[str]:
    """Resolve a single ``related:`` entry to a canonical repo-root POSIX path, or None.

    Tiers: (1) exact repo-root path → (2) docs/-prefixed path → (3) bare id/stem lookup
    (also handles .embed-meta.yaml drift) → else None.
    """
    if not isinstance(entry, str) or not entry.strip():
        return None
    e = entry.strip()
    if (repo_root / e).exists():
        return Path(e).as_posix()
    if (repo_root / "docs" / e).exists():
        return (Path("docs") / e).as_posix()
    key = _bare_stem(e)
    return doc_index.get(key)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/ian/dev/doc-audit-cc && python -m pytest carta/search/tests/test_graph.py -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
cd /Users/ian/dev/doc-audit-cc
git add carta/search/graph.py carta/search/tests/test_graph.py
git commit -m "feat(graph): related: entry resolver + doc index"
```

---

## Task 2: Undirected cached related graph

**Files:**
- Modify: `carta/search/graph.py` (rewrite `build_related_graph`; add cache; keep `walk_hops`)
- Test: `carta/search/tests/test_graph.py` (append)

- [ ] **Step 1: Write the failing tests**

Append to `carta/search/tests/test_graph.py`:

```python
def test_graph_is_undirected_backlink_reaches_connector_map(tmp_path):
    repo = _make_repo(tmp_path)
    g._GRAPH_CACHE.clear()
    adj = g.build_related_graph(repo)
    pa = "docs/hardware/vcu/power-architecture.md"
    cm = "docs/hardware/vcu/connector-map.md"
    # power-architecture -> connector-map (forward, via bare-id entry)
    assert cm in adj[pa]
    # connector-map's own related: is empty, but the edge is mirrored (undirected)
    assert pa in adj[cm]


def test_graph_resolves_canonical_path_edge(tmp_path):
    repo = _make_repo(tmp_path)
    g._GRAPH_CACHE.clear()
    adj = g.build_related_graph(repo)
    mf = "docs/CAN/MESSAGE_FLOW.md"
    safety = "docs/CAN/SAFETY-MCU-MESSAGES.md"
    assert safety in adj[mf]
    assert mf in adj[safety]   # mirrored


def test_graph_includes_root_files_as_nodes(tmp_path):
    repo = _make_repo(tmp_path)
    g._GRAPH_CACHE.clear()
    adj = g.build_related_graph(repo)
    assert "CLAUDE.md" in adj


def test_graph_cache_avoids_reparse_within_mtime_window(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    g._GRAPH_CACHE.clear()
    calls = {"n": 0}
    real = g.parse_frontmatter
    def counting(p):
        calls["n"] += 1
        return real(p)
    monkeypatch.setattr(g, "parse_frontmatter", counting)
    g.build_related_graph(repo)
    first = calls["n"]
    assert first > 0
    g.build_related_graph(repo)        # cached — no reparse
    assert calls["n"] == first
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/ian/dev/doc-audit-cc && python -m pytest carta/search/tests/test_graph.py -k "undirected or canonical_path_edge or root_files or cache" -v`
Expected: FAIL (`_GRAPH_CACHE` missing / edges not mirrored).

- [ ] **Step 3: Rewrite `build_related_graph` with an undirected, cached adjacency**

Replace the existing `build_related_graph` body in `carta/search/graph.py` with:

```python
_GRAPH_CACHE: dict[str, tuple[float, dict[str, set]]] = {}


def _docs_mtime_sig(repo_root: Path, docs_root: Path) -> float:
    """Largest mtime across all markdown docs — a cheap cache-invalidation key."""
    latest = 0.0
    for p in _iter_md_paths(repo_root, docs_root):
        try:
            latest = max(latest, p.stat().st_mtime)
        except OSError:
            continue
    return latest


def build_related_graph(
    repo_root: Path,
    docs_root: Optional[Path] = None,
    *,
    use_cache: bool = True,
) -> dict[str, set]:
    """Undirected ``related:`` adjacency over all markdown docs.

    Every entry is resolved to a canonical repo-root POSIX path (see
    :func:`resolve_entry`); each edge is mirrored so backlink-only targets (a doc
    with an empty ``related:`` that others point at) are reachable. Keys match
    search hits' ``source`` exactly. Memoized by max doc mtime.

    Returns:
        Dict mapping ``canonical_path`` → set of adjacent canonical paths.
    """
    if docs_root is None:
        docs_root = repo_root / "docs"
    if use_cache:
        sig = _docs_mtime_sig(repo_root, docs_root)
        cached = _GRAPH_CACHE.get(str(repo_root))
        if cached and cached[0] == sig:
            return cached[1]

    doc_index = build_doc_index(repo_root, docs_root)
    adj: dict[str, set] = {}

    def link(a: str, b: str) -> None:
        adj.setdefault(a, set()).add(b)
        adj.setdefault(b, set()).add(a)

    for p in _iter_md_paths(repo_root, docs_root):
        canon = p.relative_to(repo_root).as_posix()
        adj.setdefault(canon, set())
        fm = parse_frontmatter(p)
        if not fm:
            continue
        for entry in fm.get("related") or []:
            target = resolve_entry(entry, doc_index, repo_root)
            if target and target != canon:
                link(canon, target)

    if use_cache:
        _GRAPH_CACHE[str(repo_root)] = (sig, adj)
    return adj
```

Leave `walk_hops` unchanged — it iterates `graph.get(seed, [])`, which works on `set` values.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/ian/dev/doc-audit-cc && python -m pytest carta/search/tests/test_graph.py -v`
Expected: PASS (9 tests total).

- [ ] **Step 5: Commit**

```bash
cd /Users/ian/dev/doc-audit-cc
git add carta/search/graph.py carta/search/tests/test_graph.py
git commit -m "feat(graph): undirected cached related: adjacency with resolved edges"
```

---

## Task 3: Seed expansion + neighbour promotion

**Files:**
- Modify: `carta/search/graph.py` (add `hit_path`, `expand_seeds`, `promote_graph_neighbors`)
- Test: `carta/search/tests/test_graph.py` (append)

- [ ] **Step 1: Write the failing tests**

Append to `carta/search/tests/test_graph.py`:

```python
def test_hit_path_strips_visual_page_suffix():
    assert g.hit_path({"source": "docs/x.pdf (page 5)"}) == "docs/x.pdf"
    assert g.hit_path({"source": "docs/y.md"}) == "docs/y.md"
    assert g.hit_path({}) == ""


def test_expand_seeds_returns_one_hop_neighbours():
    adj = {
        "a.md": {"b.md", "c.md"},
        "b.md": {"a.md"},
        "c.md": {"a.md", "d.md"},
        "d.md": {"c.md"},
    }
    out = g.expand_seeds(["a.md"], adj, hops=1)
    assert set(out) == {"b.md", "c.md"}      # d.md is 2 hops away
    assert "a.md" not in out                 # seed excluded


def test_promote_moves_neighbours_to_just_after_seeds():
    pool = [{"source": f"{c}.md"} for c in "ABCDEFGHIJKL"]  # 12 hits
    # neighbour is the deep hit at index 10 ("K.md")
    out = g.promote_graph_neighbors(pool, {"K.md"}, seed_count=3)
    paths = [g.hit_path(h) for h in out]
    assert paths[:3] == ["A.md", "B.md", "C.md"]     # seeds untouched
    assert paths[3] == "K.md"                          # neighbour promoted to pos 3
    assert len(out) == len(pool)                       # nothing dropped
    assert set(paths) == {f"{c}.md" for c in "ABCDEFGHIJKL"}


def test_promote_is_stable_with_no_neighbours():
    pool = [{"source": f"{c}.md"} for c in "ABC"]
    out = g.promote_graph_neighbors(pool, set(), seed_count=2)
    assert [g.hit_path(h) for h in out] == ["A.md", "B.md", "C.md"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/ian/dev/doc-audit-cc && python -m pytest carta/search/tests/test_graph.py -k "hit_path or expand_seeds or promote" -v`
Expected: FAIL (`AttributeError: ... 'hit_path'`).

- [ ] **Step 3: Implement the helpers**

Add to `carta/search/graph.py`:

```python
def hit_path(hit: dict) -> str:
    """Canonical doc path for a search hit — strips the visual ' (page N)' suffix."""
    src = hit.get("source", "") or ""
    return src.split(" (page ", 1)[0]


def expand_seeds(seeds: list[str], graph: dict[str, set], hops: int = 1) -> list[str]:
    """1-hop (or `hops`) neighbour doc paths of `seeds`, excluding the seeds themselves.

    Order: ascending hop distance then path (from :func:`walk_hops`).
    """
    out: list[str] = []
    seen = set(seeds)
    for h in walk_hops(list(seeds), graph, hops):
        doc = h["doc"]
        if doc not in seen:
            seen.add(doc)
            out.append(doc)
    return out


def promote_graph_neighbors(pool: list[dict], neighbours, seed_count: int) -> list[dict]:
    """Reorder `pool` so neighbour hits move to immediately after the first `seed_count`
    hits. Stable within each group (seeds, promoted neighbours, remainder); nothing dropped.

    The top `seed_count` hits are never displaced, so when no reranker runs this cannot
    change the top-`seed_count` results — graph's recall lift is realized via the reranker.
    """
    nb = set(neighbours)
    seeds = pool[:seed_count]
    tail = pool[seed_count:]
    promoted = [h for h in tail if hit_path(h) in nb]
    rest = [h for h in tail if hit_path(h) not in nb]
    return seeds + promoted + rest
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/ian/dev/doc-audit-cc && python -m pytest carta/search/tests/test_graph.py -v`
Expected: PASS (13 tests total).

- [ ] **Step 5: Commit**

```bash
cd /Users/ian/dev/doc-audit-cc
git add carta/search/graph.py carta/search/tests/test_graph.py
git commit -m "feat(graph): seed expansion + neighbour promotion helpers"
```

---

## Task 4: Config defaults for `search.graph`

**Files:**
- Modify: `carta/config.py:22-37` (the `"search"` block of `DEFAULTS`)
- Test: `carta/tests/test_config.py` (append)

- [ ] **Step 1: Write the failing test**

Append to `carta/tests/test_config.py`:

```python
def test_graph_defaults_present():
    from carta.config import DEFAULTS
    graph = DEFAULTS["search"]["graph"]
    assert graph["enabled"] is True
    assert graph["hops"] == 1
    assert graph["seed_count"] == 10
    assert graph["candidate_depth"] == 50
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/ian/dev/doc-audit-cc && python -m pytest carta/tests/test_config.py::test_graph_defaults_present -v`
Expected: FAIL with `KeyError: 'graph'`.

- [ ] **Step 3: Add the `graph` block**

In `carta/config.py`, inside `DEFAULTS["search"]`, add after the `"rerank"` block (so `"search"` contains `top_n`, `hybrid`, `rerank`, `graph`):

```python
        "graph": {
            "enabled": True,        # on by default; set false to opt out (low-memory machines)
            "hops": 1,              # related: traversal depth
            "seed_count": 10,       # how many top fused hits seed the walk
            "candidate_depth": 50,  # deep-fetch size when graph expansion is enabled
        },
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/ian/dev/doc-audit-cc && python -m pytest carta/tests/test_config.py::test_graph_defaults_present -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
cd /Users/ian/dev/doc-audit-cc
git add carta/config.py carta/tests/test_config.py
git commit -m "feat(config): search.graph defaults (enabled, hops, seed_count, candidate_depth)"
```

---

## Task 5: Wire graph expansion into `run_search`

**Files:**
- Modify: `carta/embed/pipeline.py` (add `_apply_graph_expansion`; edit `run_search` `fetch_limit` + post-fusion call)
- Test: `carta/embed/tests/test_graph_expansion.py` (new)

- [ ] **Step 1: Write the failing tests**

```python
# carta/embed/tests/test_graph_expansion.py
import carta.embed.pipeline as pipe


def _results():
    # 12 fused hits; the relevant deep neighbour sits at index 10 (rank 11)
    return [{"source": f"docs/d{i}.md", "excerpt": "x", "type": "text"} for i in range(12)]


def test_graph_expansion_promotes_neighbour_into_pool(monkeypatch):
    # Fake graph: top seed docs/d0.md is adjacent to the deep hit docs/d10.md
    fake_adj = {"docs/d0.md": {"docs/d10.md"}, "docs/d10.md": {"docs/d0.md"}}
    monkeypatch.setattr("carta.search.graph.build_related_graph", lambda *a, **k: fake_adj)
    cfg = {"search": {"graph": {"enabled": True, "hops": 1, "seed_count": 3, "candidate_depth": 50}}}
    out = pipe._apply_graph_expansion(_results(), cfg, repo_root="/repo")
    paths = [h["source"] for h in out]
    assert paths[3] == "docs/d10.md"          # promoted to just after the 3 seeds
    assert len(out) == 12                       # nothing dropped


def test_graph_expansion_disabled_is_identity(monkeypatch):
    monkeypatch.setattr("carta.search.graph.build_related_graph",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not build")))
    cfg = {"search": {"graph": {"enabled": False}}}
    r = _results()
    out = pipe._apply_graph_expansion(r, cfg, repo_root="/repo")
    assert [h["source"] for h in out] == [h["source"] for h in r]


def test_graph_expansion_fails_open_on_error(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("graph build failed")
    monkeypatch.setattr("carta.search.graph.build_related_graph", boom)
    cfg = {"search": {"graph": {"enabled": True, "seed_count": 3}}}
    r = _results()
    out = pipe._apply_graph_expansion(r, cfg, repo_root="/repo")
    assert [h["source"] for h in out] == [h["source"] for h in r]   # unchanged, no raise
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/ian/dev/doc-audit-cc && python -m pytest carta/embed/tests/test_graph_expansion.py -v`
Expected: FAIL with `AttributeError: module 'carta.embed.pipeline' has no attribute '_apply_graph_expansion'`.

- [ ] **Step 3: Add the `_apply_graph_expansion` helper**

Add to `carta/embed/pipeline.py` (near `_rrf_merge_collections`, before `run_search`):

```python
def _apply_graph_expansion(results: list[dict], cfg: dict, repo_root) -> list[dict]:
    """Promote related:-graph neighbours of the top seeds into the candidate pool.

    Undirected 1-hop expansion from the top `seed_count` fused hits; neighbour hits are
    moved to just-after the seeds so a downstream reranker can float them up. Fail-open:
    any error (or graph disabled / no neighbours) returns `results` unchanged.
    """
    graph_cfg = cfg.get("search", {}).get("graph", {})
    if not graph_cfg.get("enabled", True) or not results:
        return results
    try:
        from carta.search.graph import build_related_graph, expand_seeds, promote_graph_neighbors, hit_path
        from pathlib import Path

        seed_count = graph_cfg.get("seed_count", 10)
        graph = build_related_graph(Path(repo_root))
        seeds = [hit_path(h) for h in results[:seed_count]]
        neighbours = expand_seeds(seeds, graph, graph_cfg.get("hops", 1))
        if not neighbours:
            return results
        return promote_graph_neighbors(results, neighbours, seed_count)
    except Exception:
        return results
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/ian/dev/doc-audit-cc && python -m pytest carta/embed/tests/test_graph_expansion.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Wire the helper into `run_search`**

In `carta/embed/pipeline.py`, **edit the `fetch_limit` computation** (currently `carta/embed/pipeline.py:1465-1468`). Replace:

```python
    rr_cfg = cfg.get("search", {}).get("rerank", {})
    rerank_enabled = rr_cfg.get("enabled", False)
    candidate_pool = rr_cfg.get("candidate_pool", 30)
    fetch_limit = max(candidate_pool, top_n) if rerank_enabled else top_n
```

with:

```python
    rr_cfg = cfg.get("search", {}).get("rerank", {})
    rerank_enabled = rr_cfg.get("enabled", False)
    candidate_pool = rr_cfg.get("candidate_pool", 30)
    graph_cfg = cfg.get("search", {}).get("graph", {})
    graph_enabled = graph_cfg.get("enabled", True)
    candidate_depth = graph_cfg.get("candidate_depth", 50)
    # Fetch deep enough for the rerank pool AND graph promotion (whichever is wider).
    fetch_limit = top_n
    if rerank_enabled:
        fetch_limit = max(fetch_limit, candidate_pool)
    if graph_enabled:
        fetch_limit = max(fetch_limit, candidate_depth)
```

Then **insert the graph step** immediately after the fusion line (`carta/embed/pipeline.py:1604`, `all_results = _rrf_merge_collections(...)`) and before the rerank block (`if rerank_enabled and all_results:`):

```python
    # Graph-aware expansion: promote related:-adjacent deep docs into the pool the
    # reranker sees. Fail-open. (No-op for top-n when reranking is off, by design.)
    if graph_enabled:
        all_results = _apply_graph_expansion(all_results, cfg, repo_root)
```

- [ ] **Step 6: Run the full search + graph test suites**

Run: `cd /Users/ian/dev/doc-audit-cc && python -m pytest carta/embed/tests/test_graph_expansion.py carta/search/tests/ -v`
Expected: PASS. Then confirm no regressions in the broader pipeline tests:
Run: `cd /Users/ian/dev/doc-audit-cc && python -m pytest carta/embed/tests/ -q`
Expected: PASS (no new failures vs. baseline).

- [ ] **Step 7: Commit**

```bash
cd /Users/ian/dev/doc-audit-cc
git add carta/embed/pipeline.py carta/embed/tests/test_graph_expansion.py
git commit -m "feat(search): graph-aware expansion in run_search (promote related: neighbours into rerank pool)"
```

---

## Task 6: Audit check for non-canonical / broken `related:` entries

**Files:**
- Modify: `carta/scanner/scanner.py` (add `check_noncanonical_related`; wire into the scan loop at `carta/scanner/scanner.py:839`)
- Test: `carta/scanner/tests/test_noncanonical_related.py` (new — create `carta/scanner/tests/__init__.py` if the dir/file is absent)

- [ ] **Step 1: Write the failing test**

```python
# carta/scanner/tests/test_noncanonical_related.py
from pathlib import Path
from carta.scanner.scanner import check_noncanonical_related, parse_frontmatter
from carta.search.graph import build_doc_index


def _repo(tmp_path: Path) -> Path:
    (tmp_path / "docs" / "hardware" / "vcu").mkdir(parents=True)
    (tmp_path / "docs" / "hardware" / "vcu" / "connector-map.md").write_text(
        "---\nid: connector-map\nrelated: []\n---\nbody\n")
    # one canonical entry, one bare-id (non-canonical), one broken
    (tmp_path / "docs" / "hardware" / "vcu" / "power.md").write_text(
        "---\nid: power\nrelated:\n"
        "  - docs/hardware/vcu/connector-map.md\n"   # canonical -> no finding
        "  - connector-map\n"                          # bare id -> non_canonical_related
        "  - nonexistent-doc\n"                        # broken -> broken_related-style finding
        "---\nbody\n")
    return tmp_path


def test_flags_noncanonical_and_broken_but_not_canonical(tmp_path):
    repo = _repo(tmp_path)
    doc_index = build_doc_index(repo)
    power = repo / "docs" / "hardware" / "vcu" / "power.md"
    fm = parse_frontmatter(power)
    issues = check_noncanonical_related(power, fm, doc_index, repo)
    types = {(i["type"], i.get("related_file")) for i in issues}
    # bare id resolves but is non-canonical, with a suggested canonical path
    assert ("noncanonical_related", "connector-map") in types
    nc = next(i for i in issues if i.get("related_file") == "connector-map")
    assert nc["suggested"] == "docs/hardware/vcu/connector-map.md"
    # unresolvable entry is flagged
    assert ("noncanonical_related", "nonexistent-doc") in types
    broken = next(i for i in issues if i.get("related_file") == "nonexistent-doc")
    assert broken["resolves"] is False
    # the canonical entry produces NO finding
    assert "docs/hardware/vcu/connector-map.md" not in {i.get("related_file") for i in issues}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/ian/dev/doc-audit-cc && python -m pytest carta/scanner/tests/test_noncanonical_related.py -v`
Expected: FAIL with `ImportError: cannot import name 'check_noncanonical_related'`.

- [ ] **Step 3: Implement the check**

Add to `carta/scanner/scanner.py` (near `check_broken_related` at `carta/scanner/scanner.py:129`). **Do NOT add a top-level import of `carta.search.graph`** — `graph.py` already imports `parse_frontmatter` from this module at load time, so a module-level import here would be circular. Import `resolve_entry` *inside* the function instead (shown below).

```python
def check_noncanonical_related(doc_path: Path, frontmatter: dict, doc_index: dict, repo_root: Path) -> list:
    """Flag related: entries that resolve only via a fallback tier (non-canonical —
    e.g. a bare id or a missing-docs/-prefix path) or do not resolve at all.

    A canonical entry (an exact, existing repo-root path) produces no finding. This feeds
    the linking sweep so entries get rewritten to canonical paths over time; search itself
    resolves them regardless via resolve_entry.
    """
    from carta.search.graph import resolve_entry  # local import — avoids circular import
    issues = []
    rel_doc = str(doc_path.relative_to(repo_root))
    for entry in frontmatter.get("related") or []:
        if not isinstance(entry, str) or not entry.strip():
            continue
        canonical = (repo_root / entry).exists()
        if canonical:
            continue
        resolved = resolve_entry(entry, doc_index, repo_root)
        if resolved is not None:
            issues.append({
                "type": "noncanonical_related",
                "severity": "warning",
                "doc": rel_doc,
                "detail": f"related: entry '{entry}' is non-canonical; use '{resolved}'",
                "related_file": entry,
                "suggested": resolved,
                "resolves": True,
            })
        else:
            issues.append({
                "type": "noncanonical_related",
                "severity": "error",
                "doc": rel_doc,
                "detail": f"related: entry '{entry}' does not resolve to any known doc",
                "related_file": entry,
                "suggested": None,
                "resolves": False,
            })
    return issues
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/ian/dev/doc-audit-cc && python -m pytest carta/scanner/tests/test_noncanonical_related.py -v`
Expected: PASS.

- [ ] **Step 5: Wire the check into the scan loop**

In `carta/scanner/scanner.py`, the per-doc loop builds findings (`carta/scanner/scanner.py:827-845`). Build the doc index once before the loop and call the new check inside it. Just before the `for doc_path in tracked_docs:` loop (line 827), add:

```python
    from carta.search.graph import build_doc_index
    doc_index = build_doc_index(repo_root)
```

Then inside the loop, after the existing `issues.extend(check_broken_related(doc_path, fm, repo_root))` (line 839), add:

```python
        issues.extend(check_noncanonical_related(doc_path, fm, doc_index, repo_root))
```

- [ ] **Step 6: Run the scanner test suite**

Run: `cd /Users/ian/dev/doc-audit-cc && python -m pytest carta/scanner/ -q`
Expected: PASS (no regressions).

- [ ] **Step 7: Commit**

```bash
cd /Users/ian/dev/doc-audit-cc
git add carta/scanner/scanner.py carta/scanner/tests/test_noncanonical_related.py
git commit -m "feat(scan): audit check for non-canonical / unresolved related: entries"
```

---

## Task 7: Docs, version bump, and live eval measurement

**Files:**
- Modify: `CHANGELOG.md`, `README.md`, `carta/__init__.py`, `.claude-plugin/plugin.json`, `marketplace.json`

- [ ] **Step 1: Run the full test suite (verification before docs)**

Run: `cd /Users/ian/dev/doc-audit-cc && python -m pytest -q`
Expected: PASS (all green, including the new graph/scanner/config tests).

- [ ] **Step 2: Live eval — confirm the recall lift**

Point Carta at the ET-embed corpus and run the eval with graph + LLM rerank on. Create a temporary config override or use the ET-embed `.carta/config.yaml` with `search.graph.enabled: true` and `search.rerank.{enabled: true, backend: llm}`:

```bash
cd /Users/ian/School/Elementrailer/ET-embed
carta eval .carta/eval/et-embed.yaml -k 5
```

Expected: `recall@5 > 0.750` and the previously-missed `SAFETY-MCU-MESSAGES` (was rank 33) and telemetry doc (was rank 43) now appear in the top-5. Record the numbers; if recall did **not** improve, STOP and report (do not proceed to release) — the graph step or resolver needs debugging via systematic-debugging.

- [ ] **Step 3: Bump the version to 0.9.0**

Edit `carta/__init__.py` — set `__version__ = "0.9.0"`. Edit `.claude-plugin/plugin.json` and `marketplace.json` — set their `version` fields to `0.9.0` (the release workflow also syncs these from the tag, but keep them consistent in-repo, matching the phase-1 release).

- [ ] **Step 4: Add the CHANGELOG entry**

Prepend under the top of `CHANGELOG.md` (above `## [0.8.0]`):

```markdown
## [0.9.0] — 2026-06-10

### Added
- **Graph-aware retrieval** (`search.graph`, on by default). `run_search` walks the `related:`
  frontmatter graph (undirected, 1 hop) from the top `seed_count` hits and promotes adjacent
  documents into the rerank candidate pool, so relevant docs that rank too deep for the pool
  (e.g. ranks 33/43) get a chance to surface. Knobs: `enabled`, `hops`, `seed_count`,
  `candidate_depth`. **Fail-open** — any graph error returns the fused order unchanged. The
  recall lift is realized **through the reranker** (enable `search.rerank`); graph expansion
  never displaces the top fused hits on its own.
- **`related:` entry resolver** — search-time normalization that maps any entry style (exact
  path, missing-`docs/`-prefix, bare id/slug, `.embed-meta.yaml` drift) to a canonical
  repo-root path, so the graph connects without editing the docs. Undirected adjacency
  (forward edges ∪ backlinks) is memoized by max doc mtime.
- **Audit check `noncanonical_related`** — `carta scan` now flags `related:` entries that
  resolve only via a fallback tier (with the suggested canonical path) or don't resolve at all,
  feeding the linking sweep.

### Measured
- ET-embed corpus, graph on + `qwen3.5:0.8b` LLM rerank: recall@5 0.750 → <fill in from Step 2>.
```

Replace `<fill in from Step 2>` with the measured number from Step 2 (do **not** leave the placeholder).

- [ ] **Step 5: Add a README subsection**

In `README.md`, under the existing "Search Reranking" section (added in 0.8.0), add a short "Graph-aware retrieval" subsection documenting the `search.graph` knobs, that it is on by default, that the recall benefit is realized through the reranker, and that it is fail-open. Mirror the tone/length of the reranker subsection.

- [ ] **Step 6: Commit**

```bash
cd /Users/ian/dev/doc-audit-cc
git add CHANGELOG.md README.md carta/__init__.py .claude-plugin/plugin.json marketplace.json
git commit -m "docs+release: graph-aware retrieval (v0.9.0) — CHANGELOG, README, version bump"
```

---

## Final Review

After all tasks: dispatch a final code reviewer over the whole branch (`git diff main...HEAD`), confirm the full suite is green (`python -m pytest -q`), then use **superpowers:finishing-a-development-branch** to open the PR / merge / tag `v0.9.0` (the release workflow publishes to PyPI + GitHub on the tag). Do not tag before the live eval (Task 7 Step 2) confirms the recall lift.
