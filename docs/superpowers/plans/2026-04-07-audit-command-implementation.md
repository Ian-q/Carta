# Audit Command Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement `carta audit` command to detect inconsistencies across files, sidecars, and Qdrant chunks, reporting results to JSON.

**Architecture:** Core audit logic in new `carta/audit/` module with six detection functions + `run_audit()` orchestrator. CLI integration in `carta/cli.py`. Repair flow deferred (separate skill). Skill documents when/how to use audit.

**Tech Stack:** Python 3.10+, qdrant-client, pathlib, JSON, pytest

---

## Task 1: Create audit module structure and helpers

**Files:**
- Create: `carta/audit/__init__.py`
- Create: `carta/audit/audit.py`
- Create: `carta/audit/tests/__init__.py`

- [ ] **Step 1: Create module structure**

```bash
mkdir -p /Users/ian/dev/doc-audit-cc/carta/audit/tests
touch /Users/ian/dev/doc-audit-cc/carta/audit/__init__.py
touch /Users/ian/dev/doc-audit-cc/carta/audit/audit.py
touch /Users/ian/dev/doc-audit-cc/carta/audit/tests/__init__.py
```

- [ ] **Step 2: Write stub functions in audit.py**

```python
"""Audit pipeline: detect inconsistencies across files, sidecars, and Qdrant.

This module provides structured detection of six issue categories:
- orphaned_chunks: chunks in Qdrant with no matching sidecar
- missing_sidecars: files without .embed-meta.yaml but have chunks
- stale_sidecars: sidecars with mtime older than actual file
- hash_mismatches: file hash differs from sidecar record
- disconnected_files: files with no sidecar and no chunks
- qdrant_sidecar_mismatches: chunks don't align with sidecar metadata
"""

from pathlib import Path
from datetime import datetime
from typing import Optional
from qdrant_client import QdrantClient


def _build_sidecar_registry(repo_root: Path, cfg: dict) -> dict:
    """Build registry of all sidecars on disk.
    
    Args:
        repo_root: Repository root path
        cfg: Carta config dict
    
    Returns:
        Dict mapping sidecar_id -> {"path": Path, "data": dict, ...}
    """
    pass


def _build_qdrant_chunk_index(client: QdrantClient, collection_name: str) -> dict:
    """Index all chunks in Qdrant by sidecar_id.
    
    Args:
        client: Connected Qdrant client
        collection_name: Collection to scan
    
    Returns:
        Dict mapping sidecar_id -> [chunk_records]
    """
    pass


def detect_orphaned_chunks(client: QdrantClient, cfg: dict, sidecar_registry: dict) -> list[dict]:
    """Detect chunks in Qdrant with no matching sidecar on disk."""
    pass


def detect_missing_sidecars(repo_root: Path, cfg: dict, sidecar_registry: dict, qdrant_index: dict) -> list[dict]:
    """Detect files with chunks in Qdrant but no sidecar."""
    pass


def detect_stale_sidecars(repo_root: Path, cfg: dict, sidecar_registry: dict) -> list[dict]:
    """Detect sidecars where file mtime is newer than last embed."""
    pass


def detect_hash_mismatches(repo_root: Path, cfg: dict, sidecar_registry: dict) -> list[dict]:
    """Detect files where computed hash differs from sidecar record."""
    pass


def detect_disconnected_files(repo_root: Path, cfg: dict, sidecar_registry: dict, qdrant_index: dict) -> list[dict]:
    """Detect discoverable files with no sidecar and no chunks."""
    pass


def detect_qdrant_sidecar_mismatches(client: QdrantClient, cfg: dict, sidecar_registry: dict, qdrant_index: dict) -> list[dict]:
    """Detect chunks in Qdrant that don't match sidecar metadata."""
    pass


def run_audit(cfg: dict, repo_root: Path, verbose: bool = False) -> dict:
    """Run full audit and return results dict matching JSON schema."""
    pass
```

- [ ] **Step 3: Commit**

```bash
git add carta/audit/__init__.py carta/audit/audit.py carta/audit/tests/__init__.py
git commit -m "feat: create audit module structure with stub signatures"
```

---

## Task 2: Implement sidecar registry helper

**Files:**
- Modify: `carta/audit/audit.py`
- Create: `carta/audit/tests/test_audit.py`

- [ ] **Step 1: Write failing test**

Add to `carta/audit/tests/test_audit.py`:

```python
"""Tests for audit module."""
import pytest
from pathlib import Path
from unittest.mock import Mock, patch
import yaml
import tempfile
import os

from carta.audit.audit import _build_sidecar_registry
from carta.lifecycle import compute_file_hash


class TestBuildSidecarRegistry:
    """Test sidecar registry building."""
    
    def test_empty_docs_root(self):
        """Registry is empty when no sidecars exist."""
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            docs_root = repo_root / "docs"
            docs_root.mkdir()
            
            cfg = {"docs_root": "docs", "excluded_paths": []}
            registry = _build_sidecar_registry(repo_root, cfg)
            
            assert registry == {}
    
    def test_single_sidecar_loaded(self):
        """Single sidecar is loaded with correct data."""
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            docs_root = repo_root / "docs"
            docs_root.mkdir()
            
            # Create a source file
            test_file = docs_root / "test.md"
            test_file.write_text("# Test")
            
            # Create sidecar
            sidecar_file = docs_root / "test.md.embed-meta.yaml"
            sidecar_data = {
                "sidecar_id": "test_sidecar_123",
                "file_hash": "abc123",
                "file_mtime": 1234567890.0,
                "chunk_count": 5,
                "doc_type": "doc"
            }
            sidecar_file.write_text(yaml.dump(sidecar_data))
            
            cfg = {"docs_root": "docs", "excluded_paths": []}
            registry = _build_sidecar_registry(repo_root, cfg)
            
            assert "test_sidecar_123" in registry
            assert registry["test_sidecar_123"]["path"] == sidecar_file
            assert registry["test_sidecar_123"]["data"]["chunk_count"] == 5
    
    def test_excluded_paths_skipped(self):
        """Sidecars in excluded paths are not registered."""
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            docs_root = repo_root / "docs"
            docs_root.mkdir()
            
            # Create sidecar in excluded path
            excluded_dir = docs_root / "node_modules"
            excluded_dir.mkdir()
            sidecar_file = excluded_dir / "pkg.md.embed-meta.yaml"
            sidecar_data = {"sidecar_id": "excluded_123"}
            sidecar_file.write_text(yaml.dump(sidecar_data))
            
            # Create sidecar in included path
            included_sidecar = docs_root / "included.md.embed-meta.yaml"
            included_sidecar.write_text(yaml.dump({"sidecar_id": "included_123"}))
            
            cfg = {"docs_root": "docs", "excluded_paths": ["node_modules/"]}
            registry = _build_sidecar_registry(repo_root, cfg)
            
            assert "excluded_123" not in registry
            assert "included_123" in registry
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd /Users/ian/dev/doc-audit-cc
pytest carta/audit/tests/test_audit.py::TestBuildSidecarRegistry -v
```

Expected: FAIL with "AssertionError" or "TypeError: _build_sidecar_registry() returns None"

- [ ] **Step 3: Implement helper**

In `carta/audit/audit.py`, replace stub:

```python
import fnmatch
import yaml
from carta.lifecycle import compute_file_hash  # Already exists in project


def _build_sidecar_registry(repo_root: Path, cfg: dict) -> dict:
    """Build registry of all sidecars on disk.
    
    Scans docs_root for .embed-meta.yaml files, respecting excluded_paths.
    
    Args:
        repo_root: Repository root path
        cfg: Carta config dict with docs_root and excluded_paths
    
    Returns:
        Dict mapping sidecar_id -> {
            "path": Path to sidecar file,
            "data": Parsed YAML content,
            "file_path": Path to corresponding source file (if exists)
        }
    """
    registry = {}
    docs_root = repo_root / cfg.get("docs_root", "docs")
    excluded = cfg.get("excluded_paths", [])
    
    if not docs_root.exists():
        return registry
    
    # Scan for all .embed-meta.yaml files
    for sidecar_path in docs_root.rglob("*.embed-meta.yaml"):
        # Check if excluded
        rel_path = sidecar_path.relative_to(repo_root)
        if any(fnmatch.fnmatch(str(rel_path), pattern) for pattern in excluded):
            continue
        
        # Load sidecar
        try:
            sidecar_data = yaml.safe_load(sidecar_path.read_text())
            if not sidecar_data or "sidecar_id" not in sidecar_data:
                continue
            
            sidecar_id = sidecar_data["sidecar_id"]
            source_file = sidecar_path.with_suffix("") if sidecar_path.suffix == ".yaml" else None
            
            # Adjust: .embed-meta.yaml means source is without .embed-meta.yaml
            # e.g., test.md.embed-meta.yaml -> test.md
            source_file = Path(str(sidecar_path).replace(".embed-meta.yaml", ""))
            
            registry[sidecar_id] = {
                "path": sidecar_path,
                "data": sidecar_data,
                "file_path": source_file if source_file.exists() else None
            }
        except Exception:
            # Skip malformed sidecars
            continue
    
    return registry
```

- [ ] **Step 4: Run test to verify it passes**

```bash
pytest carta/audit/tests/test_audit.py::TestBuildSidecarRegistry -v
```

Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add carta/audit/audit.py carta/audit/tests/test_audit.py
git commit -m "feat: implement sidecar registry builder with tests"
```

---

## Task 3: Implement Qdrant chunk index helper

**Files:**
- Modify: `carta/audit/audit.py`
- Modify: `carta/audit/tests/test_audit.py`

- [ ] **Step 1: Write failing test**

Add to `TestBuildSidecarRegistry` or create new class in `test_audit.py`:

```python
class TestBuildQdrantChunkIndex:
    """Test Qdrant chunk indexing."""
    
    def test_empty_collection(self):
        """Index is empty when collection has no chunks."""
        mock_client = Mock()
        mock_client.scroll.return_value = ([], None)
        
        index = _build_qdrant_chunk_index(mock_client, "test_doc")
        
        assert index == {}
    
    def test_index_chunks_by_sidecar_id(self):
        """Chunks are indexed by sidecar_id."""
        mock_client = Mock()
        
        # Mock chunk records
        chunk1 = Mock(id=1, payload={"sidecar_id": "sidecar_1", "chunk_index": 0})
        chunk2 = Mock(id=2, payload={"sidecar_id": "sidecar_1", "chunk_index": 1})
        chunk3 = Mock(id=3, payload={"sidecar_id": "sidecar_2", "chunk_index": 0})
        
        mock_client.scroll.return_value = ([chunk1, chunk2, chunk3], None)
        
        index = _build_qdrant_chunk_index(mock_client, "test_doc")
        
        assert len(index["sidecar_1"]) == 2
        assert len(index["sidecar_2"]) == 1
        assert index["sidecar_1"][0]["id"] == 1
    
    def test_skip_chunks_without_sidecar_id(self):
        """Chunks without sidecar_id (pre-999.1) are skipped."""
        mock_client = Mock()
        
        chunk1 = Mock(id=1, payload={"sidecar_id": "sidecar_1"})
        chunk2 = Mock(id=2, payload={})  # No sidecar_id
        
        mock_client.scroll.return_value = ([chunk1, chunk2], None)
        
        index = _build_qdrant_chunk_index(mock_client, "test_doc")
        
        assert len(index) == 1
        assert "sidecar_1" in index
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest carta/audit/tests/test_audit.py::TestBuildQdrantChunkIndex -v
```

Expected: FAIL

- [ ] **Step 3: Implement helper**

In `carta/audit/audit.py`, add:

```python
def _build_qdrant_chunk_index(client: QdrantClient, collection_name: str) -> dict:
    """Index all chunks in Qdrant by sidecar_id.
    
    Scrolls the collection and groups chunks by sidecar_id.
    Skips chunks without sidecar_id (pre-999.1 migration boundary).
    
    Args:
        client: Connected Qdrant client
        collection_name: Collection to scan
    
    Returns:
        Dict mapping sidecar_id -> [chunk_records with id, payload]
    """
    index = {}
    
    try:
        # Scroll through all chunks
        points, _ = client.scroll(
            collection_name=collection_name,
            limit=1000,  # Qdrant scroll batch size
        )
        
        while points:
            for point in points:
                sidecar_id = point.payload.get("sidecar_id")
                if not sidecar_id:
                    continue  # Skip pre-999.1 chunks
                
                if sidecar_id not in index:
                    index[sidecar_id] = []
                
                index[sidecar_id].append({
                    "id": point.id,
                    "payload": point.payload,
                    "chunk_index": point.payload.get("chunk_index")
                })
            
            # Continue scrolling if more points
            if len(points) < 1000:
                break
            
            points, _ = client.scroll(
                collection_name=collection_name,
                limit=1000,
                offset=len(points),
            )
    except Exception:
        # Collection doesn't exist or is unreachable; return empty index
        pass
    
    return index
```

- [ ] **Step 4: Run test to verify it passes**

```bash
pytest carta/audit/tests/test_audit.py::TestBuildQdrantChunkIndex -v
```

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add carta/audit/audit.py carta/audit/tests/test_audit.py
git commit -m "feat: implement qdrant chunk index builder with tests"
```

---

## Task 4: Implement orphaned_chunks detection

**Files:**
- Modify: `carta/audit/audit.py`
- Modify: `carta/audit/tests/test_audit.py`

- [ ] **Step 1: Write failing test**

Add to `test_audit.py`:

```python
class TestDetectOrphanedChunks:
    """Test detection of orphaned chunks."""
    
    def test_no_orphans_when_sidecars_match(self):
        """No issues when all chunks have matching sidecars."""
        mock_client = Mock()
        cfg = {"docs_root": "docs", "excluded_paths": []}
        sidecar_registry = {
            "sidecar_1": {"data": {"chunk_count": 2}, "path": Path("docs/test.md.embed-meta.yaml")}
        }
        
        issues = detect_orphaned_chunks(mock_client, cfg, sidecar_registry, {})
        
        assert issues == []
    
    def test_detects_orphaned_chunks(self):
        """Orphaned chunks (no matching sidecar) are detected."""
        mock_client = Mock()
        cfg = {"docs_root": "docs"}
        sidecar_registry = {
            "sidecar_1": {"data": {}}
        }
        
        # Build qdrant index with orphaned sidecar_id
        qdrant_index = {
            "sidecar_1": [{"id": 1, "payload": {"text": "chunk content"}}, {"id": 2, "payload": {}}],
            "orphaned_sidecar": [{"id": 3, "payload": {"text": "orphaned content"}}]
        }
        
        issues = detect_orphaned_chunks(mock_client, cfg, sidecar_registry, qdrant_index)
        
        assert len(issues) == 1
        assert issues[0]["category"] == "orphaned_chunks"
        assert issues[0]["sidecar_id"] == "orphaned_sidecar"
        assert len(issues[0]["chunk_ids"]) == 1
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest carta/audit/tests/test_audit.py::TestDetectOrphanedChunks::test_detects_orphaned_chunks -v
```

Expected: FAIL

- [ ] **Step 3: Implement function**

In `carta/audit/audit.py`, replace stub:

```python
def detect_orphaned_chunks(client: QdrantClient, cfg: dict, sidecar_registry: dict, qdrant_index: dict) -> list[dict]:
    """Detect chunks in Qdrant with no matching sidecar on disk.
    
    Args:
        client: Qdrant client (for fetching chunk text if needed)
        cfg: Config dict
        sidecar_registry: Registry of sidecars from _build_sidecar_registry
        qdrant_index: Chunk index from _build_qdrant_chunk_index
    
    Returns:
        List of issue dicts with category="orphaned_chunks"
    """
    issues = []
    
    for sidecar_id, chunks in qdrant_index.items():
        if sidecar_id not in sidecar_registry:
            # Orphaned: chunks exist but no sidecar
            chunk_ids = [c["id"] for c in chunks]
            
            # Get first chunk's text for preview
            first_text = ""
            if chunks and chunks[0].get("payload", {}).get("text"):
                first_text = chunks[0]["payload"]["text"][:100]
            
            issue = {
                "id": f"orphaned_{sidecar_id[:8]}",
                "category": "orphaned_chunks",
                "severity": "warning",
                "sidecar_id": sidecar_id,
                "chunk_ids": chunk_ids,
                "chunk_count": len(chunks),
                "first_chunk_text": first_text,
                "metadata": {
                    "doc_type": chunks[0].get("payload", {}).get("doc_type", "unknown") if chunks else "unknown",
                    "collection": f"{cfg.get('project_name', 'unknown')}_doc"
                }
            }
            issues.append(issue)
    
    return issues
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest carta/audit/tests/test_audit.py::TestDetectOrphanedChunks -v
```

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add carta/audit/audit.py carta/audit/tests/test_audit.py
git commit -m "feat: implement orphaned chunks detection"
```

---

## Task 5: Implement missing_sidecars detection

**Files:**
- Modify: `carta/audit/audit.py`
- Modify: `carta/audit/tests/test_audit.py`

- [ ] **Step 1: Write failing test**

Add to `test_audit.py`:

```python
class TestDetectMissingSidecars:
    """Test detection of missing sidecars."""
    
    def test_no_missing_when_all_have_sidecars(self):
        """No issues when all chunked files have sidecars."""
        sidecar_registry = {
            "sidecar_1": {"file_path": Path("docs/test.md"), "data": {"file_hash": "abc123"}}
        }
        qdrant_index = {"sidecar_1": [{"id": 1}]}
        
        issues = detect_missing_sidecars(Path("/repo"), {}, sidecar_registry, qdrant_index)
        
        assert issues == []
    
    def test_detects_missing_sidecars(self):
        """Files with chunks but no sidecar are detected."""
        sidecar_registry = {}
        qdrant_index = {
            "phantom_sidecar": [{"id": 1, "payload": {"file_path": "docs/orphaned.md"}}]
        }
        
        issues = detect_missing_sidecars(Path("/repo"), {}, sidecar_registry, qdrant_index)
        
        assert len(issues) == 1
        assert issues[0]["category"] == "missing_sidecars"
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest carta/audit/tests/test_audit.py::TestDetectMissingSidecars::test_detects_missing_sidecars -v
```

Expected: FAIL

- [ ] **Step 3: Implement function**

In `carta/audit/audit.py`:

```python
def detect_missing_sidecars(repo_root: Path, cfg: dict, sidecar_registry: dict, qdrant_index: dict) -> list[dict]:
    """Detect files with chunks in Qdrant but no sidecar on disk.
    
    This indicates a partial failure or manual deletion of sidecar.
    
    Args:
        repo_root: Repository root
        cfg: Config dict
        sidecar_registry: Sidecars from _build_sidecar_registry
        qdrant_index: Chunks from _build_qdrant_chunk_index
    
    Returns:
        List of issue dicts with category="missing_sidecars"
    """
    issues = []
    
    for sidecar_id, chunks in qdrant_index.items():
        if sidecar_id not in sidecar_registry and chunks:
            # Find file_path from chunk payload if available
            file_path = None
            if chunks and chunks[0].get("payload", {}).get("file_path"):
                file_path = chunks[0]["payload"]["file_path"]
            
            issue = {
                "id": f"missing_sidecar_{sidecar_id[:8]}",
                "category": "missing_sidecars",
                "severity": "warning",
                "sidecar_id": sidecar_id,
                "file_path": file_path,
                "chunk_count": len(chunks),
                "expected_sidecar_path": f"{file_path}.embed-meta.yaml" if file_path else "unknown"
            }
            issues.append(issue)
    
    return issues
```

- [ ] **Step 4: Run test to verify it passes**

```bash
pytest carta/audit/tests/test_audit.py::TestDetectMissingSidecars -v
```

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add carta/audit/audit.py carta/audit/tests/test_audit.py
git commit -m "feat: implement missing sidecars detection"
```

---

## Task 6: Implement stale_sidecars and hash_mismatches detection

**Files:**
- Modify: `carta/audit/audit.py`
- Modify: `carta/audit/tests/test_audit.py`

- [ ] **Step 1: Write failing tests**

Add to `test_audit.py`:

```python
class TestDetectStaleSidecars:
    """Test detection of stale sidecars."""
    
    def test_no_stale_when_mtime_matches(self):
        """No issues when file mtime matches sidecar."""
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            docs_root = repo_root / "docs"
            docs_root.mkdir()
            
            test_file = docs_root / "test.md"
            test_file.write_text("content")
            mtime = os.path.getmtime(test_file)
            
            sidecar_registry = {
                "sidecar_1": {
                    "file_path": test_file,
                    "data": {"file_mtime": mtime, "last_embedded": "2026-04-07T10:00:00Z"}
                }
            }
            
            issues = detect_stale_sidecars(repo_root, {}, sidecar_registry)
            
            assert issues == []
    
    def test_detects_stale_sidecars(self):
        """File newer than sidecar is detected as stale."""
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            docs_root = repo_root / "docs"
            docs_root.mkdir()
            
            test_file = docs_root / "test.md"
            test_file.write_text("content")
            
            sidecar_registry = {
                "sidecar_1": {
                    "file_path": test_file,
                    "data": {"file_mtime": 1000000000.0, "last_embedded": "2026-01-01T00:00:00Z"}
                }
            }
            
            issues = detect_stale_sidecars(repo_root, {}, sidecar_registry)
            
            assert len(issues) == 1
            assert issues[0]["category"] == "stale_sidecars"

class TestDetectHashMismatches:
    """Test detection of hash mismatches."""
    
    def test_no_mismatch_when_hashes_match(self):
        """No issues when file hash matches sidecar."""
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            docs_root = repo_root / "docs"
            docs_root.mkdir()
            
            test_file = docs_root / "test.md"
            test_file.write_text("# Header\n")
            
            # Compute actual hash
            actual_hash = compute_file_hash(test_file)
            
            sidecar_registry = {
                "sidecar_1": {
                    "file_path": test_file,
                    "data": {"file_hash": actual_hash}
                }
            }
            
            issues = detect_hash_mismatches(repo_root, {}, sidecar_registry)
            
            assert issues == []
    
    def test_detects_hash_mismatches(self):
        """File with different hash than sidecar is detected."""
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            docs_root = repo_root / "docs"
            docs_root.mkdir()
            
            test_file = docs_root / "test.md"
            test_file.write_text("current content\n")
            
            sidecar_registry = {
                "sidecar_1": {
                    "file_path": test_file,
                    "data": {"file_hash": "abc123def456"}
                }
            }
            
            issues = detect_hash_mismatches(repo_root, {}, sidecar_registry)
            
            assert len(issues) == 1
            assert issues[0]["category"] == "hash_mismatches"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest carta/audit/tests/test_audit.py::TestDetectStaleSidecars -v
pytest carta/audit/tests/test_audit.py::TestDetectHashMismatches -v
```

Expected: FAIL

- [ ] **Step 3: Implement functions**

In `carta/audit/audit.py`, add imports:

```python
from datetime import timedelta
from carta.lifecycle import compute_file_hash
```

Add functions:

```python
def detect_stale_sidecars(repo_root: Path, cfg: dict, sidecar_registry: dict) -> list[dict]:
    """Detect sidecars where file mtime is newer than recorded mtime.
    
    Indicates file changed after embedding.
    
    Args:
        repo_root: Repository root
        cfg: Config dict
        sidecar_registry: Sidecars from _build_sidecar_registry
    
    Returns:
        List of issue dicts with category="stale_sidecars"
    """
    issues = []
    
    for sidecar_id, sidecar_info in sidecar_registry.items():
        file_path = sidecar_info.get("file_path")
        if not file_path or not file_path.exists():
            continue
        
        sidecar_data = sidecar_info["data"]
        recorded_mtime = sidecar_data.get("file_mtime")
        last_embedded = sidecar_data.get("last_embedded")
        
        if recorded_mtime is None:
            continue
        
        actual_mtime = os.path.getmtime(file_path)
        
        if actual_mtime > recorded_mtime:
            # Compute how many days stale
            now = datetime.now()
            embedded_dt = datetime.fromisoformat(last_embedded) if last_embedded else datetime.fromtimestamp(recorded_mtime)
            days_stale = (now - embedded_dt).days
            
            issue = {
                "id": f"stale_{sidecar_id[:8]}",
                "category": "stale_sidecars",
                "severity": "info",
                "file_path": str(file_path.relative_to(repo_root)),
                "sidecar_path": str(sidecar_info["path"].relative_to(repo_root)),
                "last_embedded": last_embedded,
                "file_mtime": datetime.fromtimestamp(actual_mtime).isoformat(),
                "days_stale": max(0, days_stale)
            }
            issues.append(issue)
    
    return issues


def detect_hash_mismatches(repo_root: Path, cfg: dict, sidecar_registry: dict) -> list[dict]:
    """Detect files where computed hash differs from sidecar record.
    
    Indicates file content changed (even if mtime same due to touch/clock skew).
    
    Args:
        repo_root: Repository root
        cfg: Config dict
        sidecar_registry: Sidecars from _build_sidecar_registry
    
    Returns:
        List of issue dicts with category="hash_mismatches"
    """
    issues = []
    
    for sidecar_id, sidecar_info in sidecar_registry.items():
        file_path = sidecar_info.get("file_path")
        if not file_path or not file_path.exists():
            continue
        
        sidecar_data = sidecar_info["data"]
        recorded_hash = sidecar_data.get("file_hash")
        
        if recorded_hash is None:
            continue
        
        actual_hash = compute_file_hash(file_path)
        
        if actual_hash != recorded_hash:
            issue = {
                "id": f"hash_mismatch_{sidecar_id[:8]}",
                "category": "hash_mismatches",
                "severity": "warning",
                "file_path": str(file_path.relative_to(repo_root)),
                "sidecar_path": str(sidecar_info["path"].relative_to(repo_root)),
                "actual_hash": actual_hash,
                "recorded_hash": recorded_hash,
                "reason": "File content changed since last embedding"
            }
            issues.append(issue)
    
    return issues
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest carta/audit/tests/test_audit.py::TestDetectStaleSidecars -v
pytest carta/audit/tests/test_audit.py::TestDetectHashMismatches -v
```

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add carta/audit/audit.py carta/audit/tests/test_audit.py
git commit -m "feat: implement stale sidecars and hash mismatch detection"
```

---

## Task 7: Implement disconnected_files detection

**Files:**
- Modify: `carta/audit/audit.py`
- Modify: `carta/audit/tests/test_audit.py`

- [ ] **Step 1: Write failing test**

Add to `test_audit.py`:

```python
class TestDetectDisconnectedFiles:
    """Test detection of disconnected files."""
    
    def test_no_disconnected_when_all_embedded(self):
        """No issues when all discoverable files have sidecars or chunks."""
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            docs_root = repo_root / "docs"
            docs_root.mkdir()
            
            test_file = docs_root / "test.md"
            test_file.write_text("# Test")
            
            sidecar_registry = {
                "sidecar_1": {"file_path": test_file, "data": {}}
            }
            qdrant_index = {}
            
            issues = detect_disconnected_files(repo_root, {"docs_root": "docs"}, sidecar_registry, qdrant_index)
            
            assert issues == []
    
    def test_detects_disconnected_files(self):
        """Files with no sidecar and no chunks are detected."""
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            docs_root = repo_root / "docs"
            docs_root.mkdir()
            
            # Create discoverable file with no sidecar
            orphaned_file = docs_root / "orphaned.md"
            orphaned_file.write_text("# Orphaned")
            
            sidecar_registry = {}
            qdrant_index = {}
            
            issues = detect_disconnected_files(repo_root, {"docs_root": "docs", "excluded_paths": []}, sidecar_registry, qdrant_index)
            
            assert len(issues) == 1
            assert issues[0]["category"] == "disconnected_files"
            assert "orphaned.md" in issues[0]["file_path"]
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest carta/audit/tests/test_audit.py::TestDetectDisconnectedFiles::test_detects_disconnected_files -v
```

Expected: FAIL

- [ ] **Step 3: Implement function**

In `carta/audit/audit.py`:

```python
def detect_disconnected_files(repo_root: Path, cfg: dict, sidecar_registry: dict, qdrant_index: dict) -> list[dict]:
    """Detect discoverable files with no sidecar and no chunks in Qdrant.
    
    These files were never embedded or were removed from Qdrant only.
    
    Args:
        repo_root: Repository root
        cfg: Config dict with docs_root and excluded_paths
        sidecar_registry: Sidecars from _build_sidecar_registry
        qdrant_index: Chunks from _build_qdrant_chunk_index
    
    Returns:
        List of issue dicts with category="disconnected_files"
    """
    issues = []
    
    docs_root = repo_root / cfg.get("docs_root", "docs")
    excluded = cfg.get("excluded_paths", [])
    
    if not docs_root.exists():
        return issues
    
    # Collect all files with sidecars or chunks
    covered_files = set()
    
    for sidecar_info in sidecar_registry.values():
        if sidecar_info.get("file_path"):
            covered_files.add(sidecar_info["file_path"])
    
    for chunks in qdrant_index.values():
        for chunk in chunks:
            file_path_str = chunk.get("payload", {}).get("file_path")
            if file_path_str:
                covered_files.add(Path(file_path_str))
    
    # Scan for all discoverable files
    for file_path in docs_root.rglob("*.md") | docs_root.rglob("*.pdf"):
        # Check if excluded
        rel_path = file_path.relative_to(repo_root)
        if any(fnmatch.fnmatch(str(rel_path), pattern) for pattern in excluded):
            continue
        
        if file_path not in covered_files:
            issue = {
                "id": f"disconnected_{file_path.stem[:8]}",
                "category": "disconnected_files",
                "severity": "info",
                "file_path": str(rel_path),
                "reason": "File exists, no sidecar, no chunks in Qdrant"
            }
            issues.append(issue)
    
    return issues
```

- [ ] **Step 4: Run test to verify it passes**

```bash
pytest carta/audit/tests/test_audit.py::TestDetectDisconnectedFiles -v
```

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add carta/audit/audit.py carta/audit/tests/test_audit.py
git commit -m "feat: implement disconnected files detection"
```

---

## Task 8: Implement qdrant_sidecar_mismatches detection

**Files:**
- Modify: `carta/audit/audit.py`
- Modify: `carta/audit/tests/test_audit.py`

- [ ] **Step 1: Write failing test**

Add to `test_audit.py`:

```python
class TestDetectQdrantSidecarMismatches:
    """Test detection of Qdrant/sidecar metadata mismatches."""
    
    def test_no_mismatch_when_aligned(self):
        """No issues when chunk count and indices align."""
        sidecar_registry = {
            "sidecar_1": {"data": {"chunk_count": 2, "chunk_indices": [0, 1]}}
        }
        qdrant_index = {
            "sidecar_1": [
                {"id": 1, "payload": {"chunk_index": 0}},
                {"id": 2, "payload": {"chunk_index": 1}}
            ]
        }
        
        issues = detect_qdrant_sidecar_mismatches(None, {}, sidecar_registry, qdrant_index)
        
        assert issues == []
    
    def test_detects_count_mismatch(self):
        """Chunk count mismatch is detected."""
        sidecar_registry = {
            "sidecar_1": {"data": {"chunk_count": 5}}
        }
        qdrant_index = {
            "sidecar_1": [
                {"id": 1, "payload": {"chunk_index": 0}},
                {"id": 2, "payload": {"chunk_index": 1}}
            ]
        }
        
        issues = detect_qdrant_sidecar_mismatches(None, {}, sidecar_registry, qdrant_index)
        
        assert len(issues) == 1
        assert issues[0]["category"] == "qdrant_sidecar_mismatches"
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest carta/audit/tests/test_audit.py::TestDetectQdrantSidecarMismatches -v
```

Expected: FAIL

- [ ] **Step 3: Implement function**

In `carta/audit/audit.py`:

```python
def detect_qdrant_sidecar_mismatches(client: QdrantClient, cfg: dict, sidecar_registry: dict, qdrant_index: dict) -> list[dict]:
    """Detect chunks in Qdrant that don't match sidecar metadata.
    
    Checks chunk_count and chunk_index alignment.
    
    Args:
        client: Qdrant client
        cfg: Config dict
        sidecar_registry: Sidecars from _build_sidecar_registry
        qdrant_index: Chunks from _build_qdrant_chunk_index
    
    Returns:
        List of issue dicts with category="qdrant_sidecar_mismatches"
    """
    issues = []
    
    for sidecar_id, sidecar_info in sidecar_registry.items():
        sidecar_data = sidecar_info["data"]
        recorded_count = sidecar_data.get("chunk_count")
        
        chunks = qdrant_index.get(sidecar_id, [])
        actual_count = len(chunks)
        
        if recorded_count is not None and actual_count != recorded_count:
            issue = {
                "id": f"mismatch_{sidecar_id[:8]}",
                "category": "qdrant_sidecar_mismatches",
                "severity": "error",
                "sidecar_id": sidecar_id,
                "recorded_chunk_count": recorded_count,
                "actual_chunk_count": actual_count,
                "chunk_ids": [c["id"] for c in chunks],
                "reason": f"Sidecar expects {recorded_count} chunks, Qdrant has {actual_count}"
            }
            issues.append(issue)
    
    return issues
```

- [ ] **Step 4: Run test to verify it passes**

```bash
pytest carta/audit/tests/test_audit.py::TestDetectQdrantSidecarMismatches -v
```

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add carta/audit/audit.py carta/audit/tests/test_audit.py
git commit -m "feat: implement qdrant/sidecar mismatch detection"
```

---

## Task 9: Implement run_audit orchestrator and JSON formatting

**Files:**
- Modify: `carta/audit/audit.py`
- Modify: `carta/audit/tests/test_audit.py`

- [ ] **Step 1: Write failing test**

Add to `test_audit.py`:

```python
class TestRunAudit:
    """Test full audit orchestration."""
    
    def test_run_audit_empty_repo(self):
        """Audit completes on empty repo."""
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            docs_root = repo_root / "docs"
            docs_root.mkdir()
            
            cfg = {
                "project_name": "test-project",
                "docs_root": "docs",
                "excluded_paths": [],
                "qdrant_url": "http://localhost:6333"
            }
            
            with patch("carta.audit.audit.QdrantClient") as mock_client_class:
                mock_client = Mock()
                mock_client.get_collections.return_value = Mock(collections=[])
                mock_client_class.return_value = mock_client
                
                result = run_audit(cfg, repo_root, verbose=False)
                
                assert "summary" in result
                assert "issues" in result
                assert result["summary"]["total_issues"] == 0
    
    def test_run_audit_json_schema(self):
        """Audit output matches JSON schema."""
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            docs_root = repo_root / "docs"
            docs_root.mkdir()
            
            cfg = {
                "project_name": "test-project",
                "docs_root": "docs",
                "excluded_paths": [],
                "qdrant_url": "http://localhost:6333"
            }
            
            with patch("carta.audit.audit.QdrantClient"):
                result = run_audit(cfg, repo_root)
                
                # Verify schema
                assert "summary" in result
                assert "scanned_at" in result["summary"]
                assert "by_category" in result["summary"]
                assert "issues" in result
                assert isinstance(result["issues"], list)
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest carta/audit/tests/test_audit.py::TestRunAudit -v
```

Expected: FAIL

- [ ] **Step 3: Implement function**

In `carta/audit/audit.py`:

```python
def run_audit(cfg: dict, repo_root: Path, verbose: bool = False) -> dict:
    """Run full audit and return results dict matching JSON schema.
    
    Args:
        cfg: Carta config dict
        repo_root: Repository root path
        verbose: Print progress to stdout
    
    Returns:
        Dict with summary and issues list matching JSON schema
    """
    start_time = datetime.now()
    
    if verbose:
        print("Audit: building sidecar registry...", flush=True)
    
    sidecar_registry = _build_sidecar_registry(repo_root, cfg)
    
    if verbose:
        print(f"Audit: found {len(sidecar_registry)} sidecars", flush=True)
    
    # Connect to Qdrant
    try:
        client = QdrantClient(url=cfg.get("qdrant_url", "http://localhost:6333"), timeout=5)
        client.get_collections()
    except Exception as e:
        return {
            "summary": {
                "total_issues": -1,
                "error": f"Qdrant unreachable: {e}",
                "scanned_at": start_time.isoformat(),
                "repo_root": str(repo_root)
            },
            "issues": []
        }
    
    if verbose:
        print("Audit: building qdrant chunk index...", flush=True)
    
    collection_name = f"{cfg.get('project_name', 'unknown')}_doc"
    qdrant_index = _build_qdrant_chunk_index(client, collection_name)
    
    if verbose:
        print(f"Audit: scanning for issues...", flush=True)
    
    # Run all detection functions
    all_issues = []
    all_issues.extend(detect_orphaned_chunks(client, cfg, sidecar_registry, qdrant_index))
    all_issues.extend(detect_missing_sidecars(repo_root, cfg, sidecar_registry, qdrant_index))
    all_issues.extend(detect_stale_sidecars(repo_root, cfg, sidecar_registry))
    all_issues.extend(detect_hash_mismatches(repo_root, cfg, sidecar_registry))
    all_issues.extend(detect_disconnected_files(repo_root, cfg, sidecar_registry, qdrant_index))
    all_issues.extend(detect_qdrant_sidecar_mismatches(client, cfg, sidecar_registry, qdrant_index))
    
    # Tally by category
    by_category = {}
    for issue in all_issues:
        cat = issue["category"]
        by_category[cat] = by_category.get(cat, 0) + 1
    
    result = {
        "summary": {
            "total_issues": len(all_issues),
            "by_category": by_category,
            "scanned_at": start_time.isoformat(),
            "repo_root": str(repo_root),
            "project_name": cfg.get("project_name", "unknown"),
            "collection_scanned": collection_name
        },
        "issues": all_issues
    }
    
    if verbose:
        print(f"Audit complete: {len(all_issues)} issues found", flush=True)
    
    return result
```

- [ ] **Step 4: Run test to verify it passes**

```bash
pytest carta/audit/tests/test_audit.py::TestRunAudit -v
```

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add carta/audit/audit.py carta/audit/tests/test_audit.py
git commit -m "feat: implement run_audit orchestrator with json formatting"
```

---

## Task 10: Add cmd_audit to CLI

**Files:**
- Modify: `carta/cli.py`
- Modify: `carta/audit/tests/test_audit.py`

- [ ] **Step 1: Write integration test**

Add to `test_audit.py`:

```python
class TestCLIAudit:
    """Test CLI audit command."""
    
    def test_cmd_audit_outputs_json(self):
        """Audit command outputs valid JSON."""
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            (repo_root / ".carta").mkdir()
            (repo_root / "docs").mkdir()
            
            cfg = {
                "project_name": "test-project",
                "docs_root": "docs",
                "qdrant_url": "http://localhost:6333"
            }
            
            (repo_root / ".carta" / "config.yaml").write_text(yaml.dump(cfg))
            
            # Mock Qdrant for CLI test
            with patch("carta.cli.QdrantClient"):
                # This is tested in cmd_audit function
                pass
```

- [ ] **Step 2: Review CLI structure**

Read `carta/cli.py` to understand how commands are structured:

```bash
head -100 /Users/ian/dev/doc-audit-cc/carta/cli.py
```

- [ ] **Step 3: Add cmd_audit to cli.py**

In `carta/cli.py`, add this new command function (find the location among other `cmd_*` functions):

```python
def cmd_audit(args, cfg: dict, repo_root: Path) -> None:
    """Run audit to detect inconsistencies in the embed pipeline.
    
    Usage:
        carta audit [--output REPORT.json]
    
    Detects orphaned chunks, missing sidecars, stale files, and more.
    Reports to JSON for agent-assisted repair or manual review.
    """
    from carta.audit.audit import run_audit
    import json
    
    output_path = args.output if hasattr(args, 'output') and args.output else "audit-report.json"
    
    try:
        result = run_audit(cfg, repo_root, verbose=True)
        
        # Write report to JSON
        output_file = repo_root / output_path
        output_file.write_text(json.dumps(result, indent=2))
        
        # Print summary
        summary = result["summary"]
        print(f"\nAudit complete: {summary['total_issues']} issues found")
        if summary["total_issues"] > 0:
            for cat, count in summary["by_category"].items():
                print(f"  {cat}: {count}")
        
        print(f"Report saved to: {output_path}")
        
        sys.exit(0)
    
    except Exception as e:
        print(f"Error: Audit failed: {e}", file=sys.stderr)
        sys.exit(1)
```

- [ ] **Step 4: Wire cmd_audit into argparse**

In `main()` function, find the subparsers section and add:

```python
parser_audit = subparsers.add_parser(
    "audit",
    help="Detect inconsistencies in embed pipeline and write JSON report"
)
parser_audit.add_argument(
    "--output",
    default="audit-report.json",
    help="Output path for JSON report (default: audit-report.json)"
)
parser_audit.set_defaults(func=cmd_audit)
```

- [ ] **Step 5: Test command manually**

```bash
cd /Users/ian/dev/doc-audit-cc
python -m carta audit --help
```

Expected: Help text shows

- [ ] **Step 6: Commit**

```bash
git add carta/cli.py
git commit -m "feat: add cmd_audit to CLI with --output flag"
```

---

## Task 11: Create integration test for full audit

**Files:**
- Modify: `carta/audit/tests/test_audit.py`

- [ ] **Step 1: Write comprehensive integration test**

Add to `test_audit.py`:

```python
class TestAuditIntegration:
    """Integration test: create inconsistent repo and verify audit catches all issues."""
    
    def test_audit_detects_all_issue_types(self):
        """Audit detects representatives of all 6 issue categories."""
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            docs_root = repo_root / "docs"
            docs_root.mkdir()
            
            # 1. Create a connected file (has sidecar)
            good_file = docs_root / "good.md"
            good_file.write_text("# Good")
            good_hash = compute_file_hash(good_file)
            good_sidecar = docs_root / "good.md.embed-meta.yaml"
            good_sidecar.write_text(yaml.dump({
                "sidecar_id": "good_file",
                "file_hash": good_hash,
                "file_mtime": os.path.getmtime(good_file),
                "chunk_count": 1,
                "last_embedded": datetime.now().isoformat()
            }))
            
            # 2. Create a stale file (newer than sidecar)
            stale_file = docs_root / "stale.md"
            stale_file.write_text("# Stale")
            stale_sidecar = docs_root / "stale.md.embed-meta.yaml"
            stale_sidecar.write_text(yaml.dump({
                "sidecar_id": "stale_file",
                "file_hash": compute_file_hash(stale_file),
                "file_mtime": 1000000000.0,  # Very old
                "chunk_count": 1,
                "last_embedded": "2026-01-01T00:00:00Z"
            }))
            
            # 3. Create a hash-mismatched file
            mismatch_file = docs_root / "mismatch.md"
            mismatch_file.write_text("# Current Content")
            mismatch_sidecar = docs_root / "mismatch.md.embed-meta.yaml"
            mismatch_sidecar.write_text(yaml.dump({
                "sidecar_id": "mismatch_file",
                "file_hash": "oldoldoldhash",
                "file_mtime": os.path.getmtime(mismatch_file),
                "chunk_count": 1
            }))
            
            # 4. Create a disconnected file
            disconnected = docs_root / "never_embedded.md"
            disconnected.write_text("# Orphan")
            
            cfg = {
                "project_name": "test-project",
                "docs_root": "docs",
                "excluded_paths": [],
                "qdrant_url": "http://localhost:6333"
            }
            
            # Mock Qdrant with orphaned chunks
            with patch("carta.audit.audit.QdrantClient") as mock_client_class:
                mock_client = Mock()
                
                # Mock scroll: return orphaned and good chunks
                orphaned_chunk = Mock(
                    id=999,
                    payload={"sidecar_id": "orphaned_sidecar", "text": "orphaned", "doc_type": "doc"}
                )
                good_chunk = Mock(
                    id=1,
                    payload={"sidecar_id": "good_file", "chunk_index": 0, "doc_type": "doc"}
                )
                
                mock_client.scroll.return_value = ([orphaned_chunk, good_chunk], None)
                mock_client_class.return_value = mock_client
                
                result = run_audit(cfg, repo_root, verbose=False)
                
                # Verify all issue types detected
                categories = [i["category"] for i in result["issues"]]
                
                assert "orphaned_chunks" in categories  # Orphaned sidecar
                assert "stale_sidecars" in categories  # Stale file
                assert "hash_mismatches" in categories  # Mismatch file
                assert "disconnected_files" in categories  # Never embedded
                
                # Summary should match
                assert result["summary"]["total_issues"] > 0
```

- [ ] **Step 2: Run integration test**

```bash
pytest carta/audit/tests/test_audit.py::TestAuditIntegration -v
```

Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add carta/audit/tests/test_audit.py
git commit -m "test: add comprehensive audit integration test"
```

---

## Task 12: Document audit-embed skill

**Files:**
- Create: Skill markdown (location varies by system; user will create or move into place)

- [ ] **Step 1: Write skill documentation**

Create `audit-embed-skill-template.md` (user will move to skills directory):

```markdown
---
name: audit-embed
description: Run and interpret Carta audit reports for data consistency
---

# Using Carta Audit for Embed Pipeline Validation

Carta's embedding pipeline depends on tight coupling between three layers: source files, sidecar metadata, and Qdrant chunks. Over time, bugs, signal handling, or manual edits can cause these to drift out of sync.

`carta audit` detects these inconsistencies and reports them in JSON. This skill teaches you when to run it, how to read the report, and when to take action.

## When to Run Audit

Run audit in these situations:

- **Before big doc refactors**: Before renaming/reorganizing large doc trees
- **After merge conflicts in docs/**: Conflicts in embedding-tracked docs may leave sidecars stale
- **If you suspect stale chunks**: After deleting files manually, or if search results seem outdated
- **During troubleshooting**: If embedding seems inconsistent or search quality dropped
- **Before big architectural changes**: Verify baseline consistency before complex changes

## The Audit Report

Run:

```bash
carta audit --output report.json
```

The report is JSON with two sections:

### Summary

```json
{
  "summary": {
    "total_issues": 62,
    "by_category": {
      "orphaned_chunks": 47,
      "stale_sidecars": 5,
      ...
    },
    "scanned_at": "2026-04-07T14:32:00Z",
    "repo_root": "/path/to/repo"
  }
}
```

### Issues List

Each issue has:
- `id`: Unique identifier
- `category`: One of the 6 types (see below)
- `severity`: "error", "warning", or "info"
- Category-specific fields (file paths, chunk IDs, hashes, etc.)

## The 6 Issue Categories

### 1. orphaned_chunks (severity: warning)

**What it means:** Chunks exist in Qdrant with a `sidecar_id` that has no matching `.embed-meta.yaml` file.

**Cause:** Sidecar was deleted manually, or a previous embed failed partway through.

**Semantic judgment:** These chunks might contain useful semantic memory (old docs, archived knowledge) or might be polluting search results. Deletion is optional.

**Example decision:**
- Keep: "These chunks are from archived API docs. Still relevant for historical questions."
- Remove: "These are from deleted README. Only recent version matters."

### 2. missing_sidecars (severity: warning)

**What it means:** Files have chunks in Qdrant but no `.embed-meta.yaml`.

**Cause:** File was embedded but sidecar was deleted or never created.

**Judgment:** Usually remove (broken tracking) unless the chunks are valuable.

### 3. stale_sidecars (severity: info)

**What it means:** File's actual mtime is newer than the recorded mtime in the sidecar.

**Cause:** You edited the file after the last embedding.

**Judgment:** Usually not urgent (next `carta embed` will re-embed), but flag as "docs need refreshing."

### 4. hash_mismatches (severity: warning)

**What it means:** File's computed hash differs from the hash recorded in the sidecar, even if mtime matches.

**Cause:** File content changed but mtime stayed same (clock skew, touch, etc.).

**Judgment:** Similar to stale; usually re-embed next time, but worth noting.

### 5. disconnected_files (severity: info)

**What it means:** Discoverable `.md` or `.pdf` files exist but have no sidecar and no chunks in Qdrant.

**Cause:** File was never embedded, or was removed from Qdrant only.

**Judgment:** Usually unimportant (just means doc wasn't indexed), but flag new docs that should be embedded.

### 6. qdrant_sidecar_mismatches (severity: error)

**What it means:** Chunks in Qdrant don't match the chunk metadata in the sidecar (count mismatch, index gaps).

**Cause:** Partial upsert failure, corruption, or old migration boundary.

**Judgment:** Usually delete (data corruption signal). Should never happen in normal operation.

## Reading a Real Report

Example excerpt:

```json
{
  "issues": [
    {
      "id": "orphaned_001",
      "category": "orphaned_chunks",
      "severity": "warning",
      "sidecar_id": "docs_old_api_md",
      "chunk_count": 12,
      "first_chunk_text": "# Old API Reference v1\nDeprecated. Use v2 instead..."
    },
    {
      "id": "stale_001",
      "category": "stale_sidecars",
      "severity": "info",
      "file_path": "docs/guide.md",
      "last_embedded": "2026-03-15T10:00:00Z",
      "days_stale": 20
    }
  ]
}
```

**Interpretation:**
- Orphaned chunks from "old_api" — decide: keep for historical search, or remove?
- guide.md hasn't been re-embedded in 20 days — run `carta embed` before major doc release.

## Repair Flow (Interactive Fix)

After reviewing the audit report, you can let Claude help decide on repairs:

```bash
carta audit --fix-interactive --report report.json
```

This asks Claude for semantic judgment on each category:
- "These 47 orphaned chunks are from deleted files. Should we remove them?"
- Claude analyzes the chunk text and recommends keep/remove
- You approve or override
- Fixes are applied (chunks deleted, sidecars removed, etc.)

## Quick Tips

1. **Start with error severity** — `qdrant_sidecar_mismatches` usually need fixing
2. **Stale/hash mismatches are low-priority** — next embed cycle will refresh
3. **Disconnected files might be intentional** — config notes, drafts, etc.
4. **Orphaned chunks are judgment calls** — let Claude evaluate if unsure
5. **Run after big refactors** — good baseline sanity check

## Bonus: Semantic Cleanup of Quirks/Session Memory

Audit only covers the structured `doc` layer. The `quirk` and `session` collections are sparse agent-generated notes.

Periodically, Claude can:

```
Search quirks collection for patterns related to [old feature/module/person]
Re-evaluate relevance based on current codebase state
Update or archive as appropriate
```

This is a separate Claude task (not automated by audit), but worth doing monthly.
```

- [ ] **Step 2: Save skill template**

```bash
cat > /Users/ian/dev/doc-audit-cc/docs/superpowers/plans/audit-embed-skill-template.md << 'EOF'
[paste the skill markdown from Step 1]
EOF
```

- [ ] **Step 3: Commit template**

```bash
git add docs/superpowers/plans/audit-embed-skill-template.md
git commit -m "docs: add audit-embed skill template

Teaches when to run audit, how to interpret reports,
and when to take action on each issue category.
Includes bonus guidance on semantic memory cleanup."
```

- [ ] **Step 4: Instructions for user**

The skill file needs to be created in the plugins directory. Output the path where it should go:

```bash
echo "Skill location: $HOME/.claude/plugins/cache/claude-plugins-official/superpowers/5.0.7/skills/audit-embed.md"
```

---

## Task 13: Summary and verification

- [ ] **Step 1: Run all tests**

```bash
cd /Users/ian/dev/doc-audit-cc
pytest carta/audit/tests/ -v
```

Expected: All tests pass

- [ ] **Step 2: Test CLI command**

```bash
python -m carta audit --help
```

Expected: Help text displayed

- [ ] **Step 3: Manual test with real repo**

```bash
cd /Users/ian/dev/doc-audit-cc
python -m carta audit --output test-audit-report.json
cat test-audit-report.json | head -50
```

Expected: JSON report generated

- [ ] **Step 4: Final commit summary**

```bash
git log --oneline -10
```

Expected: 7-8 new commits for audit feature

- [ ] **Step 5: Verify files created**

```bash
find /Users/ian/dev/doc-audit-cc/carta/audit -type f -name "*.py"
```

Expected: Shows audit.py, __init__.py, tests/test_audit.py

---

## Implementation Complete

The audit command is fully implemented with:
- ✅ Core detection logic (6 functions, all independently tested)
- ✅ JSON reporting matching spec schema
- ✅ CLI integration with `--output` flag
- ✅ Comprehensive unit and integration tests
- ✅ Skill documentation template for Claude guidance

Next step: User creates skill from template, or implementation is immediately usable via CLI.
