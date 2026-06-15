---
id: 2026-06-11-note-capture
title: "Note Capture (Quirks & Notes, v0.10.0) Implementation Plan"
status: shipped
related:
  - 2026-06-10-note-capture-design
date: 2026-06-11
---

# Note Capture (Quirks & Notes, v0.10.0) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship `carta_remember` (MCP) + `carta remember` (CLI) so curated project notes (quirk / bug-note / helpful-note) are written as repo markdown files and embedded into `{project}_notes` — fixing the dead `collection_for_doc_type` routing end-to-end on the way.

**Architecture:** A new `carta/memory/capture.py` core writes a frontmatter'd markdown file into `docs/quirks/` or `docs/notes/` and reuses `run_embed_file()` for sidecar/chunk/upsert. Three routing fixes make doc_type actually mean something: frontmatter override in induct, doc_type-aware collection selection in `upsert_chunks` (currently hardcoded `_doc`), and bootstrap creating `_notes`. Search/hook output labels note hits `[quirk]` etc.

**Tech Stack:** Python 3.10+, pytest, unittest.mock, PyYAML. Spec: `docs/superpowers/specs/2026-06-10-note-capture-design.md` (in this worktree).

**Conventions:** strict TDD; patch function-local imports at their SOURCE module (e.g. `carta.embed.pipeline.run_embed_file`) — this works because the import executes at call time. All commits end with `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.

---

### Task 1: Frontmatter doc_type override + path map (induct)

**Files:**
- Modify: `carta/embed/induct.py` (lines 9, 13-20, 37-42, 59-60, 75)
- Test: `carta/tests/test_induct.py` (append class)

- [ ] **Step 1: Write the failing tests** — append to `carta/tests/test_induct.py`:

```python
class TestDocTypeResolution:
    """Frontmatter doc_type wins over parent-dir inference; quirks/notes dirs map;
    the stub's collection field routes via collection_for_doc_type."""

    def _stub(self, tmp_path, rel, content="# T"):
        fp = tmp_path / rel
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content)
        cfg = {"project_name": "p", "qdrant_url": "http://localhost:6333"}
        return generate_sidecar_stub(fp, tmp_path, cfg)

    def test_frontmatter_doc_type_wins_over_path(self, tmp_path):
        stub = self._stub(tmp_path, "docs/reference/x.md",
                          "---\ndoc_type: quirk\n---\n\nBody text")
        assert stub["doc_type"] == "quirk"
        assert stub["collection"] == "p_notes"

    def test_quirks_dir_maps_to_quirk(self, tmp_path):
        stub = self._stub(tmp_path, "docs/quirks/x.md")
        assert stub["doc_type"] == "quirk"
        assert stub["collection"] == "p_notes"

    def test_notes_dir_maps_to_helpful_note(self, tmp_path):
        stub = self._stub(tmp_path, "docs/notes/x.md")
        assert stub["doc_type"] == "helpful-note"
        assert stub["collection"] == "p_notes"

    def test_unmapped_dir_no_frontmatter_unchanged(self, tmp_path):
        stub = self._stub(tmp_path, "docs/misc/x.md")
        assert stub["doc_type"] == "unknown"
        assert stub["collection"] == "p_doc"

    def test_mapped_dir_without_frontmatter_still_routes_doc(self, tmp_path):
        stub = self._stub(tmp_path, "docs/reference/datasheets/x.md")
        assert stub["doc_type"] == "datasheet"
        assert stub["collection"] == "p_doc"

    def test_malformed_frontmatter_falls_back_to_path(self, tmp_path):
        stub = self._stub(tmp_path, "docs/quirks/x.md", "---\n: : bad yaml [\n---\nBody")
        assert stub["doc_type"] == "quirk"
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m pytest carta/tests/test_induct.py::TestDocTypeResolution -v`
Expected: FAIL — `stub["doc_type"]` is `"unknown"` for frontmatter/quirks cases, `collection` is `"p_doc"`.

- [ ] **Step 3: Implement** in `carta/embed/induct.py`:

Import (line 9): `from carta.config import collection_name, collection_for_doc_type`

Extend the map (lines 13-20):

```python
_PATH_TYPE_MAP = {
    "datasheets": "datasheet",
    "manuals": "manual",
    "schematics": "schematic",
    "reference": "reference",
    "specs": "spec",
    "guides": "guide",
    "quirks": "quirk",
    "notes": "helpful-note",
}
```

Add after `infer_doc_type` (line 42):

```python
def resolve_doc_type(file_path: Path, rel_path: Path) -> str:
    """Resolve a file's doc_type: explicit frontmatter wins, else parent-dir inference.

    Args:
        file_path: absolute path (read for frontmatter when markdown).
        rel_path: repo-relative path (parent names drive inference).
    """
    if file_path.suffix == ".md":
        from carta.scanner.scanner import parse_frontmatter
        try:
            fm = parse_frontmatter(file_path) or {}
        except Exception:
            fm = {}
        fm_type = fm.get("doc_type")
        if isinstance(fm_type, str) and fm_type.strip():
            return fm_type.strip()
    return infer_doc_type(rel_path)
```

In `generate_sidecar_stub`, change line 60 `doc_type = infer_doc_type(rel_path)` to
`doc_type = resolve_doc_type(file_path, rel_path)`, and line 75
`"collection": collection_name(cfg, "doc"),` to
`"collection": collection_for_doc_type(cfg, doc_type),`.

(If importing `carta.scanner.scanner` at module level were circular, the function-local
import above avoids it; keep it function-local regardless.)

- [ ] **Step 4: Run tests** — `python3 -m pytest carta/tests/test_induct.py -v` → all PASS.

- [ ] **Step 5: Commit**

```bash
git add carta/embed/induct.py carta/tests/test_induct.py
git commit -m "feat(induct): frontmatter doc_type override + quirks/notes path mapping"
```

---

### Task 2: `upsert_chunks` routes by doc_type (the dead-code fix)

**Files:**
- Modify: `carta/embed/embed.py:184` (and its `from carta.config import ...` line)
- Test: `carta/tests/test_embed.py` (append class)

- [ ] **Step 1: Write the failing tests** — append to `carta/tests/test_embed.py`:

```python
class TestUpsertChunksRouting:
    """upsert_chunks must route by the chunks' doc_type via collection_for_doc_type —
    it previously hardcoded {project}_doc, making note types unreachable."""

    def _run(self, doc_type):
        from unittest.mock import patch, MagicMock
        from carta.embed.embed import upsert_chunks
        chunks = [{"slug": "s", "text": "hello world", "chunk_index": 0,
                   "doc_type": doc_type}]
        cfg = {"project_name": "p", "qdrant_url": "http://localhost:6333",
               "embed": {"ollama_url": "http://localhost:11434",
                          "ollama_model": "m", "embedding_workers": 1}}
        client = MagicMock()
        with patch("carta.embed.embed.get_embedding", return_value=[0.0] * 768), \
             patch("carta.embed.embed.collection_is_hybrid", return_value=False), \
             patch("carta.embed.embed.ensure_collection") as ens:
            upsert_chunks(chunks, cfg, client=client)
        ensured = ens.call_args[0][1]
        # the same name must be used for the actual upsert call
        assert ensured in str(client.upsert.call_args)
        return ensured

    def test_quirk_routes_to_notes(self):
        assert self._run("quirk") == "p_notes"

    def test_bug_note_routes_to_notes(self):
        assert self._run("bug-note") == "p_notes"

    def test_helpful_note_routes_to_notes(self):
        assert self._run("helpful-note") == "p_notes"

    def test_plain_doc_type_still_routes_to_doc(self):
        assert self._run("datasheet") == "p_doc"

    def test_image_description_still_routes_to_doc(self):
        assert self._run("image_description") == "p_doc"
```

(If `get_embedding` lives behind a different internal name in `upsert_chunks` — e.g. a
batch helper — adjust the patch target to whatever `upsert_chunks` actually calls, keeping
the assertions identical.)

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m pytest carta/tests/test_embed.py::TestUpsertChunksRouting -v`
Expected: the three note-type tests FAIL with `'p_doc' == 'p_notes'` mismatches.

- [ ] **Step 3: Implement** in `carta/embed/embed.py`:

Add `collection_for_doc_type` to the existing `from carta.config import ...` line, then
replace line 184 `coll_name = collection_name(cfg, "doc")` with:

```python
    # Route by the batch's doc_type (batches come from a single file, so they are
    # doc_type-homogeneous). Note types (quirk/bug-note/helpful-note) land in
    # {project}_notes; everything else (incl. image/visual chunk types) stays in _doc.
    batch_doc_type = chunks[0].get("doc_type", "unknown") if chunks else "unknown"
    coll_name = collection_for_doc_type(cfg, batch_doc_type)
```

- [ ] **Step 4: Run tests** — `python3 -m pytest carta/tests/test_embed.py -v` → all PASS (zero regressions).

- [ ] **Step 5: Commit**

```bash
git add carta/embed/embed.py carta/tests/test_embed.py
git commit -m "fix(embed): upsert_chunks routes by doc_type — collection_for_doc_type was dead code"
```

---

### Task 3: Bootstrap creates `_notes` (not `_quirk`)

**Files:**
- Modify: `carta/install/bootstrap.py:13` (VECTOR_DIMENSIONS), `:255` (summary string), `:359` (creation list)
- Test: `carta/tests/test_bootstrap.py` (append)

- [ ] **Step 1: Write the failing test** — append to `carta/tests/test_bootstrap.py`:

```python
def test_bootstrap_creates_notes_not_quirk(monkeypatch):
    """Init must create {project}_notes (what routing/search use), not legacy _quirk."""
    from unittest.mock import MagicMock
    from carta.install import bootstrap as bs

    calls = []

    def fake_put(url, json=None, timeout=None):
        calls.append(url)
        r = MagicMock()
        r.status_code = 200
        return r

    monkeypatch.setattr(bs.requests, "put", fake_put)
    ok = bs._create_qdrant_collections("p", "http://localhost:6333")
    assert ok
    assert any(u.endswith("/collections/p_notes") for u in calls)
    assert any(u.endswith("/collections/p_doc") for u in calls)
    assert any(u.endswith("/collections/p_session") for u in calls)
    assert not any(u.endswith("/collections/p_quirk") for u in calls)
```

- [ ] **Step 2: Run to verify failure** — `python3 -m pytest carta/tests/test_bootstrap.py::test_bootstrap_creates_notes_not_quirk -v` → FAIL (p_quirk created, p_notes not).

- [ ] **Step 3: Implement** in `carta/install/bootstrap.py`:

Line 13: `VECTOR_DIMENSIONS = {"doc": 768, "session": 768, "notes": 768}`
Line 359: `for type_ in ["doc", "session", "notes"]:`
Line 255: `colls = f"{project_name}_doc, {project_name}_session, {project_name}_notes"`

- [ ] **Step 4: Run tests** — `python3 -m pytest carta/tests/test_bootstrap.py -v` → all PASS.

- [ ] **Step 5: Commit**

```bash
git add carta/install/bootstrap.py carta/tests/test_bootstrap.py
git commit -m "fix(bootstrap): create {project}_notes collection (routing/search target), drop _quirk"
```

---

### Task 4: Capture core (`carta/memory/capture.py`) + config defaults

**Files:**
- Create: `carta/memory/__init__.py` (empty), `carta/memory/capture.py`
- Modify: `carta/config.py` (DEFAULTS gains `memory:` block; add `NOTE_DOC_TYPES`; `collection_for_doc_type` uses it)
- Test: `carta/tests/test_capture.py` (new)

- [ ] **Step 1: Write the failing tests** — create `carta/tests/test_capture.py`:

```python
"""Tests for carta/memory/capture.py — note capture core."""

from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from carta.memory.capture import capture_note


def _cfg():
    return {
        "project_name": "p",
        "qdrant_url": "http://localhost:6333",
        "memory": {"quirks_dir": "docs/quirks", "notes_dir": "docs/notes"},
        "embed": {"ollama_url": "http://localhost:11434", "ollama_model": "m"},
    }


def _capture(tmp_path, text="The bench PSU must be on for CAN tests", **kw):
    kw.setdefault("note_type", "quirk")
    with patch("carta.embed.pipeline.run_embed_file",
               return_value={"status": "ok", "chunks": 2}) as emb:
        out = capture_note(_cfg(), tmp_path, text, **kw)
    return out, emb


class TestCaptureNote:
    def test_quirk_file_written_with_frontmatter(self, tmp_path):
        out, emb = _capture(tmp_path, title="Bench PSU quirk", tags=["bench", "can"])
        p = tmp_path / out["path"]
        assert p.parent == tmp_path / "docs" / "quirks"
        content = p.read_text()
        assert content.startswith("---\n")
        fm = yaml.safe_load(content.split("---")[1])
        assert fm["doc_type"] == "quirk"
        assert fm["title"] == "Bench PSU quirk"
        assert fm["tags"] == ["bench", "can"]
        assert "created" in fm
        assert "The bench PSU must be on" in content
        assert out["collection"] == "p_notes"
        assert out["chunks"] == 2
        emb.assert_called_once()

    def test_bug_note_and_helpful_note_go_to_notes_dir(self, tmp_path):
        for nt in ("bug-note", "helpful-note"):
            out, _ = _capture(tmp_path, note_type=nt)
            assert (tmp_path / out["path"]).parent == tmp_path / "docs" / "notes"

    def test_title_drives_slug_else_first_words(self, tmp_path):
        out, _ = _capture(tmp_path, title="EZKontrol CAN Handshake!")
        assert "ezkontrol-can-handshake" in out["path"]
        out2, _ = _capture(tmp_path, text="Always check the shunt resistor first", title="")
        assert "always-check-the-shunt" in out2["path"]

    def test_filename_collision_appends_suffix(self, tmp_path):
        out1, _ = _capture(tmp_path, title="Same Title")
        out2, _ = _capture(tmp_path, title="Same Title")
        assert out1["path"] != out2["path"]
        assert out2["path"].endswith("-2.md")

    def test_invalid_note_type_raises(self, tmp_path):
        with pytest.raises(ValueError, match="note_type"):
            capture_note(_cfg(), tmp_path, "x", note_type="session")

    def test_empty_text_raises(self, tmp_path):
        with pytest.raises(ValueError, match="empty"):
            capture_note(_cfg(), tmp_path, "   ", note_type="quirk")

    def test_embed_failure_keeps_file_and_raises(self, tmp_path):
        with patch("carta.embed.pipeline.run_embed_file",
                   side_effect=RuntimeError("qdrant down")):
            with pytest.raises(RuntimeError, match="carta embed"):
                capture_note(_cfg(), tmp_path, "important fact", note_type="quirk")
        written = list((tmp_path / "docs" / "quirks").glob("*.md"))
        assert len(written) == 1, "the note file must survive an embed failure"

    def test_custom_dirs_from_config(self, tmp_path):
        cfg = _cfg()
        cfg["memory"]["quirks_dir"] = "docs/carta/quirks"
        with patch("carta.embed.pipeline.run_embed_file",
                   return_value={"status": "ok", "chunks": 1}):
            out = capture_note(cfg, tmp_path, "fact", note_type="quirk")
        assert out["path"].startswith("docs/carta/quirks/")
```

- [ ] **Step 2: Run to verify failure** — `python3 -m pytest carta/tests/test_capture.py -v` → FAIL with `ModuleNotFoundError: carta.memory`.

- [ ] **Step 3: Implement.**

`carta/config.py` — add near `REQUIRED_FIELDS`:

```python
# Curated note types — routed to {project}_notes and labeled in search output.
NOTE_DOC_TYPES = ("quirk", "bug-note", "helpful-note")
```

In `collection_for_doc_type`, replace `if doc_type in ("quirk", "bug-note", "helpful-note"):`
with `if doc_type in NOTE_DOC_TYPES:`.

In `DEFAULTS`, add (top level, after `"contradiction_types"`):

```python
    "memory": {
        "quirks_dir": "docs/quirks",     # note_type: quirk
        "notes_dir": "docs/notes",       # note_type: bug-note, helpful-note
    },
```

`carta/memory/__init__.py`: empty file.

`carta/memory/capture.py`:

```python
"""Note capture — write a curated note as a repo markdown file and embed it.

Notes are knowledge artifacts: content-named files in the user's docs tree
(docs/quirks/, docs/notes/ by default), plain markdown with generic frontmatter,
useful with or without Carta. See the repo footprint policy in
docs/superpowers/specs/2026-06-10-note-capture-design.md.
"""
from __future__ import annotations

import re
from datetime import date
from pathlib import Path

import yaml

from carta.config import NOTE_DOC_TYPES, collection_for_doc_type


def _slugify(text: str, max_words: int = 6) -> str:
    words = re.sub(r"[^a-zA-Z0-9\s-]", "", text).split()[:max_words]
    slug = "-".join(w.lower() for w in words)
    return slug or "note"


def _note_dir(cfg: dict, note_type: str) -> str:
    mem = cfg.get("memory", {})
    if note_type == "quirk":
        return mem.get("quirks_dir", "docs/quirks")
    return mem.get("notes_dir", "docs/notes")


def capture_note(cfg: dict, repo_root: Path, text: str, *,
                 note_type: str, title: str = "",
                 tags: list[str] | None = None) -> dict:
    """Write a note file with frontmatter and embed it via the standard pipeline.

    Args:
        cfg: carta config dict.
        repo_root: absolute repo root path.
        text: the note body (stored verbatim).
        note_type: one of NOTE_DOC_TYPES.
        title: optional title; drives the filename slug and frontmatter title.
        tags: optional list of tags for the frontmatter.

    Returns:
        {"path": <repo-relative str>, "collection": <name>, "chunks": <int>}

    Raises:
        ValueError: invalid note_type or empty text.
        RuntimeError: file written but embedding failed (file is kept).
    """
    if note_type not in NOTE_DOC_TYPES:
        raise ValueError(
            f"invalid note_type {note_type!r} — must be one of {', '.join(NOTE_DOC_TYPES)}"
        )
    if not text or not text.strip():
        raise ValueError("note text is empty")

    target_dir = Path(repo_root) / _note_dir(cfg, note_type)
    target_dir.mkdir(parents=True, exist_ok=True)

    slug = _slugify(title or text)
    today = date.today().isoformat()
    path = target_dir / f"{today}-{slug}.md"
    n = 2
    while path.exists():
        path = target_dir / f"{today}-{slug}-{n}.md"
        n += 1

    frontmatter = {
        "doc_type": note_type,
        "title": title or " ".join(text.split()[:8]),
        "created": today,
    }
    if tags:
        frontmatter["tags"] = list(tags)

    content = (
        "---\n"
        + yaml.dump(frontmatter, default_flow_style=False, sort_keys=False)
        + "---\n\n"
        + text.strip()
        + "\n"
    )
    path.write_text(content)
    rel = str(path.relative_to(repo_root))

    from carta.embed.pipeline import run_embed_file
    try:
        result = run_embed_file(path, cfg) or {}
    except Exception as e:
        raise RuntimeError(
            f"note written to {rel} but embedding failed: {e} — "
            f"run `carta embed` to index it"
        ) from e

    return {
        "path": rel,
        "collection": collection_for_doc_type(cfg, note_type),
        "chunks": result.get("chunks", 0),
    }
```

Note: `run_embed_file` derives repo_root from `find_config()` internally, so callers must
run with cwd inside the project — true for both CLI and MCP surfaces.

- [ ] **Step 4: Run tests** — `python3 -m pytest carta/tests/test_capture.py carta/tests/test_config.py -v` → all PASS.

- [ ] **Step 5: Commit**

```bash
git add carta/memory/ carta/config.py carta/tests/test_capture.py
git commit -m "feat(memory): capture_note core — frontmatter'd note files embedded via the standard pipeline"
```

---

### Task 5: MCP tool `carta_remember`

**Files:**
- Modify: `carta/mcp/server.py` (new tool after `carta_scan`)
- Test: `carta/tests/test_mcp_server.py` (append)

- [ ] **Step 1: Write the failing tests** — append to `carta/tests/test_mcp_server.py` (the file already mocks the `mcp` modules at import; `_remember` is a plain function so it stays testable):

```python
def test_remember_returns_ok_shape(tmp_path):
    from carta.mcp import server
    with patch.object(server, "_load_cfg", return_value={"project_name": "p"}), \
         patch.object(server, "_repo_root_from_cfg", return_value=tmp_path), \
         patch("carta.memory.capture.capture_note",
               return_value={"path": "docs/quirks/x.md", "collection": "p_notes", "chunks": 1}):
        out = server._remember("the bench PSU must be on", note_type="quirk")
    assert out == {"status": "ok", "path": "docs/quirks/x.md",
                   "collection": "p_notes", "chunks": 1}


def test_remember_invalid_type_maps_to_invalid_request(tmp_path):
    from carta.mcp import server
    with patch.object(server, "_load_cfg", return_value={"project_name": "p"}), \
         patch.object(server, "_repo_root_from_cfg", return_value=tmp_path), \
         patch("carta.memory.capture.capture_note", side_effect=ValueError("bad note_type")):
        out = server._remember("x", note_type="nope")
    assert out["error"] == "invalid_request"


def test_remember_no_config_maps_to_service_unavailable():
    from carta.mcp import server
    from carta.config import ConfigError
    with patch.object(server, "_load_cfg", side_effect=ConfigError("no .carta")):
        out = server._remember("x")
    assert out["error"] == "service_unavailable"
```

- [ ] **Step 2: Run to verify failure** — `python3 -m pytest carta/tests/test_mcp_server.py -v` → new tests FAIL (`AttributeError: _remember`).

- [ ] **Step 3: Implement** in `carta/mcp/server.py`, after the `carta_scan` tool:

```python
def _remember(text: str, *, note_type: str = "helpful-note", title: str = "",
              tags: list[str] | None = None) -> dict:
    """Plain-function core for carta_remember (kept undecorated for testability)."""
    try:
        cfg = _load_cfg()
        repo_root = _repo_root_from_cfg()
    except (ConfigError, FileNotFoundError) as e:
        return {"error": "service_unavailable", "detail": str(e)}
    try:
        from carta.memory.capture import capture_note
        result = capture_note(cfg, repo_root, text, note_type=note_type,
                              title=title, tags=tags)
        return {"status": "ok", **result}
    except ValueError as e:
        return {"error": "invalid_request", "detail": str(e)}
    except Exception as e:
        _logger.warning("carta_remember error: %s", e)
        return {"error": "service_unavailable", "detail": str(e)}


@mcp_server.tool()
def carta_remember(
    text: str,
    note_type: str = "helpful-note",
    title: str = "",
    tags: list[str] | None = None,
) -> dict:
    """Save a curated project note as a repo markdown file and embed it for search.

    Use when you learn something durable about THIS project worth remembering across
    sessions: note_type="quirk" for surprising system/hardware behavior,
    "bug-note" for bug-investigation findings, "helpful-note" for other durable
    knowledge. The note lands in docs/quirks/ or docs/notes/ (git-shareable) and is
    immediately retrievable via carta_search and proactive recall.

    Returns:
        {"status": "ok", "path", "collection", "chunks"} or {"error", "detail"}.
    """
    return _remember(text, note_type=note_type, title=title, tags=tags)
```

(Match the file's existing import of `ConfigError` — add it to the `from carta.config
import ...` line if not already imported.)

- [ ] **Step 4: Run tests** — `python3 -m pytest carta/tests/test_mcp_server.py -v` → all PASS.

- [ ] **Step 5: Commit**

```bash
git add carta/mcp/server.py carta/tests/test_mcp_server.py
git commit -m "feat(mcp): carta_remember tool — capture quirks/notes from Claude mid-session"
```

---

### Task 6: CLI `carta remember` + AGENTS.md bootstrap text

**Files:**
- Modify: `carta/cli.py` (new `cmd_remember` near `cmd_eval`; subparser near the eval parser; dispatch dict)
- Modify: `carta/install/bootstrap.py:487-493` (AGENTS.md template text)
- Test: `carta/tests/test_cli.py` (append class)

- [ ] **Step 1: Write the failing tests** — append to `carta/tests/test_cli.py`:

```python
class TestCmdRemember:
    def _args(self, **kw):
        import argparse
        kw.setdefault("text", "the bench PSU must be on")
        kw.setdefault("type", "quirk")
        kw.setdefault("title", "")
        kw.setdefault("tags", "")
        return argparse.Namespace(**kw)

    def _run(self, args, capture_result=None, capture_error=None):
        from unittest.mock import patch
        from carta.cli import cmd_remember
        cfg = {"project_name": "p", "qdrant_url": "http://localhost:6333"}
        kwargs = {}
        if capture_error:
            kwargs["side_effect"] = capture_error
        else:
            kwargs["return_value"] = capture_result or {
                "path": "docs/quirks/2026-06-11-x.md", "collection": "p_notes", "chunks": 2}
        with patch("carta.cli.find_config", return_value=Path("/fake/.carta/config.yaml")), \
             patch("carta.config.load_config", return_value=cfg), \
             patch("carta.memory.capture.capture_note", **kwargs) as cap:
            try:
                cmd_remember(args)
            except SystemExit as e:
                return e.code, cap
        return None, cap

    def test_happy_path_prints_path_and_collection(self, capsys):
        code, cap = self._run(self._args(tags="bench, can"))
        out = capsys.readouterr().out
        assert code is None
        assert "docs/quirks/2026-06-11-x.md" in out
        assert "p_notes" in out
        # comma-string tags become a list
        assert cap.call_args.kwargs["tags"] == ["bench", "can"]

    def test_no_tags_passes_none(self):
        code, cap = self._run(self._args(tags=""))
        assert cap.call_args.kwargs["tags"] is None

    def test_capture_error_exits_1(self, capsys):
        code, _ = self._run(self._args(), capture_error=ValueError("bad"))
        assert code == 1
        assert "bad" in capsys.readouterr().err
```

- [ ] **Step 2: Run to verify failure** — `python3 -m pytest carta/tests/test_cli.py::TestCmdRemember -v` → FAIL (`ImportError: cmd_remember`).

- [ ] **Step 3: Implement** in `carta/cli.py`.

Command handler (after `cmd_eval`):

```python
def cmd_remember(args):
    """Save a curated project note (quirk/bug-note/helpful-note) and embed it."""
    from carta.config import load_config
    from carta.memory.capture import capture_note
    cfg_path = find_config()
    cfg = load_config(cfg_path)
    repo_root = cfg_path.parent.parent
    tags = [t.strip() for t in (args.tags or "").split(",") if t.strip()] or None
    try:
        result = capture_note(cfg, repo_root, args.text, note_type=args.type,
                              title=args.title, tags=tags)
    except (ValueError, RuntimeError) as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    print(f"✓ Note saved: {result['path']} → {result['collection']} "
          f"({result['chunks']} chunks)")
```

Subparser (next to the eval parser registration):

```python
    remember_p = sub.add_parser(
        "remember",
        help="Save a project note (quirk/bug-note/helpful-note) and embed it",
    )
    remember_p.add_argument("text", help="The note text")
    remember_p.add_argument(
        "--type", choices=["quirk", "bug-note", "helpful-note"],
        default="helpful-note", help="Note type (default: helpful-note)",
    )
    remember_p.add_argument("--title", default="", help="Optional title (drives the filename slug)")
    remember_p.add_argument("--tags", default="", help="Comma-separated tags")
```

Dispatch dict: add `"remember": cmd_remember,`.

`carta/install/bootstrap.py` (~line 487) — replace the `### /session-memory <text>` AGENTS.md
block (heading, description, example) with:

```markdown
### Saving project notes

Use the `carta_remember` MCP tool (or `carta remember "text" --type quirk`) to save durable
project knowledge — surprising quirks, bug-investigation findings, helpful notes. Notes are
written to docs/quirks/ or docs/notes/ (git-shareable) and are immediately searchable.
```

- [ ] **Step 4: Run tests** — `python3 -m pytest carta/tests/test_cli.py carta/tests/test_bootstrap.py -v` → all PASS.

- [ ] **Step 5: Commit**

```bash
git add carta/cli.py carta/install/bootstrap.py carta/tests/test_cli.py
git commit -m "feat(cli): carta remember command; AGENTS.md documents real capture surfaces"
```

---

### Task 7: Recall labeling (`[quirk]` prefixes in search + hook)

**Files:**
- Modify: `carta/embed/pipeline.py:1613-1620` (text-hit dict gains `doc_type`)
- Modify: `carta/cli.py` `cmd_search` result loop
- Modify: `carta/hook/hook.py` `_inject`
- Test: `carta/tests/test_pipeline.py`, `carta/hook/tests/test_hook.py` (append)

- [ ] **Step 1: Write the failing tests.**

Append to `carta/tests/test_pipeline.py` (inside `TestRunSearchRerankStats` is wrong scope —
add a standalone test next to `TestRunSearch`, reusing the same mock pattern):

```python
class TestRunSearchDocType:
    def test_text_hits_carry_doc_type_from_payload(self):
        from unittest.mock import patch, MagicMock
        from carta.embed.pipeline import run_search

        point = MagicMock()
        point.score = 0.9
        point.payload = {"file_path": "docs/quirks/x.md", "text": "alpha",
                         "doc_type": "quirk"}
        mock_client = MagicMock()
        mock_client.query_points.return_value = MagicMock(points=[point])
        cfg = {"project_name": "p", "qdrant_url": "http://localhost:6333",
               "embed": {"ollama_url": "u", "ollama_model": "m", "colpali_enabled": False},
               "search": {"top_n": 5}, "modules": {"doc_search": True}}

        with patch("carta.embed.pipeline.QdrantClient", return_value=mock_client), \
             patch("carta.embed.pipeline.get_embedding", return_value=[0.0] * 768), \
             patch("carta.embed.pipeline.collection_is_hybrid", return_value=False), \
             patch("carta.search.scoped.get_search_collections", return_value=["p_doc"]), \
             patch("carta.embed.pipeline.find_config", return_value="/fake/.carta/config.yaml"):
            results = run_search("q", cfg)

        assert results and results[0]["doc_type"] == "quirk"
```

Append to `carta/hook/tests/test_hook.py`:

```python
def test_inject_labels_note_hits():
    """Recalled notes are labeled with their type so Claude can tell curated memory
    from plain docs; plain docs stay unlabeled."""
    hits = [
        {"score": 0.92, "source": "docs/quirks/2026-06-11-psu.md",
         "excerpt": "bench PSU must be on", "doc_type": "quirk"},
        {"score": 0.91, "source": "docs/CAN/TOPOLOGY.md",
         "excerpt": "two CAN buses", "doc_type": ""},
    ]
    cfg = _make_cfg()
    with (
        patch("sys.stdin", _stdin("query")),
        patch("carta.hook.hook.find_config", return_value=Path("/fake/.carta/config.yaml")),
        patch("carta.hook.hook.load_config", return_value=cfg),
        patch("carta.hook.hook.run_search", return_value=hits),
    ):
        out = _capture_main()
    data = json.loads(out.strip())
    assert "[quirk] docs/quirks/2026-06-11-psu.md" in data["context"]
    assert "[" not in data["context"].split("TOPOLOGY.md")[0].split("Source: ")[-1] or \
           "Source: docs/CAN/TOPOLOGY.md" in data["context"]
```

(Simplify the second assertion to `assert "Source: docs/CAN/TOPOLOGY.md" in data["context"]`
— the plain doc keeps its unprefixed form.)

- [ ] **Step 2: Run to verify failure** — both new tests FAIL (`doc_type` key missing / no `[quirk]` label).

- [ ] **Step 3: Implement.**

`carta/embed/pipeline.py` — in the text-collection result loop (~line 1615), add the key:

```python
                    coll_results.append({
                        "score": r.score,
                        "source": payload.get("file_path", payload.get("slug", "")),
                        "excerpt": payload.get("text", ""),
                        "type": "text",
                        "doc_type": payload.get("doc_type", ""),
                    })
```

`carta/hook/hook.py` — extend the config import line to
`from carta.config import find_config, load_config, NOTE_DOC_TYPES`, and in `_inject`:

```python
    context_lines = ["## Relevant documentation\n"]
    for h in hits:
        tag = f"[{h['doc_type']}] " if h.get("doc_type") in NOTE_DOC_TYPES else ""
        context_lines.append(
            f"**Source: {tag}{h['source']} (score: {h['score']:.2f})**\n"
            f"> {h['excerpt'][:200]}\n"
        )
```

`carta/cli.py` `cmd_search` — replace the result loop:

```python
    from carta.config import NOTE_DOC_TYPES
    for r in results:
        tag = f"[{r['doc_type']}] " if r.get("doc_type") in NOTE_DOC_TYPES else ""
        print(f"[{r['score']:.2f}] {tag}{r['source']} — {r['excerpt']}")
```

- [ ] **Step 4: Run tests** — `python3 -m pytest carta/tests/test_pipeline.py carta/hook/tests/ carta/tests/test_cli.py -v` → all PASS.

- [ ] **Step 5: Commit**

```bash
git add carta/embed/pipeline.py carta/hook/hook.py carta/cli.py carta/tests/test_pipeline.py carta/hook/tests/test_hook.py
git commit -m "feat(search,hook): label recalled notes ([quirk]/[bug-note]/[helpful-note])"
```

---

### Task 8: README + CHANGELOG

**Files:**
- Modify: `CHANGELOG.md` (new 0.10.0 section above 0.9.1)
- Modify: `README.md` (new "Capturing notes" section; place it after the search/reranking material, before "Graph-aware retrieval" or another natural seam — read the surrounding structure first)

- [ ] **Step 1: CHANGELOG** — insert above `## [0.9.1]`:

```markdown
## [0.10.0] — 2026-06-11

### Added
- **Note capture — the write side of session memory.** `carta_remember` (MCP tool) and
  `carta remember` (CLI) save curated project knowledge as plain markdown files with
  `doc_type` frontmatter — `quirk` → `docs/quirks/`, `bug-note`/`helpful-note` →
  `docs/notes/` (paths configurable via `memory.quirks_dir`/`memory.notes_dir`) — and embed
  them into `{project}_notes` through the standard pipeline. Notes are git-shareable repo
  docs: they show up in `carta scan`/audit, export with `carta export`, and survive
  re-embeds. Search results and proactive-recall injections label them (`[quirk] …`).
- **Frontmatter `doc_type` override.** A `doc_type:` key in markdown frontmatter now wins
  over parent-directory inference, and `quirks/` / `notes/` directories map to note types —
  hand-written notes route correctly on (re-)embed.

### Fixed
- **`collection_for_doc_type` was dead code — note types never reached `_notes`.**
  `upsert_chunks` hardcoded the `_doc` collection; it now routes by the batch's doc_type.
  `carta init` creates `{project}_notes` (instead of the never-used `_quirk`); existing
  projects need no migration — the collection is auto-created on first capture.
```

- [ ] **Step 2: README** — add a section (and a one-line mention in the features/skills table if one lists capabilities):

```markdown
### Capturing notes (quirks, bug notes, helpful notes)

The write side of session memory. When you (or Claude) learn something durable about the
project, save it:

​```bash
carta remember "EZKontrol bench tests silently fail unless motor CAN is powered" \
  --type quirk --title "EZKontrol bench power" --tags can,bench
​```

or from Claude via the `carta_remember` MCP tool. Notes are plain markdown files with
`doc_type` frontmatter — `quirk` → `docs/quirks/`, `bug-note`/`helpful-note` → `docs/notes/`
(configurable via `memory.quirks_dir` / `memory.notes_dir`) — embedded into
`{project}_notes` and retrieved by the same hybrid search, reranker, and proactive-recall
hook as your docs. Search output labels them: `[quirk] docs/quirks/2026-06-11-….md`.

Notes are knowledge artifacts, not tool state: git-shareable, audited by `carta scan`
(staleness, links), exported by `carta export`, and still useful if you remove Carta.
Hand-written files work too — drop a markdown file in `docs/quirks/` (or set
`doc_type: quirk` in frontmatter anywhere) and `carta embed` routes it correctly.
```

(Strip the zero-width characters around the inner code fence when inserting.)

- [ ] **Step 3: Commit**

```bash
git add CHANGELOG.md README.md
git commit -m "docs: README + CHANGELOG for 0.10.0 note capture"
```

---

### Task 9: Full suite, review, live validation, release

- [ ] **Step 1: Full suite** — `python3 -m pytest carta/ -q` → all pass (baseline 770 passed, 2 skipped + new tests).
- [ ] **Step 2: Final code review** of `git diff main...HEAD` (superpowers:requesting-code-review); fix findings.
- [ ] **Step 3: Live validation** (in this repo, which has a live `.carta` + Qdrant):
  - `PYTHONPATH=<worktree> ~/.local/pipx/venvs/carta-cc/bin/python -m carta remember "test quirk from live validation" --type quirk --title "Live validation quirk"` → file in `docs/quirks/`, output names `doc-audit-cc_notes`.
  - Verify vectors: scroll `doc-audit-cc_notes` for the new file_path.
  - `... -m carta search "live validation quirk"` → result labeled `[quirk]`.
  - Remove the test note file + its sidecar and delete the test points (or leave the note if it documents something real).
- [ ] **Step 4: Release** — push branch, open PR (`gh pr create`), wait CI green, squash-merge, `git -C /Users/ian/dev/doc-audit-cc pull`, tag `v0.10.0` on the merge commit, push tag, watch release.yml, verify PyPI `carta-cc` 0.10.0 + GitHub Release, `pipx install --force carta-cc==0.10.0`.
