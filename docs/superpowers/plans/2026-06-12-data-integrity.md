# Carta v0.11.0 Data Integrity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the point-ID collision and empty-chunk fail-open data-loss bugs, add corpus-integrity detection to `carta doctor` and repair via `carta embed --repair`, per the approved spec at `docs/superpowers/specs/2026-06-12-data-integrity-design.md`.

**Architecture:** Point IDs become path-based (md5 of repo-relative file_path + chunk_index + generation) so same-stem files can no longer overwrite each other. Re-embeds stamp a `doc_generation` on every chunk and finish by deleting that file's points from other generations (organic migration of legacy IDs). Empty-text chunks are dropped at the `upsert_chunks` choke point; files yielding zero usable chunks are flagged `extraction_failed` instead of silently embedding garbage. A new `carta/embed/integrity.py` scanner powers both a read-only doctor section and the `embed --repair` flow.

**Tech Stack:** Python 3.10+, qdrant-client, pytest with `unittest.mock.MagicMock` for Qdrant and `patch("carta.embed.embed.requests.post")` for Ollama (existing convention in `carta/tests/test_embed.py`).

**Worktree:** Execute in an isolated worktree (branch `data-integrity`) created via superpowers:using-git-worktrees, matching prior release cycles.

**Verification baseline:** Before Task 1, run `python3 -m pytest carta/ -q` and record the pass count (expected: 798 passed, 2 skipped).

---

## Background for the implementer (read first)

Today `carta/embed/embed.py` derives Qdrant point IDs from the filename-stem slug:

```python
def _point_id(slug: str, chunk_index: int) -> str:
    raw = f"{slug}:{chunk_index}"           # "readme:0" — same for EVERY README.md!

def _point_id_versioned(slug: str, chunk_index: int, generation: int) -> str:
    raw = f"{slug}:{chunk_index}:g{generation}"

def _visual_point_id(slug: str, page_num: int) -> str:
    raw = f"{slug}:visual:{page_num}"
```

Four `README.md` files in the ET-embed corpus share slug `readme`; each embed
overwrites the previous file's points. Chunks already carry a repo-relative
`file_path` key (set in `_embed_one_file`, `pipeline.py:343`), which is the
correct collision-free ID basis.

Separately, `upsert_chunks` happily embeds chunks whose `text` is empty
(43 fully-empty files / ~1,400 identical garbage vectors in ET-embed), and
`run_embed_file` (`pipeline.py:1146`) merges `lifecycle_updates` containing
`status: "stale"` OVER the success updates, leaving every re-embedded sidecar
permanently `stale`.

Chunk dicts currently do NOT carry `doc_generation` (so `build_point` falls
into the legacy `_point_id` branch; the payload default `doc_generation: 1`
comes from `build_point` itself). Task 2 starts stamping it.

---

### Task 1: Path-based point IDs

**Files:**
- Modify: `carta/embed/embed.py` (`_point_id_versioned` ~line 160, `_visual_point_id` ~line 312, `build_point` ~line 203, visual `point_id` ~line 379; delete `_point_id` ~line 154)
- Test: `carta/tests/test_embed.py`

- [ ] **Step 1: Write the failing tests**

Add to `carta/tests/test_embed.py` (and update the import line to drop `_point_id`, which this task removes — see Step 3):

```python
from carta.embed.embed import _point_id_versioned, _visual_point_id, upsert_chunks


class TestPathBasedPointIds:
    """Same-stem files must never share point IDs (the README-collision bug)."""

    def test_same_stem_different_paths_get_distinct_ids(self):
        id_a = _point_id_versioned("docs/ci/README.md", 0, 1)
        id_b = _point_id_versioned("docs/diagrams/README.md", 0, 1)
        assert id_a != id_b

    def test_id_is_deterministic(self):
        assert (_point_id_versioned("docs/ci/README.md", 3, 2)
                == _point_id_versioned("docs/ci/README.md", 3, 2))

    def test_visual_same_stem_different_paths_distinct(self):
        id_a = _visual_point_id("docs/a/spec.pdf", 1)
        id_b = _visual_point_id("docs/b/spec.pdf", 1)
        assert id_a != id_b

    @patch("carta.embed.embed.requests.post")
    def test_upsert_uses_file_path_for_point_id(self, mock_post):
        """build_point derives the ID from chunk['file_path'], not slug."""
        mock_post.return_value.status_code = 200
        mock_post.return_value.json.return_value = {"embedding": [0.1] * 768}
        mock_client = MagicMock()
        mock_client.collection_exists.return_value = True
        # Force legacy (non-hybrid) schema so no fastembed import is needed
        info = MagicMock()
        info.config.params.vectors = None
        info.config.params.sparse_vectors = None
        mock_client.get_collection.return_value = info

        cfg = {
            "project_name": "test",
            "qdrant_url": "http://localhost:6333",
            "embed": {
                "ollama_url": "http://localhost:11434",
                "ollama_model": "nomic-embed-text:latest",
                "embedding_workers": 1,
            },
        }
        chunk = {"slug": "readme", "file_path": "docs/ci/README.md",
                 "chunk_index": 0, "text": "hello world", "doc_type": "unknown"}
        upsert_chunks([chunk], cfg, client=mock_client)

        points = mock_client.upsert.call_args.kwargs["points"]
        expected = _point_id_versioned("docs/ci/README.md", 0, 1)
        assert points[0].id == expected
```

Note: `collection_is_hybrid` inspects `info.config.params.vectors` /
`.sparse_vectors`; check how existing tests in this file fake non-hybrid
collections and reuse that exact pattern if it differs from the above.

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest carta/tests/test_embed.py -q`
Expected: the new tests FAIL (`upsert_uses_file_path_for_point_id` asserts a
path-based ID that the current slug-based code does not produce). Existing
`TestPointIdVersioned` tests still pass (signature unchanged, semantics of the
first argument change from slug to key).

- [ ] **Step 3: Implement**

In `carta/embed/embed.py`:

```python
def _point_id_versioned(key: str, chunk_index: int, generation: int) -> str:
    """Deterministic UUID from key + chunk_index + generation.

    `key` is the repo-relative file_path (collision-free); legacy points used
    the filename-stem slug, which collided across same-stem files. Different
    generations produce different UUIDs, enabling retries without collisions.
    """
    raw = f"{key}:{chunk_index}:g{generation}"
    return str(uuid.UUID(hashlib.md5(raw.encode()).hexdigest()))


def _visual_point_id(key: str, page_num: int) -> str:
    """Deterministic UUID for visual page embeddings, keyed by file path."""
    raw = f"{key}:visual:{page_num}"
    return str(uuid.UUID(hashlib.md5(raw.encode()).hexdigest()))
```

Delete `_point_id` entirely. In `build_point` (inside `upsert_chunks`), replace the ID branch:

```python
        id_key = chunk.get("file_path") or chunk["slug"]
        point_id = _point_id_versioned(
            id_key, chunk["chunk_index"], chunk.get("doc_generation", 1)
        )
```

In `upsert_visual_pages`, replace the ID line:

```python
            id_key = page.get("file_path") or page["slug"]
            point_id = _visual_point_id(id_key, page["page_num"])
```

Then `grep -rn "_point_id\b" carta/` — update any remaining caller/import
(tests included) to `_point_id_versioned`. Update the old
`TestPointIdVersioned.test_point_id_versioned_differs_from_point_id` test:
it imported `_point_id`; replace it with a check that two different keys
differ (or delete it — the new class covers it).

- [ ] **Step 4: Run the full suite**

Run: `python3 -m pytest carta/ -q`
Expected: PASS (798+4 new, 2 skipped). If other tests pinned `_point_id`,
update them deliberately — the scheme change is the point of this task.

- [ ] **Step 5: Commit**

```bash
git add carta/embed/embed.py carta/tests/test_embed.py
git commit -m "fix(embed): derive point IDs from file path, not filename-stem slug

Same-stem files (README.md x4 in a real corpus) overwrote each other's
Qdrant points because IDs hashed only slug:chunk_index. IDs now hash the
repo-relative file_path. Visual page IDs fixed identically."
```

---

### Task 2: Stamp doc_generation on chunks + delete other generations after upsert

**Files:**
- Modify: `carta/embed/pipeline.py` (`_embed_one_file` metadata ~line 341 and both `image_chunks` literals ~lines 467, 501; `run_embed_file` ~line 1128)
- Modify: `carta/embed/lifecycle.py` (new function)
- Test: `carta/tests/test_lifecycle.py`

- [ ] **Step 1: Write the failing tests**

Add to `carta/tests/test_lifecycle.py`:

```python
from unittest.mock import MagicMock
from carta.embed.lifecycle import delete_other_generations


class TestDeleteOtherGenerations:
    def test_deletes_by_file_path_excluding_current_generation(self):
        client = MagicMock()
        delete_other_generations(client, "proj_doc", "docs/ci/README.md", keep_generation=3)

        client.delete.assert_called_once()
        kwargs = client.delete.call_args.kwargs
        assert kwargs["collection_name"] == "proj_doc"
        sel = kwargs["points_selector"]
        # must: file_path == docs/ci/README.md ; must_not: doc_generation == 3
        assert sel.must[0].key == "file_path"
        assert sel.must[0].match.value == "docs/ci/README.md"
        assert sel.must_not[0].key == "doc_generation"
        assert sel.must_not[0].match.value == 3

    def test_swallows_qdrant_errors(self):
        """Cleanup is best-effort: a delete failure must not fail the embed."""
        client = MagicMock()
        client.delete.side_effect = RuntimeError("boom")
        delete_other_generations(client, "proj_doc", "x.md", keep_generation=1)  # no raise
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest carta/tests/test_lifecycle.py -q`
Expected: FAIL with `ImportError: cannot import name 'delete_other_generations'`.

- [ ] **Step 3: Implement the lifecycle helper**

In `carta/embed/lifecycle.py` (it already imports `Filter`, `FieldCondition`, `MatchValue` — verify and extend the import if needed):

```python
def delete_other_generations(
    client, collection_name: str, rel_path: str, keep_generation: int
) -> None:
    """Delete a file's points from every generation except keep_generation.

    Runs after a successful upsert so stale-generation chunks (and any
    legacy slug-keyed points for this file) stop appearing in search.
    Best-effort: errors are reported but never fail the embed that
    just succeeded.
    """
    selector = Filter(
        must=[FieldCondition(key="file_path", match=MatchValue(value=rel_path))],
        must_not=[FieldCondition(key="doc_generation", match=MatchValue(value=keep_generation))],
    )
    try:
        client.delete(collection_name=collection_name, points_selector=selector)
    except Exception as e:
        print(f"Warning: stale-generation cleanup failed for {rel_path} — {e}", flush=True)
```

- [ ] **Step 4: Stamp doc_generation and wire the cleanup**

In `_embed_one_file` (`pipeline.py` ~line 341), add generation to the shared metadata:

```python
    generation = int(file_info.get("generation") or 1)
    metadata = {
        "slug": slug,
        "file_path": str(file_path.relative_to(repo_root)),
        "doc_type": file_info.get("doc_type", "unknown"),
        "doc_generation": generation,
    }
```

Add `"doc_generation": generation,` to BOTH `image_chunks.append({...})`
literals (~lines 467 and 501) — image-description chunks share the file's
file_path, so if they carried a different generation the cleanup below would
delete them.

At the END of `_embed_one_file`, just before the `return`, after all upserts
(text + image chunks) have happened:

```python
    from carta.embed.lifecycle import delete_other_generations
    from carta.config import collection_for_doc_type
    if count + image_chunk_count > 0:
        coll = collection_for_doc_type(cfg, file_info.get("doc_type", "unknown"))
        delete_other_generations(
            client, coll, str(file_path.relative_to(repo_root)), generation
        )
```

(Image chunks land in `_doc` via `collection_for_doc_type(cfg, "image_description")`
— same collection as non-note text chunks, so one cleanup call covers both. Note
files route to `_notes`; their doc_type comes from `file_info`, so the same line
picks the right collection.)

In `run_embed_file` (~line 1128), pass the new generation through:

```python
    file_info = {
        "slug": sidecar_data.get("slug", file_path.stem),
        "doc_type": sidecar_data.get("doc_type", "unknown"),
        "sidecar_path": sc_path,
        "file_path": file_path,
        "generation": new_generation,
    }
```

The bulk `run_embed` path embeds files discovered as pending (first embed) —
its `file_info` has no `generation` key, so chunks default to generation 1 and
the cleanup is a harmless no-op delete. Verify `run_embed`'s `file_info`
construction (around `pipeline.py:1152-1370`) and leave it unchanged.

- [ ] **Step 5: Add a pipeline-level test**

Add to `carta/tests/test_embed_targeted.py` (check its existing fixtures for
how `run_embed_file`/`_embed_one_file` are exercised; follow that pattern). If
no convenient harness exists, test at the unit seam instead — assert that
`_embed_one_file` calls `delete_other_generations` with the file's relative
path and generation, with `upsert_chunks` patched:

```python
@patch("carta.embed.pipeline.upsert_chunks", return_value=2)
@patch("carta.embed.lifecycle.delete_other_generations")
def test_embed_one_file_cleans_other_generations(self, mock_del, mock_upsert, tmp_path):
    from carta.embed.pipeline import _embed_one_file
    repo = tmp_path
    doc = repo / "docs" / "a.md"
    doc.parent.mkdir(parents=True)
    doc.write_text("# Title\n\nsome content here\n")
    cfg = {
        "project_name": "test", "qdrant_url": "http://localhost:6333",
        "embed": {"ollama_url": "http://x", "ollama_model": "m"},
    }
    file_info = {"slug": "a", "doc_type": "unknown", "generation": 2}
    count, updates = _embed_one_file(doc, file_info, cfg, MagicMock(), repo, 800, 0.15)
    mock_del.assert_called_once()
    args = mock_del.call_args.args
    assert args[2] == "docs/a.md"
    assert args[3] == 2
```

Adjust the patch target if `_embed_one_file` imports the helper inside the
function body (`carta.embed.lifecycle.delete_other_generations` is correct for
an inline `from carta.embed.lifecycle import delete_other_generations`).

- [ ] **Step 6: Run the full suite**

Run: `python3 -m pytest carta/ -q`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add carta/embed/pipeline.py carta/embed/lifecycle.py carta/tests/
git commit -m "feat(embed): stamp doc_generation on chunks and delete stale generations after upsert

Old-generation points were only payload-stamped stale and stayed searchable
forever. Re-embeds now end with a filtered delete of the file's other
generations, which also organically migrates legacy slug-keyed points."
```

---

### Task 3: Empty-chunk guard

**Files:**
- Modify: `carta/embed/embed.py` (`upsert_chunks` top)
- Modify: `carta/embed/pipeline.py` (`_embed_one_file` after `enriched` is built)
- Test: `carta/tests/test_embed.py`, `carta/tests/test_embed_targeted.py`

- [ ] **Step 1: Write the failing tests**

In `carta/tests/test_embed.py`:

```python
class TestEmptyChunkGuard:
    @patch("carta.embed.embed.requests.post")
    def test_empty_chunks_are_not_upserted(self, mock_post):
        mock_post.return_value.status_code = 200
        mock_post.return_value.json.return_value = {"embedding": [0.1] * 768}
        mock_client = MagicMock()
        mock_client.collection_exists.return_value = True
        info = MagicMock()
        info.config.params.vectors = None
        info.config.params.sparse_vectors = None
        mock_client.get_collection.return_value = info
        cfg = {
            "project_name": "test", "qdrant_url": "http://localhost:6333",
            "embed": {"ollama_url": "http://x", "ollama_model": "m",
                      "embedding_workers": 1},
        }
        chunks = [
            {"slug": "d", "file_path": "d.pdf", "chunk_index": 0, "text": ""},
            {"slug": "d", "file_path": "d.pdf", "chunk_index": 1, "text": "   \n"},
            {"slug": "d", "file_path": "d.pdf", "chunk_index": 2, "text": "real content"},
        ]
        count = upsert_chunks(chunks, cfg, client=mock_client)
        assert count == 1
        points = mock_client.upsert.call_args.kwargs["points"]
        assert len(points) == 1
        assert points[0].payload["text"] == "real content"

    def test_all_empty_returns_zero_without_upsert(self):
        mock_client = MagicMock()
        mock_client.collection_exists.return_value = True
        cfg = {
            "project_name": "test", "qdrant_url": "http://localhost:6333",
            "embed": {"ollama_url": "http://x", "ollama_model": "m"},
        }
        chunks = [{"slug": "d", "file_path": "d.pdf", "chunk_index": 0, "text": ""}]
        count = upsert_chunks(chunks, cfg, client=mock_client)
        assert count == 0
        mock_client.upsert.assert_not_called()
```

In `carta/tests/test_embed_targeted.py` (same harness style as Task 2 Step 5):

```python
@patch("carta.embed.pipeline.upsert_chunks", return_value=0)
def test_zero_usable_chunks_marks_extraction_failed(self, mock_upsert, tmp_path, capsys):
    from carta.embed.pipeline import _embed_one_file
    repo = tmp_path
    doc = repo / "docs" / "scan.md"
    doc.parent.mkdir(parents=True)
    doc.write_text("")  # extraction yields nothing
    cfg = {
        "project_name": "test", "qdrant_url": "http://localhost:6333",
        "embed": {"ollama_url": "http://x", "ollama_model": "m"},
    }
    count, updates = _embed_one_file(doc, {"slug": "scan", "doc_type": "unknown"},
                                     cfg, MagicMock(), repo, 800, 0.15)
    assert count == 0
    assert updates["status"] == "extraction_failed"
    assert "0 extractable characters" in capsys.readouterr().out
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest carta/tests/test_embed.py carta/tests/test_embed_targeted.py -q`
Expected: FAIL — empty chunks are currently embedded; no `extraction_failed` status exists.

- [ ] **Step 3: Implement**

Top of `upsert_chunks` in `embed.py`, before `batch_doc_type` is computed:

```python
    n_total = len(chunks)
    chunks = [c for c in chunks if (c.get("text") or "").strip()]
    n_dropped = n_total - len(chunks)
    if n_dropped:
        src = chunks[0].get("file_path") if chunks else "(all chunks)"
        print(f"Warning: dropped {n_dropped} empty chunk(s) for {src} — "
              f"extraction produced no text for them", flush=True)
    if not chunks:
        return 0
```

In `_embed_one_file` (`pipeline.py`), right after `enriched` is built and
before `upsert_chunks` is called, detect the nothing-usable case for markdown
and PDFs alike:

```python
    usable = [c for c in enriched if (c.get("text") or "").strip()]
    if not usable and file_path.suffix != ".pdf":
        print(f"Warning: {file_path.name}: 0 extractable characters — skipped "
              f"(empty or unreadable file)", flush=True)
        return 0, {"status": "extraction_failed", "chunk_count": 0,
                   "image_count": 0, "image_chunks": 0}
    count = upsert_chunks(enriched, cfg, client=client)
```

For PDFs, image-description chunks may still rescue the file, so the check
happens at the END of `_embed_one_file` instead — in the existing
`sidecar_updates` construction (~line 523), set status when nothing usable was
upserted:

```python
    sidecar_updates = {
        "chunk_count": count + image_chunk_count,
        ...existing keys...
    }
    if count + image_chunk_count == 0:
        sidecar_updates["status"] = "extraction_failed"
        print(f"Warning: {file_path.name}: 0 extractable characters — skipped "
              f"(scanned PDF? OCR may be required)", flush=True)
```

Read the actual `sidecar_updates` construction at `pipeline.py:520-557` first
and integrate (don't duplicate keys; the markdown early-return above must
return the same key shape).

Also make `run_embed` count these: in its per-file result handling
(find where `summary` totals embedded/skipped/errors, ~`pipeline.py:1152+`),
treat `status == "extraction_failed"` from the sidecar updates as its own
counter `extraction_failed` and include it in the returned summary dict and
the printed summary line. Follow the existing summary-shape exactly — read it
before editing; if threading the status through is invasive, count files whose
`_embed_one_file` returned `(0, {"status": "extraction_failed", ...})`.

- [ ] **Step 4: Run the full suite**

Run: `python3 -m pytest carta/ -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add carta/embed/embed.py carta/embed/pipeline.py carta/tests/
git commit -m "fix(embed): never upsert empty chunks; flag zero-text files extraction_failed

Silent extraction failures previously produced unfindable points with
identical embedding-of-empty-string vectors (1,400+ in a real corpus)."
```

---

### Task 4: Sidecar status bookkeeping + true force re-embed

**Files:**
- Modify: `carta/embed/pipeline.py` (`run_embed_file` ~lines 1074-1148)
- Test: `carta/tests/test_embed_targeted.py` (or wherever `run_embed_file` is currently tested — `grep -rn "run_embed_file" carta/tests/`)

- [ ] **Step 1: Write the failing tests**

```python
class TestRunEmbedFileLifecycle:
    def _cfg(self):
        return {
            "project_name": "test", "qdrant_url": "http://localhost:6333",
            "embed": {"ollama_url": "http://x", "ollama_model": "m"},
        }

    @patch("carta.embed.pipeline.QdrantClient")
    @patch("carta.embed.pipeline.ensure_collection")
    @patch("carta.embed.pipeline._embed_one_file", return_value=(3, {"chunk_count": 3}))
    @patch("carta.embed.pipeline.find_config")
    def test_successful_embed_ends_current_not_stale(
            self, mock_fc, mock_embed, mock_ensure, mock_qc, tmp_path):
        from carta.embed.pipeline import run_embed_file
        from carta.embed.induct import sidecar_path, read_sidecar
        repo = tmp_path
        (repo / ".carta").mkdir()
        mock_fc.return_value = repo / ".carta" / "config.yaml"
        doc = repo / "docs" / "a.md"
        doc.parent.mkdir(parents=True)
        doc.write_text("# hi\n\ncontent\n")

        result = run_embed_file(doc, self._cfg(), force=True)
        assert result["status"] == "ok"
        sc = read_sidecar(sidecar_path(doc, repo))
        assert sc["status"] == "current"
        assert sc["stale_as_of"] is None

    @patch("carta.embed.pipeline.QdrantClient")
    @patch("carta.embed.pipeline.ensure_collection")
    @patch("carta.embed.pipeline._embed_one_file", return_value=(2, {"chunk_count": 2}))
    @patch("carta.embed.pipeline.find_config")
    def test_force_reembeds_even_when_hash_unchanged(
            self, mock_fc, mock_embed, mock_ensure, mock_qc, tmp_path):
        from carta.embed.pipeline import run_embed_file
        repo = tmp_path
        (repo / ".carta").mkdir()
        mock_fc.return_value = repo / ".carta" / "config.yaml"
        doc = repo / "docs" / "a.md"
        doc.parent.mkdir(parents=True)
        doc.write_text("# hi\n\ncontent\n")

        first = run_embed_file(doc, self._cfg(), force=True)
        assert first["status"] == "ok"
        second = run_embed_file(doc, self._cfg(), force=True)  # hash unchanged
        assert second["status"] == "ok"          # currently returns "skipped"
        assert mock_embed.call_count == 2
```

Check the sidecar helper signatures (`sidecar_path(file_path, repo_root)`,
`read_sidecar(path)`) in `carta/embed/induct.py` before writing; adapt if they
differ. `run_embed_file` also calls `mark_sidecar_stale` and creates a
`QdrantClient` — both patched above.

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest carta/tests/test_embed_targeted.py -q`
Expected: FAIL — first test sees `status: "stale"`; second sees `"skipped"` on the second call.

- [ ] **Step 3: Implement**

In `run_embed_file`:

1. Hash short-circuit (~line 1082): only when not forced —

```python
    if not force and current_hash == old_hash and old_hash is not None:
```

2. Lifecycle updates (~line 1112): remove `"status": "stale"` and
   `"stale_as_of": ...` from `lifecycle_updates` (the in-Qdrant stale stamp via
   `mark_sidecar_stale` stays — it marks the OLD generation's points during the
   re-embed window).

3. Final merge (~line 1146): after `_embed_one_file` returns, set the closing
   state — `_embed_one_file` may have set `status: extraction_failed` (Task 3),
   which must win:

```python
    sidecar_updates.update(lifecycle_updates)
    sidecar_updates.setdefault("status", "current")
    sidecar_updates["stale_as_of"] = None
    sidecar_updates.pop("_vision_events", None)
    _update_sidecar(sc_path, sidecar_updates)
```

(`setdefault` keeps `extraction_failed` when present; otherwise `current`.
With the order above, also remove `status`/`stale_as_of` from
`lifecycle_updates` so they can't clobber — verify by reading the final dict
flow.)

- [ ] **Step 4: Run the full suite**

Run: `python3 -m pytest carta/ -q`
Expected: PASS. Some existing lifecycle tests may assert the old
`status: stale` end-state — update them deliberately (the old behavior is the
bug).

- [ ] **Step 5: Commit**

```bash
git add carta/embed/pipeline.py carta/tests/
git commit -m "fix(embed): successful re-embed ends status=current; force bypasses hash short-circuit

lifecycle_updates previously clobbered the success state, leaving every
re-embedded sidecar permanently 'stale' (169/971 in a real corpus)."
```

---

### Task 5: Corpus-integrity scanner module

**Files:**
- Create: `carta/embed/integrity.py`
- Test: `carta/tests/test_integrity.py` (new)

- [ ] **Step 1: Write the failing tests**

Create `carta/tests/test_integrity.py`:

```python
"""Tests for corpus-integrity scanning (doctor + embed --repair)."""
from unittest.mock import MagicMock

from carta.embed.integrity import scan_corpus_integrity


def _point(file_path, slug, chunk_index, text):
    p = MagicMock()
    p.payload = {"file_path": file_path, "slug": slug,
                 "chunk_index": chunk_index, "text": text}
    return p


def _client_with_points(points):
    client = MagicMock()
    client.collection_exists.return_value = True
    client.scroll.return_value = (points, None)  # single page
    return client


CFG = {"project_name": "test", "qdrant_url": "http://localhost:6333",
       "embed": {"ollama_url": "http://x", "ollama_model": "m"}}


class TestScanCorpusIntegrity:
    def test_detects_slug_collisions(self, tmp_path):
        pts = [
            _point("docs/ci/README.md", "readme", 8, "a"),
            _point("docs/diagrams/README.md", "readme", 0, "b"),
            _point("docs/unique.md", "unique", 0, "c"),
        ]
        report = scan_corpus_integrity(CFG, tmp_path, client=_client_with_points(pts))
        assert report["slug_collisions"] == {
            "readme": sorted(["docs/ci/README.md", "docs/diagrams/README.md"])}

    def test_detects_empty_text_files(self, tmp_path):
        pts = [
            _point("docs/scan.pdf", "scan", 0, ""),
            _point("docs/scan.pdf", "scan", 1, ""),
            _point("docs/partial.pdf", "partial", 0, ""),
            _point("docs/partial.pdf", "partial", 1, "real"),
            _point("docs/ok.md", "ok", 0, "fine"),
        ]
        report = scan_corpus_integrity(CFG, tmp_path, client=_client_with_points(pts))
        assert report["empty_files"] == ["docs/scan.pdf"]
        assert report["partial_empty_files"] == {"docs/partial.pdf": 1}

    def test_clean_corpus_reports_nothing(self, tmp_path):
        pts = [_point("docs/ok.md", "ok", 0, "fine")]
        report = scan_corpus_integrity(CFG, tmp_path, client=_client_with_points(pts))
        assert report["slug_collisions"] == {}
        assert report["empty_files"] == []
        assert report["partial_empty_files"] == {}
        assert report["affected_files"] == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest carta/tests/test_integrity.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'carta.embed.integrity'`.

- [ ] **Step 3: Implement**

Create `carta/embed/integrity.py`:

```python
"""Corpus-integrity scanning: detect point-ID collisions, empty-text points,
sidecar/Qdrant count mismatches, and stuck-stale sidecars.

Read-only — used by `carta doctor` (report) and `carta embed --repair`
(which re-embeds/purges what this module finds).
"""
from __future__ import annotations

from collections import defaultdict
from pathlib import Path

from qdrant_client import QdrantClient

from carta.config import collection_name
from carta.embed.lifecycle import compute_file_hash
from carta.embed.induct import read_sidecar


def _scroll_all(client, coll: str):
    """Yield every point's payload in a collection (no vectors)."""
    offset = None
    while True:
        points, offset = client.scroll(
            coll, limit=1000, offset=offset, with_payload=True, with_vectors=False
        )
        for p in points:
            yield p.payload or {}
        if offset is None:
            return


def scan_corpus_integrity(cfg: dict, repo_root: Path, client=None) -> dict:
    """Scan the project's _doc collection and sidecars for integrity issues.

    Returns a dict:
        slug_collisions:      {slug: [file_path, ...]} for slugs with >1 file
        empty_files:          [file_path] — every point has empty text
        partial_empty_files:  {file_path: n_empty} — some points empty
        count_mismatches:     {file_path: {"sidecar": n, "qdrant": n}}
        stuck_stale:          [rel sidecar source path] — status stale, hash matches disk
        affected_files:       sorted union of files needing re-embed/purge
    """
    if client is None:
        client = QdrantClient(url=cfg["qdrant_url"], timeout=30)
    coll = collection_name(cfg, "doc")

    slug_files: dict[str, set] = defaultdict(set)
    per_file_counts: dict[str, int] = defaultdict(int)
    per_file_empty: dict[str, int] = defaultdict(int)

    if client.collection_exists(coll):
        for payload in _scroll_all(client, coll):
            fp = payload.get("file_path", "")
            slug_files[payload.get("slug", "")].add(fp)
            per_file_counts[fp] += 1
            if not (payload.get("text") or "").strip():
                per_file_empty[fp] += 1

    slug_collisions = {
        slug: sorted(files) for slug, files in slug_files.items() if len(files) > 1
    }
    empty_files = sorted(
        fp for fp, n in per_file_empty.items() if n == per_file_counts[fp]
    )
    partial_empty_files = {
        fp: n for fp, n in sorted(per_file_empty.items())
        if 0 < n < per_file_counts[fp]
    }

    # Sidecar-side checks
    count_mismatches: dict[str, dict] = {}
    stuck_stale: list[str] = []
    sidecars_root = repo_root / ".carta" / "sidecars"
    if sidecars_root.exists():
        for sc_path in sidecars_root.rglob("*.embed-meta.yaml"):
            sc = read_sidecar(sc_path) or {}
            rel = sc.get("current_path")
            if not rel:
                continue
            src = repo_root / rel
            if rel in per_file_counts and sc.get("chunk_count") not in (None, per_file_counts[rel]):
                count_mismatches[rel] = {
                    "sidecar": sc.get("chunk_count"), "qdrant": per_file_counts[rel]
                }
            if sc.get("status") == "stale" and src.exists():
                try:
                    if compute_file_hash(src) == sc.get("file_hash"):
                        stuck_stale.append(rel)
                except OSError:
                    pass

    affected = set(empty_files) | set(partial_empty_files) | set(count_mismatches)
    for files in slug_collisions.values():
        affected.update(files)

    return {
        "slug_collisions": slug_collisions,
        "empty_files": empty_files,
        "partial_empty_files": partial_empty_files,
        "count_mismatches": count_mismatches,
        "stuck_stale": sorted(stuck_stale),
        "affected_files": sorted(affected),
    }
```

Verify the sidecar key for the relative source path: the inspected ET-embed
sidecars use `current_path: docs/ci/README.md`. Confirm via
`grep -n "current_path" carta/embed/induct.py` and adapt if the canonical key
differs. Likewise confirm `compute_file_hash`'s import location
(`carta/embed/lifecycle.py:` per `pipeline.py`'s import).

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest carta/tests/test_integrity.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add carta/embed/integrity.py carta/tests/test_integrity.py
git commit -m "feat(integrity): corpus-integrity scanner (collisions, empty points, mismatches, stuck-stale)"
```

---

### Task 6: Doctor integration (read-only report)

**Files:**
- Modify: `carta/cli.py` (`cmd_doctor`, ~line 441)
- Test: `carta/tests/test_cli.py`

- [ ] **Step 1: Write the failing test**

Add to `carta/tests/test_cli.py` (match its existing patching style — read the
top of the file first; it sets PYTHONPATH/uses runner helpers):

```python
class TestDoctorCorpusIntegrity:
    @patch("carta.embed.integrity.scan_corpus_integrity")
    @patch("carta.cli.find_config")
    def test_doctor_prints_integrity_section_inside_project(
            self, mock_fc, mock_scan, tmp_path, capsys, monkeypatch):
        from carta import cli
        (tmp_path / ".carta").mkdir()
        cfg_file = tmp_path / ".carta" / "config.yaml"
        cfg_file.write_text("project_name: test\nqdrant_url: http://localhost:6333\n")
        mock_fc.return_value = cfg_file
        mock_scan.return_value = {
            "slug_collisions": {"readme": ["docs/a/README.md", "docs/b/README.md"]},
            "empty_files": ["docs/scan.pdf"],
            "partial_empty_files": {},
            "count_mismatches": {},
            "stuck_stale": [],
            "affected_files": ["docs/a/README.md", "docs/b/README.md", "docs/scan.pdf"],
        }
        # Bypass the preflight machinery: patch PreflightChecker to a no-op
        with patch("carta.install.preflight.PreflightChecker") as mock_chk:
            result = MagicMock()
            result.fixable_failures = []
            result.to_json.return_value = "{}"
            mock_chk.return_value.run.return_value = result
            args = MagicMock(json=False, fix=False, yes=True, verbose=False)
            cli.cmd_doctor(args)
        out = capsys.readouterr().out
        assert "Corpus integrity" in out
        assert "readme" in out
        assert "docs/scan.pdf" in out
        assert "carta embed --repair" in out
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest carta/tests/test_cli.py -q -k integrity`
Expected: FAIL — no integrity section printed.

- [ ] **Step 3: Implement**

At the end of `cmd_doctor` in `carta/cli.py` (after the existing preflight
report/fix flow), append a project-scoped integrity section. Doctor must keep
working outside a carta project, so everything is guarded:

```python
    # Corpus integrity (project-scoped, read-only). Never break doctor itself.
    try:
        cfg_path = find_config()
    except Exception:
        cfg_path = None
    if cfg_path is not None:
        try:
            from carta.config import load_config
            from carta.embed.integrity import scan_corpus_integrity
            cfg = load_config(cfg_path)
            repo_root = cfg_path.parent.parent
            report = scan_corpus_integrity(cfg, repo_root)
            if args.json:
                import json as _json
                print(_json.dumps({"corpus_integrity": report}))
            else:
                print("\n📦 Corpus integrity")
                if not report["affected_files"] and not report["stuck_stale"]:
                    print("  ✅ no issues found")
                else:
                    for slug, files in report["slug_collisions"].items():
                        print(f"  ⚠️  slug collision '{slug}': {', '.join(files)}")
                    for fp in report["empty_files"]:
                        print(f"  ⚠️  all chunks empty: {fp}")
                    for fp, n in report["partial_empty_files"].items():
                        print(f"  ⚠️  {n} empty chunk(s): {fp}")
                    for fp, c in report["count_mismatches"].items():
                        print(f"  ⚠️  count mismatch: {fp} (sidecar {c['sidecar']} vs qdrant {c['qdrant']})")
                    if report["stuck_stale"]:
                        print(f"  ⚠️  {len(report['stuck_stale'])} sidecar(s) stuck 'stale' with unchanged files")
                    print(f"  → run `carta embed --repair` to fix "
                          f"({len(report['affected_files'])} file(s) affected)")
        except Exception as e:
            if not args.json:
                print(f"\n📦 Corpus integrity: check skipped ({e})")
```

Note on `--json`: the existing flow prints `result.to_json()` separately;
keeping integrity as its own JSON object on a separate line is acceptable for
this release (doctor's JSON consumers are tests only — verify with
`grep -rn "doctor" carta/tests/ | grep -i json`). If a test asserts the whole
stdout is one JSON document, merge instead: parse `result.to_json()`, add the
`corpus_integrity` key, print once.

- [ ] **Step 4: Run the full suite**

Run: `python3 -m pytest carta/ -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add carta/cli.py carta/tests/test_cli.py
git commit -m "feat(doctor): read-only corpus-integrity section (collisions, empty points, mismatches)"
```

---

### Task 7: `carta embed --repair`

**Files:**
- Create: `carta/embed/repair.py`
- Modify: `carta/cli.py` (embed parser ~line 661; `cmd_embed` ~line 179)
- Test: `carta/tests/test_integrity.py` (repair tests live with integrity tests)

- [ ] **Step 1: Write the failing tests**

Add to `carta/tests/test_integrity.py`:

```python
from carta.embed.repair import run_repair


class TestRunRepair:
    @patch("carta.embed.repair.run_embed_file")
    @patch("carta.embed.repair.scan_corpus_integrity")
    @patch("carta.embed.repair.QdrantClient")
    def test_repair_deletes_points_then_reembeds_each_affected_file(
            self, mock_qc, mock_scan, mock_reembed, tmp_path):
        mock_scan.return_value = {
            "slug_collisions": {"readme": ["docs/a/README.md", "docs/b/README.md"]},
            "empty_files": [], "partial_empty_files": {}, "count_mismatches": {},
            "stuck_stale": [],
            "affected_files": ["docs/a/README.md", "docs/b/README.md"],
        }
        mock_reembed.return_value = {"status": "ok", "chunks": 5}
        for rel in ("docs/a/README.md", "docs/b/README.md"):
            f = tmp_path / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text("# x\ncontent\n")

        summary = run_repair(tmp_path, CFG)

        client = mock_qc.return_value
        assert client.delete.call_count == 2          # one purge per file
        assert mock_reembed.call_count == 2
        assert summary["repaired"] == 2
        assert summary["purged_only"] == 0

    @patch("carta.embed.repair.run_embed_file")
    @patch("carta.embed.repair.scan_corpus_integrity")
    @patch("carta.embed.repair.QdrantClient")
    def test_missing_file_is_purged_not_reembedded(
            self, mock_qc, mock_scan, mock_reembed, tmp_path):
        mock_scan.return_value = {
            "slug_collisions": {}, "empty_files": ["docs/gone.pdf"],
            "partial_empty_files": {}, "count_mismatches": {}, "stuck_stale": [],
            "affected_files": ["docs/gone.pdf"],
        }
        summary = run_repair(tmp_path, CFG)
        assert mock_qc.return_value.delete.call_count == 1
        mock_reembed.assert_not_called()
        assert summary["purged_only"] == 1

    @patch("carta.embed.repair.run_embed_file")
    @patch("carta.embed.repair.scan_corpus_integrity")
    @patch("carta.embed.repair.QdrantClient")
    def test_extraction_failed_counts_as_flagged(
            self, mock_qc, mock_scan, mock_reembed, tmp_path):
        mock_scan.return_value = {
            "slug_collisions": {}, "empty_files": ["docs/scan.pdf"],
            "partial_empty_files": {}, "count_mismatches": {}, "stuck_stale": [],
            "affected_files": ["docs/scan.pdf"],
        }
        f = tmp_path / "docs" / "scan.pdf"
        f.parent.mkdir(parents=True)
        f.write_bytes(b"%PDF-1.4 fake")
        mock_reembed.return_value = {"status": "ok", "chunks": 0}
        summary = run_repair(tmp_path, CFG)
        assert summary["flagged"] == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest carta/tests/test_integrity.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'carta.embed.repair'`.

- [ ] **Step 3: Implement**

Create `carta/embed/repair.py`:

```python
"""carta embed --repair: fix corpus-integrity issues found by integrity.scan.

Per affected file: delete ALL of its points (any generation, any legacy ID),
then force re-embed through the fixed pipeline. Files that no longer exist on
disk, or whose extraction yields nothing, end up purged + flagged rather than
re-upserted. Stuck-stale sidecars get their status corrected in place.
"""
from __future__ import annotations

from pathlib import Path

from qdrant_client import QdrantClient
from qdrant_client.models import FieldCondition, Filter, MatchValue

from carta.config import collection_name
from carta.embed.integrity import scan_corpus_integrity
from carta.embed.pipeline import run_embed_file


def _delete_file_points(client, coll: str, rel_path: str) -> None:
    selector = Filter(
        must=[FieldCondition(key="file_path", match=MatchValue(value=rel_path))]
    )
    client.delete(collection_name=coll, points_selector=selector)


def run_repair(repo_root: Path, cfg: dict, verbose: bool = True) -> dict:
    """Detect and repair corpus-integrity issues. Returns a summary dict."""
    client = QdrantClient(url=cfg["qdrant_url"], timeout=30)
    report = scan_corpus_integrity(cfg, repo_root, client=client)
    coll = collection_name(cfg, "doc")

    repaired = purged_only = flagged = failed = 0
    for rel in report["affected_files"]:
        src = repo_root / rel
        if verbose:
            print(f"  repairing {rel}...", flush=True)
        try:
            _delete_file_points(client, coll, rel)
        except Exception as e:
            print(f"  Warning: could not purge points for {rel} — {e}", flush=True)
        if not src.exists():
            purged_only += 1
            if verbose:
                print(f"    purged (file no longer on disk)", flush=True)
            continue
        try:
            result = run_embed_file(src, cfg, force=True)
            if result.get("chunks", 0) > 0:
                repaired += 1
            else:
                flagged += 1   # extraction_failed: purged + sidecar flagged
        except Exception as e:
            failed += 1
            print(f"  Error: re-embed failed for {rel} — {e}", flush=True)

    # Stuck-stale sidecars that aren't otherwise affected: fix status in place.
    stale_fixed = 0
    affected = set(report["affected_files"])
    if report["stuck_stale"]:
        from carta.embed.induct import sidecar_path
        from carta.embed.pipeline import _update_sidecar
        for rel in report["stuck_stale"]:
            if rel in affected:
                continue  # re-embed above already rewrote it
            sc = sidecar_path(repo_root / rel, repo_root)
            if sc.exists():
                _update_sidecar(sc, {"status": "current", "stale_as_of": None})
                stale_fixed += 1

    summary = {
        "affected": len(report["affected_files"]),
        "repaired": repaired,
        "purged_only": purged_only,
        "flagged": flagged,
        "failed": failed,
        "stale_fixed": stale_fixed,
    }
    if verbose:
        print(
            f"Repair complete: {repaired} re-embedded, {purged_only} purged, "
            f"{flagged} flagged extraction_failed, {failed} failed, "
            f"{stale_fixed} stale sidecar(s) corrected.",
            flush=True,
        )
    return summary
```

In `carta/cli.py`, add the flag to the embed parser (~line 661 block):

```python
    embed_p.add_argument(
        "--repair",
        action="store_true",
        help="Detect and repair corpus-integrity issues (point-ID collisions, "
             "empty chunks, count mismatches), then exit.",
    )
```

In `cmd_embed`, immediately after the module-enabled check (before the
`--visual` branch), add:

```python
    if getattr(args, "repair", False) is True:
        from carta.embed.repair import run_repair
        repo_root = cfg_path.parent.parent
        summary = run_repair(repo_root, cfg, verbose=True)
        _notify_if_update(cfg_path, cfg)
        sys.exit(1 if summary["failed"] else 0)
```

- [ ] **Step 4: Run the full suite**

Run: `python3 -m pytest carta/ -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add carta/embed/repair.py carta/cli.py carta/tests/test_integrity.py
git commit -m "feat(embed): carta embed --repair — purge + force re-embed integrity-affected files"
```

---

### Task 8: Docs, version bump, changelog

**Files:**
- Modify: `carta/__init__.py` (version), `pyproject.toml` (if version duplicated there — check), `CHANGELOG.md`, `README.md`

- [ ] **Step 1: Bump version**

`carta/__init__.py`: `__version__ = "0.11.0"`. Run
`grep -rn "0\.10\.0" pyproject.toml carta/__init__.py` and update every
version source (prior releases synced both).

- [ ] **Step 2: CHANGELOG entry**

Add at the top of `CHANGELOG.md`, matching its existing format:

```markdown
## 0.11.0 — data integrity

### Fixed
- **Point-ID collision (data loss):** point IDs hashed only the filename-stem
  slug, so same-stem files (e.g. multiple `README.md`) silently overwrote each
  other's Qdrant points. IDs now hash the repo-relative file path. Visual page
  IDs fixed identically.
- **Empty-chunk fail-open:** PDF extraction failures produced points with empty
  text and identical garbage vectors (unfindable, dense-space noise). Empty
  chunks are now dropped at upsert; files yielding zero text are flagged
  `extraction_failed` with a loud warning instead of embedding nothing.
- **Stale-generation leak:** re-embeds now delete the file's points from prior
  generations after a successful upsert (previously they stayed searchable
  forever). This also organically migrates legacy slug-keyed points.
- **Sidecar status bookkeeping:** a successful re-embed now ends with
  `status: current` instead of permanently `stale`; `carta embed FILE` with
  force now truly re-embeds even when the file hash is unchanged.

### Added
- `carta doctor` corpus-integrity section: detects slug collisions, empty-text
  points, sidecar/Qdrant chunk-count mismatches, and stuck-stale sidecars.
- `carta embed --repair`: purges and force re-embeds affected files.
```

- [ ] **Step 3: README**

Add a short "Corpus integrity" subsection to README.md near the doctor/embed
docs describing `carta doctor` detection + `carta embed --repair` (3-6 lines,
match surrounding tone). Run `grep -n "doctor" README.md` to find the spot.

- [ ] **Step 4: Run the full suite, commit**

Run: `python3 -m pytest carta/ -q` → PASS.

```bash
git add carta/__init__.py pyproject.toml CHANGELOG.md README.md
git commit -m "chore: bump to 0.11.0, changelog + README for data-integrity release"
```

---

### Task 9: Branch finish — review, PR, merge, release

- [ ] **Step 1: Integration review** — use superpowers:requesting-code-review against the spec (`docs/superpowers/specs/2026-06-12-data-integrity-design.md`); fix valid findings, rebut invalid ones with evidence.
- [ ] **Step 2: Full suite + version sanity**

Run: `python3 -m pytest carta/ -q` → all pass.
Run: `python3 -c "import carta; print(carta.__version__)"` → `0.11.0`.

- [ ] **Step 3: PR** — push branch, `gh pr create` titled "Data integrity: path-based point IDs, empty-chunk guard, doctor/repair (v0.11.0)", body summarizing the four fixes + two features, linking issue #19 and the spec. Watch CI.
- [ ] **Step 4: Merge + release** — squash-merge, tag `v0.11.0`, confirm release workflow publishes to PyPI (expect the known ~15-min simple-index lag; use `pipx install --force carta-cc` per the recorded quirk).

---

### Task 10: ET-embed corpus repair + eval validation (post-release)

All commands run from `~/School/Elementrailer/ET-embed` with carta-cc 0.11.0
installed (or `PYTHONPATH=<checkout> ~/.local/pipx/venvs/carta-cc/bin/python -m carta ...`
to validate pre-release).

- [ ] **Step 1: Detection sanity** — `carta doctor`: expect the integrity section to report 1 slug collision (`readme`, 4 files), 43 fully-empty files, ~24 partial-empty, count mismatches incl. `docs/ci/README.md` (sidecar 10 vs qdrant 2), and ~169 stuck-stale sidecars. Numbers should match the 2026-06-12 investigation.
- [ ] **Step 2: Repair** — `carta embed --repair`. Expect: READMEs re-embedded with distinct points; ~1,400+ garbage points purged; scanned patents flagged `extraction_failed`; stuck-stale sidecars corrected. Re-run `carta doctor` → integrity clean except `extraction_failed` files (still listed as empty until OCR work — acceptable; they have no points now, so they should drop out of the empty-files list and appear only via their sidecar status).
- [ ] **Step 3: Eval expect fix** — in `.carta/eval/et-embed.yaml`, change the
  termination query's expectation:

```yaml
  # grounded: vcu/pcb-design-checklist.md — CAN_A/CAN_B termination table, PESD2CAN TVS
  - q: "what are the CAN bus termination rules for CAN_A and CAN_B on the VCU PCB and where does each 120 ohm resistor go"
    expect: ["vcu/pcb-design-checklist"]
```

- [ ] **Step 4: Re-run the 62-query eval** — hybrid-only and reranked
  (`OMP_NUM_THREADS=1`, rerank block per the config comment). Expect at
  minimum: ci/README and the pcb query flip to hits (reranked ≥ 56/62,
  recall@5 ≥ 0.90). US-11965795 remains a miss (honest, flagged). Record both
  runs in `.carta/eval/RESULTS.md` with a dated section attributing deltas to
  the v0.11.0 repair.
- [ ] **Step 5: Issue bookkeeping** — comment on #19 with the investigation
  findings table + new numbers; file v0.12.0 issues: visual pool dilution
  (Bug C), reranker demotion (Bug D), OCR recovery for extraction_failed PDFs.

---

## Self-review notes (already applied)

- Spec §1-§7 each map to Tasks 1-7+10; Bug E is Task 4; release mechanics Task 8-9.
- Type/name consistency: `_point_id_versioned(key, chunk_index, generation)`,
  `delete_other_generations(client, collection_name, rel_path, keep_generation)`,
  `scan_corpus_integrity(cfg, repo_root, client=None)`,
  `run_repair(repo_root, cfg, verbose=True)` — used identically across tasks.
- Known soft spots called out inline rather than hidden: exact
  `sidecar_updates` shape in `_embed_one_file` (Task 3 Step 3), doctor JSON
  merging (Task 6 Step 3), sidecar `current_path` key name (Task 5 Step 3) —
  each step says what to verify and how.
