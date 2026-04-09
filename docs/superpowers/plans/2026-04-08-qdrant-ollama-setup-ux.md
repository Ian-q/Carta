# Qdrant & Ollama Setup UX Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Improve Carta's Qdrant/Ollama setup UX — richer prerequisite docs, `carta doctor` always prints fix commands inline, adds judge model check, and updates the default judge model to `qwen3.5:0.8b`.

**Architecture:** Four targeted changes across two layers: (1) `carta/config.py` default update, (2) `carta/install/preflight.py` behaviour fixes, (3) `docs/install.md` new Prerequisites section, (4) `README.md` trimmed prereq block. All changes are independent and can be committed individually.

**Tech Stack:** Python 3.10+, pytest, unittest.mock — no new dependencies.

---

## File Map

| File | Change |
|------|--------|
| `carta/config.py` | Update `proactive_recall.ollama_model` default to `"qwen3.5:0.8b"` |
| `carta/install/preflight.py` | (1) Always show suggestions for fail/warn; (2) Add `qwen3.5:0.8b` model check; (3) Update Qdrant suggestion to include `-v` volume flag; (4) Add actionable fix footer to summary |
| `carta/install/tests/test_preflight.py` | Tests for all four preflight changes |
| `docs/install.md` | New Prerequisites section (Qdrant + Ollama + models + verify) |
| `README.md` | Replace minimal prereq lines with richer block + pointer to install.md |

---

## Task 1: Update default judge model in config.py

**Files:**
- Modify: `carta/config.py` (line 62 — `proactive_recall.ollama_model`)
- Test: `carta/tests/test_config.py`

- [ ] **Step 1: Write the failing test**

Add to `carta/tests/test_config.py`:

```python
from carta.config import DEFAULTS

def test_judge_model_default_is_qwen35():
    assert DEFAULTS["proactive_recall"]["ollama_model"] == "qwen3.5:0.8b"
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest carta/tests/test_config.py::test_judge_model_default_is_qwen35 -v
```

Expected: `FAILED — AssertionError: assert 'qwen2.5:0.5b' == 'qwen3.5:0.8b'`

- [ ] **Step 3: Update the default in config.py**

In `carta/config.py`, find line 62 (inside `"proactive_recall"` dict):

```python
# Before
"ollama_model": "qwen2.5:0.5b",

# After
"ollama_model": "qwen3.5:0.8b",
```

- [ ] **Step 4: Run test to verify it passes**

```bash
pytest carta/tests/test_config.py::test_judge_model_default_is_qwen35 -v
```

Expected: `PASSED`

- [ ] **Step 5: Commit**

```bash
git add carta/config.py carta/tests/test_config.py
git commit -m "feat(config): update default judge model to qwen3.5:0.8b"
```

---

## Task 2: Update Qdrant suggestion to include persistence volume flag

**Files:**
- Modify: `carta/install/preflight.py` (`_check_qdrant_running`)
- Test: `carta/install/tests/test_preflight.py`

- [ ] **Step 1: Write the failing test**

Add to `carta/install/tests/test_preflight.py`:

```python
import pytest
from unittest.mock import patch
import requests
from carta.install.preflight import PreflightChecker


class TestQdrantSuggestion:
    def test_suggestion_includes_volume_flag(self):
        checker = PreflightChecker(interactive=False)
        with patch("requests.get", side_effect=requests.ConnectionError()):
            result = checker._check_qdrant_running()
        assert result.status == "fail"
        assert "-v ~/.carta/qdrant_storage:/qdrant/storage" in result.suggestion

    def test_suggestion_includes_detached_flag(self):
        checker = PreflightChecker(interactive=False)
        with patch("requests.get", side_effect=requests.ConnectionError()):
            result = checker._check_qdrant_running()
        assert "-d" in result.suggestion
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest carta/install/tests/test_preflight.py::TestQdrantSuggestion -v
```

Expected: `FAILED — AssertionError` (volume flag not present)

- [ ] **Step 3: Update both suggestion strings in `_check_qdrant_running`**

In `carta/install/preflight.py`, `_check_qdrant_running` has the same suggestion in two branches (the `else` of the status check and the `except requests.ConnectionError` handler). Update both to:

```python
suggestion="Start with: docker run -d -p 6333:6333 -v ~/.carta/qdrant_storage:/qdrant/storage --name qdrant qdrant/qdrant",
```

There are three places (the `else` after status check, `except requests.ConnectionError`, and `except Exception`). All three get the same updated string.

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest carta/install/tests/test_preflight.py::TestQdrantSuggestion -v
```

Expected: `PASSED`

- [ ] **Step 5: Commit**

```bash
git add carta/install/preflight.py carta/install/tests/test_preflight.py
git commit -m "fix(preflight): add persistence volume to Qdrant start suggestion"
```

---

## Task 3: Always show suggestions for fail/warn in `_print_check`

**Files:**
- Modify: `carta/install/preflight.py` (`_print_check` in `PreflightResult`)
- Test: `carta/install/tests/test_preflight.py`

- [ ] **Step 1: Write the failing test**

Add to `carta/install/tests/test_preflight.py`:

```python
import io
from unittest.mock import patch
from carta.install.preflight import PreflightCheck, PreflightResult


class TestPrintCheckSuggestions:
    def _make_result(self, status: str) -> PreflightResult:
        check = PreflightCheck(
            name="test_check",
            status=status,
            message="Something went wrong",
            category="infrastructure",
            suggestion="Run: fix-it --now",
        )
        return PreflightResult(checks=[check])

    def test_suggestion_shown_for_fail_without_verbose(self, capsys):
        result = self._make_result("fail")
        result.print_report(verbose=False)
        captured = capsys.readouterr()
        assert "Run: fix-it --now" in captured.out

    def test_suggestion_shown_for_warn_without_verbose(self, capsys):
        result = self._make_result("warn")
        result.print_report(verbose=False)
        captured = capsys.readouterr()
        assert "Run: fix-it --now" in captured.out

    def test_suggestion_not_shown_for_pass(self, capsys):
        check = PreflightCheck(
            name="ok_check",
            status="pass",
            message="All good",
            category="infrastructure",
            suggestion="You shouldn't see this",
        )
        result = PreflightResult(checks=[check])
        result.print_report(verbose=False)
        captured = capsys.readouterr()
        assert "You shouldn't see this" not in captured.out
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest carta/install/tests/test_preflight.py::TestPrintCheckSuggestions -v
```

Expected: `FAILED — suggestion not found in output` (verbose gate blocks it)

- [ ] **Step 3: Update `_print_check` in `PreflightResult`**

In `carta/install/preflight.py`, replace the `_print_check` method:

```python
def _print_check(self, check: PreflightCheck, verbose: bool) -> None:
    icons = {
        "pass": "✅",
        "fail": "❌",
        "warn": "⚠️ ",
        "skip": "⏭️ ",
    }
    icon = icons.get(check.status, "❓")
    print(f"  {icon} {check.name}: {check.message}")

    if check.suggestion and check.status in ("fail", "warn"):
        print(f"     → {check.suggestion}")

    if verbose and check.details:
        for key, value in check.details.items():
            print(f"     • {key}: {value}")
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest carta/install/tests/test_preflight.py::TestPrintCheckSuggestions -v
```

Expected: `PASSED`

- [ ] **Step 5: Commit**

```bash
git add carta/install/preflight.py carta/install/tests/test_preflight.py
git commit -m "fix(preflight): always show fix suggestion for fail/warn checks"
```

---

## Task 4: Add judge model check to Phase 3

**Files:**
- Modify: `carta/install/preflight.py` (`_phase3_models` in `PreflightChecker`)
- Test: `carta/install/tests/test_preflight.py`

- [ ] **Step 1: Write the failing test**

Add to `carta/install/tests/test_preflight.py`:

```python
class TestJudgeModelCheck:
    def test_judge_model_checked_when_ollama_running(self):
        checker = PreflightChecker(interactive=False)
        # Inject a passing ollama_running check so phase 3 runs model checks
        from carta.install.preflight import PreflightCheck
        checker.checks = [
            PreflightCheck(
                name="ollama_running",
                status="pass",
                message="Ollama server running",
                category="infrastructure",
            )
        ]
        with patch("subprocess.run") as mock_run:
            # ollama list returns output that does NOT include the judge model
            mock_run.return_value = type("R", (), {
                "returncode": 0,
                "stdout": "nomic-embed-text:latest\nllava:latest\n",
                "stderr": "",
            })()
            checker._phase3_models()

        check_names = [c.name for c in checker.checks]
        assert "ollama_model_qwen3.5:0.8b" in check_names

    def test_judge_model_warn_when_not_pulled(self):
        checker = PreflightChecker(interactive=False)
        from carta.install.preflight import PreflightCheck
        checker.checks = [
            PreflightCheck(
                name="ollama_running",
                status="pass",
                message="Ollama server running",
                category="infrastructure",
            )
        ]
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = type("R", (), {
                "returncode": 0,
                "stdout": "nomic-embed-text:latest\n",
                "stderr": "",
            })()
            checker._phase3_models()

        judge_check = next(
            c for c in checker.checks if c.name == "ollama_model_qwen3.5:0.8b"
        )
        assert judge_check.status == "warn"
        assert "qwen3.5:0.8b" in judge_check.suggestion
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest carta/install/tests/test_preflight.py::TestJudgeModelCheck -v
```

Expected: `FAILED — StopIteration` (no judge model check exists yet)

- [ ] **Step 3: Add judge model check to `_phase3_models`**

In `carta/install/preflight.py`, update `_phase3_models`:

```python
def _phase3_models(self) -> None:
    """Check Ollama models, ColPali cache."""
    ollama_check = next(
        (c for c in self.checks if c.name == "ollama_running"), None
    )
    if ollama_check and ollama_check.status == "pass":
        self.checks.append(self._check_ollama_model("nomic-embed-text"))
        self.checks.append(self._check_ollama_model("llava"))
        self.checks.append(self._check_ollama_model("qwen3.5:0.8b"))
    else:
        self.checks.append(
            PreflightCheck(
                name="ollama_models",
                status="skip",
                message="Skipped (Ollama not running)",
                category="models",
            )
        )

    self.checks.append(self._check_colpali_available())
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest carta/install/tests/test_preflight.py::TestJudgeModelCheck -v
```

Expected: `PASSED`

- [ ] **Step 5: Commit**

```bash
git add carta/install/preflight.py carta/install/tests/test_preflight.py
git commit -m "feat(preflight): add qwen3.5:0.8b judge model check to phase 3"
```

---

## Task 5: Add actionable fix footer to `_print_summary`

**Files:**
- Modify: `carta/install/preflight.py` (`_print_summary` in `PreflightResult`)
- Test: `carta/install/tests/test_preflight.py`

- [ ] **Step 1: Write the failing test**

Add to `carta/install/tests/test_preflight.py`:

```python
class TestFixFooter:
    def _make_failing_result(self) -> PreflightResult:
        checks = [
            PreflightCheck(
                name="qdrant_running",
                status="fail",
                message="Qdrant not running at http://localhost:6333",
                category="infrastructure",
                fixable=True,
                suggestion="docker run -d -p 6333:6333 -v ~/.carta/qdrant_storage:/qdrant/storage --name qdrant qdrant/qdrant",
            ),
            PreflightCheck(
                name="ollama_model_qwen3.5:0.8b",
                status="warn",
                message="Model 'qwen3.5:0.8b' not pulled",
                category="models",
                suggestion="ollama pull qwen3.5:0.8b",
            ),
        ]
        return PreflightResult(checks=checks)

    def test_fix_footer_shown_when_failures_exist(self, capsys):
        result = self._make_failing_result()
        result.print_report()
        captured = capsys.readouterr()
        assert "To fix" in captured.out
        assert "docker run" in captured.out
        assert "ollama pull qwen3.5:0.8b" in captured.out

    def test_fix_footer_not_shown_when_all_pass(self, capsys):
        check = PreflightCheck(
            name="qdrant_running",
            status="pass",
            message="Qdrant ready",
            category="infrastructure",
        )
        result = PreflightResult(checks=[check])
        result.print_report()
        captured = capsys.readouterr()
        assert "To fix" not in captured.out

    def test_fix_footer_lists_all_actionable_checks(self, capsys):
        result = self._make_failing_result()
        result.print_report()
        captured = capsys.readouterr()
        assert "Qdrant not running" in captured.out
        assert "Model 'qwen3.5:0.8b' not pulled" in captured.out
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest carta/install/tests/test_preflight.py::TestFixFooter -v
```

Expected: `FAILED — "To fix" not found in output`

- [ ] **Step 3: Update `_print_summary` in `PreflightResult`**

In `carta/install/preflight.py`, replace `_print_summary`:

```python
def _print_summary(self) -> None:
    total = len(self.checks)
    passed = len(self.passed)
    failed = len(self.failed)
    warnings = len(self.warnings)
    fixable = len(self.fixable_failures)

    print(f"\n📊 Summary: {passed}/{total} passed", end="")
    if failed > 0:
        print(f", {failed} failed ({fixable} fixable)", end="")
    if warnings > 0:
        print(f", {warnings} warnings", end="")
    print()

    if self.can_proceed():
        print("\n✅ All checks passed. Ready to initialize Carta.")
    elif self.fixable_failures and not self.critical_failures:
        print(f"\n🔧 {fixable} issue(s) can be auto-fixed.")
    else:
        critical = len(self.critical_failures)
        print(f"\n🔴 {critical} critical issue(s) must be resolved manually.")

    actionable = [
        c for c in self.checks
        if c.status in ("fail", "warn") and c.suggestion
    ]
    if actionable:
        print(f"\n{'━' * 55}")
        count = len(actionable)
        print(f"\n🔧 To fix ({count} issue{'s' if count > 1 else ''}):\n")
        for i, check in enumerate(actionable, 1):
            print(f"  {i}. {check.message}")
            print(f"     → {check.suggestion}\n")
```

- [ ] **Step 4: Run all preflight tests to verify**

```bash
pytest carta/install/tests/test_preflight.py -v
```

Expected: all tests `PASSED`

- [ ] **Step 5: Commit**

```bash
git add carta/install/preflight.py carta/install/tests/test_preflight.py
git commit -m "feat(preflight): add actionable fix footer to carta doctor summary"
```

---

## Task 6: Update `docs/install.md` with full Prerequisites section

**Files:**
- Modify: `docs/install.md` (replace lines 8–11)

No automated test — verify by reading the rendered output.

- [ ] **Step 1: Replace the existing Prerequisites section**

In `docs/install.md`, replace:

```markdown
## Prerequisites

- **Python 3.10+**
- **pip** or **pipx** (recommended on macOS to avoid PEP 668 "externally managed environment" errors)
- Optional: **Docker** (Qdrant), **Ollama** (embeddings/search) — only if you use embed/search
```

With:

```markdown
## Prerequisites

- **Python 3.10+**
- **pip** or **pipx** (recommended on macOS to avoid PEP 668 "externally managed environment" errors)

If **`pipx` is not installed**: macOS `brew install pipx` then `pipx ensurepath`, or `python3 -m pip install --user pipx` and add pipx's bin dir to `PATH`.

### Qdrant (vector store)

Carta stores embeddings in a local [Qdrant](https://qdrant.tech) vector database. Run it via Docker:

```bash
docker run -d \
  -p 6333:6333 \
  -v ~/.carta/qdrant_storage:/qdrant/storage \
  --name qdrant \
  qdrant/qdrant
```

- `-d` runs the container detached so it starts automatically with Docker.
- `-v ~/.carta/qdrant_storage:/qdrant/storage` persists your collections across container restarts and upgrades. Without this flag, all embedded documents are lost when the container stops.
- For TLS, resource limits, or upgrades, see the [Qdrant quickstart](https://qdrant.tech/documentation/quickstart/).

### Ollama (embeddings, vision, hook judge)

Install Ollama from [ollama.ai/download](https://ollama.ai/download), then pull the required models:

```bash
# Required — text embeddings (used by carta embed and carta search)
ollama pull nomic-embed-text

# Required — hook relevance judge
# Filters retrieved context before it reaches your prompt.
# Default is qwen3.5:0.8b (0.8B params, low latency).
# Set proactive_recall.ollama_model in .carta/config.yaml to swap in a larger model.
ollama pull qwen3.5:0.8b

# Optional — visual embedding (only needed for carta embed --visual)
ollama pull llava
```

### Verify

Once Qdrant and Ollama are running, confirm everything is detected:

```bash
carta doctor
```

All Phase 2 (Infrastructure) and Phase 3 (Models) checks should pass before running `carta init`.
```

- [ ] **Step 2: Remove the duplicate pipx paragraph** that already exists below the Prerequisites section (lines 13–13 in original), since it's now embedded in the section above. Check for duplication and remove if present.

- [ ] **Step 3: Commit**

```bash
git add docs/install.md
git commit -m "docs(install): add full Qdrant and Ollama prerequisites section"
```

---

## Task 7: Update `README.md` prerequisite block

**Files:**
- Modify: `README.md` (lines 106–111)

- [ ] **Step 1: Replace the existing Setup prerequisites block**

In `README.md`, replace:

```markdown
**Prerequisites:**

- [Qdrant](https://qdrant.tech/documentation/quick-start/) running locally (Docker: `docker run -p 6333:6333 qdrant/qdrant`)
- [Ollama](https://ollama.ai) with `nomic-embed-text` pulled: `ollama pull nomic-embed-text`

Both are optional if you only want the structural audit and semantic contradiction detection (no embedding, no search). Set `modules.doc_embed: false` (and optionally `modules.doc_search: false`) in `.carta/config.yaml`.
```

With:

```markdown
**Prerequisites:**

```bash
# 1. Qdrant — run with persistence so collections survive restarts
docker run -d -p 6333:6333 -v ~/.carta/qdrant_storage:/qdrant/storage --name qdrant qdrant/qdrant

# 2. Ollama — install from ollama.ai, then pull required models
ollama pull nomic-embed-text   # text embeddings
ollama pull qwen3.5:0.8b       # hook judge (swap for larger model if preferred)
ollama pull llava               # optional: visual embedding only
```

Both services are optional if you only want structural audit without embedding or search. See **[docs/install.md](docs/install.md)** for the full setup walkthrough and `carta doctor` to verify your environment.
```

- [ ] **Step 2: Run full test suite to confirm nothing broken**

```bash
pytest carta/ -v --tb=short 2>&1 | tail -20
```

Expected: all existing tests pass.

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "docs(readme): expand prerequisites with Qdrant/Ollama setup commands"
```

---

## Self-Review

**Spec coverage:**
- ✅ Qdrant Docker command with `-v` flag — Task 2 + Task 6 + Task 7
- ✅ Ollama model pulls with explanations — Task 6 + Task 7
- ✅ Judge model updated to `qwen3.5:0.8b` — Task 1
- ✅ Note about swapping judge model — Task 6 (docs)
- ✅ carta doctor always-on suggestions — Task 3
- ✅ Judge model check in Phase 3 — Task 4
- ✅ Actionable fix footer — Task 5
- ✅ `carta init` inherits improvements automatically (no change needed — it calls preflight)

**Placeholder scan:** None found. All code steps are complete.

**Type consistency:** `PreflightCheck`, `PreflightResult`, `PreflightChecker` — used consistently across all tasks matching the existing class definitions.
