# CLAUDE.md /doc-search guidance block — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Expand `carta init`'s CLAUDE.md injection from a one-line comment into an on-by-default `## Carta Knowledge Graph` guidance block that tells any agent the knowledge graph exists, when to `/doc-search`, and how — creating CLAUDE.md if absent, appending if present, idempotently.

**Architecture:** A pure `_carta_claude_block(project_name)` builds the marker-wrapped block; the reworked `_append_claude_md` writes it (create-if-absent / append-if-present) guarded against double-injection by the start marker or the legacy `"Carta is active"` string.

**Tech Stack:** Python 3.10+ (stdlib `pathlib`), pytest.

**Spec:** `docs/superpowers/specs/2026-06-16-claude-md-doc-search-guidance-design.md`

---

## File structure

| File | Responsibility |
|------|----------------|
| `carta/install/bootstrap.py` (modify) | Add `_carta_claude_block`; rework `_append_claude_md` (create/append/idempotent) |
| `carta/install/tests/test_bootstrap.py` (modify) | New unit tests for the block + the three write paths; keep the existing append test green |
| `docs/superpowers/specs/2026-06-16-claude-md-doc-search-guidance-design.md` (modify) | `status: draft` → `status: shipped` |

**Run the whole suite with:** `python -m pytest carta/ -q`

---

## Task 1: `_carta_claude_block` + reworked `_append_claude_md`

The current `_append_claude_md` (in `carta/install/bootstrap.py`) appends a single
`<!-- Carta is active. Collections: … -->` comment, only if CLAUDE.md exists. Replace it
with a block-writer that creates-if-absent, appends-if-present, and is idempotent.

**Files:**
- Modify: `carta/install/bootstrap.py` (`_append_claude_md`, ~lines 424-431)
- Test: `carta/install/tests/test_bootstrap.py` (append new tests)

- [ ] **Step 1: Write the failing tests**

Append to `carta/install/tests/test_bootstrap.py`:

```python
def test_carta_claude_block_content():
    from carta.install.bootstrap import _carta_claude_block
    block = _carta_claude_block("acme")
    assert "<!-- carta:guidance:start -->" in block
    assert "<!-- carta:guidance:end -->" in block
    assert "## Carta Knowledge Graph" in block
    assert "Search the docs before you assume." in block
    assert '`/doc-search "<name> responsibilities"`' in block   # trigger table row
    assert "/doc-audit" in block and "/doc-embed" in block       # maintenance line
    # collections comment interpolates the project name and stays inside the block
    assert "Carta is active. Collections: acme_doc, acme_session, acme_notes" in block


def test_append_claude_md_appends_to_existing(tmp_path):
    from carta.install.bootstrap import _append_claude_md
    (tmp_path / "CLAUDE.md").write_text("# My Project\n\nExisting guidance.\n")
    _append_claude_md(tmp_path, "acme")
    content = (tmp_path / "CLAUDE.md").read_text()
    assert "Existing guidance." in content                       # prior content preserved
    assert "## Carta Knowledge Graph" in content
    assert content.count("<!-- carta:guidance:start -->") == 1


def test_append_claude_md_creates_when_absent(tmp_path):
    from carta.install.bootstrap import _append_claude_md
    assert not (tmp_path / "CLAUDE.md").exists()
    _append_claude_md(tmp_path, "acme")
    content = (tmp_path / "CLAUDE.md").read_text()
    assert content.startswith("# acme")
    assert "## Carta Knowledge Graph" in content
    assert "<!-- carta:guidance:start -->" in content


def test_append_claude_md_idempotent_on_marker(tmp_path):
    from carta.install.bootstrap import _append_claude_md
    (tmp_path / "CLAUDE.md").write_text("# My Project\n")
    _append_claude_md(tmp_path, "acme")
    _append_claude_md(tmp_path, "acme")                          # second call is a no-op
    content = (tmp_path / "CLAUDE.md").read_text()
    assert content.count("<!-- carta:guidance:start -->") == 1


def test_append_claude_md_skips_legacy_oneliner(tmp_path):
    from carta.install.bootstrap import _append_claude_md
    # A repo bootstrapped by an older Carta has only the legacy comment.
    (tmp_path / "CLAUDE.md").write_text(
        "# My Project\n\n<!-- Carta is active. Collections: acme_doc, acme_session, acme_notes -->\n"
    )
    _append_claude_md(tmp_path, "acme")
    content = (tmp_path / "CLAUDE.md").read_text()
    assert "<!-- carta:guidance:start -->" not in content        # not upgraded / not doubled
    assert content.count("Carta is active") == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest carta/install/tests/test_bootstrap.py -k "carta_claude_block or append_claude_md" -v`
Expected: FAIL — `ImportError: cannot import name '_carta_claude_block'` and/or the
append/create/idempotent assertions fail against the current one-line implementation.

- [ ] **Step 3: Implement the block builder + reworked writer**

In `carta/install/bootstrap.py`, replace the current `_append_claude_md` function
(the 8-line version that appends only the `note` comment) with the following — the new
`_carta_claude_block` helper, two module-local sentinel constants, and the reworked
`_append_claude_md`:

```python
_CARTA_CLAUDE_SENTINEL = "<!-- carta:guidance:start -->"
_CARTA_CLAUDE_LEGACY = "Carta is active"


def _carta_claude_block(project_name: str) -> str:
    """The Carta /doc-search guidance block injected into a project's CLAUDE.md.

    Wrapped in carta:guidance markers for idempotent (re)writes; the trailing
    'Carta is active' comment is both a diagnostic breadcrumb and the legacy
    idempotency string."""
    return (
        "<!-- carta:guidance:start -->\n"
        "## Carta Knowledge Graph\n"
        "\n"
        "**Search the docs before you assume.** This project's specs (components, protocols,\n"
        "config keys, design decisions) may have changed since the code — or your training — was\n"
        "written. Carta provides semantic search over them via `/doc-search`.\n"
        "\n"
        "**Run `/doc-search` whenever a prompt names one of these — query it like so:**\n"
        "\n"
        "| Prompt mentions… | Search |\n"
        "|---|---|\n"
        "| A component or module | `/doc-search \"<name> responsibilities\"` |\n"
        "| An API, protocol, or data format | `/doc-search \"<name> spec\"` |\n"
        "| A config key or flag | `/doc-search \"<key> configuration\"` |\n"
        "| A file/path or a design decision | `/doc-search \"<topic> design\"` |\n"
        "\n"
        "Maintenance (only when asked, or after editing docs): `/doc-audit` (flag\n"
        "stale/contradictory docs), `/doc-embed` (re-index).\n"
        "\n"
        f"<!-- Carta is active. Collections: {project_name}_doc, "
        f"{project_name}_session, {project_name}_notes -->\n"
        "<!-- carta:guidance:end -->\n"
    )


def _append_claude_md(project_root: Path, project_name: str) -> None:
    """Inject the Carta guidance block into CLAUDE.md (create if absent, append if
    present). Idempotent: skips if the block marker or the legacy 'Carta is active'
    string is already present."""
    claude_md = project_root / "CLAUDE.md"
    block = _carta_claude_block(project_name)
    if claude_md.exists():
        text = claude_md.read_text(encoding="utf-8")
        if _CARTA_CLAUDE_SENTINEL in text or _CARTA_CLAUDE_LEGACY in text:
            return
        with open(claude_md, "a", encoding="utf-8") as f:
            f.write("\n" + block)
    else:
        claude_md.write_text(f"# {project_name}\n\n{block}", encoding="utf-8")
```

> Note: the block contains non-ASCII (`…`, `—`), so all reads/writes use
> `encoding="utf-8"` to stay correct on non-UTF-8-default platforms (e.g. Windows).

- [ ] **Step 4: Run tests to verify they pass (new + the existing append test)**

Run: `python -m pytest carta/install/tests/test_bootstrap.py -v`
Expected: PASS — the 5 new tests pass AND the existing `test_bootstrap_appends_claude_md`
stays green (it asserts `"Carta is active" in content`, still true because the collections
comment lives inside the block).

- [ ] **Step 5: Commit**

```bash
git add carta/install/bootstrap.py carta/install/tests/test_bootstrap.py
git commit -m "feat(init): full /doc-search guidance block in CLAUDE.md bootstrap (#10 slice 4)"
```

End the commit message with a blank line then:
`Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`

---

## Task 2: Mark spec shipped + full-suite verification + smoke

**Files:**
- Modify: `docs/superpowers/specs/2026-06-16-claude-md-doc-search-guidance-design.md`

- [ ] **Step 1: Mark the spec shipped**

In `docs/superpowers/specs/2026-06-16-claude-md-doc-search-guidance-design.md`, change the
frontmatter `status: draft` to `status: shipped`.

- [ ] **Step 2: Run the FULL suite**

Run: `python -m pytest carta/ -q`
Expected: PASS — slice-1/2 baseline was 977 passed / 1 skipped; expect 977 + the 5 new
Task 1 tests, 0 failures, 1 skip.

- [ ] **Step 3: Manual smoke (no services required)**

Run (exercises both the create and append paths via the real functions):
```bash
cd /tmp && rm -rf cartamdtest && mkdir -p cartamdtest/empty cartamdtest/existing
printf '# Existing\n\nSome notes.\n' > cartamdtest/existing/CLAUDE.md
WT=/Users/ian/dev/doc-audit-cc/.claude/worktrees/scanner-noise-fixes
PYTHONPATH="$WT" python -c "
from pathlib import Path
from carta.install.bootstrap import _append_claude_md
_append_claude_md(Path('/tmp/cartamdtest/empty'), 'demo')
_append_claude_md(Path('/tmp/cartamdtest/existing'), 'demo')
_append_claude_md(Path('/tmp/cartamdtest/existing'), 'demo')  # idempotent
for p in ['empty/CLAUDE.md', 'existing/CLAUDE.md']:
    t = Path('/tmp/cartamdtest', p).read_text()
    print(p, '| starts#', t.startswith('# '), '| blocks', t.count('carta:guidance:start'), '| has table', '/doc-search \"<name> spec\"' in t)
"
rm -rf /tmp/cartamdtest
```
Expected: `empty/CLAUDE.md` starts with `#` and has exactly 1 block; `existing/CLAUDE.md`
has exactly 1 block (idempotent across the two calls) and preserves "Some notes."

- [ ] **Step 4: Commit**

```bash
git add docs/superpowers/specs/2026-06-16-claude-md-doc-search-guidance-design.md
git commit -m "docs: mark CLAUDE.md guidance spec shipped (#10 slice 4)"
```

End the commit message with a blank line then:
`Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`

---

## Self-review notes (for the implementer)

- **Spec coverage:** block content + markers (Task 1 `_carta_claude_block`); append/create/idempotent-marker/idempotent-legacy (Task 1 tests + writer); on-by-default & no config gate (no config touched); collections comment interpolation (block); spec-shipped + verify (Task 2). All spec sections map to a task.
- **No regression:** the existing `test_bootstrap_appends_claude_md` stays green unchanged (asserts `"Carta is active"`, still present). Do NOT delete or weaken it.
- **Encoding:** all CLAUDE.md reads/writes use `encoding="utf-8"` because the block has `…`/`—`.
- **Scope:** only `_append_claude_md`/`_carta_claude_block` change. Do not touch `_create_agents_md`, the `anchor_doc` config, or this repo's own CLAUDE.md.
