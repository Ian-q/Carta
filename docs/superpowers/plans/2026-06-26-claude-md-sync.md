# CLAUDE.md ↔ docs Sync Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Detect CLAUDE.md sections the updated docs have superseded and hand the in-session agent the evidence to draft corrections — local detection, human-gated rewrites.

**Architecture:** Reuse `carta/hook/stale_scan.py::run_stale_scan` (section → search graph → ≤2B judge → findings) with CLAUDE.md fed in as a `ChangedDoc` built directly (not via the git collectors), so no `_in_doc_scope` change is needed. A new `carta/hook/claude_md.py` orchestrates: section CLAUDE.md, skip pinned/unchanged sections via an out-of-band sync sidecar, run the scan, group findings back up to their headings with the superseding excerpts attached. A `carta claude-md {check,record}` CLI emits JSON for a `/claude-md-sync` skill; the agent drafts and the human approves.

**Tech Stack:** Python 3.10+, PyYAML, pytest. Reuses `run_stale_scan`, `sections_from_markdown`, the shared `ollama_yesno` judge, and `hooks.stale_scan.*` config.

## Global Constraints

- Python 3.10+ syntax; 4-space indent; type hints on signatures; `snake_case` functions, `UPPERCASE` constants.
- **Local only:** detection uses the existing ≤2B Ollama judge (`hooks.stale_scan.ollama_model`, default `qwen3.5:0.8b`). No larger model, no new infra.
- **Fail open everywhere:** judge `None` → not flagged; search exception → skip section; missing/corrupt sidecar → treat sections as unpinned/unhashed; never block; **never auto-write CLAUDE.md**.
- **CLAUDE.md is a scan target only** — never embedded as a graph source.
- Reuse `hooks.stale_scan.*` config: `candidate_threshold` 0.65, `max_judge_calls` 30, `ollama_model`, `judge_timeout_s` 5.
- Sidecar metadata is **out-of-band** at `.carta/sidecars/CLAUDE.md.sync.yaml` — never written into CLAUDE.md (it is injected into every session).
- TDD: failing test first, minimal implementation, frequent commits. Tests inject fake `search_fn`/`judge_fn` — no live Qdrant/Ollama.

---

## File Structure

- `carta/hook/stale_scan.py` — **modify**: add `candidate_excerpt` to `StaleFinding`; populate it in `run_stale_scan`.
- `carta/hook/claude_md_sidecar.py` — **create**: sync-sidecar persistence (path, hash, load/write) + the graph-change guard.
- `carta/hook/claude_md.py` — **create**: `scan_claude_md`, `group_findings_by_heading`, `record_sync`.
- `carta/cli.py` — **modify**: add `cmd_claude_md`; wire the `claude-md` subparser; add the pre-PR nudge to `cmd_hook`'s `check` path.
- `carta/config.py` — **modify**: add `claude_md_nudge: True` under `hooks.stale_scan`.
- `carta/skills/claude-md-sync/SKILL.md` — **create**: the agent-facing end-of-session skill.
- `carta/hook/tests/test_stale_scan.py` — **modify**: cover the new field.
- `carta/hook/tests/test_claude_md_sidecar.py` — **create**.
- `carta/hook/tests/test_claude_md.py` — **create**.
- `carta/tests/test_cli.py` — **modify**: CLI dispatch smoke test.
- `CLAUDE.md`, `README.md` — **modify**: document the new command (Task 10).

---

## Task 1: Enrich `StaleFinding` with the superseding excerpt

**Files:**
- Modify: `carta/hook/stale_scan.py` (the `StaleFinding` dataclass ~line 21; the `result.findings.append(...)` in `run_stale_scan` ~line 237)
- Test: `carta/hook/tests/test_stale_scan.py`

**Interfaces:**
- Produces: `StaleFinding` now has `candidate_excerpt: str = ""`, populated from `hits[0].get("excerpt", "")`. All later tasks read `finding.candidate_excerpt`.

- [ ] **Step 1: Write the failing test**

Append to `carta/hook/tests/test_stale_scan.py`:

```python
def test_finding_carries_candidate_excerpt():
    from carta.hook.stale_scan import run_stale_scan, ChangedDoc

    doc = ChangedDoc(path="docs/a.md", text="## Title\n\nOld approach uses polling.")
    search_fn = lambda q: [{
        "source": "docs/b.md",
        "score": 0.9,
        "excerpt": "The polling approach was replaced by push events.",
    }]
    judge_fn = lambda section_text, candidate: True

    result = run_stale_scan(
        repo_root=None, cfg={}, changed_docs=[doc],
        search_fn=search_fn, judge_fn=judge_fn,
    )
    assert len(result.findings) == 1
    assert result.findings[0].candidate_excerpt == "The polling approach was replaced by push events."
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest carta/hook/tests/test_stale_scan.py::test_finding_carries_candidate_excerpt -v`
Expected: FAIL — `TypeError: __init__() got an unexpected keyword argument 'candidate_excerpt'` (or `AttributeError`).

- [ ] **Step 3: Add the field and populate it**

In `carta/hook/stale_scan.py`, add the field to the dataclass:

```python
@dataclass
class StaleFinding:
    file: str
    section: str
    snippet: str
    candidate_path: str
    candidate_score: float
    candidate_excerpt: str = ""
```

And in `run_stale_scan`, extend the append:

```python
            if verdict:
                result.findings.append(StaleFinding(
                    file=doc.path,
                    section=chunk.get("section_heading", ""),
                    snippet=chunk["text"][:160],
                    candidate_path=hits[0].get("source", ""),
                    candidate_score=hits[0].get("score", 0.0),
                    candidate_excerpt=hits[0].get("excerpt", ""),
                ))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest carta/hook/tests/test_stale_scan.py -v`
Expected: PASS (new test plus all existing stale_scan tests).

- [ ] **Step 5: Commit**

```bash
git add carta/hook/stale_scan.py carta/hook/tests/test_stale_scan.py
git commit -m "feat(stale-scan): carry candidate_excerpt on StaleFinding"
```

---

## Task 2: Sync-sidecar persistence module

**Files:**
- Create: `carta/hook/claude_md_sidecar.py`
- Test: `carta/hook/tests/test_claude_md_sidecar.py`

**Interfaces:**
- Produces:
  - `sync_sidecar_path(repo_root: Path) -> Path` → `<repo>/.carta/sidecars/CLAUDE.md.sync.yaml`
  - `section_hash(text: str) -> str` (sha256 hex)
  - `load_sync_sidecar(repo_root: Path) -> dict` → always a dict with keys `schema`, `last_synced`, `sections` (missing/corrupt → defaults)
  - `write_sync_sidecar(repo_root: Path, data: dict) -> None`

- [ ] **Step 1: Write the failing test**

Create `carta/hook/tests/test_claude_md_sidecar.py`:

```python
from pathlib import Path

from carta.hook import claude_md_sidecar as sc


def test_section_hash_is_deterministic_and_changes_with_text():
    assert sc.section_hash("hello") == sc.section_hash("hello")
    assert sc.section_hash("hello") != sc.section_hash("world")


def test_sync_sidecar_path(tmp_path: Path):
    assert sc.sync_sidecar_path(tmp_path) == tmp_path / ".carta" / "sidecars" / "CLAUDE.md.sync.yaml"


def test_load_missing_returns_defaults(tmp_path: Path):
    data = sc.load_sync_sidecar(tmp_path)
    assert data == {"schema": 1, "last_synced": None, "sections": {}}


def test_load_corrupt_returns_defaults(tmp_path: Path):
    p = sc.sync_sidecar_path(tmp_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{not: valid: yaml: [", encoding="utf-8")
    data = sc.load_sync_sidecar(tmp_path)
    assert data["sections"] == {}


def test_write_then_load_round_trips(tmp_path: Path):
    sc.write_sync_sidecar(tmp_path, {
        "schema": 1,
        "last_synced": "2026-06-26T00:00:00+00:00",
        "sections": {"## A": {"hash": "abc", "pinned": True, "last_reviewed": "2026-06-26T00:00:00+00:00"}},
    })
    data = sc.load_sync_sidecar(tmp_path)
    assert data["last_synced"] == "2026-06-26T00:00:00+00:00"
    assert data["sections"]["## A"]["pinned"] is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest carta/hook/tests/test_claude_md_sidecar.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'carta.hook.claude_md_sidecar'`.

- [ ] **Step 3: Write the module**

Create `carta/hook/claude_md_sidecar.py`:

```python
"""Out-of-band sync metadata for CLAUDE.md (pins, per-section hashes, last_synced).

Lives at .carta/sidecars/CLAUDE.md.sync.yaml — never written into CLAUDE.md itself,
which Claude Code injects verbatim into every session. Fails open: missing or corrupt
sidecar reads back as empty defaults so the scan simply treats every section as fresh."""
from __future__ import annotations

import hashlib
from pathlib import Path

import yaml

SYNC_SIDECAR_REL = Path(".carta") / "sidecars" / "CLAUDE.md.sync.yaml"


def sync_sidecar_path(repo_root: Path) -> Path:
    return repo_root / SYNC_SIDECAR_REL


def section_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_sync_sidecar(repo_root: Path) -> dict:
    """Read the sync sidecar; always return a dict with schema/last_synced/sections."""
    path = sync_sidecar_path(repo_root)
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        data = None
    if not isinstance(data, dict):
        return {"schema": 1, "last_synced": None, "sections": {}}
    data.setdefault("schema", 1)
    data.setdefault("last_synced", None)
    if not isinstance(data.get("sections"), dict):
        data["sections"] = {}
    return data


def write_sync_sidecar(repo_root: Path, data: dict) -> None:
    path = sync_sidecar_path(repo_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest carta/hook/tests/test_claude_md_sidecar.py -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add carta/hook/claude_md_sidecar.py carta/hook/tests/test_claude_md_sidecar.py
git commit -m "feat(claude-md): sync-sidecar persistence (path, hash, load/write)"
```

---

## Task 3: Graph-change guard

The safe skip rule: a section is only skipped as "unchanged" when **both** its text hash matches **and** no doc was re-embedded since the last sync. This task adds the graph-change half. Staleness is triggered by *docs* changing, so a section with stable text can still be freshly superseded — ignoring that is the exact failure this feature exists to prevent.

**Files:**
- Modify: `carta/hook/claude_md_sidecar.py`
- Test: `carta/hook/tests/test_claude_md_sidecar.py`

**Interfaces:**
- Produces:
  - `latest_embed_time(repo_root: Path) -> datetime | None` — newest `indexed_at` across `.embed-meta.yaml` sidecars
  - `graph_changed_since(repo_root: Path, last_synced: str | None) -> bool` — True (re-scan) unless we can prove nothing was embedded after `last_synced`

- [ ] **Step 1: Write the failing test**

Append to `carta/hook/tests/test_claude_md_sidecar.py`:

```python
def _write_embed_sidecar(repo_root: Path, name: str, indexed_at):
    p = repo_root / ".carta" / "sidecars" / "docs" / f"{name}.embed-meta.yaml"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"slug: {name}\nindexed_at: {indexed_at}\n", encoding="utf-8")


def test_graph_changed_when_no_last_synced(tmp_path: Path):
    assert sc.graph_changed_since(tmp_path, None) is True


def test_graph_unchanged_when_no_embed_newer(tmp_path: Path):
    _write_embed_sidecar(tmp_path, "a", "2026-06-20T00:00:00+00:00")
    assert sc.graph_changed_since(tmp_path, "2026-06-25T00:00:00+00:00") is False


def test_graph_changed_when_embed_is_newer(tmp_path: Path):
    _write_embed_sidecar(tmp_path, "a", "2026-06-26T12:00:00+00:00")
    assert sc.graph_changed_since(tmp_path, "2026-06-25T00:00:00+00:00") is True


def test_graph_changed_handles_z_suffix(tmp_path: Path):
    _write_embed_sidecar(tmp_path, "a", "2026-06-26T12:00:00Z")
    assert sc.graph_changed_since(tmp_path, "2026-06-25T00:00:00Z") is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest carta/hook/tests/test_claude_md_sidecar.py -k graph -v`
Expected: FAIL — `AttributeError: module 'carta.hook.claude_md_sidecar' has no attribute 'graph_changed_since'`.

- [ ] **Step 3: Implement the guard**

Add to `carta/hook/claude_md_sidecar.py` (extend the imports and append the functions):

```python
from datetime import datetime, timezone


def _parse_iso(ts) -> datetime | None:
    """Tolerant ISO-8601 parse (handles a trailing 'Z'); naive → UTC. None on failure."""
    if not isinstance(ts, str):
        return None
    s = ts.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def latest_embed_time(repo_root: Path) -> datetime | None:
    """Newest indexed_at across all embed sidecars, or None if none/unreadable."""
    sidecar_dir = repo_root / ".carta" / "sidecars"
    latest: datetime | None = None
    try:
        paths = list(sidecar_dir.rglob("*.embed-meta.yaml"))
    except OSError:
        return None
    for p in paths:
        try:
            meta = yaml.safe_load(p.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            continue
        if not isinstance(meta, dict):
            continue
        dt = _parse_iso(meta.get("indexed_at"))
        if dt is not None and (latest is None or dt > latest):
            latest = dt
    return latest


def graph_changed_since(repo_root: Path, last_synced: str | None) -> bool:
    """True if a doc may have been embedded after last_synced — so unchanged CLAUDE.md
    sections must still be re-scanned. Fails toward True (re-scan) on any missing data."""
    base = _parse_iso(last_synced)
    if base is None:
        return True
    latest = latest_embed_time(repo_root)
    if latest is None:
        return True
    return latest > base
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest carta/hook/tests/test_claude_md_sidecar.py -v`
Expected: PASS (9 tests total).

- [ ] **Step 5: Commit**

```bash
git add carta/hook/claude_md_sidecar.py carta/hook/tests/test_claude_md_sidecar.py
git commit -m "feat(claude-md): graph-change guard for safe section skipping"
```

---

## Task 4: Group findings back up to their headings

`chunk_text` splits a section into ~400-token chunks, so a single `###` section can produce several chunk-level findings. This task rolls them up to one entry per heading, attaching the full section text and the deduped superseding excerpts.

**Files:**
- Create: `carta/hook/claude_md.py`
- Test: `carta/hook/tests/test_claude_md.py`

**Interfaces:**
- Consumes: `StaleFinding` (with `section`, `candidate_path`, `candidate_excerpt`, `candidate_score`); section dicts `{"page", "text", "headings": [heading]}` from `sections_from_markdown`.
- Produces: `group_findings_by_heading(findings: list, sections: list[dict]) -> list[dict]`, each `{"heading": str, "section_text": str, "superseding": [{"source", "excerpt", "score"}]}`.

- [ ] **Step 1: Write the failing test**

Create `carta/hook/tests/test_claude_md.py`:

```python
from carta.hook.stale_scan import StaleFinding
from carta.hook import claude_md


def test_group_rolls_chunks_up_to_heading_and_dedupes():
    sections = [
        {"page": 1, "text": "### Surface\n\nbig table ...", "headings": ["### Surface"]},
        {"page": 2, "text": "### Other\n\n...", "headings": ["### Other"]},
    ]
    findings = [
        StaleFinding("CLAUDE.md", "### Surface", "snip", "docs/a.md", 0.81, "A says new."),
        StaleFinding("CLAUDE.md", "### Surface", "snip", "docs/a.md", 0.79, "A dup."),
        StaleFinding("CLAUDE.md", "### Surface", "snip", "docs/b.md", 0.77, "B says new."),
    ]
    grouped = claude_md.group_findings_by_heading(findings, sections)

    assert len(grouped) == 1
    entry = grouped[0]
    assert entry["heading"] == "### Surface"
    assert entry["section_text"].startswith("### Surface")
    # deduped by candidate_path: docs/a.md once, docs/b.md once
    assert [s["source"] for s in entry["superseding"]] == ["docs/a.md", "docs/b.md"]
    assert entry["superseding"][0]["excerpt"] == "A says new."
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest carta/hook/tests/test_claude_md.py::test_group_rolls_chunks_up_to_heading_and_dedupes -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'carta.hook.claude_md'`.

- [ ] **Step 3: Write the module skeleton + grouping**

Create `carta/hook/claude_md.py`:

```python
"""CLAUDE.md ↔ docs sync: scan CLAUDE.md against the docs graph and surface the
sections the docs have superseded, so the in-session agent can draft corrections.

Detection only — never edits CLAUDE.md. Reuses run_stale_scan with CLAUDE.md fed in
as a ChangedDoc built directly here (bypassing the git collectors). Fails open."""
from __future__ import annotations

from pathlib import Path

from carta.embed.parse import sections_from_markdown
from carta.hook.stale_scan import ChangedDoc, run_stale_scan
from carta.hook import claude_md_sidecar as sc

CLAUDE_MD = "CLAUDE.md"


def _read_sections(repo_root: Path) -> list[dict]:
    text = (repo_root / CLAUDE_MD).read_text(encoding="utf-8-sig", errors="replace")
    sections, _ = sections_from_markdown(text)
    return sections


def group_findings_by_heading(findings: list, sections: list[dict]) -> list[dict]:
    """Roll chunk-level findings up to one entry per heading, with the full section
    text and the superseding excerpts (deduped by candidate path, order preserved)."""
    text_by_heading = {s["headings"][0]: s["text"] for s in sections}
    grouped: dict[str, dict] = {}
    for f in findings:
        entry = grouped.setdefault(f.section, {
            "heading": f.section,
            "section_text": text_by_heading.get(f.section, ""),
            "superseding": [],
            "_seen": set(),
        })
        if f.candidate_path in entry["_seen"]:
            continue
        entry["_seen"].add(f.candidate_path)
        entry["superseding"].append({
            "source": f.candidate_path,
            "excerpt": f.candidate_excerpt,
            "score": round(f.candidate_score, 4),
        })
    out = []
    for entry in grouped.values():
        entry.pop("_seen", None)
        out.append(entry)
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest carta/hook/tests/test_claude_md.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add carta/hook/claude_md.py carta/hook/tests/test_claude_md.py
git commit -m "feat(claude-md): group findings by heading with superseding excerpts"
```

---

## Task 5: `scan_claude_md` orchestrator

**Files:**
- Modify: `carta/hook/claude_md.py`
- Test: `carta/hook/tests/test_claude_md.py`

**Interfaces:**
- Consumes: `claude_md_sidecar.{load_sync_sidecar, graph_changed_since, section_hash}`; `group_findings_by_heading`; `run_stale_scan`.
- Produces: `scan_claude_md(repo_root: Path, cfg: dict, *, search_fn=None, judge_fn=None) -> dict` →
  `{"scanned": bool, "findings": [...], "skipped_pinned": int, "skipped_unchanged": int, "judge_calls": int}` (or `{"scanned": False, "reason": str, "findings": []}` when CLAUDE.md is absent).

- [ ] **Step 1: Write the failing test**

Append to `carta/hook/tests/test_claude_md.py`:

```python
def _write_claude_md(repo_root, body):
    (repo_root / "CLAUDE.md").write_text(body, encoding="utf-8")


def _write_embed_sidecar(repo_root, name, indexed_at):
    p = repo_root / ".carta" / "sidecars" / "docs" / f"{name}.embed-meta.yaml"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"slug: {name}\nindexed_at: {indexed_at}\n", encoding="utf-8")


def test_scan_skips_pinned_and_flags_stale(tmp_path):
    _write_claude_md(tmp_path, (
        "## Constraints\n\nAlways use TDD.\n\n"
        "### Surface\n\nThe embed command does the old thing.\n"
    ))
    # pin the Constraints section
    sc.write_sync_sidecar(tmp_path, {
        "schema": 1, "last_synced": None,
        "sections": {"## Constraints": {"hash": "x", "pinned": True}},
    })

    seen = {}
    def search_fn(q):
        seen["q"] = q
        return [{"source": "docs/embed.md", "score": 0.9, "excerpt": "embed now does the new thing"}]
    judge_fn = lambda section_text, candidate: "Surface" in section_text or "embed" in section_text

    out = claude_md.scan_claude_md(tmp_path, {}, search_fn=search_fn, judge_fn=judge_fn)

    assert out["scanned"] is True
    assert out["skipped_pinned"] == 1
    headings = [f["heading"] for f in out["findings"]]
    assert "### Surface" in headings
    assert "## Constraints" not in headings  # pinned never scanned


def test_scan_returns_not_scanned_when_no_claude_md(tmp_path):
    out = claude_md.scan_claude_md(tmp_path, {})
    assert out["scanned"] is False
    assert out["findings"] == []


def test_scan_skips_unchanged_when_graph_unchanged(tmp_path):
    from carta.embed.parse import sections_from_markdown
    body = "### Surface\n\nstable text here\n"
    _write_claude_md(tmp_path, body)
    secs, _ = sections_from_markdown(body)
    section_text = secs[0]["text"]
    # A doc exists in the graph, but was embedded BEFORE last_synced → graph unchanged
    # since the last sync, so an unchanged section is safe to skip.
    _write_embed_sidecar(tmp_path, "old", "2026-06-20T00:00:00+00:00")
    sc.write_sync_sidecar(tmp_path, {
        "schema": 1,
        "last_synced": "2026-06-25T00:00:00+00:00",
        "sections": {"### Surface": {"hash": sc.section_hash(section_text), "pinned": False}},
    })

    called = {"n": 0}
    def search_fn(q):
        called["n"] += 1
        return []
    out = claude_md.scan_claude_md(tmp_path, {}, search_fn=search_fn, judge_fn=lambda *a: False)
    assert out["skipped_unchanged"] == 1
    assert called["n"] == 0  # unchanged + graph-unchanged → never searched
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest carta/hook/tests/test_claude_md.py -k scan -v`
Expected: FAIL — `AttributeError: module 'carta.hook.claude_md' has no attribute 'scan_claude_md'`.

- [ ] **Step 3: Implement `scan_claude_md`**

Append to `carta/hook/claude_md.py`:

```python
def scan_claude_md(repo_root: Path, cfg: dict, *, search_fn=None, judge_fn=None) -> dict:
    """Scan CLAUDE.md against the docs graph and return superseded sections.

    Skips pinned sections, and sections whose text is unchanged AND whose graph is
    unchanged since last_synced. Builds a ChangedDoc from the remaining sections and
    reuses run_stale_scan. Detection only; never edits CLAUDE.md."""
    if not (repo_root / CLAUDE_MD).exists():
        return {"scanned": False, "reason": "no CLAUDE.md", "findings": []}

    sections = _read_sections(repo_root)
    sidecar = sc.load_sync_sidecar(repo_root)
    meta = sidecar.get("sections", {})
    graph_changed = sc.graph_changed_since(repo_root, sidecar.get("last_synced"))

    to_scan: list[dict] = []
    skipped_pinned = 0
    skipped_unchanged = 0
    for s in sections:
        heading = s["headings"][0]
        entry = meta.get(heading, {})
        if entry.get("pinned"):
            skipped_pinned += 1
            continue
        if not graph_changed and entry.get("hash") == sc.section_hash(s["text"]):
            skipped_unchanged += 1
            continue
        to_scan.append(s)

    findings: list = []
    judge_calls = 0
    if to_scan:
        scan_text = "\n\n".join(s["text"] for s in to_scan)
        result = run_stale_scan(
            repo_root, cfg, [ChangedDoc(path=CLAUDE_MD, text=scan_text)],
            search_fn=search_fn, judge_fn=judge_fn,
        )
        findings = result.findings
        judge_calls = result.judge_calls

    return {
        "scanned": True,
        "findings": group_findings_by_heading(findings, sections),
        "skipped_pinned": skipped_pinned,
        "skipped_unchanged": skipped_unchanged,
        "judge_calls": judge_calls,
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest carta/hook/tests/test_claude_md.py -v`
Expected: PASS (all scan + grouping tests).

- [ ] **Step 5: Commit**

```bash
git add carta/hook/claude_md.py carta/hook/tests/test_claude_md.py
git commit -m "feat(claude-md): scan_claude_md orchestrator with pin/unchanged skip"
```

---

## Task 6: `record_sync` finalizer

After the agent applies approved edits, re-hash every current section and stamp `last_synced` so the next run skips unchanged sections. Pins are preserved; vanished sections are dropped.

**Files:**
- Modify: `carta/hook/claude_md.py`
- Test: `carta/hook/tests/test_claude_md.py`

**Interfaces:**
- Produces: `record_sync(repo_root: Path, now_iso: str) -> dict` (the written sidecar). `now_iso` is injected so tests stay deterministic; the CLI passes `datetime.now(timezone.utc).isoformat()`.

- [ ] **Step 1: Write the failing test**

Append to `carta/hook/tests/test_claude_md.py`:

```python
def test_record_sync_hashes_sections_and_preserves_pins(tmp_path):
    _write_claude_md(tmp_path, "## Constraints\n\nAlways TDD.\n\n### Surface\n\nNew text.\n")
    sc.write_sync_sidecar(tmp_path, {
        "schema": 1, "last_synced": None,
        "sections": {"## Constraints": {"hash": "old", "pinned": True}},
    })

    written = claude_md.record_sync(tmp_path, "2026-06-26T09:00:00+00:00")

    assert written["last_synced"] == "2026-06-26T09:00:00+00:00"
    assert written["sections"]["## Constraints"]["pinned"] is True   # pin preserved
    assert written["sections"]["## Constraints"]["hash"] != "old"    # re-hashed
    assert "### Surface" in written["sections"]                      # new section recorded
    # round-trips through disk
    assert sc.load_sync_sidecar(tmp_path)["sections"]["### Surface"]["hash"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest carta/hook/tests/test_claude_md.py::test_record_sync_hashes_sections_and_preserves_pins -v`
Expected: FAIL — `AttributeError: module 'carta.hook.claude_md' has no attribute 'record_sync'`.

- [ ] **Step 3: Implement `record_sync`**

Append to `carta/hook/claude_md.py`:

```python
def record_sync(repo_root: Path, now_iso: str) -> dict:
    """Re-hash all current CLAUDE.md sections and stamp last_synced. Preserves pins;
    drops sections that no longer exist. Call after approved edits are applied."""
    sidecar = sc.load_sync_sidecar(repo_root)
    prev = sidecar.get("sections", {})
    new_sections: dict[str, dict] = {}
    for s in _read_sections(repo_root):
        heading = s["headings"][0]
        new_sections[heading] = {
            "hash": sc.section_hash(s["text"]),
            "pinned": bool(prev.get(heading, {}).get("pinned", False)),
            "last_reviewed": now_iso,
        }
    sidecar["schema"] = 1
    sidecar["sections"] = new_sections
    sidecar["last_synced"] = now_iso
    sc.write_sync_sidecar(repo_root, sidecar)
    return sidecar
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest carta/hook/tests/test_claude_md.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add carta/hook/claude_md.py carta/hook/tests/test_claude_md.py
git commit -m "feat(claude-md): record_sync finalizer (re-hash, stamp, preserve pins)"
```

---

## Task 7: CLI `carta claude-md {check,record}` + config default

**Files:**
- Modify: `carta/cli.py` (add `cmd_claude_md`; register the subparser in `main()`)
- Modify: `carta/config.py` (add `claude_md_nudge: True` under `hooks.stale_scan`)
- Test: `carta/tests/test_cli.py`

**Interfaces:**
- Consumes: `claude_md.scan_claude_md`, `claude_md.record_sync`, `find_config`, `load_config`.
- Produces: CLI command `carta claude-md check` (prints findings JSON) and `carta claude-md record` (re-hashes + stamps, prints `{"recorded": true, ...}`). Both exit 0 (fail-open).

- [ ] **Step 1: Write the failing test**

Append to `carta/tests/test_cli.py` (import `pytest` and `json` if not already imported at the top of the file):

```python
def test_cmd_claude_md_check_prints_json(tmp_path, monkeypatch, capsys):
    import json
    from carta import cli

    cfg_path = tmp_path / ".carta" / "config.yaml"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text("project_name: demo\n", encoding="utf-8")

    monkeypatch.setattr(cli, "find_config", lambda: cfg_path)
    monkeypatch.setattr("carta.config.load_config", lambda p: {"project_name": "demo"})
    monkeypatch.setattr(
        "carta.hook.claude_md.scan_claude_md",
        lambda repo_root, cfg, **kw: {"scanned": True, "findings": [], "skipped_pinned": 0,
                                      "skipped_unchanged": 0, "judge_calls": 0},
    )

    args = type("A", (), {"claude_md_action": "check"})()
    with pytest.raises(SystemExit) as ex:
        cli.cmd_claude_md(args)
    assert ex.value.code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["scanned"] is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest carta/tests/test_cli.py::test_cmd_claude_md_check_prints_json -v`
Expected: FAIL — `AttributeError: module 'carta.cli' has no attribute 'cmd_claude_md'`.

- [ ] **Step 3: Add `cmd_claude_md` and wire the subparser**

In `carta/cli.py`, add the command function (place it near `cmd_hook`):

```python
def cmd_claude_md(args):
    import json
    from datetime import datetime, timezone
    from carta.config import load_config
    from carta.hook import claude_md

    try:
        cfg_path = find_config()
    except FileNotFoundError:
        print(json.dumps({"scanned": False, "reason": "not a Carta repo", "findings": []}))
        sys.exit(0)
    cfg = load_config(cfg_path)
    repo_root = cfg_path.parent.parent
    action = getattr(args, "claude_md_action", None) or "check"

    if action == "record":
        now_iso = datetime.now(timezone.utc).isoformat()
        claude_md.record_sync(repo_root, now_iso)
        print(json.dumps({"recorded": True, "last_synced": now_iso}))
        sys.exit(0)

    try:
        out = claude_md.scan_claude_md(repo_root, cfg)
    except Exception as e:
        print(json.dumps({"scanned": False, "reason": f"scan error (fail-open): {e}", "findings": []}))
        sys.exit(0)
    print(json.dumps(out, indent=2))
    sys.exit(0)
```

In `main()`, register the subparser alongside the other `sub.add_parser(...)` calls:

```python
    claude_md_p = sub.add_parser("claude-md", help="Sync CLAUDE.md against the docs graph")
    cm_sub = claude_md_p.add_subparsers(dest="claude_md_action")
    cm_sub.add_parser("check", help="Report CLAUDE.md sections the docs have superseded (JSON)")
    cm_sub.add_parser("record", help="Re-hash sections and stamp last_synced after a sync")
    claude_md_p.set_defaults(func=cmd_claude_md)
```

> Dispatch: `main()` already invokes `args.func(args)` for `set_defaults(func=...)` commands (e.g. `cmd_audit`). Follow that same path; no new dispatch code is needed.

In `carta/config.py`, add the nudge default under `hooks.stale_scan` (after `max_judge_calls`):

```python
        "stale_scan": {
            "enabled": True,
            "block_on_stale": False,
            "candidate_threshold": 0.65,
            "judge_timeout_s": 5,
            "ollama_model": "qwen3.5:0.8b",
            "max_judge_calls": 30,
            "claude_md_nudge": True,
        },
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest carta/tests/test_cli.py::test_cmd_claude_md_check_prints_json -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add carta/cli.py carta/config.py carta/tests/test_cli.py
git commit -m "feat(cli): carta claude-md {check,record} + claude_md_nudge default"
```

---

## Task 8: Pre-PR nudge in `carta hook check`

Cheap reminder: when the stale-scan path saw changed docs and CLAUDE.md exists, print one line pointing at the sync. No extra judge calls — it keys off `result.scanned`.

**Files:**
- Modify: `carta/cli.py` (`cmd_hook`, in the `action == "check"` branch, after `_print_stale_result(result, scfg)`)
- Test: `carta/tests/test_cli.py`

**Interfaces:**
- Consumes: `scfg` (`hooks.stale_scan`), `result.scanned`, `repo_root`. Produces only stderr output; no behavior change to exit codes.

- [ ] **Step 1: Write the failing test**

Append to `carta/tests/test_cli.py`:

```python
def test_hook_check_emits_claude_md_nudge(tmp_path, capsys):
    from carta.cli import _maybe_claude_md_nudge

    (tmp_path / "CLAUDE.md").write_text("# x\n", encoding="utf-8")
    result = type("R", (), {"scanned": 2})()
    _maybe_claude_md_nudge(result, {"claude_md_nudge": True}, tmp_path)

    err = capsys.readouterr().err
    assert "CLAUDE.md may need a sync" in err


def test_hook_check_nudge_silent_when_disabled(tmp_path, capsys):
    from carta.cli import _maybe_claude_md_nudge

    (tmp_path / "CLAUDE.md").write_text("# x\n", encoding="utf-8")
    result = type("R", (), {"scanned": 2})()
    _maybe_claude_md_nudge(result, {"claude_md_nudge": False}, tmp_path)

    assert "sync" not in capsys.readouterr().err
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest carta/tests/test_cli.py -k claude_md_nudge -v`
Expected: FAIL — `ImportError: cannot import name '_maybe_claude_md_nudge'`.

- [ ] **Step 3: Add the helper and call it**

In `carta/cli.py`, add the helper (near `_print_stale_result`):

```python
def _maybe_claude_md_nudge(result, scfg, repo_root):
    """One-line reminder that changed docs may have left CLAUDE.md stale. Cheap:
    no judge calls — fires only when docs were actually scanned. Fail-open."""
    try:
        if not scfg.get("claude_md_nudge", True):
            return
        if getattr(result, "scanned", 0) and (repo_root / "CLAUDE.md").exists():
            print(
                f"  ↪ {result.scanned} doc(s) changed — CLAUDE.md may need a sync; "
                f"run /claude-md-sync (or `carta claude-md check`).",
                file=sys.stderr,
            )
    except Exception:
        pass
```

Then call it in `cmd_hook`, immediately after `_print_stale_result(result, scfg)`:

```python
        _print_stale_result(result, scfg)
        _maybe_claude_md_nudge(result, scfg, repo_root)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest carta/tests/test_cli.py -k claude_md_nudge -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add carta/cli.py carta/tests/test_cli.py
git commit -m "feat(hook): nudge toward /claude-md-sync when docs change"
```

---

## Task 9: `/claude-md-sync` skill

**Files:**
- Create: `carta/skills/claude-md-sync/SKILL.md`

**Interfaces:**
- Consumes: `carta claude-md check` (JSON findings), `carta claude-md record`. No code; verified by file existence + a structural assertion.

- [ ] **Step 1: Write the failing test**

Append to `carta/hook/tests/test_claude_md.py`:

```python
def test_skill_file_exists_and_names_the_commands():
    from pathlib import Path
    p = Path(__file__).resolve().parents[2] / "skills" / "claude-md-sync" / "SKILL.md"
    assert p.exists(), "claude-md-sync SKILL.md missing"
    body = p.read_text(encoding="utf-8")
    assert "carta claude-md check" in body
    assert "carta claude-md record" in body
    assert "name: claude-md-sync" in body
```

> Path note: `carta/hook/tests/test_claude_md.py` → `parents[2]` is `carta/`, so this resolves `carta/skills/claude-md-sync/SKILL.md`.

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest carta/hook/tests/test_claude_md.py::test_skill_file_exists_and_names_the_commands -v`
Expected: FAIL — `AssertionError: claude-md-sync SKILL.md missing`.

- [ ] **Step 3: Write the skill**

Create `carta/skills/claude-md-sync/SKILL.md`:

```markdown
---
name: claude-md-sync
description: Reconcile CLAUDE.md against the docs after a change. Detect sections the updated docs have superseded and draft corrections, human-gated. Use near end-of-session once docs are edited and embedded.
---

# /claude-md-sync Skill

Keep CLAUDE.md — which every session and subagent loads as authoritative — in sync with the
docs after a change. Carta detects the stale sections; **you** draft the fixes using the full
context of what changed this session; the human approves. Carta never edits CLAUDE.md.

Run this near the **end of a session**, after the docs are edited and re-embedded
(`carta embed`), so the graph reflects the new truth.

---

## Step 1: Detect superseded sections

```bash
carta claude-md check
```

> Uses the installed `carta` CLI. If it isn't on PATH, run `python -m carta claude-md check`.

Parse the JSON. If `findings` is empty, report "CLAUDE.md is in sync with the docs." and stop.
Each finding has:

- `heading` — the CLAUDE.md section heading
- `section_text` — the full current text of that section
- `superseding` — `[{source, excerpt, score}]`: the doc passages that replaced it

`skipped_pinned` / `skipped_unchanged` are informational (pinned or already-reconciled sections).

---

## Step 2: Draft a correction per finding

For each finding, draft replacement text for `section_text` that reflects the `superseding`
excerpts **and** your session context. Hold the line on what CLAUDE.md is for:

- **Only correct descriptive claims** about systems, commands, and structure.
- **Never "correct" a durable directive or convention** ("use TDD", "Ollama judge ≤2B params",
  naming rules) toward what the code happens to do — those are the source of truth, not the docs.
  If a finding targets a directive, treat it as a false positive and skip it (optionally tell the
  user it can be pinned in `.carta/sidecars/CLAUDE.md.sync.yaml`).

---

## Step 3: Present diffs and get approval

Show each proposed change as a before/after diff. Apply **only** the changes the user approves;
edit CLAUDE.md with the Edit tool. Leave rejected sections untouched.

---

## Step 4: Record the sync

After edits are applied (or if the user approved no changes but you want to checkpoint the
current state), finalize the sidecar so unchanged sections skip next time:

```bash
carta claude-md record
```

---

## Completion

Report a summary:

> "CLAUDE.md sync complete. N sections flagged, M corrected and approved, K skipped. Sidecar recorded."
```

> Install note: `carta/skills/` is the canonical package source — `carta init` copies skills
> into the project on bootstrap (same as `doc-embed`). To dogfood `/claude-md-sync` in *this*
> repo immediately, also copy the directory to the repo-root `skills/claude-md-sync/` where the
> other installed skills live.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest carta/hook/tests/test_claude_md.py::test_skill_file_exists_and_names_the_commands -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add carta/skills/claude-md-sync/SKILL.md carta/hook/tests/test_claude_md.py
git commit -m "feat(skill): /claude-md-sync end-of-session reconciliation skill"
```

---

## Task 10: Document the new command (in the file it protects)

**Files:**
- Modify: `CLAUDE.md` (the "Carta surface — authoritative reference" CLI table + a one-line hook note)
- Modify: `README.md` (the CLI command list / "Which command?" section)

**Interfaces:** none (documentation).

- [ ] **Step 1: Add the CLI row to CLAUDE.md**

In `CLAUDE.md`, in the "### CLI" table, add a row after `hook`:

```markdown
| `claude-md` | Reconcile CLAUDE.md against the docs graph: `check` reports superseded sections (JSON), `record` stamps the sync sidecar. Pairs with the `/claude-md-sync` skill |
```

And under "### Hook", append:

```markdown
After a doc change, `carta hook check` also nudges toward `/claude-md-sync` when CLAUDE.md may
have drifted from the docs. The sync itself runs via the `/claude-md-sync` skill (detect →
agent drafts → human approves → `carta claude-md record`); metadata lives out-of-band in
`.carta/sidecars/CLAUDE.md.sync.yaml`.
```

- [ ] **Step 2: Add to README**

In `README.md`, add `carta claude-md` to the CLI command list with a one-line description matching the CLAUDE.md row (keep wording consistent with the existing entries).

- [ ] **Step 3: Verify the docs scanner still parses CLAUDE.md cleanly**

Run: `carta scan`
Expected: exits 0; no new errors attributable to the edited sections.

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md README.md
git commit -m "docs: document carta claude-md + /claude-md-sync"
```

---

## Final verification

- [ ] **Run the full hook test suite**

Run: `pytest carta/hook/tests/ carta/tests/test_cli.py -v`
Expected: all green, including the new sidecar, claude_md, and CLI tests.

- [ ] **Run the whole suite for regressions**

Run: `pytest -q`
Expected: no regressions vs. the pre-branch baseline.

- [ ] **Manual smoke test (optional, needs a live Carta repo with embedded docs)**

```bash
carta claude-md check | head -40
```
Expected: valid JSON; `scanned: true`; findings reference real docs as `superseding` sources.

---

## Spec coverage map

| Spec component | Task(s) |
|---|---|
| 1. Scan-target generalization (CLAUDE.md as ChangedDoc) | 5 (built directly; no `_in_doc_scope` change) |
| 2. Enrich `StaleFinding` for drafting | 1 (excerpt), 4 (section_text + heading rollup) |
| 3. Section granularity / grouping | 4 |
| 4. Sync sidecar (path, hash, pins, graph guard) | 2, 3, 6 |
| 5. CLI `carta claude-md check` (+ `record`) | 7 |
| 6. `/claude-md-sync` skill | 9 |
| 7. Pre-PR hook nudge | 8 |
| Fail-open everywhere | 1–8 (each module/path) |
| CLAUDE.md never embedded as source | 5 (target-only; self-filter in `run_stale_scan`) |
| Non-goal: no code-reference drift | not implemented (deferred, per spec) |
| Non-goal: no auto-rewrite | enforced — detection only; agent drafts, human approves |
| Follow-up: granularity tuning (#81) | tracked, not in this plan |
```
