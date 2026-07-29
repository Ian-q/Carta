# Demand-Driven Deep Scanning Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Flagged/vector-CAD PDFs become retrievable: agents flag documents via `carta flag`, the visual drain processes flagged files first with a high-DPI tiled topology-aware tier, and Claude-authored enrichment docs are embedded + staleness-tracked.

**Architecture:** Sidecar-resident flags (no new stores) drive drain ordering and a deep extraction path in `SmartRouter`. Enrichment is a markdown derivative whose canonical location is per-project config (repo-visible vs `.carta/companions`), embedded as a normal doc with an `enriches` payload link. Spec: `docs/superpowers/specs/2026-07-29-demand-driven-deep-scan-design.md`.

**Tech Stack:** Python (pipx-installed `carta-cc`), PyMuPDF (`fitz`), Ollama vision (`qwen3-vl:8b`, `glm-ocr`), Qdrant, pytest.

## Global Constraints

- **Never import torch/transformers/fitz at module level** in `carta/embed/pipeline.py` or anything the hook imports — guarded by `carta/embed/tests/test_import_cost.py`. Import `fitz` inside functions.
- **PyMuPDF is not thread-safe**: every `fitz` call in `SmartRouter` must hold `self._fitz_lock` (see `router.py:141-158`).
- **Sidecar updates are shallow merges** (`_update_sidecar`, `pipeline.py:186-191`) — never nest new fields under an existing dict key like `vision`.
- **Config keys need BOTH a `DEFAULTS` entry (`carta/config.py:82-133`) and matching inline `.get(..., default)` fallbacks at call sites** — call sites do not trust merged config.
- New sidecar fields this plan introduces (top-level): `priority`, `deep_scan`, `deep_scan_reason`, `deep_scan_requested_at`, `enrichment_path`, `enrichment_source_hash`.
- CLI handlers are `cmd_<name>(args)` in `carta/cli.py`, lazy imports inside the function, dispatched via the dict at `cli.py:1197-1218`.
- Run tests from the repo root: `python -m pytest <path> -v` (root `conftest.py` provides fixtures).

---

### Task 1: Un-gate the OCR/vision queue from ColPali scoping + log the fail-closed skip

The `colpali_scoped_paths` config silently gated (a) pass-1 visual queueing (`pipeline.py:71-73`) and (b) the whole drain (`_filter_visual_pending_in_scope` call at `pipeline.py:1163-1173`). ColPali scoping must gate ONLY the ColPali embed step inside `_visual_embed_one_page`.

**Files:**
- Modify: `carta/embed/pipeline.py` (`_mark_or_collect_visual_pages` :52-78; drain discovery :1163-1173; ColPali block inside `_visual_embed_one_page` ~:1080-1109; fail-closed warning :382-387)
- Test: `carta/embed/tests/test_colpali_scoping.py`, `carta/embed/tests/test_visual_drainer.py`

**Interfaces:**
- Produces: `_mark_or_collect_visual_pages(page_classes, cfg, rel_path="") -> dict` (same signature, no scope gate). Drain OCRs every queued file; ColPali step alone respects `colpali_scoped_paths` via existing `_colpali_path_in_scope(rel_path, scopes)`.

- [ ] **Step 1: Write failing tests**

In `carta/embed/tests/test_colpali_scoping.py` add:

```python
def test_queueing_ignores_colpali_scope():
    """OCR/vision queueing must not be gated by colpali_scoped_paths (spec Component 1)."""
    from carta.embed.pipeline import _mark_or_collect_visual_pages
    from carta.vision.classifier import PageClass
    cfg = {"embed": {"two_pass_visual": True,
                     "colpali_scoped_paths": ["docs/components/"]}}
    updates = _mark_or_collect_visual_pages(
        [PageClass.FLATTENED, PageClass.PURE_TEXT], cfg,
        rel_path="docs/reference/suppliers/CTS/schematic.pdf")  # OUT of scope
    assert updates.get("visual_pending") == [1]
```

In `carta/embed/tests/test_visual_drainer.py` add a test that an out-of-scope file is still drained (mirror the existing `_mock_router_embedder(monkeypatch)` + patched `_discover_visual_pending` pattern at `test_visual_drainer.py:8-35`; cfg has `colpali_scoped_paths: ["docs/components/"]`, sidecar `current_path: "docs/reference/x.pdf"`, `visual_pending: [1]`; assert `pages_embedded == 1`).

- [ ] **Step 2: Run to verify both fail**

Run: `python -m pytest carta/embed/tests/test_colpali_scoping.py carta/embed/tests/test_visual_drainer.py -v`
Expected: the two new tests FAIL (queue gated / file filtered out); pre-existing tests pass.

- [ ] **Step 3: Implement**

(a) In `_mark_or_collect_visual_pages` delete the two scope lines (`pipeline.py:71-73`) and rewrite the docstring:

```python
    """Return sidecar updates queuing 1-indexed image-heavy pages when two_pass_visual is on.

    Deliberately independent of colpali_scoped_paths: ColPali scoping gates the
    ColPali embedder only (see _visual_embed_one_page) — the OCR/vision drain
    covers every file. The old scope gate here silently left out-of-scope PDFs
    with zero visual coverage (2026-07 dark-corpus incident).
    """
```

Keep the `rel_path` parameter (call sites pass it) but stop using it.

(b) At `pipeline.py:1163-1173` remove the `_filter_visual_pending_in_scope(queued, scopes)` call so `queued` flows through unfiltered.

(c) Inside `_visual_embed_one_page`, wrap ONLY the ColPali section (the `embedder.embed_pdf_pages(...)` → `upsert_visual_pages(...)` block, ~:1089-1109):

```python
        scopes = (cfg.get("embed", {}) or {}).get("colpali_scoped_paths", []) or []
        if scopes and not _colpali_path_in_scope(rel_path, scopes):
            pass  # out of ColPali scope: OCR chunks above still upserted
        else:
            <existing ColPali block>
```

(`rel_path` is available in that function as the sidecar's `current_path`; if the local name differs, use the same variable the OCR chunk dict uses for `file_path`.)

(d) Extend the fail-closed warning (`pipeline.py:382-387`) to end with: `"; two-pass visual queueing also skipped — file has no visual coverage until flagged (carta flag) or re-embedded"`.

- [ ] **Step 4: Run full affected suites**

Run: `python -m pytest carta/embed/tests/ carta/tests/test_pipeline.py -v`
Expected: PASS. If an existing `test_colpali_scoping.py` case asserts the old queue-gating behavior, update it to assert the drain-side ColPali gate instead — that behavior (ColPali skipped out-of-scope) is retained.

- [ ] **Step 5: Commit**

```bash
git add carta/embed/pipeline.py carta/embed/tests/test_colpali_scoping.py carta/embed/tests/test_visual_drainer.py
git commit -m "fix(embed): colpali_scoped_paths no longer gates OCR/vision queueing or the drain"
```

---

### Task 2: `carta/embed/flags.py` + `carta flag` CLI + status line

**Files:**
- Create: `carta/embed/flags.py`
- Modify: `carta/cli.py` (new `cmd_flag`; parser + dispatch entry near :1073 and :1197-1218), `carta/status.py` (`_gather_corpus` :88-106, `format_current` :234-244)
- Test: `carta/embed/tests/test_flags.py`, `carta/tests/test_status_command.py`

**Interfaces:**
- Produces: `flag_file(repo_root: Path, cfg: dict, rel: Path, reason: str) -> dict`, `clear_flag(repo_root: Path, rel: Path) -> bool`, `list_flagged(repo_root: Path) -> list[dict]`, `FLAG_FIELDS` tuple. Sidecar fields: `priority: "high"`, `deep_scan: "requested"|"done"`, `deep_scan_reason: str`, `deep_scan_requested_at: iso8601 str`.
- Consumes: `sidecar_path`, `read_sidecar`, `write_sidecar`, `generate_sidecar_stub`, `iter_canonical_sidecars` from `carta.embed.induct`; `VISUAL_PENDING_KEY`, `VISUAL_DONE_KEY` from `carta.embed.visual_queue`.

- [ ] **Step 1: Write failing tests** — `carta/embed/tests/test_flags.py`:

```python
import yaml
from pathlib import Path
import pytest

from carta.embed.flags import flag_file, clear_flag, list_flagged, FLAG_FIELDS
from carta.embed.induct import sidecar_path, read_sidecar

CFG = {"project_name": "t", "qdrant_url": "http://localhost:6333", "embed": {}}


@pytest.fixture
def repo(tmp_path):
    (tmp_path / ".carta").mkdir()
    d = tmp_path / "docs" / "reference"
    d.mkdir(parents=True)
    (d / "note.md").write_text("# hello\nbody\n")
    return tmp_path


def test_flag_sets_fields_and_stub_for_unembedded(repo):
    sc = flag_file(repo, CFG, Path("docs/reference/note.md"), "MSD miss 2026-07-29")
    assert sc["priority"] == "high"
    assert sc["deep_scan"] == "requested"
    assert sc["deep_scan_reason"] == "MSD miss 2026-07-29"
    assert sc["deep_scan_requested_at"]
    on_disk = read_sidecar(sidecar_path(repo / "docs/reference/note.md", repo))
    assert on_disk["priority"] == "high"


def test_flag_pdf_force_queues_all_pages(repo, monkeypatch):
    (repo / "docs/reference/draw.pdf").write_bytes(b"%PDF-fake")
    monkeypatch.setattr("carta.embed.flags._pdf_page_count", lambda p: 3)
    sc = flag_file(repo, CFG, Path("docs/reference/draw.pdf"), "vector CAD dark")
    assert sc["visual_pending"] == [1, 2, 3]
    assert sc["visual_done"] == []


def test_flag_unknown_path_raises(repo):
    with pytest.raises(FileNotFoundError):
        flag_file(repo, CFG, Path("docs/nope.pdf"), "x")


def test_clear_and_list(repo):
    flag_file(repo, CFG, Path("docs/reference/note.md"), "r")
    assert [Path(s["current_path"]).name for s in list_flagged(repo)] == ["note.md"]
    assert clear_flag(repo, Path("docs/reference/note.md")) is True
    assert list_flagged(repo) == []
    on_disk = read_sidecar(sidecar_path(repo / "docs/reference/note.md", repo))
    for f in FLAG_FIELDS:
        assert f not in on_disk
```

- [ ] **Step 2: Run to verify fails** — `python -m pytest carta/embed/tests/test_flags.py -v` → FAIL (module not found).

- [ ] **Step 3: Implement `carta/embed/flags.py`**

```python
"""Sidecar priority flags — demand-driven deep-scan requests.

Agents mark a document high-priority via `carta flag`; the visual drain
processes flagged files first and applies the deep extraction tier.
Design: docs/superpowers/specs/2026-07-29-demand-driven-deep-scan-design.md
"""
from datetime import datetime, timezone
from pathlib import Path

from carta.embed.induct import (
    generate_sidecar_stub,
    iter_canonical_sidecars,
    read_sidecar,
    sidecar_path,
    write_sidecar,
)
from carta.embed.visual_queue import VISUAL_DONE_KEY, VISUAL_PENDING_KEY

FLAG_FIELDS = ("priority", "deep_scan", "deep_scan_reason", "deep_scan_requested_at")


def _pdf_page_count(path: Path) -> int:
    import fitz  # lazy: keep module import cheap (test_import_cost)

    with fitz.open(path) as doc:
        return doc.page_count


def flag_file(repo_root: Path, cfg: dict, rel: Path, reason: str) -> dict:
    """Mark *rel* high-priority for deep scanning. Creates a sidecar stub if none exists.

    For PDFs, deep scan is a redo: visual_done resets and every page is queued so
    the drain picks the file up regardless of past classification (point IDs
    overwrite in place, so re-draining is idempotent).
    """
    src = repo_root / rel
    if not src.is_file():
        raise FileNotFoundError(f"not a file under the repo: {rel}")
    sc = read_sidecar(sidecar_path(src, repo_root)) or generate_sidecar_stub(
        src, repo_root, cfg
    )
    sc["priority"] = "high"
    sc["deep_scan"] = "requested"
    sc["deep_scan_reason"] = reason
    sc["deep_scan_requested_at"] = datetime.now(timezone.utc).isoformat()
    if src.suffix.lower() == ".pdf":
        sc[VISUAL_DONE_KEY] = []
        sc[VISUAL_PENDING_KEY] = list(range(1, _pdf_page_count(src) + 1))
    write_sidecar(src, sc, repo_root)
    return sc


def clear_flag(repo_root: Path, rel: Path) -> bool:
    """Remove flag fields from *rel*'s sidecar. Returns False when no sidecar exists."""
    src = repo_root / rel
    sc_path = sidecar_path(src, repo_root)
    sc = read_sidecar(sc_path)
    if sc is None:
        return False
    for f in FLAG_FIELDS:
        sc.pop(f, None)
    write_sidecar(src, sc, repo_root)
    return True


def list_flagged(repo_root: Path) -> list[dict]:
    """All sidecars with priority: high, oldest deep_scan_requested_at first."""
    rows = [
        sc
        for _, sc in iter_canonical_sidecars(repo_root)
        if sc.get("priority") == "high"
    ]
    rows.sort(key=lambda s: str(s.get("deep_scan_requested_at") or ""))
    return rows
```

If `write_sidecar`'s signature rejects a missing parent dir, mkdir first (`sidecar_path(...).parent.mkdir(parents=True, exist_ok=True)`) — check `induct.py:145` behavior while implementing.

- [ ] **Step 4: Run** — `python -m pytest carta/embed/tests/test_flags.py -v` → PASS.

- [ ] **Step 5: CLI — parser, dispatch, handler**

In `carta/cli.py` near the other parsers (after `search_p`, ~:1084):

```python
    flag_p = sub.add_parser(
        "flag", help="Mark a document high-priority for deep visual scanning"
    )
    flag_p.add_argument("path", nargs="?", help="Repo-relative source path (omit to list flags)")
    flag_p.add_argument("--reason", help="One-line reason (required when flagging)")
    flag_p.add_argument("--clear", action="store_true", help="Remove the flag")
```

Add `"flag": cmd_flag,` to the dispatch dict (:1197-1218). Handler (module level, mirroring `cmd_search` conventions — lazy imports, `repo_root = cfg_path.parent.parent`). Take the embed lock exactly the way `cmd_embed` does at `cli.py:160-184` (flags mutate sidecars the drain also writes):

```python
def cmd_flag(args):
    from pathlib import Path as _P

    from carta.config import load_config

    cfg_path = find_config()
    cfg = load_config(cfg_path)
    repo_root = cfg_path.parent.parent
    from carta.embed.flags import clear_flag, flag_file, list_flagged

    if not args.path:
        rows = list_flagged(repo_root)
        if not rows:
            print("no flagged documents")
            return
        for sc in rows:
            state = sc.get("deep_scan", "?")
            reason = sc.get("deep_scan_reason", "")
            print(f"[{state}] {sc.get('current_path')} — {reason}")
        return
    rel = _P(args.path)
    if args.clear:
        ok = clear_flag(repo_root, rel)
        print(f"cleared: {rel}" if ok else f"no sidecar for: {rel}")
        return
    if not args.reason:
        print("error: --reason is required when flagging", file=sys.stderr)
        sys.exit(1)
    try:
        flag_file(repo_root, cfg, rel, args.reason)
    except FileNotFoundError as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)
    print(f"flagged high-priority: {rel}")
```

- [ ] **Step 6: Status line**

In `carta/status.py` `_gather_corpus` (:88-106) tally three counters inside the existing sidecar loop and add to the returned dict:

```python
    flagged = awaiting = enrich_stale = 0
    ...
        if sc.get("priority") == "high":
            flagged += 1
            if sc.get("deep_scan") == "requested":
                awaiting += 1
        rec = sc.get("enrichment_source_hash")
        if rec and rec != sc.get("file_hash", ""):
            enrich_stale += 1
    ...
    return {..., "flagged": flagged, "awaiting_deep_scan": awaiting,
            "enrichment_stale": enrich_stale}
```

New renderer + wire into `format_current` (:234-244) after the corpus line, only when non-zero:

```python
def _flag_line(co: dict, color: bool) -> str:
    if not (co.get("flagged") or co.get("enrichment_stale")):
        return ""
    parts = []
    if co.get("flagged"):
        parts.append(f"flagged {co['flagged']} ({co.get('awaiting_deep_scan', 0)} awaiting deep scan)")
    if co.get("enrichment_stale"):
        parts.append(f"enrichment stale: {co['enrichment_stale']}")
    return "flags   " + " · ".join(parts)
```

Tests: extend `carta/tests/test_status_command.py` with a direct-call test (build a temp sidecar tree with one flagged sidecar, assert the gathered dict keys + `_flag_line` output string equals `"flags   flagged 1 (1 awaiting deep scan)"`). CLI black-box: in `carta/tests/test_cli.py` use the `run_carta` helper (`test_cli.py:10-21`) in a temp repo: `carta flag docs/x.md --reason r` → exit 0; `carta flag` lists it; `carta flag docs/x.md --clear` → exit 0; `carta flag docs/missing.pdf --reason r` → exit 1.

- [ ] **Step 7: Run everything** — `python -m pytest carta/embed/tests/test_flags.py carta/tests/test_status_command.py carta/tests/test_cli.py -v` → PASS.

- [ ] **Step 8: Commit**

```bash
git add carta/embed/flags.py carta/cli.py carta/status.py carta/embed/tests/test_flags.py carta/tests/test_status_command.py carta/tests/test_cli.py
git commit -m "feat(flags): carta flag CLI — sidecar priority + deep-scan request + status line"
```

---

### Task 3: Drain ordering (flagged → triage paths → FIFO) + `deep_scan: done`

**Files:**
- Modify: `carta/embed/pipeline.py` (`run_visual_embed` — sort after `_discover_visual_pending`; per-file completion block ~:1236), `carta/config.py` (DEFAULTS: `"visual_triage_paths": []` in the `embed` dict)
- Test: `carta/embed/tests/test_visual_drainer.py`

**Interfaces:**
- Produces: `_drain_sort_key(item: tuple, triage_paths: list[str]) -> tuple` (module-level in pipeline.py, testable). Config `embed.visual_triage_paths: list[str]` (path prefixes).
- Consumes: sidecar fields from Task 2.

- [ ] **Step 1: Failing tests** — in `test_visual_drainer.py`:

```python
def test_drain_sort_key_orders_flagged_then_triage_then_fifo():
    from carta.embed.pipeline import _drain_sort_key
    triage = ["docs/reference/suppliers/"]
    flagged = ("p1", {"current_path": "docs/z.pdf", "priority": "high",
                      "deep_scan_requested_at": "2026-07-29T00:00:00+00:00"})
    supplier = ("p2", {"current_path": "docs/reference/suppliers/CTS/a.pdf"})
    other = ("p3", {"current_path": "docs/a.pdf"})
    items = [other, supplier, flagged]
    items.sort(key=lambda it: _drain_sort_key(it, triage))
    assert [i[0] for i in items] == ["p1", "p2", "p3"]
```

Plus an integration-style test with the existing monkeypatch idiom: patch `_discover_visual_pending` to return two files (one flagged, one not, discovery order reversed), record the order `_visual_embed_one_page` receives them, assert flagged first. And: a file whose sidecar has `deep_scan: "requested"` and drains cleanly ends with `deep_scan: "done"` in the `_update_sidecar` calls (assert on the patched `_update_sidecar` call list).

- [ ] **Step 2: Run → FAIL** (`_drain_sort_key` undefined).

- [ ] **Step 3: Implement** in `pipeline.py` (module level, near `_discover_visual_pending`):

```python
def _drain_sort_key(item: tuple, triage_paths: list[str]) -> tuple:
    """Drain order: flagged (oldest request first) -> triage-path prefixes -> FIFO."""
    _sc_path, sc = item
    rel = str(sc.get("current_path") or "")
    if sc.get("priority") == "high":
        return (0, str(sc.get("deep_scan_requested_at") or ""), rel)
    if any(rel.startswith(p) for p in triage_paths):
        return (1, "", rel)
    return (2, "", rel)
```

In `run_visual_embed`, right after discovery (and after Task 1 removed the scope filter):

```python
    triage_paths = (cfg.get("embed", {}) or {}).get("visual_triage_paths", []) or []
    queued.sort(key=lambda it: _drain_sort_key(it, triage_paths))
```

In the per-file loop, after the clean-completion branch (where `_delete_visual_orphans` runs, ~:1236):

```python
            if not file_failed and sc.get("deep_scan") == "requested":
                _update_sidecar(sc_path, {"deep_scan": "done"})
```

Add `"visual_triage_paths": [],` to the `embed` DEFAULTS dict in `carta/config.py`.

- [ ] **Step 4: Run** — `python -m pytest carta/embed/tests/test_visual_drainer.py carta/tests/test_config.py -v` → PASS.

- [ ] **Step 5: Commit**

```bash
git add carta/embed/pipeline.py carta/config.py carta/embed/tests/test_visual_drainer.py
git commit -m "feat(drain): flagged-first ordering + visual_triage_paths + deep_scan done marking"
```

---### Task 4: Enrichment — locations, ingestion attribution, staleness

**Files:**
- Create: `carta/embed/enrichment.py`
- Modify: `carta/embed/pipeline.py` (metadata block in `_embed_one_file`, next to the spreadsheet `derived` block :417-420; post-upsert sidecar write to call `record_enrichment`), `carta/config.py` (DEFAULTS `embed.enrichment`), `carta/embed/integrity.py` (stale-enrichment listing alongside `visual_count_mismatches` :166-173)
- Test: `carta/embed/tests/test_enrichment.py`

**Interfaces:**
- Produces:
  - `enrichment_rel_path(source_rel: Path, cfg: dict) -> Path` — full source **name** appended + suffix (`a.pdf` → `a.pdf.extraction.md`); repo-visible → sibling of source; else `.carta/companions/<mirror>/`. (Deviation from the spec's `<stem>` wording, for the same collision reason as `companion_rel_path` — noted here deliberately.)
  - `source_rel_for_enrichment(enrichment_rel: Path, cfg: dict) -> Path | None` — inverse mapping; returns None for non-enrichment paths.
  - `record_enrichment(repo_root: Path, source_rel: Path, enrichment_rel: Path) -> None` — stamps source sidecar `enrichment_path`, `enrichment_source_hash` (= its current `file_hash`), and promotes `deep_scan: requested → done`.
  - `enrichment_is_stale(sc: dict) -> bool` — recorded hash present and ≠ current `file_hash`.
- Config: `embed.enrichment = {"repo_visible": False, "suffix": ".extraction.md"}` in DEFAULTS.

- [ ] **Step 1: Failing tests** — `carta/embed/tests/test_enrichment.py`:

```python
from pathlib import Path

from carta.embed.enrichment import (
    enrichment_is_stale,
    enrichment_rel_path,
    record_enrichment,
    source_rel_for_enrichment,
)

REPO_VISIBLE = {"embed": {"enrichment": {"repo_visible": True, "suffix": ".extraction.md"}}}
INTERNAL = {"embed": {"enrichment": {"repo_visible": False, "suffix": ".extraction.md"}}}
SRC = Path("docs/reference/suppliers/CTS/schematic.pdf")


def test_repo_visible_path_is_sibling():
    assert enrichment_rel_path(SRC, REPO_VISIBLE) == Path(
        "docs/reference/suppliers/CTS/schematic.pdf.extraction.md")


def test_internal_path_mirrors_companions():
    assert enrichment_rel_path(SRC, INTERNAL) == Path(
        ".carta/companions/docs/reference/suppliers/CTS/schematic.pdf.extraction.md")


def test_inverse_mapping_both_branches():
    for cfg in (REPO_VISIBLE, INTERNAL):
        assert source_rel_for_enrichment(enrichment_rel_path(SRC, cfg), cfg) == SRC
    assert source_rel_for_enrichment(Path("docs/notes.md"), REPO_VISIBLE) is None


def test_staleness_by_source_hash():
    assert enrichment_is_stale({"enrichment_source_hash": "aa", "file_hash": "bb"})
    assert not enrichment_is_stale({"enrichment_source_hash": "aa", "file_hash": "aa"})
    assert not enrichment_is_stale({"file_hash": "aa"})
```

Plus a `record_enrichment` round-trip on a tmp repo (write a source sidecar with `file_hash: "abc"`, `deep_scan: "requested"`; call; assert sidecar gains `enrichment_path`, `enrichment_source_hash == "abc"`, `deep_scan == "done"`).

- [ ] **Step 2: Run → FAIL** (module missing).

- [ ] **Step 3: Implement `carta/embed/enrichment.py`**

```python
"""Enrichment docs — Claude/human-authored structured extractions of visual sources.

Canonical location is per-project config (embed.enrichment.repo_visible);
the mechanism is Carta's. Staleness is tracked against the SOURCE file hash.
Design: docs/superpowers/specs/2026-07-29-demand-driven-deep-scan-design.md
"""
from pathlib import Path
from typing import Optional

from carta.embed.induct import read_sidecar, sidecar_path, write_sidecar

_COMPANIONS = (".carta", "companions")


def _enrichment_cfg(cfg: dict) -> dict:
    return (cfg.get("embed", {}) or {}).get("enrichment", {}) or {}


def enrichment_suffix(cfg: dict) -> str:
    return _enrichment_cfg(cfg).get("suffix", ".extraction.md")


def enrichment_rel_path(source_rel: Path, cfg: dict) -> Path:
    """Canonical repo-relative enrichment path for *source_rel*.

    The full source NAME is appended (a.pdf -> a.pdf.extraction.md) so two
    sources differing only by extension cannot collide — same rule as
    tabular.companion_rel_path.
    """
    name = source_rel.name + enrichment_suffix(cfg)
    if _enrichment_cfg(cfg).get("repo_visible", False):
        return source_rel.parent / name
    return Path(*_COMPANIONS) / source_rel.parent / name


def source_rel_for_enrichment(path: Path, cfg: dict) -> Optional[Path]:
    """Inverse of enrichment_rel_path, or None when *path* is not an enrichment."""
    suffix = enrichment_suffix(cfg)
    if not path.name.endswith(suffix):
        return None
    src_name = path.name[: -len(suffix)]
    parent = path.parent
    if parent.parts[:2] == _COMPANIONS:
        parent = Path(*parent.parts[2:]) if len(parent.parts) > 2 else Path(".")
    return parent / src_name


def record_enrichment(repo_root: Path, source_rel: Path, enrichment_rel: Path) -> None:
    """Stamp the SOURCE sidecar: enrichment ingested at the current source hash."""
    src = repo_root / source_rel
    sc_path = sidecar_path(src, repo_root)
    sc = read_sidecar(sc_path) or {}
    sc["enrichment_path"] = str(enrichment_rel)
    sc["enrichment_source_hash"] = sc.get("file_hash", "")
    if sc.get("deep_scan") == "requested":
        sc["deep_scan"] = "done"
    write_sidecar(src, sc, repo_root)


def enrichment_is_stale(sc: dict) -> bool:
    rec = sc.get("enrichment_source_hash")
    return bool(rec) and rec != sc.get("file_hash", "")
```

- [ ] **Step 4: Run** → PASS.

- [ ] **Step 5: Pipeline attribution + ingestion hook**

In `_embed_one_file`'s metadata block (immediately after the spreadsheet `derived` block, `pipeline.py:417-420`):

```python
    from carta.embed.enrichment import enrichment_suffix, source_rel_for_enrichment

    rel_of_file = file_path.relative_to(repo_root)
    if rel_of_file.name.endswith(enrichment_suffix(cfg)):
        src_rel = source_rel_for_enrichment(rel_of_file, cfg)
        if src_rel and (repo_root / src_rel).is_file():
            metadata["enriches"] = str(src_rel)
```

And where `_embed_one_file` finishes a successful embed (the same place `sidecar_updates["status"] = "embedded"` is set, ~:647-656), when `metadata.get("enriches")` is set, call `record_enrichment(repo_root, Path(metadata["enriches"]), rel_of_file)`. Payload propagation is free — `upsert_chunks` passes every chunk key through (`embed.py:258-286`), so `enriches` lands on every chunk of the enrichment doc.

Note: repo-visible enrichments under `docs_root` auto-induct via the normal scan; companion-internal ones live under `.carta` (excluded from `_iter_inductable_files`, `pipeline.py:90-94`) and are embedded via explicit `carta embed <path>` — add a test asserting `run_embed_file` accepts a `.carta/companions/**.extraction.md` path (mock `upsert_chunks` + Qdrant client, mirror `carta/tests/test_pipeline.py`'s `temp_repo` fixture :18-42) and that the source sidecar was stamped.

Add `"enrichment": {"repo_visible": False, "suffix": ".extraction.md"},` to `embed` DEFAULTS.

- [ ] **Step 6: Integrity listing** — in `carta/embed/integrity.py`, alongside `visual_count_mismatches` (:166-173), collect `stale_enrichments = [sc["current_path"] for _, sc in <the existing sidecar iteration> if enrichment_is_stale(sc)]`, include in the report dict, and print in the consumer at `cli.py:578-585` as `enrichment stale: <path>` lines. Test with a direct-call unit in the integrity test module (build two sidecars, one stale).

- [ ] **Step 7: Run** — `python -m pytest carta/embed/tests/test_enrichment.py carta/tests/test_pipeline.py carta/embed/tests/ -v` → PASS.

- [ ] **Step 8: Commit**

```bash
git add carta/embed/enrichment.py carta/embed/pipeline.py carta/config.py carta/embed/integrity.py carta/cli.py carta/embed/tests/test_enrichment.py
git commit -m "feat(enrichment): repo-visible/companion extraction docs with enriches payload + hash staleness"
```

---

### Task 5: Classifier — `VECTOR_DRAWING` class + parameterized render DPI

The real-world miss can come from EITHER a classification exception OR a title block pushing `text_length` past 150 → PURE_TEXT/STRUCTURED_TEXT (never queued). The vector-CAD signature closes both misclassification paths at the source.

**Files:**
- Modify: `carta/vision/classifier.py` (PageClass, PageProfile, `PageAnalyzer.__init__`, `analyze`, `_classify`), `carta/embed/pipeline.py` (`_IMAGE_HEAVY` :45-49), `carta/vision/router.py` (`_route` :281-305 + the three `dpi=150` literals :309, :348, :367 → `self.render_dpi`), `carta/config.py` (DEFAULTS `embed.deep_scan`, `embed.vision_render_dpi`)
- Test: `carta/vision/tests/test_classifier.py` (or the existing classifier test module), `carta/vision/tests/test_router.py`

**Interfaces:**
- Produces: `PageClass.VECTOR_DRAWING`; `PageProfile.drawing_count: int` (new field, default 0); `_classify(self, text_length, has_images, has_tables, has_captions, drawing_count=0)`; `SmartRouter.render_dpi` (from `embed.vision_render_dpi`, default 150).
- Config: `embed.deep_scan = {"dpi": 300, "tile_px": 1280, "tile_overlap": 0.15, "vector_min_paths": 50, "vector_text_max_chars": 1000}`; `embed.vision_render_dpi = 150`.

- [ ] **Step 1: Failing tests**

```python
def test_vector_drawing_classification():
    from carta.vision.classifier import PageAnalyzer, PageClass
    a = PageAnalyzer({"embed": {"deep_scan": {"vector_min_paths": 50,
                                              "vector_text_max_chars": 1000}}})
    # raster-free, drawing-dense, sparse text -> VECTOR_DRAWING even when
    # text_length exceeds the FLATTENED threshold (title-block case)
    assert a._classify(300, False, False, False, drawing_count=200) is PageClass.VECTOR_DRAWING
    # raster images present -> not vector CAD
    assert a._classify(300, True, False, False, drawing_count=200) is not PageClass.VECTOR_DRAWING
    # text-heavy pages with incidental rules/underlines stay text
    assert a._classify(2000, False, False, False, drawing_count=200) is PageClass.PURE_TEXT
```

Router test (mirror `test_router.py` idiom): `page = MagicMock()`, `page.get_pixmap.return_value = _pixmap()`, profile `_profile(PageClass.VECTOR_DRAWING, drawing_count=200)`, patch `_call_ollama_vision`; assert `_route` dispatches to the flattened path and `page.get_pixmap.call_args[1]["dpi"] == 150`. Second test: `SmartRouter(_cfg(vision_render_dpi=220))` → `dpi == 220`.

Also assert `PageClass.VECTOR_DRAWING in carta.embed.pipeline._IMAGE_HEAVY`.

- [ ] **Step 2: Run → FAIL.**

- [ ] **Step 3: Implement**

`classifier.py`:

```python
class PageClass(Enum):
    PURE_TEXT = "pure_text"
    STRUCTURED_TEXT = "structured_text"
    TEXT_WITH_IMAGES = "text_with_images"
    FLATTENED = "flattened"
    VECTOR_DRAWING = "vector_drawing"
```

`PageProfile` gains `drawing_count: int = 0` (place after `has_captions`; keep `page_class` last). `PageAnalyzer.__init__` additions:

```python
        deep = embed.get("deep_scan", {}) or {}
        self.vector_min_paths: int = deep.get("vector_min_paths", 50)
        self.vector_text_max: int = deep.get("vector_text_max_chars", 1000)
```

`analyze` computes `drawing_count = len(page.get_drawings()) if hasattr(page, "get_drawings") else 0`, passes it to `_classify`, stores it on the profile. (Docstring: this drops the "zero model calls" purity claim slightly — `get_drawings()` walks the display list; still no model calls.) `_classify` head:

```python
    def _classify(self, text_length, has_images, has_tables, has_captions, drawing_count=0):
        if (not has_images
                and drawing_count >= self.vector_min_paths
                and text_length < self.vector_text_max):
            return PageClass.VECTOR_DRAWING
        if text_length < self.text_min:
            return PageClass.FLATTENED
        ...
```

`pipeline.py`: `_IMAGE_HEAVY = {PageClass.TEXT_WITH_IMAGES, PageClass.FLATTENED, PageClass.VECTOR_DRAWING}`.

`router.py`: `self.render_dpi = int(embed.get("vision_render_dpi", 150))` in `__init__`; replace the three `dpi=150` literals with `dpi=self.render_dpi`; in `_route`, treat `VECTOR_DRAWING` exactly like `FLATTENED` in every branch, including the `vision_routing` override modes at :285-305 (it must never fall into the PURE_TEXT early-outs).

- [ ] **Step 4: Run** — `python -m pytest carta/vision/tests/ carta/embed/tests/ -v` → PASS (update any router test asserting the literal 150 via a changed call pattern).

- [ ] **Step 5: Commit**

```bash
git add carta/vision/classifier.py carta/vision/router.py carta/embed/pipeline.py carta/config.py carta/vision/tests/
git commit -m "feat(vision): VECTOR_DRAWING page class + configurable render DPI"
```

---

### Task 6: Deep tiled extraction + drain integration

**Files:**
- Modify: `carta/vision/router.py` (new `tile_rects` module function, `DEEP_STRUCTURE_PROMPT`, `SmartRouter.extract_page_deep`, `_make_chunk` optional fields), `carta/embed/pipeline.py` (`_visual_embed_one_page` deep dispatch; `run_visual_embed` passes per-file deep flag)
- Test: `carta/vision/tests/test_router.py`, `carta/embed/tests/test_visual_drainer.py`

**Interfaces:**
- Produces:
  - `tile_rects(x0: float, y0: float, x1: float, y1: float, dpi: int, tile_px: int, overlap: float) -> list[tuple[float, float, float, float]]` — pure geometry, module-level.
  - `SmartRouter.extract_page_deep(page, page_num: int) -> list[dict]` — chunks additionally carry `tile: int` and `extraction: "transcription"|"structure"`, `page_class: "deep_scan"`.
  - `_visual_embed_one_page(..., deep: bool = False)`; drain computes `deep = sc.get("deep_scan") == "requested"` per file; deep OR `profile.page_class is PageClass.VECTOR_DRAWING` routes to `extract_page_deep`.

- [ ] **Step 1: Failing geometry tests** (model-free):

```python
def test_tile_rects_small_page_single_tile():
    from carta.vision.router import tile_rects
    # 612x792pt letter at 300dpi -> 2550x3300px; tile_px 4000 covers it whole
    assert tile_rects(0, 0, 612, 792, 300, 4000, 0.15) == [(0, 0, 612, 792)]


def test_tile_rects_cover_and_overlap():
    from carta.vision.router import tile_rects
    rects = tile_rects(0, 0, 1000, 800, 300, 1280, 0.15)
    assert len(rects) > 1
    xs = sorted({r[0] for r in rects}); ys = sorted({r[1] for r in rects})
    tile_pts = 1280 / (300 / 72.0)
    step = tile_pts * 0.85
    assert all(abs((b - a) - step) < 1e-6 for a, b in zip(xs, xs[1:]))
    assert max(r[2] for r in rects) == 1000 and max(r[3] for r in rects) == 800
```

Router test: `extract_page_deep` with mocked page (`page.rect` a MagicMock carrying x0/y0/x1/y1 floats; `page.get_pixmap.return_value = _pixmap()`) and patched `_call_ollama_vision` returning "txt"; assert per tile there are two chunks with `extraction` values `{"transcription", "structure"}` and correct `tile` ints, and that a tile whose vision call raises is skipped without aborting (patch side_effect list). Drainer test: sidecar with `deep_scan: "requested"` → patched router's `extract_page_deep` called instead of `_route`.

- [ ] **Step 2: Run → FAIL.**

- [ ] **Step 3: Implement in `router.py`**

```python
DEEP_STRUCTURE_PROMPT = (
    "Describe what this technical drawing shows: name each component, state what "
    "connects to what, and note what sits between which elements. Transcribe labels "
    "verbatim, including non-English text. Do not invent details; omit anything "
    "unreadable."
)


def tile_rects(x0, y0, x1, y1, dpi, tile_px, overlap):
    """Grid of (x0, y0, x1, y1) point-space clips; each renders <= tile_px per edge at dpi."""
    tile_pts = tile_px / (dpi / 72.0)
    if (x1 - x0) <= tile_pts and (y1 - y0) <= tile_pts:
        return [(x0, y0, x1, y1)]
    step = tile_pts * (1.0 - overlap)
    rects = []
    y = y0
    while True:
        x = x0
        while True:
            rects.append((x, y, min(x + tile_pts, x1), min(y + tile_pts, y1)))
            if x + tile_pts >= x1:
                break
            x += step
        if y + tile_pts >= y1:
            break
        y += step
    return rects
```

`SmartRouter.__init__` additions: `self.deep_cfg = embed.get("deep_scan", {}) or {}`. Method:

```python
    def extract_page_deep(self, page: Any, page_num: int) -> list[dict]:
        """High-DPI tiled extraction: transcription + structure prompt per tile."""
        dpi = int(self.deep_cfg.get("dpi", 300))
        tile_px = int(self.deep_cfg.get("tile_px", 1280))
        overlap = float(self.deep_cfg.get("tile_overlap", 0.15))
        with self._fitz_lock:
            r = page.rect
            tiles = tile_rects(r.x0, r.y0, r.x1, r.y1, dpi, tile_px, overlap)
        chunks: list[dict] = []
        for t_idx, (tx0, ty0, tx1, ty1) in enumerate(tiles):
            import fitz  # lazy

            with self._fitz_lock:
                pix = page.get_pixmap(dpi=dpi, clip=fitz.Rect(tx0, ty0, tx1, ty1))
                png = pix.tobytes("png")
            for extraction, model, prompt in (
                ("transcription", self.ocr_model, GLM_OCR_PROMPT),
                ("structure", self.vision_model, DEEP_STRUCTURE_PROMPT),
            ):
                try:
                    text = self._call_ollama_vision(
                        png, model=model, prompt=prompt,
                        timeout=self.vision_call_timeout,
                    )
                except Exception as exc:  # a failed tile degrades, never aborts
                    print(
                        f"Warning: deep {extraction} failed page {page_num} "
                        f"tile {t_idx}: {exc}",
                        file=sys.stderr, flush=True,
                    )
                    continue
                if not text.strip():
                    continue
                ch = self._make_chunk(page_num, t_idx, text, model, "deep_scan")
                ch["tile"] = t_idx
                ch["extraction"] = extraction
                chunks.append(ch)
        return chunks
```

`pipeline.py` — `_visual_embed_one_page(sidecar, page, cfg, client, repo_root, router, embedder, verbose=False, deep=False)`; at the router call site (:1035-1036):

```python
        from carta.vision.classifier import PageClass

        profile = router.analyzer.analyze(fitz_page)
        if deep or profile.page_class is PageClass.VECTOR_DRAWING:
            chunks = router.extract_page_deep(fitz_page, page)
        else:
            chunks = router._route(fitz_page, page, profile, doc)
```

`run_visual_embed` per-file loop: `deep = sc.get("deep_scan") == "requested"`, passed through. `tile`/`extraction` flow into Qdrant payloads for free (`embed.py:258-286` passes all chunk keys).

- [ ] **Step 4: Run** — `python -m pytest carta/vision/tests/ carta/embed/tests/test_visual_drainer.py -v` → PASS.

- [ ] **Step 5: Commit**

```bash
git add carta/vision/router.py carta/embed/pipeline.py carta/vision/tests/test_router.py carta/embed/tests/test_visual_drainer.py
git commit -m "feat(vision): deep tiled two-prompt extraction for flagged/vector-CAD pages"
```

---

### Task 7: ET-embed rollout + eval regression guard (operational — runs in ET-embed, not this repo)

**Files (ET-embed repo, `/Users/ian/School/Elementrailer/ET-embed`):**
- Modify: `.carta/config.yaml` (add under `embed:`: `visual_triage_paths: ["docs/reference/suppliers/"]` and `enrichment: {repo_visible: true, suffix: ".extraction.md"}`)
- Create: `docs/reference/suppliers/CTS/H-FL-400150-1_电气原理图V1.3_20260105 2.pdf.extraction.md` (Claude-authored; content per spec Component 4: title block, element inventory with verbatim + translated labels, topology statements incl. **MSD 300A mid-string between the two battery module groups**, relay/precharge/fuse elements, BMS sense taps, page anchors), `.carta/eval.yaml`
- ET-embed lands repo files via branch + PR (repo convention).

- [ ] **Step 1:** `pipx upgrade carta-cc` (or install from the feature branch) so the new CLI is live; `carta doctor` clean.
- [ ] **Step 2:** Apply the `.carta/config.yaml` additions above.
- [ ] **Step 3:** Flag the five dark drawings (reasons matter — they're the audit trail):

```bash
carta flag "docs/reference/suppliers/CTS/H-FL-400150-1_电气原理图V1.3_20260105 2.pdf" --reason "MSD topology question missed 2026-07-29"
carta flag "docs/reference/suppliers/CTS/CTS-battery-20260107/H-FL-400150-1-总成V1.1-26.1.7.PDF" --reason "M8 ground nut + MSD location callouts, vector CAD"
carta flag "docs/reference/suppliers/Goldgun/E-axle and Motor controller/E-Axle/1693505 A1(2).pdf" --reason "6x190 BCD geometry, scanned drawing"
carta flag "docs/reference/suppliers/Goldgun/OBC/TCKH-1684A 外形图 PB1D-21-70C (4).pdf" --reason "PCU mounting outline, image-only scan"
carta flag "docs/reference/suppliers/Goldgun/E-axle and Motor controller/Motor Controller/TZ220XS710A1丰川 380V 50 100 kW 电机 用户手册V1.pdf" --reason "torque specs live here, scanned manual"
```

(Verify each path with `ls` first; the TCKH filename above must be corrected to the actual on-disk name.)
- [ ] **Step 4:** Author the CTS-schematic extraction doc; `carta embed "docs/reference/suppliers/CTS/H-FL-400150-1_电气原理图V1.3_20260105 2.pdf.extraction.md"`; confirm the source sidecar gained `enrichment_path`/`enrichment_source_hash` and `deep_scan: done`.
- [ ] **Step 5:** Create `.carta/eval.yaml`:

```yaml
# Regression guard for the 2026-07-29 dark-schematic incident.
queries:
  - q: "does the MSD split the battery pack mid-string"
    expect: ["电气原理图", "extraction"]
  - q: "e-axle wheel hub bolt circle diameter 6x190"
    expect: ["1693505", "eaxle"]
```

Run `carta eval .carta/eval.yaml` — the MSD query must hit.
- [ ] **Step 6:** Overnight drain: `carta embed --visual` (repeat nightly; resumable per-page). Flagged five drain first, then `docs/reference/suppliers/**`.
- [ ] **Step 7:** `carta search "MSD manual service disconnect mid-string"` — the extraction/schematic must rank top-3. Then amend ET-embed issue #276's comment (the "mid-string is a repo inference" caveat → confirmed, with the extraction doc as citation) and commit the ET-embed files via branch + PR.

---

## Self-review notes

- **Spec coverage:** Component 1 → Task 1 (+ the fail-closed log line); Component 2 → Task 2; Component 3 → Tasks 5+6; Component 4 → Task 4; Component 5 → Task 3; Component 6 → Task 7. Spec's "repair re-runs classification" line is satisfied structurally: repair already classifies (`run_embed_file` → `_embed_one_file`); the actual dark-file mechanisms (scope gate, misclassification, exception) are closed by Tasks 1, 5, and 2's force-queue respectively.
- **Deviation from spec (deliberate, documented in Task 4):** enrichment filename appends the full source name (`a.pdf.extraction.md`), not the stem, matching `companion_rel_path`'s collision rule.
- **Type consistency check:** `flag_file/clear_flag/list_flagged` names used identically in Tasks 2 and 7; `_drain_sort_key` tuple `(rank, requested_at, rel)` used only in Task 3; `extract_page_deep` name identical in Tasks 6 and drain dispatch; `PageClass.VECTOR_DRAWING` spelled identically in Tasks 5-6; config keys `visual_triage_paths`, `deep_scan.*`, `enrichment.*` consistent across Tasks 3-7.
