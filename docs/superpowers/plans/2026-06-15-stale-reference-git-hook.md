# Stale-reference git hook — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `carta hook` command that installs a managed git hook (pre-push by default) which scans changed Carta-tracked docs and warns when a section has been superseded by an authoritative doc already in the knowledge graph.

**Architecture:** Thin git **collectors** (`collect_staged` / `collect_pushed`) turn "what changed" into `ChangedDoc(path, text)`; a stage-agnostic **scan core** (`run_stale_scan`) sections each doc with the embedding chunker, searches the graph per section, and runs a small Ollama **stale judge** on strong external matches. A managed `.git/hooks/<stage>` shim calls `carta hook check`. Everything fails open — a push/commit is never blocked by Carta infrastructure.

**Tech Stack:** Python 3.10+, argparse, `subprocess` (git), `requests` (Ollama), Qdrant via the existing `run_search`, pytest + `unittest.mock`.

**Spec:** `docs/superpowers/specs/2026-06-15-stale-reference-git-hook-design.md`

---

## File structure

| File | Responsibility |
|------|----------------|
| `carta/config.py` (modify) | Add `hooks.stale_scan` defaults to `DEFAULTS` |
| `carta/embed/parse.py` (modify) | Extract `sections_from_markdown(text)` so a raw string can be sectioned (today only `extract_markdown_text(path)` exists) |
| `carta/hook/judge.py` (create) | Reusable `ollama_yesno(...)` — the shared yes/no Ollama call |
| `carta/hook/hook.py` (modify) | Rewrite `_call_ollama_judge` to delegate to `ollama_yesno` (behavior-preserving) |
| `carta/hook/stale_scan.py` (create) | Dataclasses, `run_stale_scan` core, `_stale_judge`, collectors, scope + git helpers |
| `carta/hook/git_hook.py` (create) | `install_hook` / `uninstall_hook` — managed `.git/hooks/<stage>` shim |
| `carta/cli.py` (modify) | `carta hook` subcommand group + `cmd_hook` handler + dispatch entry |
| `carta/hook/tests/test_judge.py` (create) | Tests for `ollama_yesno` |
| `carta/embed/tests/test_parse.py` (create) | Tests for `sections_from_markdown` |
| `carta/hook/tests/test_stale_scan.py` (create) | Tests for scan core + collectors |
| `carta/hook/tests/test_git_hook.py` (create) | Tests for install/uninstall |
| `carta/tests/test_cli.py` (modify) | Tests for `cmd_hook` exit codes |
| `CLAUDE.md` (modify) | Update the Hook / CLI surface tables |

**Run the whole suite with:** `python -m pytest carta/ -q`

---

## Task 1: Config defaults for `hooks.stale_scan`

**Files:**
- Modify: `carta/config.py` (the `DEFAULTS` dict, between the `proactive_recall` block ending at line 126 and `cross_project_recall` at line 127)
- Test: `carta/hook/tests/test_stale_scan.py` (create — first test goes here)

- [ ] **Step 1: Write the failing test**

Create `carta/hook/tests/test_stale_scan.py`:

```python
"""Tests for carta.hook.stale_scan — scan core, collectors, and config defaults."""
from carta.config import DEFAULTS


def test_stale_scan_defaults_present():
    sc = DEFAULTS["hooks"]["stale_scan"]
    assert sc["enabled"] is True
    assert sc["block_on_stale"] is False
    assert sc["candidate_threshold"] == 0.65
    assert sc["judge_timeout_s"] == 5
    assert sc["ollama_model"] == "qwen3.5:0.8b"
    assert sc["max_judge_calls"] == 30
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest carta/hook/tests/test_stale_scan.py::test_stale_scan_defaults_present -v`
Expected: FAIL with `KeyError: 'hooks'`

- [ ] **Step 3: Add the defaults**

In `carta/config.py`, insert a new `"hooks"` block immediately after the `proactive_recall` block (after line 126's `},` and before `"cross_project_recall": {`):

```python
    "hooks": {
        "stale_scan": {
            "enabled": True,
            "block_on_stale": False,
            "candidate_threshold": 0.65,
            "judge_timeout_s": 5,
            "ollama_model": "qwen3.5:0.8b",
            "max_judge_calls": 30,
        },
    },
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest carta/hook/tests/test_stale_scan.py::test_stale_scan_defaults_present -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add carta/config.py carta/hook/tests/test_stale_scan.py
git commit -m "feat(hook): add hooks.stale_scan config defaults (#10)"
```

---

## Task 2: `sections_from_markdown(text)` in parse.py

The scan core must section a raw markdown **string** (from `git show`), but `extract_markdown_text` only takes a file path. Extract the string logic into a reusable function; `extract_markdown_text` then becomes a thin wrapper (no behavior change).

**Files:**
- Modify: `carta/embed/parse.py:113-166` (`extract_markdown_text`)
- Test: `carta/embed/tests/test_parse.py` (create)

- [ ] **Step 1: Write the failing test**

Create `carta/embed/tests/test_parse.py`:

```python
"""Tests for carta.embed.parse string-level helpers."""
from carta.embed.parse import sections_from_markdown


def test_sections_from_markdown_splits_on_headings_and_strips_frontmatter():
    text = (
        "---\ntitle: Demo\n---\n"
        "intro paragraph\n\n"
        "## First\nbody one\n\n"
        "## Second\nbody two\n"
    )
    sections, fm = sections_from_markdown(text)
    assert fm == {"title": "Demo"}
    headings = [s["headings"][0] for s in sections]
    assert "## First" in headings
    assert "## Second" in headings
    # the intro (no heading) is captured as "(intro)"
    assert any(h == "(intro)" for h in headings)


def test_sections_from_markdown_empty_string():
    sections, fm = sections_from_markdown("")
    assert sections == []
    assert fm == {}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest carta/embed/tests/test_parse.py -v`
Expected: FAIL with `ImportError: cannot import name 'sections_from_markdown'`

- [ ] **Step 3: Refactor `extract_markdown_text`**

In `carta/embed/parse.py`, replace the body of `extract_markdown_text` (lines 113-166) with a wrapper that delegates to a new string-based function. The new function holds the existing logic verbatim except it takes `text` instead of reading a file:

```python
def sections_from_markdown(text: str) -> tuple[list[dict], dict]:
    """Split markdown *text* into heading-anchored sections.

    Returns (sections, frontmatter_meta) where sections is a list of dicts with
    the same shape as extract_pdf_text: {"page": int, "text": str, "headings": list[str]}.
    """
    text, frontmatter_meta = _strip_frontmatter(text)

    # Split on ## or ### heading boundaries; keep the delimiter with each section
    raw_sections = re.split(r'(?=^#{2,3}\s)', text, flags=re.MULTILINE)

    sections = []
    for i, section in enumerate(raw_sections):
        section = section.strip()
        if not section:
            continue

        lines = section.splitlines()
        first_line = lines[0].strip() if lines else ""

        if first_line.startswith("#"):
            heading = first_line
            body = "\n".join(lines[1:]).strip()
        else:
            heading = "(intro)"
            body = section

        if not body and heading == "(intro)":
            continue
        if not body and not first_line:
            continue
        combined = (heading + " " + body).strip()
        if not combined or combined == heading.strip() and not body:
            if not body:
                continue

        sections.append({
            "page": i + 1,
            "text": section,
            "headings": [heading],
        })

    return sections, frontmatter_meta


def extract_markdown_text(md_path: Path) -> tuple[list[dict], dict]:
    """Extract heading-anchored sections from a Markdown file (reads then delegates)."""
    return sections_from_markdown(md_path.read_text(encoding="utf-8"))
```

- [ ] **Step 4: Run tests to verify they pass (new + existing parse consumers)**

Run: `python -m pytest carta/embed/tests/test_parse.py carta/embed/tests/test_embed.py -v`
Expected: PASS (new tests pass; existing `extract_markdown_text` consumers unaffected)

- [ ] **Step 5: Commit**

```bash
git add carta/embed/parse.py carta/embed/tests/test_parse.py
git commit -m "refactor(parse): extract sections_from_markdown(text) from extract_markdown_text (#10)"
```

---

## Task 3: Shared `ollama_yesno` judge + behavior-preserving hook refactor

**Files:**
- Create: `carta/hook/judge.py`
- Modify: `carta/hook/hook.py:159-198` (`_call_ollama_judge`)
- Test: `carta/hook/tests/test_judge.py` (create)

- [ ] **Step 1: Write the failing test**

Create `carta/hook/tests/test_judge.py`:

```python
"""Tests for carta.hook.judge.ollama_yesno."""
from unittest.mock import MagicMock, patch

from carta.hook.judge import ollama_yesno


def _resp(content):
    m = MagicMock()
    m.json.return_value = {"message": {"content": content}}
    return m


def test_ollama_yesno_true_on_yes():
    with patch("requests.post", return_value=_resp("Yes, replaced")):
        assert ollama_yesno("http://x", "m", "sys", "usr", timeout_s=1) is True


def test_ollama_yesno_false_on_no():
    with patch("requests.post", return_value=_resp("no")):
        assert ollama_yesno("http://x", "m", "sys", "usr", timeout_s=1) is False


def test_ollama_yesno_none_on_error():
    with patch("requests.post", side_effect=Exception("connection refused")):
        assert ollama_yesno("http://x", "m", "sys", "usr", timeout_s=1) is None


def test_ollama_yesno_sends_system_and_user():
    with patch("requests.post", return_value=_resp("yes")) as mock_post:
        ollama_yesno("http://x", "mymodel", "SYS", "USR", timeout_s=2)
    payload = mock_post.call_args[1]["json"]
    assert payload["model"] == "mymodel"
    roles = {m["role"]: m["content"] for m in payload["messages"]}
    assert roles["system"] == "SYS"
    assert roles["user"] == "USR"
    assert payload["stream"] is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest carta/hook/tests/test_judge.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'carta.hook.judge'`

- [ ] **Step 3: Create `carta/hook/judge.py`**

```python
"""Shared Ollama yes/no judge, used by the proactive-recall hook and the
stale-reference scan. Returns None on any error so callers can fail open."""
from __future__ import annotations

import requests


def ollama_yesno(
    ollama_url: str,
    model: str,
    system: str,
    user: str,
    *,
    timeout_s: float = 4,
) -> bool | None:
    """Ask Ollama a yes/no question via /api/chat.

    Returns True if the answer begins with 'yes', False for any other answer,
    and None on any network/parse error (so the caller can fail open).
    """
    try:
        resp = requests.post(
            f"{ollama_url}/api/chat",
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "stream": False,
            },
            timeout=timeout_s,
        )
        answer = resp.json()["message"]["content"].strip().lower()
        return answer.startswith("yes")
    except Exception:
        return None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest carta/hook/tests/test_judge.py -v`
Expected: PASS

- [ ] **Step 5: Rewrite `_call_ollama_judge` to delegate (behavior-preserving)**

In `carta/hook/hook.py`, replace `_call_ollama_judge` (lines 159-198). Keep the exact system/user strings so `test_call_ollama_judge_sends_correct_format` still passes; map `None → False` to preserve fail-open:

```python
def _call_ollama_judge(prompt: str, hits: list[dict], cfg: dict) -> bool:
    """Judge whether the documentation candidates are relevant. Returns True only
    on a 'yes'; any error or non-yes answer returns False (fail-open)."""
    from carta.hook.judge import ollama_yesno

    ollama_url = cfg["embed"]["ollama_url"]
    model = cfg["proactive_recall"]["ollama_model"]
    excerpts = "\n---\n".join(h["excerpt"][:200] for h in hits)
    user_msg = (
        f"Prompt: {prompt[:300]}\n\n"
        f"Documentation candidates:\n{excerpts}\n\n"
        f"Are any of these relevant?"
    )
    system = (
        "You decide if documentation is relevant to a coding prompt. "
        "Answer only 'yes' or 'no'."
    )
    return bool(ollama_yesno(ollama_url, model, system, user_msg, timeout_s=4))
```

- [ ] **Step 6: Run the full hook suite to verify no regression**

Run: `python -m pytest carta/hook/tests/ -v`
Expected: PASS (all 27 existing hook tests + the 4 new judge tests)

- [ ] **Step 7: Commit**

```bash
git add carta/hook/judge.py carta/hook/hook.py carta/hook/tests/test_judge.py
git commit -m "refactor(hook): extract shared ollama_yesno judge (#10)"
```

---

## Task 4: Data types + `_search_cfg` + scope helper

**Files:**
- Create: `carta/hook/stale_scan.py`
- Test: `carta/hook/tests/test_stale_scan.py` (append)

- [ ] **Step 1: Write the failing test**

Append to `carta/hook/tests/test_stale_scan.py`:

```python
from pathlib import Path

from carta.hook.stale_scan import ChangedDoc, StaleFinding, StaleScanResult, _in_doc_scope, _search_cfg


def test_dataclasses_construct():
    cd = ChangedDoc(path="docs/a.md", text="hi")
    assert cd.path == "docs/a.md"
    f = StaleFinding(file="docs/a.md", section="## X", snippet="...", candidate_path="docs/b.md", candidate_score=0.9)
    assert f.candidate_score == 0.9
    r = StaleScanResult()
    assert r.findings == [] and r.scanned == 0 and r.judge_calls == 0 and r.skipped_overflow == 0


def test_search_cfg_forces_rerank_and_colpali_off():
    cfg = {"embed": {"colpali_enabled": True}, "search": {"rerank": {"enabled": True}}}
    out = _search_cfg(cfg)
    assert out["embed"]["colpali_enabled"] is False
    assert out["search"]["rerank"]["enabled"] is False


def test_in_doc_scope(tmp_path):
    cfg = {"docs_root": "docs/", "excluded_paths": []}
    assert _in_doc_scope("docs/guide.md", cfg, tmp_path) is True
    assert _in_doc_scope("src/main.py", cfg, tmp_path) is False      # not .md
    assert _in_doc_scope("notes/x.md", cfg, tmp_path) is False        # outside docs_root
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest carta/hook/tests/test_stale_scan.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'carta.hook.stale_scan'`

- [ ] **Step 3: Create `carta/hook/stale_scan.py` with types + helpers**

```python
"""Stale-reference scan: section changed docs, search the graph per section, and
ask a small Ollama judge whether any section has been superseded. Stage-agnostic
core (run_stale_scan) plus thin git collectors. Fails open everywhere."""
from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from carta.scanner.scanner import is_excluded


@dataclass
class ChangedDoc:
    path: str   # repo-relative doc path
    text: str   # changed/staged content to scan


@dataclass
class StaleFinding:
    file: str
    section: str
    snippet: str
    candidate_path: str
    candidate_score: float


@dataclass
class StaleScanResult:
    findings: list = field(default_factory=list)
    scanned: int = 0
    judge_calls: int = 0
    skipped_overflow: int = 0


def _search_cfg(cfg: dict) -> dict:
    """Text-only search config: no ColPali, no rerank — the prompt-hook latency pattern."""
    return {
        **cfg,
        "embed": {**cfg.get("embed", {}), "colpali_enabled": False},
        "search": {
            **cfg.get("search", {}),
            "rerank": {**cfg.get("search", {}).get("rerank", {}), "enabled": False},
        },
    }


def _in_doc_scope(rel_path: str, cfg: dict, repo_root: Path) -> bool:
    """True if rel_path is a Markdown doc under docs_root and not excluded."""
    if not rel_path.endswith(".md"):
        return False
    docs_root = cfg.get("docs_root", "docs/").rstrip("/")
    if not (rel_path == docs_root or rel_path.startswith(docs_root + "/")):
        return False
    return not is_excluded(repo_root / rel_path, cfg, repo_root)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest carta/hook/tests/test_stale_scan.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add carta/hook/stale_scan.py carta/hook/tests/test_stale_scan.py
git commit -m "feat(hook): stale-scan dataclasses, search-cfg, scope helper (#10)"
```

---

## Task 5: `run_stale_scan` core (the heart)

**Files:**
- Modify: `carta/hook/stale_scan.py` (add `run_stale_scan`)
- Test: `carta/hook/tests/test_stale_scan.py` (append)

- [ ] **Step 1: Write the failing tests**

Append to `carta/hook/tests/test_stale_scan.py`:

```python
from carta.hook.stale_scan import run_stale_scan

_CFG = {"embed": {"chunking": {"max_tokens": 400}}, "hooks": {"stale_scan": {"candidate_threshold": 0.65, "max_judge_calls": 30}}}


def _doc(path="docs/a.md"):
    return ChangedDoc(path=path, text="## micro-ROS UART\nThe UART transport uses micro-ROS.\n")


def test_stale_section_with_yes_judge_yields_finding():
    search = lambda q: [{"source": "docs/cobs.md", "score": 0.91, "excerpt": "COBS+JSON replaced micro-ROS"}]
    judge = lambda section_text, candidate: True
    result = run_stale_scan(Path("/repo"), _CFG, [_doc()], search_fn=search, judge_fn=judge)
    assert result.scanned == 1
    assert len(result.findings) == 1
    f = result.findings[0]
    assert f.file == "docs/a.md"
    assert f.candidate_path == "docs/cobs.md"
    assert f.candidate_score == 0.91


def test_related_section_with_no_judge_yields_nothing():
    search = lambda q: [{"source": "docs/cobs.md", "score": 0.91, "excerpt": "related but not superseding"}]
    judge = lambda section_text, candidate: False
    result = run_stale_scan(Path("/repo"), _CFG, [_doc()], search_fn=search, judge_fn=judge)
    assert result.findings == []
    assert result.judge_calls == 1


def test_below_threshold_skips_judge():
    calls = []
    search = lambda q: [{"source": "docs/cobs.md", "score": 0.40, "excerpt": "weak match"}]
    judge = lambda section_text, candidate: calls.append(1) or True
    result = run_stale_scan(Path("/repo"), _CFG, [_doc()], search_fn=search, judge_fn=judge)
    assert result.findings == []
    assert result.judge_calls == 0
    assert calls == []


def test_judge_none_fails_open():
    search = lambda q: [{"source": "docs/cobs.md", "score": 0.91, "excerpt": "x"}]
    judge = lambda section_text, candidate: None
    result = run_stale_scan(Path("/repo"), _CFG, [_doc()], search_fn=search, judge_fn=judge)
    assert result.findings == []


def test_self_hits_filtered():
    # Only hit is the doc itself -> no external candidate -> no judge, no finding
    search = lambda q: [{"source": "docs/a.md", "score": 0.99, "excerpt": "itself"}]
    judge = lambda section_text, candidate: True
    result = run_stale_scan(Path("/repo"), _CFG, [_doc("docs/a.md")], search_fn=search, judge_fn=judge)
    assert result.judge_calls == 0
    assert result.findings == []


def test_search_error_fails_open_per_section():
    def boom(q):
        raise RuntimeError("qdrant down")
    result = run_stale_scan(Path("/repo"), _CFG, [_doc()], search_fn=boom, judge_fn=lambda s, c: True)
    assert result.findings == []


def test_max_judge_calls_cap_reports_overflow():
    cfg = {"embed": {"chunking": {"max_tokens": 400}}, "hooks": {"stale_scan": {"candidate_threshold": 0.65, "max_judge_calls": 1}}}
    docs = [
        ChangedDoc(path="docs/a.md", text="## A\nalpha body text\n"),
        ChangedDoc(path="docs/b.md", text="## B\nbeta body text\n"),
    ]
    search = lambda q: [{"source": "docs/other.md", "score": 0.9, "excerpt": "x"}]
    judge = lambda section_text, candidate: True
    result = run_stale_scan(Path("/repo"), cfg, docs, search_fn=search, judge_fn=judge)
    assert result.judge_calls == 1
    assert result.skipped_overflow == 1
    assert len(result.findings) == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest carta/hook/tests/test_stale_scan.py -k "stale_section or related_section or below_threshold or judge_none or self_hits or search_error or max_judge" -v`
Expected: FAIL with `ImportError: cannot import name 'run_stale_scan'`

- [ ] **Step 3: Implement `run_stale_scan`**

Add to `carta/hook/stale_scan.py` (imports at top: extend with `sections_from_markdown`, `chunk_text`):

```python
from carta.embed.parse import chunk_text, sections_from_markdown


def run_stale_scan(repo_root, cfg, changed_docs, *, search_fn=None, judge_fn=None) -> StaleScanResult:
    """Scan changed_docs for superseded sections.

    search_fn(query) -> list of hit dicts {"source","score","excerpt"}.
    judge_fn(section_text, candidate_hit) -> True (stale) / False / None (unknown).
    Both default to the real search + Ollama stale judge; injectable for tests.
    """
    if search_fn is None:
        from carta.embed.pipeline import run_search
        search_fn = lambda q: run_search(q, _search_cfg(cfg))  # noqa: E731
    if judge_fn is None:
        judge_fn = lambda section_text, candidate: _stale_judge(section_text, candidate, cfg)  # noqa: E731

    sc = cfg.get("hooks", {}).get("stale_scan", {})
    threshold = sc.get("candidate_threshold", 0.65)
    max_judge_calls = sc.get("max_judge_calls", 30)
    max_tokens = cfg.get("embed", {}).get("chunking", {}).get("max_tokens", 400)

    result = StaleScanResult()
    for doc in changed_docs:
        result.scanned += 1
        sections, _ = sections_from_markdown(doc.text)
        chunks = chunk_text(sections, max_tokens=max_tokens)
        for chunk in chunks:
            try:
                hits = search_fn(chunk["text"])
            except Exception:
                continue  # fail open for this section
            hits = [h for h in (hits or []) if h.get("source") != doc.path]
            if not hits or hits[0].get("score", 0.0) < threshold:
                continue
            if result.judge_calls >= max_judge_calls:
                result.skipped_overflow += 1
                continue
            result.judge_calls += 1
            try:
                verdict = judge_fn(chunk["text"], hits[0])
            except Exception:
                verdict = None
            if verdict:
                result.findings.append(StaleFinding(
                    file=doc.path,
                    section=chunk.get("section_heading", ""),
                    snippet=chunk["text"][:160],
                    candidate_path=hits[0].get("source", ""),
                    candidate_score=hits[0].get("score", 0.0),
                ))
    return result
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest carta/hook/tests/test_stale_scan.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add carta/hook/stale_scan.py carta/hook/tests/test_stale_scan.py
git commit -m "feat(hook): run_stale_scan core with per-section search+judge gate (#10)"
```

---

## Task 6: `_stale_judge` default judge

**Files:**
- Modify: `carta/hook/stale_scan.py` (add `_stale_judge`)
- Test: `carta/hook/tests/test_stale_scan.py` (append)

- [ ] **Step 1: Write the failing test**

Append to `carta/hook/tests/test_stale_scan.py`:

```python
from unittest.mock import patch
from carta.hook.stale_scan import _stale_judge

_JCFG = {"embed": {"ollama_url": "http://x"}, "hooks": {"stale_scan": {"ollama_model": "m", "judge_timeout_s": 5}}}


def test_stale_judge_calls_ollama_with_both_excerpts():
    cand = {"source": "docs/cobs.md", "excerpt": "COBS+JSON replaced micro-ROS"}
    with patch("carta.hook.stale_scan.ollama_yesno", return_value=True) as oj:
        out = _stale_judge("micro-ROS UART section", cand, _JCFG)
    assert out is True
    args, kwargs = oj.call_args
    # positional: (ollama_url, model, system, user)
    assert args[0] == "http://x"
    assert args[1] == "m"
    assert "micro-ROS UART section" in args[3]
    assert "COBS+JSON replaced micro-ROS" in args[3]
    assert kwargs["timeout_s"] == 5
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest carta/hook/tests/test_stale_scan.py::test_stale_judge_calls_ollama_with_both_excerpts -v`
Expected: FAIL with `ImportError: cannot import name '_stale_judge'`

- [ ] **Step 3: Implement `_stale_judge`**

Add to `carta/hook/stale_scan.py` (import `ollama_yesno` at top: `from carta.hook.judge import ollama_yesno`):

```python
def _stale_judge(section_text: str, candidate: dict, cfg: dict):
    """Ask the small model whether `candidate` indicates `section_text` is superseded.
    Returns True/False/None (None on error → caller fails open)."""
    sc = cfg.get("hooks", {}).get("stale_scan", {})
    ollama_url = cfg["embed"]["ollama_url"]
    model = sc.get("ollama_model", "qwen3.5:0.8b")
    timeout_s = sc.get("judge_timeout_s", 5)
    system = (
        "You decide whether a documentation section has been SUPERSEDED. "
        "Answer only 'yes' or 'no'. Answer 'yes' only if the knowledge-base "
        "excerpt clearly indicates the approach, component, or protocol in the "
        "committed section has been replaced or deprecated. If they are merely "
        "related or complementary, answer 'no'."
    )
    user = (
        f"Committed section:\n{section_text[:600]}\n\n"
        f"Knowledge-base excerpt ({candidate.get('source', '')}):\n"
        f"{candidate.get('excerpt', '')[:600]}\n\n"
        f"Has the committed section been replaced or deprecated?"
    )
    return ollama_yesno(ollama_url, model, system, user, timeout_s=timeout_s)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest carta/hook/tests/test_stale_scan.py::test_stale_judge_calls_ollama_with_both_excerpts -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add carta/hook/stale_scan.py carta/hook/tests/test_stale_scan.py
git commit -m "feat(hook): default stale judge prompt over ollama_yesno (#10)"
```

---

## Task 7: Git collectors `collect_staged` / `collect_pushed`

**Files:**
- Modify: `carta/hook/stale_scan.py` (add `_git`, `_default_branch`, `ZERO_OID`, `collect_staged`, `collect_pushed`)
- Test: `carta/hook/tests/test_stale_scan.py` (append; uses a real temp git repo)

- [ ] **Step 1: Write the failing tests**

Append to `carta/hook/tests/test_stale_scan.py`:

```python
import subprocess
import pytest
from carta.hook.stale_scan import collect_staged, collect_pushed


def _git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.email", "t@t.t")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "docs").mkdir()
    return tmp_path


def test_collect_staged_returns_scoped_docs(repo):
    (repo / "docs" / "a.md").write_text("## A\nbody\n")
    (repo / "src.py").write_text("print()\n")
    _git(repo, "add", "docs/a.md", "src.py")
    cfg = {"docs_root": "docs/", "excluded_paths": []}
    docs = collect_staged(repo, cfg)
    paths = [d.path for d in docs]
    assert paths == ["docs/a.md"]              # .py excluded by scope
    assert "## A" in docs[0].text


def test_collect_pushed_uses_range_and_pushed_tip(repo):
    (repo / "docs" / "a.md").write_text("v1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "c1")
    base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True).stdout.strip()
    (repo / "docs" / "a.md").write_text("## A\nv2 changed\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "c2")
    tip = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True).stdout.strip()
    cfg = {"docs_root": "docs/", "excluded_paths": []}
    stdin_lines = [f"refs/heads/main {tip} refs/heads/main {base}"]
    docs = collect_pushed(repo, cfg, stdin_lines)
    assert [d.path for d in docs] == ["docs/a.md"]
    assert "v2 changed" in docs[0].text


def test_collect_pushed_new_branch_zero_oid(repo):
    # remote_sha is the zero OID -> fall back to default-branch merge-base range
    (repo / "docs" / "a.md").write_text("## A\nnew branch content\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "c1")
    tip = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True).stdout.strip()
    cfg = {"docs_root": "docs/", "excluded_paths": []}
    zero = "0" * 40
    stdin_lines = [f"refs/heads/feature {tip} refs/heads/feature {zero}"]
    docs = collect_pushed(repo, cfg, stdin_lines)
    assert [d.path for d in docs] == ["docs/a.md"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest carta/hook/tests/test_stale_scan.py -k collect -v`
Expected: FAIL with `ImportError: cannot import name 'collect_staged'`

- [ ] **Step 3: Implement the collectors**

Add to `carta/hook/stale_scan.py`:

```python
ZERO_OID = "0" * 40


def _git(repo_root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo_root, capture_output=True, text=True, check=True,
    ).stdout


def _default_branch(repo_root: Path) -> str:
    try:
        ref = _git(repo_root, "rev-parse", "--abbrev-ref", "origin/HEAD").strip()
        return ref.split("/", 1)[1] if "/" in ref else ref
    except subprocess.CalledProcessError:
        return "main"


def collect_staged(repo_root: Path, cfg: dict) -> list[ChangedDoc]:
    out = _git(repo_root, "diff", "--cached", "--name-only", "--diff-filter=ACM")
    docs: list[ChangedDoc] = []
    for rel in out.splitlines():
        rel = rel.strip()
        if not rel or not _in_doc_scope(rel, cfg, repo_root):
            continue
        try:
            text = _git(repo_root, "show", f":{rel}")
        except subprocess.CalledProcessError:
            continue
        docs.append(ChangedDoc(path=rel, text=text))
    return docs


def collect_pushed(repo_root: Path, cfg: dict, stdin_lines: list[str]) -> list[ChangedDoc]:
    ranges: list[tuple[str, str]] = []  # (range_spec, tip_sha)
    for line in stdin_lines:
        parts = line.split()
        if len(parts) < 4:
            continue
        _, local_sha, _, remote_sha = parts[0], parts[1], parts[2], parts[3]
        if set(local_sha) == {"0"}:
            continue  # branch deletion — nothing to scan
        if set(remote_sha) == {"0"}:
            base = _default_branch(repo_root)
            try:
                mb = _git(repo_root, "merge-base", base, local_sha).strip()
                rng = f"{mb}..{local_sha}"
            except subprocess.CalledProcessError:
                rng = local_sha  # all commits reachable from tip
        else:
            rng = f"{remote_sha}..{local_sha}"
        ranges.append((rng, local_sha))

    if not stdin_lines:  # manual invocation — scan default-branch..HEAD
        ranges = [(f"{_default_branch(repo_root)}..HEAD", "HEAD")]

    seen: dict[str, ChangedDoc] = {}
    for rng, tip in ranges:
        try:
            out = _git(repo_root, "diff", "--name-only", "--diff-filter=ACM", rng)
        except subprocess.CalledProcessError:
            continue
        for rel in out.splitlines():
            rel = rel.strip()
            if not rel or rel in seen or not _in_doc_scope(rel, cfg, repo_root):
                continue
            try:
                text = _git(repo_root, "show", f"{tip}:{rel}")
            except subprocess.CalledProcessError:
                continue
            seen[rel] = ChangedDoc(path=rel, text=text)
    return list(seen.values())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest carta/hook/tests/test_stale_scan.py -k collect -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add carta/hook/stale_scan.py carta/hook/tests/test_stale_scan.py
git commit -m "feat(hook): staged + pushed-range git collectors (#10)"
```

---

## Task 8: Managed git-hook install/uninstall

**Files:**
- Create: `carta/hook/git_hook.py`
- Test: `carta/hook/tests/test_git_hook.py` (create)

- [ ] **Step 1: Write the failing tests**

Create `carta/hook/tests/test_git_hook.py`:

```python
"""Tests for carta.hook.git_hook install/uninstall of the managed shim."""
import pytest

from carta.hook.git_hook import SENTINEL_START, install_hook, uninstall_hook


def _hooks(tmp_path):
    d = tmp_path / ".git" / "hooks"
    d.mkdir(parents=True)
    return d


def test_install_fresh_writes_executable_shim(tmp_path):
    _hooks(tmp_path)
    status = install_hook(tmp_path, "pre-push")
    hook = tmp_path / ".git" / "hooks" / "pre-push"
    assert status == "installed"
    text = hook.read_text()
    assert SENTINEL_START in text
    assert "carta hook check --stage pre-push" in text
    assert hook.stat().st_mode & 0o100  # owner-executable


def test_install_is_idempotent(tmp_path):
    _hooks(tmp_path)
    install_hook(tmp_path, "pre-push")
    status = install_hook(tmp_path, "pre-push")
    assert status == "already-installed"
    text = (tmp_path / ".git" / "hooks" / "pre-push").read_text()
    assert text.count(SENTINEL_START) == 1


def test_install_refuses_foreign_hook(tmp_path):
    d = _hooks(tmp_path)
    (d / "pre-push").write_text("#!/bin/sh\necho mine\n")
    with pytest.raises(FileExistsError):
        install_hook(tmp_path, "pre-push")
    # foreign content untouched
    assert "echo mine" in (d / "pre-push").read_text()


def test_uninstall_removes_managed_file(tmp_path):
    _hooks(tmp_path)
    install_hook(tmp_path, "pre-push")
    status = uninstall_hook(tmp_path, "pre-push")
    assert status == "removed-file"
    assert not (tmp_path / ".git" / "hooks" / "pre-push").exists()


def test_uninstall_strips_block_from_chained_hook(tmp_path):
    d = _hooks(tmp_path)
    hook = d / "pre-push"
    hook.write_text(
        "#!/bin/sh\necho mine\n"
        f"{SENTINEL_START}\ncarta hook check --stage pre-push || exit $?\n# <<< carta managed <<<\n"
    )
    status = uninstall_hook(tmp_path, "pre-push")
    assert status == "removed-block"
    remaining = hook.read_text()
    assert "echo mine" in remaining
    assert SENTINEL_START not in remaining


def test_install_rejects_bad_stage(tmp_path):
    _hooks(tmp_path)
    with pytest.raises(ValueError):
        install_hook(tmp_path, "post-merge")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest carta/hook/tests/test_git_hook.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'carta.hook.git_hook'`

- [ ] **Step 3: Create `carta/hook/git_hook.py`**

```python
"""Install/remove a managed git-hook shim that runs `carta hook check`.

The shim is wrapped in sentinel comments so it can live alongside (chained into)
a user's existing hook and be removed cleanly."""
from __future__ import annotations

from pathlib import Path

SENTINEL_START = "# >>> carta managed >>>"
SENTINEL_END = "# <<< carta managed <<<"
VALID_STAGES = ("pre-push", "pre-commit")


def _shim_block(stage: str) -> str:
    return (
        f"{SENTINEL_START}\n"
        f"carta hook check --stage {stage} || exit $?\n"
        f"{SENTINEL_END}\n"
    )


def _hook_path(repo_root: Path, stage: str) -> Path:
    return repo_root / ".git" / "hooks" / stage


def install_hook(repo_root: Path, stage: str) -> str:
    if stage not in VALID_STAGES:
        raise ValueError(f"unknown hook stage: {stage}")
    hook = _hook_path(repo_root, stage)
    hook.parent.mkdir(parents=True, exist_ok=True)
    if not hook.exists():
        hook.write_text("#!/bin/sh\n" + _shim_block(stage), encoding="utf-8")
        hook.chmod(0o755)
        return "installed"
    existing = hook.read_text(encoding="utf-8")
    if SENTINEL_START in existing:
        return "already-installed"
    raise FileExistsError(
        f"A non-Carta {stage} hook already exists at {hook}. "
        f"Chain Carta in by adding this line:\n"
        f"  carta hook check --stage {stage} || exit $?"
    )


def uninstall_hook(repo_root: Path, stage: str) -> str:
    if stage not in VALID_STAGES:
        raise ValueError(f"unknown hook stage: {stage}")
    hook = _hook_path(repo_root, stage)
    if not hook.exists():
        return "absent"
    text = hook.read_text(encoding="utf-8")
    if SENTINEL_START not in text:
        return "not-managed"
    out, skipping = [], False
    for ln in text.splitlines(keepends=True):
        stripped = ln.strip()
        if stripped == SENTINEL_START:
            skipping = True
            continue
        if stripped == SENTINEL_END:
            skipping = False
            continue
        if not skipping:
            out.append(ln)
    remaining = "".join(out).strip()
    if remaining in ("", "#!/bin/sh"):
        hook.unlink()
        return "removed-file"
    hook.write_text("".join(out), encoding="utf-8")
    return "removed-block"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest carta/hook/tests/test_git_hook.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add carta/hook/git_hook.py carta/hook/tests/test_git_hook.py
git commit -m "feat(hook): managed git-hook install/uninstall shim (#10)"
```

---

## Task 9: CLI `carta hook` subcommand + `cmd_hook`

**Files:**
- Modify: `carta/cli.py` (add `cmd_hook` handler near other `cmd_*`; register `hook` subparser before the `dispatch` dict at line 944; add `"hook": cmd_hook` to `dispatch`)
- Test: `carta/tests/test_cli.py` (append)

- [ ] **Step 1: Write the failing tests**

Append to `carta/tests/test_cli.py`:

```python
def test_cmd_hook_check_warn_only_exits_zero(tmp_path, monkeypatch, capsys):
    import carta.cli as cli
    from carta.hook.stale_scan import StaleFinding, StaleScanResult

    cfg_path = tmp_path / ".carta" / "config.yaml"
    cfg_path.parent.mkdir(parents=True)
    cfg_path.write_text("")  # contents irrelevant; load_config is patched

    cfg = {"hooks": {"stale_scan": {"enabled": True, "block_on_stale": False}}}
    monkeypatch.setattr(cli, "find_config", lambda: cfg_path)
    monkeypatch.setattr("carta.config.load_config", lambda p: cfg)
    monkeypatch.setattr("carta.hook.stale_scan.collect_staged", lambda r, c: [object()])
    result = StaleScanResult(findings=[StaleFinding("docs/a.md", "## A", "snip", "docs/b.md", 0.9)], scanned=1)
    monkeypatch.setattr("carta.hook.stale_scan.run_stale_scan", lambda r, c, d: result)

    args = type("A", (), {"command": "hook", "hook_action": "check", "stage": "pre-commit"})()
    with pytest.raises(SystemExit) as exc:
        cli.cmd_hook(args)
    assert exc.value.code == 0
    assert "may be stale" in capsys.readouterr().err


def test_cmd_hook_check_block_on_stale_exits_one(tmp_path, monkeypatch):
    import carta.cli as cli
    from carta.hook.stale_scan import StaleFinding, StaleScanResult

    cfg_path = tmp_path / ".carta" / "config.yaml"
    cfg_path.parent.mkdir(parents=True)
    cfg_path.write_text("")
    cfg = {"hooks": {"stale_scan": {"enabled": True, "block_on_stale": True}}}
    monkeypatch.setattr(cli, "find_config", lambda: cfg_path)
    monkeypatch.setattr("carta.config.load_config", lambda p: cfg)
    monkeypatch.setattr("carta.hook.stale_scan.collect_staged", lambda r, c: [object()])
    result = StaleScanResult(findings=[StaleFinding("docs/a.md", "## A", "s", "docs/b.md", 0.9)], scanned=1)
    monkeypatch.setattr("carta.hook.stale_scan.run_stale_scan", lambda r, c, d: result)

    args = type("A", (), {"command": "hook", "hook_action": "check", "stage": "pre-commit"})()
    with pytest.raises(SystemExit) as exc:
        cli.cmd_hook(args)
    assert exc.value.code == 1


def test_cmd_hook_check_disabled_exits_zero(tmp_path, monkeypatch):
    import carta.cli as cli
    cfg_path = tmp_path / ".carta" / "config.yaml"
    cfg_path.parent.mkdir(parents=True)
    cfg_path.write_text("")
    cfg = {"hooks": {"stale_scan": {"enabled": False}}}
    monkeypatch.setattr(cli, "find_config", lambda: cfg_path)
    monkeypatch.setattr("carta.config.load_config", lambda p: cfg)

    args = type("A", (), {"command": "hook", "hook_action": "check", "stage": "pre-commit"})()
    with pytest.raises(SystemExit) as exc:
        cli.cmd_hook(args)
    assert exc.value.code == 0


def test_cmd_hook_install_uses_git_root_not_carta_config(tmp_path):
    """install works in a plain git repo (no carta init) — repo root from git."""
    import subprocess
    import carta.cli as cli

    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    args = type("A", (), {"command": "hook", "hook_action": "install", "stage": "pre-push", "uninstall": False})()
    # cmd_hook resolves the repo root via `git rev-parse` from cwd
    import os
    cwd = os.getcwd()
    try:
        os.chdir(tmp_path)
        cli.cmd_hook(args)  # returns (no SystemExit) on success
    finally:
        os.chdir(cwd)
    assert (tmp_path / ".git" / "hooks" / "pre-push").exists()
```

> Note: `carta/tests/test_cli.py` does **not** currently import `pytest` (it uses `unittest.mock.patch` and bare `monkeypatch` fixtures). These new tests use `pytest.raises`, so add `import pytest` to the top of the file as part of this step.

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest carta/tests/test_cli.py -k cmd_hook -v`
Expected: FAIL with `AttributeError: module 'carta.cli' has no attribute 'cmd_hook'`

- [ ] **Step 3: Add `cmd_hook` and `_print_stale_result` to `carta/cli.py`**

Add near the other `cmd_*` handlers:

```python
def _print_stale_result(result, scfg):
    import sys
    if not result.findings:
        return
    print(f"carta stale-scan: scanned {result.scanned} doc(s)...", file=sys.stderr)
    for f in result.findings:
        section = f"Section \"{f.section}\" " if f.section and f.section != "(intro)" else ""
        print(f"  ⚠  {f.file}", file=sys.stderr)
        print(
            f"     {section}may be stale — knowledge base suggests it was replaced "
            f"({f.candidate_path}, score {f.candidate_score:.2f}).",
            file=sys.stderr,
        )
        if f.section and f.section != "(intro)":
            print(f"     Run: /doc-search \"{f.section.lstrip('# ').strip()}\"", file=sys.stderr)
    if result.skipped_overflow:
        print(f"  ({result.skipped_overflow} more section(s) not checked — max_judge_calls cap)", file=sys.stderr)
    if not scfg.get("block_on_stale", False):
        print("  (warn-only; set hooks.stale_scan.block_on_stale: true to fail)", file=sys.stderr)


def cmd_hook(args):
    import subprocess
    import sys

    action = getattr(args, "hook_action", None)

    # install/uninstall need only the git root — NOT a full Carta setup, so a
    # user can install the hook before/independent of `carta init`.
    if action == "install":
        from carta.hook.git_hook import install_hook, uninstall_hook
        try:
            repo_root = Path(subprocess.run(
                ["git", "rev-parse", "--show-toplevel"],
                capture_output=True, text=True, check=True,
            ).stdout.strip())
        except (subprocess.CalledProcessError, FileNotFoundError):
            print("Not a git repository.", file=sys.stderr)
            sys.exit(1)
        stage = args.stage
        if getattr(args, "uninstall", False):
            print(f"carta hook ({stage}): {uninstall_hook(repo_root, stage)}")
            return
        try:
            status = install_hook(repo_root, stage)
        except FileExistsError as e:
            print(str(e), file=sys.stderr)
            sys.exit(1)
        print(f"carta hook ({stage}): {status} → .git/hooks/{stage}")
        return

    if action == "check":
        from carta.config import load_config
        from carta.hook import stale_scan
        try:
            cfg_path = find_config()
        except FileNotFoundError:
            sys.exit(0)  # not a Carta repo → nothing to check, fail-open
        cfg = load_config(cfg_path)
        repo_root = cfg_path.parent.parent
        scfg = cfg.get("hooks", {}).get("stale_scan", {})
        if not scfg.get("enabled", True):
            sys.exit(0)
        stage = args.stage
        try:
            if stage == "pre-commit":
                docs = stale_scan.collect_staged(repo_root, cfg)
            else:
                stdin_lines = [] if sys.stdin.isatty() else sys.stdin.read().splitlines()
                docs = stale_scan.collect_pushed(repo_root, cfg, stdin_lines)
        except Exception as e:
            print(f"carta stale-scan: collection error (fail-open): {e}", file=sys.stderr)
            sys.exit(0)
        if not docs:
            sys.exit(0)
        try:
            result = stale_scan.run_stale_scan(repo_root, cfg, docs)
        except Exception as e:
            print(f"carta stale-scan: scan error (fail-open): {e}", file=sys.stderr)
            sys.exit(0)
        _print_stale_result(result, scfg)
        if result.findings and scfg.get("block_on_stale", False):
            sys.exit(1)
        sys.exit(0)

    print("usage: carta hook {install,check} [--stage pre-push|pre-commit]", file=sys.stderr)
    sys.exit(1)
```

> The tests patch `carta.hook.stale_scan.collect_staged` / `run_stale_scan`, so the handler MUST reference them as `stale_scan.collect_staged(...)` / `stale_scan.run_stale_scan(...)` (module-qualified), not via `from ... import`.

- [ ] **Step 4: Register the subparser and dispatch entry**

In `carta/cli.py`, before the `dispatch = {` dict (line 944), add:

```python
    hook_p = sub.add_parser("hook", help="Manage Carta git hooks (stale-reference scan)")
    hook_sub = hook_p.add_subparsers(dest="hook_action")
    hook_install = hook_sub.add_parser("install", help="Install/remove the managed git hook")
    hook_install.add_argument("--stage", choices=["pre-push", "pre-commit"], default="pre-push")
    hook_install.add_argument("--uninstall", action="store_true", help="Remove the managed hook")
    hook_check = hook_sub.add_parser("check", help="Run the stale-reference scan (used by the git shim)")
    hook_check.add_argument("--stage", choices=["pre-push", "pre-commit"], default="pre-push")
```

Then add to the `dispatch` dict:

```python
        "hook": cmd_hook,
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest carta/tests/test_cli.py -k cmd_hook -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add carta/cli.py carta/tests/test_cli.py
git commit -m "feat(cli): carta hook install/check subcommands (#10)"
```

---

## Task 10: Docs + full-suite verification + manual smoke

**Files:**
- Modify: `CLAUDE.md` (Hook + CLI surface sections)
- Modify: `docs/superpowers/specs/2026-06-15-stale-reference-git-hook-design.md` (status → shipped)

- [ ] **Step 1: Update CLAUDE.md surface tables**

In the CLI table, add a row:

```markdown
| `hook` | Install/run the stale-reference git hook (`hook install`, `hook check`; pre-push default) |
```

In the Hook section, append:

```markdown
A second, opt-in hook — `carta hook` — installs a managed git `pre-push` (or
`pre-commit`) shim that scans changed docs and warns when a section has been
superseded by an authoritative doc in the graph. Warn-only by default; fail-open.
```

- [ ] **Step 2: Mark the spec shipped**

In `docs/superpowers/specs/2026-06-15-stale-reference-git-hook-design.md`, change the frontmatter `status: draft` to `status: shipped`.

- [ ] **Step 3: Run the FULL suite**

Run: `python -m pytest carta/ -q`
Expected: PASS — previous baseline was 939 tests; expect ~939 + the new tests, 0 failures, 2 pre-existing skips.

- [ ] **Step 4: Manual smoke test (real git repo, no Qdrant/Ollama required for install path)**

Run:
```bash
cd /tmp && rm -rf cartahooktest && mkdir cartahooktest && cd cartahooktest && git init -q
carta hook install --stage pre-push        # expect: "installed → .git/hooks/pre-push"
test -x .git/hooks/pre-push && echo OK-executable
carta hook install --stage pre-push        # expect: "already-installed"
carta hook install --stage pre-push --uninstall   # expect: "removed-file"
```
Expected: the three messages above; second install does not duplicate; uninstall removes the file.

> The `check` path needs a Carta-initialised repo with Qdrant/Ollama; that is the maintainer's corpus validation, not part of this commit. With services down it must still exit 0 (fail-open) — covered by the disabled/empty-docs CLI tests.

- [ ] **Step 5: Commit**

```bash
git add CLAUDE.md docs/superpowers/specs/2026-06-15-stale-reference-git-hook-design.md
git commit -m "docs(hook): document carta hook surface; mark spec shipped (#10)"
```

---

## Self-review notes (for the implementer)

- **Spec coverage:** detection (Tasks 5–6), docs-only scope (Task 4 `_in_doc_scope` + collectors), pre-push default / stage-selectable (Tasks 7, 8, 9), shared-judge refactor (Task 3), config block (Task 1), fail-open (Tasks 5 + 9), install/foreign-hook handling (Task 8), output + overflow reporting (Tasks 5, 9). All spec sections map to a task.
- **Type consistency:** `run_stale_scan(repo_root, cfg, changed_docs, *, search_fn, judge_fn)`; `search_fn(query) -> hits`; `judge_fn(section_text, candidate) -> bool|None`; hit dict keys `source`/`score`/`excerpt`; `StaleFinding(file, section, snippet, candidate_path, candidate_score)`. Used identically across Tasks 5, 6, 9.
- **No silent caps:** `max_judge_calls` overflow is surfaced in `_print_stale_result` (Task 9) and counted in `StaleScanResult.skipped_overflow` (Task 5).
- **CLI patch targets:** `cmd_hook` references `stale_scan.collect_staged` / `stale_scan.run_stale_scan` module-qualified so the Task 9 monkeypatches bind correctly.
```
