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
    assert g.resolve_entry("../escape.md", idx, repo) is None


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
