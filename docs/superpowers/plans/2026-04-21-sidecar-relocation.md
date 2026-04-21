# Sidecar Relocation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move all `.embed-meta.yaml` sidecar files from co-located (next to source docs) to `.carta/sidecars/` mirroring the repo directory structure, with auto-migration and orphan detection.

**Architecture:** Add a `sidecar_path(file, repo_root)` pure function in `induct.py` as the single source of truth for sidecar path computation. Migrate and discovery are centralized in `pipeline.py`. Scanner and audit are updated to scope rglob to `.carta/sidecars/`. Auto-migration runs at the top of `run_embed()` using `shutil.move`.

**Tech Stack:** Python pathlib, shutil, PyYAML, pytest

---

## File Map

| File | Change |
|------|--------|
| `carta/embed/induct.py` | Add `sidecar_path()` helper; update `write_sidecar()` signature to accept `repo_root` and create parent dirs |
| `carta/embed/pipeline.py` | Import `sidecar_path`; replace all hardcoded sidecar path constructions; scope rglob to `.carta/sidecars/`; add `migrate_sidecars()` and `detect_orphaned_sidecars()`; wire both into `run_embed()` |
| `carta/scanner/scanner.py` | Import `sidecar_path as get_sidecar_path`; update `_iter_sidecar_files()` to scope under `.carta/sidecars/`; update `check_embed_induction_needed()`, `check_embed_drift()`, `check_embed_transcript_unprocessed()` |
| `carta/audit/audit.py` | Scope `_build_sidecar_registry()` discovery to `.carta/sidecars/`; replace string-replace source inference with `current_path` field lookup; add `detect_missing_source_sidecars()` and wire into `run_audit()` |
| `carta/embed/tests/test_embed.py` | Update `test_write_sidecar_creates_yaml`, `test_sidecar_round_trip`; add `test_sidecar_path_*` tests |
| `carta/tests/test_pipeline.py` | Update all sidecar path constructions; add `current_path` to test sidecar dicts; add `migrate_sidecars` and `detect_orphaned_sidecars` tests |
| `carta/tests/test_mcp_server.py` | Update sidecar path constructions in `TestDiscoverStaleFilesIntegration` |
| `carta/scanner/tests/test_scanner.py` | Update sidecar path constructions |
| `carta/audit/tests/test_audit.py` | Update sidecar path constructions; add `detect_missing_source_sidecars` test |

---

## Task 1: `sidecar_path()` helper + `write_sidecar()` update

**Files:**
- Modify: `carta/embed/induct.py`
- Modify: `carta/embed/tests/test_embed.py`

- [ ] **Step 1: Write the failing tests for `sidecar_path()`**

In `carta/embed/tests/test_embed.py`, add after the existing `generate_sidecar_stub` tests:

```python
# ---------------------------------------------------------------------------
# induct.py — sidecar_path()
# ---------------------------------------------------------------------------

def test_sidecar_path_mirrors_repo_structure(tmp_path):
    file_path = tmp_path / "docs" / "manuals" / "chip.pdf"
    result = sidecar_path(file_path, tmp_path)
    expected = tmp_path / ".carta" / "sidecars" / "docs" / "manuals" / "chip.embed-meta.yaml"
    assert result == expected


def test_sidecar_path_markdown(tmp_path):
    file_path = tmp_path / "docs" / "guide.md"
    result = sidecar_path(file_path, tmp_path)
    expected = tmp_path / ".carta" / "sidecars" / "docs" / "guide.embed-meta.yaml"
    assert result == expected


def test_sidecar_path_at_repo_root(tmp_path):
    file_path = tmp_path / "README.md"
    result = sidecar_path(file_path, tmp_path)
    expected = tmp_path / ".carta" / "sidecars" / "README.embed-meta.yaml"
    assert result == expected
```

Also update the import line at the top of `test_embed.py` to add `sidecar_path`:
```python
from carta.embed.induct import (
    slug_from_filename, infer_doc_type, generate_sidecar_stub,
    write_sidecar, read_sidecar, sidecar_path,
)
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /Users/ian/dev/doc-audit-cc && python -m pytest carta/embed/tests/test_embed.py::test_sidecar_path_mirrors_repo_structure carta/embed/tests/test_embed.py::test_sidecar_path_markdown carta/embed/tests/test_embed.py::test_sidecar_path_at_repo_root -v
```

Expected: FAIL with `ImportError: cannot import name 'sidecar_path'`

- [ ] **Step 3: Implement `sidecar_path()` in `carta/embed/induct.py`**

Add after the `_PATH_TYPE_MAP` dict and before `slug_from_filename`:

```python
def sidecar_path(file_path: Path, repo_root: Path) -> Path:
    """Return the canonical .carta/sidecars/ path for a source file's sidecar."""
    rel = file_path.relative_to(repo_root)
    return repo_root / ".carta" / "sidecars" / rel.with_suffix(".embed-meta.yaml")
```

- [ ] **Step 4: Run tests to verify `sidecar_path` passes**

```bash
cd /Users/ian/dev/doc-audit-cc && python -m pytest carta/embed/tests/test_embed.py::test_sidecar_path_mirrors_repo_structure carta/embed/tests/test_embed.py::test_sidecar_path_markdown carta/embed/tests/test_embed.py::test_sidecar_path_at_repo_root -v
```

Expected: PASS

- [ ] **Step 5: Update `write_sidecar()` signature and body in `carta/embed/induct.py`**

Replace the current `write_sidecar` function:

```python
def write_sidecar(file_path: Path, stub: dict, repo_root: Path) -> Path:
    """Write sidecar YAML to .carta/sidecars/ mirroring repo structure. Returns the sidecar path."""
    path = sidecar_path(file_path, repo_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        yaml.dump(stub, f, default_flow_style=False, sort_keys=False)
    return path
```

- [ ] **Step 6: Update `test_write_sidecar_creates_yaml` and `test_sidecar_round_trip` in `carta/embed/tests/test_embed.py`**

Replace:
```python
def test_write_sidecar_creates_yaml(tmp_path):
    f = tmp_path / "chip.pdf"
    f.touch()
    stub = {"slug": "chip", "status": "pending", "doc_type": "datasheet"}
    path = write_sidecar(f, stub)
    assert path.exists()
    assert path.name == "chip.embed-meta.yaml"


def test_sidecar_round_trip(tmp_path):
    f = tmp_path / "chip.pdf"
    f.touch()
    stub = {"slug": "chip", "status": "pending", "doc_type": "datasheet", "notes": ""}
    sidecar_path = write_sidecar(f, stub)
    loaded = read_sidecar(sidecar_path)
    assert loaded == stub
```

With:
```python
def test_write_sidecar_creates_yaml(tmp_path):
    repo_root = tmp_path
    f = tmp_path / "docs" / "chip.pdf"
    f.parent.mkdir()
    f.touch()
    stub = {"slug": "chip", "status": "pending", "doc_type": "datasheet"}
    path = write_sidecar(f, stub, repo_root)
    assert path.exists()
    assert path == tmp_path / ".carta" / "sidecars" / "docs" / "chip.embed-meta.yaml"


def test_sidecar_round_trip(tmp_path):
    repo_root = tmp_path
    f = tmp_path / "docs" / "chip.pdf"
    f.parent.mkdir()
    f.touch()
    stub = {"slug": "chip", "status": "pending", "doc_type": "datasheet", "notes": ""}
    sc_path = write_sidecar(f, stub, repo_root)
    loaded = read_sidecar(sc_path)
    assert loaded == stub
```

- [ ] **Step 7: Run all induct tests**

```bash
cd /Users/ian/dev/doc-audit-cc && python -m pytest carta/embed/tests/test_embed.py -k "sidecar" -v
```

Expected: all sidecar tests PASS

- [ ] **Step 8: Commit**

```bash
cd /Users/ian/dev/doc-audit-cc && git add carta/embed/induct.py carta/embed/tests/test_embed.py && git commit -m "feat(induct): add sidecar_path() helper and update write_sidecar() to use .carta/sidecars/"
```

---

## Task 2: Update `pipeline.py` — path constructions and discovery

**Files:**
- Modify: `carta/embed/pipeline.py`
- Modify: `carta/tests/test_pipeline.py`

- [ ] **Step 1: Update imports in `carta/embed/pipeline.py`**

Add `sidecar_path` to the induct import and add `shutil`:

```python
import shutil  # add at top with other stdlib imports

from carta.embed.induct import generate_sidecar_stub, read_sidecar, write_sidecar, sidecar_path
```

- [ ] **Step 2: Replace `discover_pending_files()` in `carta/embed/pipeline.py`**

Replace the entire function:

```python
def discover_pending_files(repo_root: Path) -> list[dict]:
    """Find all sidecars under .carta/sidecars/ with status: pending.

    Returns list of dicts: sidecar data + 'sidecar_path' + 'file_path'.
    """
    results = []
    sidecars_root = repo_root / ".carta" / "sidecars"
    if not sidecars_root.exists():
        return results
    for sc_path in sidecars_root.rglob("*.embed-meta.yaml"):
        data = read_sidecar(sc_path)
        if data is None or data.get("status") != "pending":
            continue
        current_path = data.get("current_path")
        if not current_path:
            continue
        source_file = repo_root / current_path
        if source_file.exists():
            results.append({**data, "sidecar_path": sc_path, "file_path": source_file})
    return results
```

- [ ] **Step 3: Replace `discover_stale_files()` in `carta/embed/pipeline.py`**

Replace the entire function:

```python
def discover_stale_files(repo_root: Path) -> list[Path]:
    """Find all files with sidecars under .carta/sidecars/ marked status: stale.

    Returns list of source file paths.
    """
    results = []
    sidecars_root = repo_root / ".carta" / "sidecars"
    if not sidecars_root.exists():
        return results
    for sc_path in sidecars_root.rglob("*.embed-meta.yaml"):
        data = read_sidecar(sc_path)
        if data is None or data.get("status") != "stale":
            continue
        current_path = data.get("current_path")
        if not current_path:
            continue
        source_file = repo_root / current_path
        if source_file.exists():
            results.append(source_file)
    return results
```

- [ ] **Step 4: Replace `_heal_sidecar_current_paths()` in `carta/embed/pipeline.py`**

Replace the entire function:

```python
def _heal_sidecar_current_paths(repo_root: Path, verbose: bool = False) -> int:
    """Add current_path to sidecars under .carta/sidecars/ that are missing the field."""
    healed = 0
    sidecars_root = repo_root / ".carta" / "sidecars"
    if not sidecars_root.exists():
        return healed
    for sc_path in sidecars_root.rglob("*.embed-meta.yaml"):
        data = read_sidecar(sc_path)
        if data is None or "current_path" in data:
            continue
        # Infer source path from sidecar's mirror position
        rel_from_sidecars = sc_path.relative_to(sidecars_root)
        stem = sc_path.name.replace(".embed-meta.yaml", "")
        parent_dirs = rel_from_sidecars.parent
        for ext in _SUPPORTED_EXTENSIONS:
            candidate = repo_root / parent_dirs / f"{stem}{ext}"
            if candidate.exists():
                data["current_path"] = str(parent_dirs / f"{stem}{ext}")
                _update_sidecar(sc_path, data)
                healed += 1
                break
    if verbose and healed:
        print(f"carta embed: healed {healed} sidecar(s) missing current_path", flush=True)
    return healed
```

- [ ] **Step 5: Update `run_embed_file()` in `carta/embed/pipeline.py`**

Find and replace the three sidecar path lines in `run_embed_file` (around lines 546, 551, 554, 567, 631):

Replace:
```python
    sidecar_path = file_path.parent / (file_path.stem + ".embed-meta.yaml")

    # Generate sidecar if it doesn't exist
    if not sidecar_path.exists():
        stub = generate_sidecar_stub(file_path, repo_root, cfg)
        write_sidecar(file_path, stub)

    # Read sidecar for file_info
    sidecar_data = read_sidecar(sidecar_path) or {}
```

With:
```python
    sc_path = sidecar_path(file_path, repo_root)

    # Generate sidecar if it doesn't exist
    if not sc_path.exists():
        stub = generate_sidecar_stub(file_path, repo_root, cfg)
        write_sidecar(file_path, stub, repo_root)

    # Read sidecar for file_info
    sidecar_data = read_sidecar(sc_path) or {}
```

Also replace all remaining `sidecar_path` local variable references in `run_embed_file` with `sc_path`:

```python
    # Replace:
    if current_hash == old_hash and old_hash is not None:
        _update_sidecar(sidecar_path, { ...
    # With:
    if current_hash == old_hash and old_hash is not None:
        _update_sidecar(sc_path, { ...
```

```python
    # Replace:
    file_info = {
        ...
        "sidecar_path": sidecar_path,
        ...
    }
    # With:
    file_info = {
        ...
        "sidecar_path": sc_path,
        ...
    }
```

```python
    # Replace (near end of function):
    _update_sidecar(sidecar_path, sidecar_updates)
    # With:
    _update_sidecar(sc_path, sidecar_updates)
```

- [ ] **Step 6: Update auto-induct block in `run_embed()` in `carta/embed/pipeline.py`**

Find and replace (around lines 674-679):

```python
            # Replace:
            for file_path in docs_root_path.rglob(f"*{ext}"):
                sidecar_path = file_path.parent / (file_path.stem + ".embed-meta.yaml")
                if not sidecar_path.exists():
                    stub = generate_sidecar_stub(file_path, repo_root, cfg)
                    write_sidecar(file_path, stub)
                    if verbose:
                        print(f"  inducted: {file_path.relative_to(repo_root)}", flush=True)
            # With:
            for file_path in docs_root_path.rglob(f"*{ext}"):
                sc_path = sidecar_path(file_path, repo_root)
                if not sc_path.exists():
                    stub = generate_sidecar_stub(file_path, repo_root, cfg)
                    write_sidecar(file_path, stub, repo_root)
                    if verbose:
                        print(f"  inducted: {file_path.relative_to(repo_root)}", flush=True)
```

- [ ] **Step 7: Run the pipeline tests to check for breakage**

```bash
cd /Users/ian/dev/doc-audit-cc && python -m pytest carta/tests/test_pipeline.py -v 2>&1 | head -60
```

Expected: failures in tests that construct co-located sidecars — that's expected and will be fixed in the next step.

- [ ] **Step 8: Update `TestDiscoverStaleFiles` tests in `carta/tests/test_pipeline.py`**

The `discover_stale_files` tests create co-located sidecars. Replace all three test methods in `TestDiscoverStaleFiles` with versions that put sidecars in `.carta/sidecars/` and add `current_path`:

```python
class TestDiscoverStaleFiles:
    """Test discover_stale_files helper function."""

    def test_discover_stale_files_returns_stale_paths(self, temp_repo):
        repo_root, cfg = temp_repo
        docs_dir = repo_root / "docs"
        docs_dir.mkdir()

        stale_file = docs_dir / "stale.md"
        stale_file.write_text("# Stale Document")
        sc_dir = repo_root / ".carta" / "sidecars" / "docs"
        sc_dir.mkdir(parents=True)
        with open(sc_dir / "stale.embed-meta.yaml", "w") as f:
            yaml.dump({"status": "stale", "slug": "stale", "current_path": "docs/stale.md"}, f)

        embedded_file = docs_dir / "embedded.md"
        embedded_file.write_text("# Embedded Document")
        with open(sc_dir / "embedded.embed-meta.yaml", "w") as f:
            yaml.dump({"status": "embedded", "slug": "embedded", "current_path": "docs/embedded.md"}, f)

        results = discover_stale_files(repo_root)

        assert len(results) == 1
        assert results[0] == stale_file

    def test_discover_stale_files_returns_empty_when_none_stale(self, temp_repo):
        repo_root, cfg = temp_repo
        docs_dir = repo_root / "docs"
        docs_dir.mkdir()

        embedded_file = docs_dir / "embedded.md"
        embedded_file.write_text("# Embedded Document")
        sc_dir = repo_root / ".carta" / "sidecars" / "docs"
        sc_dir.mkdir(parents=True)
        with open(sc_dir / "embedded.embed-meta.yaml", "w") as f:
            yaml.dump({"status": "embedded", "slug": "embedded", "current_path": "docs/embedded.md"}, f)

        results = discover_stale_files(repo_root)
        assert results == []

    def test_discover_stale_files_skips_missing_status(self, temp_repo):
        repo_root, cfg = temp_repo
        docs_dir = repo_root / "docs"
        docs_dir.mkdir()

        file_no_status = docs_dir / "no_status.md"
        file_no_status.write_text("# Document")
        sc_dir = repo_root / ".carta" / "sidecars" / "docs"
        sc_dir.mkdir(parents=True)
        with open(sc_dir / "no_status.embed-meta.yaml", "w") as f:
            yaml.dump({"slug": "no_status", "current_path": "docs/no_status.md"}, f)

        stale_file = docs_dir / "stale.md"
        stale_file.write_text("# Stale Document")
        with open(sc_dir / "stale.embed-meta.yaml", "w") as f:
            yaml.dump({"status": "stale", "slug": "stale", "current_path": "docs/stale.md"}, f)

        results = discover_stale_files(repo_root)
        assert len(results) == 1
        assert results[0] == stale_file
```

- [ ] **Step 9: Update `TestRunEmbedFileMinimalPath` sidecar paths in `carta/tests/test_pipeline.py`**

For each test method in `TestRunEmbedFileMinimalPath`, replace the co-located sidecar construction. The pattern to find is:

```python
        sidecar_path = test_file.parent / (test_file.stem + ".embed-meta.yaml")
        ...
        with open(sidecar_path, "w") as f:
            yaml.dump(sidecar, f)
```

Replace with (add `from carta.embed.induct import sidecar_path as get_sidecar_path` to the imports at the top of the test file, then in each test):

```python
        from carta.embed.induct import sidecar_path as get_sidecar_path
        sc_path = get_sidecar_path(test_file, repo_root)
        sc_path.parent.mkdir(parents=True, exist_ok=True)
        ...
        with open(sc_path, "w") as f:
            yaml.dump(sidecar, f)
```

Apply this pattern to all tests in `TestRunEmbedFileMinimalPath` (lines ~65, ~110, ~166, ~237, ~294).

- [ ] **Step 10: Update `TestRunEmbedStaleAlert` sidecar paths in `carta/tests/test_pipeline.py`**

Find (around line 350):
```python
        for fname in test_files:
            stem = fname.replace(".md", "")
            sidecar_path = docs_dir / (stem + ".embed-meta.yaml")
```

Replace with:
```python
        from carta.embed.induct import sidecar_path as get_sidecar_path
        for fname in test_files:
            stem = fname.replace(".md", "")
            sc_path = get_sidecar_path(docs_dir / fname, repo_root)
            sc_path.parent.mkdir(parents=True, exist_ok=True)
```

And update `with open(sidecar_path, "w")` to `with open(sc_path, "w")`.

- [ ] **Step 11: Run pipeline tests**

```bash
cd /Users/ian/dev/doc-audit-cc && python -m pytest carta/tests/test_pipeline.py -v
```

Expected: all tests PASS

- [ ] **Step 12: Commit**

```bash
cd /Users/ian/dev/doc-audit-cc && git add carta/embed/pipeline.py carta/tests/test_pipeline.py && git commit -m "feat(pipeline): scope sidecar discovery to .carta/sidecars/, use sidecar_path() throughout"
```

---

## Task 3: Add `migrate_sidecars()` to pipeline

**Files:**
- Modify: `carta/embed/pipeline.py`
- Modify: `carta/tests/test_pipeline.py`

- [ ] **Step 1: Write failing tests for `migrate_sidecars()`**

Add to `carta/tests/test_pipeline.py`:

```python
from carta.embed.pipeline import run_embed_file, run_embed, discover_stale_files, migrate_sidecars
```

Then add this test class:

```python
class TestMigrateSidecars:
    """Test migrate_sidecars() moves co-located sidecars to .carta/sidecars/."""

    def test_migrate_moves_colocated_sidecar(self, temp_repo):
        repo_root, cfg = temp_repo
        docs = repo_root / "docs"
        docs.mkdir()
        old = docs / "chip.embed-meta.yaml"
        old.write_text("slug: chip\nstatus: pending\ncurrent_path: docs/chip.pdf\n")

        migrate_sidecars(repo_root)

        expected = repo_root / ".carta" / "sidecars" / "docs" / "chip.embed-meta.yaml"
        assert expected.exists()
        assert not old.exists()

    def test_migrate_skips_already_in_carta(self, temp_repo):
        repo_root, cfg = temp_repo
        sc = repo_root / ".carta" / "sidecars" / "docs" / "chip.embed-meta.yaml"
        sc.parent.mkdir(parents=True)
        sc.write_text("slug: chip\nstatus: pending\n")

        migrate_sidecars(repo_root)

        assert sc.exists()  # unchanged

    def test_migrate_returns_count(self, temp_repo):
        repo_root, cfg = temp_repo
        docs = repo_root / "docs"
        docs.mkdir()
        (docs / "a.embed-meta.yaml").write_text("slug: a\nstatus: pending\n")
        (docs / "b.embed-meta.yaml").write_text("slug: b\nstatus: pending\n")

        count = migrate_sidecars(repo_root)

        assert count == 2

    def test_migrate_nested_preserves_directory_structure(self, temp_repo):
        repo_root, cfg = temp_repo
        nested = repo_root / "docs" / "manuals" / "sub"
        nested.mkdir(parents=True)
        old = nested / "spec.embed-meta.yaml"
        old.write_text("slug: spec\nstatus: pending\n")

        migrate_sidecars(repo_root)

        expected = repo_root / ".carta" / "sidecars" / "docs" / "manuals" / "sub" / "spec.embed-meta.yaml"
        assert expected.exists()
        assert not old.exists()
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /Users/ian/dev/doc-audit-cc && python -m pytest carta/tests/test_pipeline.py::TestMigrateSidecars -v
```

Expected: FAIL with `ImportError: cannot import name 'migrate_sidecars'`

- [ ] **Step 3: Implement `migrate_sidecars()` in `carta/embed/pipeline.py`**

Add after `_heal_sidecar_current_paths()`:

```python
def migrate_sidecars(repo_root: Path) -> int:
    """Move co-located *.embed-meta.yaml files to .carta/sidecars/. Returns count moved."""
    moved = 0
    carta_dir = repo_root / ".carta"
    for old_path in repo_root.rglob("*.embed-meta.yaml"):
        try:
            old_path.relative_to(carta_dir)
            continue  # already inside .carta/ — skip
        except ValueError:
            pass
        rel = old_path.relative_to(repo_root)
        new_path = repo_root / ".carta" / "sidecars" / rel
        new_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(old_path), str(new_path))
        print(f"  migrated: {rel} → {new_path.relative_to(repo_root)}", flush=True)
        moved += 1
    return moved
```

- [ ] **Step 4: Wire `migrate_sidecars()` into `run_embed()` in `carta/embed/pipeline.py`**

Migration is a local file operation and must run before the Qdrant check (so it runs even if Qdrant is down). Place it as the very first statement inside `run_embed()`, before the verbose print and Qdrant check:

```python
def run_embed(repo_root: Path, cfg: dict, verbose: bool = False, progress=None) -> dict:
    summary: dict = {"embedded": 0, "skipped": 0, "errors": []}

    # Migrate any co-located sidecars from old format to .carta/sidecars/
    migrate_sidecars(repo_root)

    # Pre-flight: check Qdrant reachability ...
    if verbose:
        print("carta embed: checking Qdrant connectivity...", flush=True)
    ...
```

- [ ] **Step 5: Run migration tests**

```bash
cd /Users/ian/dev/doc-audit-cc && python -m pytest carta/tests/test_pipeline.py::TestMigrateSidecars -v
```

Expected: all PASS

- [ ] **Step 6: Commit**

```bash
cd /Users/ian/dev/doc-audit-cc && git add carta/embed/pipeline.py carta/tests/test_pipeline.py && git commit -m "feat(pipeline): add migrate_sidecars() auto-migration on carta embed"
```

---

## Task 4: Add `detect_orphaned_sidecars()` to pipeline

**Files:**
- Modify: `carta/embed/pipeline.py`
- Modify: `carta/tests/test_pipeline.py`

- [ ] **Step 1: Write failing tests for `detect_orphaned_sidecars()`**

Update the import in `carta/tests/test_pipeline.py`:

```python
from carta.embed.pipeline import run_embed_file, run_embed, discover_stale_files, migrate_sidecars, detect_orphaned_sidecars
```

Add test class:

```python
class TestDetectOrphanedSidecars:
    """Test detect_orphaned_sidecars() identifies sidecars with missing source files."""

    def test_detects_sidecar_with_missing_source(self, temp_repo):
        repo_root, cfg = temp_repo
        sc_dir = repo_root / ".carta" / "sidecars" / "docs"
        sc_dir.mkdir(parents=True)
        sc = sc_dir / "deleted.embed-meta.yaml"
        sc.write_text("slug: deleted\nstatus: embedded\ncurrent_path: docs/deleted.pdf\n")

        orphans = detect_orphaned_sidecars(repo_root)

        assert len(orphans) == 1
        assert orphans[0] == sc

    def test_ignores_sidecar_with_existing_source(self, temp_repo):
        repo_root, cfg = temp_repo
        docs = repo_root / "docs"
        docs.mkdir()
        source = docs / "present.pdf"
        source.touch()
        sc_dir = repo_root / ".carta" / "sidecars" / "docs"
        sc_dir.mkdir(parents=True)
        sc = sc_dir / "present.embed-meta.yaml"
        sc.write_text("slug: present\nstatus: embedded\ncurrent_path: docs/present.pdf\n")

        orphans = detect_orphaned_sidecars(repo_root)

        assert orphans == []

    def test_skips_sidecar_with_no_current_path(self, temp_repo):
        repo_root, cfg = temp_repo
        sc_dir = repo_root / ".carta" / "sidecars" / "docs"
        sc_dir.mkdir(parents=True)
        sc = sc_dir / "no_path.embed-meta.yaml"
        sc.write_text("slug: no_path\nstatus: embedded\n")

        orphans = detect_orphaned_sidecars(repo_root)

        assert orphans == []

    def test_returns_empty_when_no_sidecars_dir(self, temp_repo):
        repo_root, cfg = temp_repo
        orphans = detect_orphaned_sidecars(repo_root)
        assert orphans == []
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /Users/ian/dev/doc-audit-cc && python -m pytest carta/tests/test_pipeline.py::TestDetectOrphanedSidecars -v
```

Expected: FAIL with `ImportError: cannot import name 'detect_orphaned_sidecars'`

- [ ] **Step 3: Implement `detect_orphaned_sidecars()` in `carta/embed/pipeline.py`**

Add after `migrate_sidecars()`:

```python
def detect_orphaned_sidecars(repo_root: Path) -> list[Path]:
    """Return sidecar paths under .carta/sidecars/ whose current_path source no longer exists."""
    orphans = []
    sidecars_root = repo_root / ".carta" / "sidecars"
    if not sidecars_root.exists():
        return orphans
    for sc_path in sidecars_root.rglob("*.embed-meta.yaml"):
        data = read_sidecar(sc_path)
        if data is None:
            continue
        current_path = data.get("current_path")
        if not current_path:
            continue  # skip pre-lifecycle sidecars without current_path
        if not (repo_root / current_path).exists():
            orphans.append(sc_path)
    return orphans
```

- [ ] **Step 4: Wire `detect_orphaned_sidecars()` into `run_embed()` in `carta/embed/pipeline.py`**

After the `_heal_sidecar_current_paths(repo_root, verbose=verbose)` call, add:

```python
    # Warn about sidecars whose source files no longer exist
    for orphan in detect_orphaned_sidecars(repo_root):
        orphan_data = read_sidecar(orphan) or {}
        print(
            f"Warning: orphaned sidecar (source not found): {orphan.relative_to(repo_root)}\n"
            f"  → source was: {orphan_data.get('current_path', 'unknown')}\n"
            f"  Run 'carta audit' for full orphan report.",
            file=sys.stderr, flush=True,
        )
```

- [ ] **Step 5: Run orphan tests**

```bash
cd /Users/ian/dev/doc-audit-cc && python -m pytest carta/tests/test_pipeline.py::TestDetectOrphanedSidecars -v
```

Expected: all PASS

- [ ] **Step 6: Run all pipeline tests to check for regressions**

```bash
cd /Users/ian/dev/doc-audit-cc && python -m pytest carta/tests/test_pipeline.py -v
```

Expected: all PASS

- [ ] **Step 7: Commit**

```bash
cd /Users/ian/dev/doc-audit-cc && git add carta/embed/pipeline.py carta/tests/test_pipeline.py && git commit -m "feat(pipeline): add detect_orphaned_sidecars() with stderr warnings on embed"
```

---

## Task 5: Update `scanner.py`

**Files:**
- Modify: `carta/scanner/scanner.py`
- Modify: `carta/scanner/tests/test_scanner.py`

- [ ] **Step 1: Add import to `carta/scanner/scanner.py`**

Add to the existing imports (after the `carta.*` imports):

```python
from carta.embed.induct import sidecar_path as get_sidecar_path
```

- [ ] **Step 2: Replace `_iter_sidecar_files()` in `carta/scanner/scanner.py`**

Replace the entire function (around line 460):

```python
def _iter_sidecar_files(repo_root: Path, cfg: dict):
    """Yield all .embed-meta.yaml files under .carta/sidecars/, skipping excluded_paths."""
    sidecars_root = repo_root / ".carta" / "sidecars"
    if not sidecars_root.exists():
        return
    for p in sidecars_root.rglob("*.embed-meta.yaml"):
        if is_excluded(p, cfg, repo_root):
            continue
        yield p
```

- [ ] **Step 3: Update `check_embed_induction_needed()` in `carta/scanner/scanner.py`**

Find both occurrences where sidecar path is constructed from the source file (around lines 555 and 563):

```python
            # Replace:
            sidecar = f.parent / (f.stem + ".embed-meta.yaml")
            if not sidecar.exists():
            # With:
            sidecar = get_sidecar_path(f, repo_root)
            if not sidecar.exists():
```

Apply for both the `if not sidecar.exists()` block and the `else` block that calls `parse_sidecar(sidecar)`.

- [ ] **Step 4: Update `check_embed_drift()` in `carta/scanner/scanner.py`**

Find (around line 593):

```python
            # Replace:
            sidecar = f.parent / (f.stem + ".embed-meta.yaml")
            # With:
            sidecar = get_sidecar_path(f, repo_root)
```

- [ ] **Step 5: Update `check_embed_transcript_unprocessed()` in `carta/scanner/scanner.py`**

Find (around line 664):

```python
        # Replace:
        sidecar = inputs_dir / f"{stem}.embed-meta.yaml"
        # With:
        sidecar = repo_root / ".carta" / "sidecars" / inputs_dir.relative_to(repo_root) / f"{stem}.embed-meta.yaml"
```

- [ ] **Step 6: Run scanner tests to identify failures**

```bash
cd /Users/ian/dev/doc-audit-cc && python -m pytest carta/scanner/tests/test_scanner.py -v 2>&1 | head -80
```

Expected: failures in tests that construct co-located sidecars.

- [ ] **Step 7: Update sidecar path constructions in `carta/scanner/tests/test_scanner.py`**

Six lines in the scanner tests place sidecars co-located with source files (lines ~479, ~492, ~505, ~523, ~544, ~561). The pattern for each is:

```python
# OLD (example at line ~479, where ds = tmp_path / "docs/reference"):
(ds / "ads1263.embed-meta.yaml").write_text("slug: ads1263\nstatus: pending\n")

# NEW — use tmp_path as repo_root:
sc_dir = tmp_path / ".carta" / "sidecars" / ds.relative_to(tmp_path)
sc_dir.mkdir(parents=True, exist_ok=True)
(sc_dir / "ads1263.embed-meta.yaml").write_text("slug: ads1263\nstatus: pending\n")
```

For audio tests (lines ~523, ~544, ~561, where `audio_in = tmp_path / "docs/audio/inputs"`):

```python
# OLD:
(audio_in / "meeting.embed-meta.yaml").write_text("slug: meeting\nstatus: embedded\ndoc_type: audio\n")

# NEW:
sc_dir = tmp_path / ".carta" / "sidecars" / audio_in.relative_to(tmp_path)
sc_dir.mkdir(parents=True, exist_ok=True)
(sc_dir / "meeting.embed-meta.yaml").write_text("slug: meeting\nstatus: embedded\ndoc_type: audio\n")
```

Note: any test that uses `_iter_sidecar_files` or `check_sidecar_path_drift` must place the sidecar under `.carta/sidecars/` or the function will find nothing.

- [ ] **Step 8: Run all scanner tests**

```bash
cd /Users/ian/dev/doc-audit-cc && python -m pytest carta/scanner/tests/test_scanner.py -v
```

Expected: all PASS

- [ ] **Step 9: Commit**

```bash
cd /Users/ian/dev/doc-audit-cc && git add carta/scanner/scanner.py carta/scanner/tests/test_scanner.py && git commit -m "feat(scanner): scope sidecar discovery to .carta/sidecars/"
```

---

## Task 6: Update `audit.py` + remaining tests

**Files:**
- Modify: `carta/audit/audit.py`
- Modify: `carta/audit/tests/test_audit.py`
- Modify: `carta/tests/test_mcp_server.py`

- [ ] **Step 1: Replace `_build_sidecar_registry()` in `carta/audit/audit.py`**

Replace the entire function:

```python
def _build_sidecar_registry(repo_root: Path, cfg: dict) -> dict:
    """Build registry of all sidecars under .carta/sidecars/.

    Returns:
        Dict mapping sidecar_id -> {
            "path": Path to sidecar file,
            "data": Parsed YAML content,
            "file_path": Path to source file if it exists, else None
        }
    """
    registry = {}
    sidecars_root = repo_root / ".carta" / "sidecars"
    excluded = cfg.get("excluded_paths", [])

    if not sidecars_root.exists():
        return registry

    for sidecar_path in sidecars_root.rglob("*.embed-meta.yaml"):
        rel_path_str = str(sidecar_path.relative_to(repo_root)).replace("\\", "/")
        if any(
            fnmatch.fnmatch(rel_path_str, p) or fnmatch.fnmatch(rel_path_str, f"*/{p}*")
            for p in excluded
        ):
            continue

        try:
            sidecar_data = yaml.safe_load(sidecar_path.read_text())
            if not sidecar_data or "sidecar_id" not in sidecar_data:
                continue

            sidecar_id = sidecar_data["sidecar_id"]
            current_path = sidecar_data.get("current_path")
            source_file = (repo_root / current_path) if current_path else None

            registry[sidecar_id] = {
                "path": sidecar_path,
                "data": sidecar_data,
                "file_path": source_file if (source_file and source_file.exists()) else None,
            }
        except Exception:
            continue

    return registry
```

- [ ] **Step 2: Add `detect_missing_source_sidecars()` to `carta/audit/audit.py`**

Add after `detect_qdrant_sidecar_mismatches()`:

```python
def detect_missing_source_sidecars(repo_root: Path, cfg: dict, sidecar_registry: dict) -> list[dict]:
    """Detect sidecars in .carta/sidecars/ whose source file no longer exists.

    Args:
        repo_root: Repository root
        cfg: Config dict
        sidecar_registry: Sidecars from _build_sidecar_registry

    Returns:
        List of issue dicts with category="missing_source_sidecars"
    """
    issues = []
    sidecars_root = repo_root / ".carta" / "sidecars"
    if not sidecars_root.exists():
        return issues
    for sc_path in sidecars_root.rglob("*.embed-meta.yaml"):
        try:
            sidecar_data = yaml.safe_load(sc_path.read_text())
        except Exception:
            continue
        if not sidecar_data:
            continue
        current_path = sidecar_data.get("current_path")
        if not current_path:
            continue  # pre-lifecycle sidecar — skip
        if not (repo_root / current_path).exists():
            issues.append({
                "id": f"missing_source_{sc_path.stem[:8]}",
                "category": "missing_source_sidecars",
                "severity": "warning",
                "sidecar_path": str(sc_path.relative_to(repo_root)),
                "missing_source": current_path,
            })
    return issues
```

- [ ] **Step 3: Wire `detect_missing_source_sidecars()` into `run_audit()` in `carta/audit/audit.py`**

In `run_audit()`, find the block that builds `all_issues` and add:

```python
    all_issues.extend(detect_missing_source_sidecars(repo_root, cfg, sidecar_registry))
```

Place it after the `detect_qdrant_sidecar_mismatches` call.

Also update `detect_missing_sidecars` expected sidecar path (line ~212) to use the new format:

```python
            # Replace:
            "expected_sidecar_path": f"{file_path}.embed-meta.yaml" if file_path else "unknown"
            # With:
            "expected_sidecar_path": f".carta/sidecars/{file_path}.embed-meta.yaml" if file_path else "unknown"
```

- [ ] **Step 4: Run audit tests to identify failures**

```bash
cd /Users/ian/dev/doc-audit-cc && python -m pytest carta/audit/tests/test_audit.py -v 2>&1 | head -80
```

- [ ] **Step 5: Update sidecar path constructions in `carta/audit/tests/test_audit.py`**

All occurrences like:
```python
sidecar_file = docs_root / "test.md.embed-meta.yaml"
```

Need to become:
```python
sidecar_file = repo_root / ".carta" / "sidecars" / "docs" / "test.embed-meta.yaml"
sidecar_file.parent.mkdir(parents=True, exist_ok=True)
```

Note: the old audit sidecar format was `test.md.embed-meta.yaml` (source extension preserved). The new `sidecar_path()` produces `test.embed-meta.yaml` (source extension replaced). Update all test sidecar filename constructions accordingly.

Apply to all occurrences in `test_audit.py` (lines ~53, ~80, ~85, ~147, ~240, ~291, ~455, ~467, ~479).

Also add a test for `detect_missing_source_sidecars`:

```python
def test_detect_missing_source_sidecars_finds_orphan(tmp_path):
    from carta.audit.audit import detect_missing_source_sidecars
    sc_dir = tmp_path / ".carta" / "sidecars" / "docs"
    sc_dir.mkdir(parents=True)
    sc = sc_dir / "deleted.embed-meta.yaml"
    sc.write_text("sidecar_id: abc\nslug: deleted\nstatus: embedded\ncurrent_path: docs/deleted.pdf\n")

    issues = detect_missing_source_sidecars(tmp_path, {}, {})

    assert len(issues) == 1
    assert issues[0]["category"] == "missing_source_sidecars"
    assert issues[0]["missing_source"] == "docs/deleted.pdf"


def test_detect_missing_source_sidecars_ignores_existing_source(tmp_path):
    from carta.audit.audit import detect_missing_source_sidecars
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "present.pdf").touch()
    sc_dir = tmp_path / ".carta" / "sidecars" / "docs"
    sc_dir.mkdir(parents=True)
    sc = sc_dir / "present.embed-meta.yaml"
    sc.write_text("sidecar_id: abc\nslug: present\nstatus: embedded\ncurrent_path: docs/present.pdf\n")

    issues = detect_missing_source_sidecars(tmp_path, {}, {})

    assert issues == []
```

- [ ] **Step 6: Update `TestDiscoverStaleFilesIntegration` in `carta/tests/test_mcp_server.py`**

Find all co-located sidecar constructions (e.g. `stale_sidecar = docs_dir / "stale.embed-meta.yaml"`) and replace with `.carta/sidecars/` paths. Also add `current_path` to the sidecar YAML content. Apply the same pattern used in Task 2 Step 8.

- [ ] **Step 7: Run all audit and MCP server tests**

```bash
cd /Users/ian/dev/doc-audit-cc && python -m pytest carta/audit/tests/test_audit.py carta/tests/test_mcp_server.py -v
```

Expected: all PASS

- [ ] **Step 8: Run the full test suite**

```bash
cd /Users/ian/dev/doc-audit-cc && python -m pytest --tb=short 2>&1 | tail -30
```

Expected: all tests PASS (same count as before this feature branch, plus new tests)

- [ ] **Step 9: Commit**

```bash
cd /Users/ian/dev/doc-audit-cc && git add carta/audit/audit.py carta/audit/tests/test_audit.py carta/tests/test_mcp_server.py && git commit -m "feat(audit): scope registry to .carta/sidecars/, add detect_missing_source_sidecars()"
```

---

## Post-Implementation Checklist

- [ ] All tests pass: `python -m pytest --tb=short`
- [ ] `carta embed` on an existing repo with co-located sidecars prints migration lines and moves files
- [ ] `.carta/sidecars/` directory is created automatically; co-located sidecars are gone
- [ ] `carta audit` reports orphaned sidecars under `missing_source_sidecars` category
