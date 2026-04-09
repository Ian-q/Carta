# Skill Installation (`carta init`) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend `carta init` to copy Carta skill markdown files from `docs/superpowers/skills/` into `~/.claude/skills/` (global) or `{project}/.claude/skills/` (project), with interactive G/P/S prompt, `--skip-skills`, idempotent skip-if-present behavior, and tests.

**Architecture:** Pure filesystem copy in `carta/install/bootstrap.py` after existing init steps (after `_create_agents_md` or immediately before final success messages—see Task 4). Source path is `project_root / "docs/superpowers/skills"`. Each `*.md` file becomes `{dest_base}/{stem}/{stem}.md`. No overwrite: if the destination file already exists, skip that skill. Missing source directory: warn on stderr, return without failing. `run_bootstrap()` gains optional `skip_skills: bool`; `cmd_init` passes `--skip-skills` from argparse.

**Tech Stack:** Python 3.10+, pathlib, shutil, pytest, unittest.mock

---

## File structure (what changes)

| Path | Role |
|------|------|
| `docs/superpowers/skills/audit-embed.md` | Skill: audit report usage (frontmatter + body) |
| `docs/superpowers/skills/carta-workflow.md` | Skill: general Carta workflows |
| `docs/superpowers/skills/carta-repair.md` | Skill: stub for future repair flow |
| `carta/install/bootstrap.py` | `_skills_source_dir`, `_prompt_skills_choice`, `_install_skills`, `run_bootstrap(..., skip_skills=...)` integration |
| `carta/cli.py` | `init` subparser with `--skip-skills`; `cmd_init` passes flag |
| `carta/install/tests/test_skills.py` | Unit tests for install helpers and idempotency |
| `carta/install/tests/test_bootstrap.py` | Remove obsolete `test_install_skills_removed`; optionally patch new skills step where needed |

---

## Self-review (spec coverage)

| Spec requirement | Task |
|------------------|------|
| Prompt G/P/S interactive | Task 3 |
| Copy all `.md` from `docs/superpowers/skills/` | Task 2, 4 |
| Conflicts: skip existing, no overwrite | Task 2 (check destination **file**) |
| Three skills: audit-embed, carta-workflow, carta-repair | Task 1 |
| `--skip-skills` | Task 5 |
| Idempotent | Task 2 + tests Task 6 |
| Non-interactive → global, no prompt | Task 4 |
| Source missing: warn, no fail | Task 2 |
| Dest permission errors: warn, continue | Task 2 |
| Tests | Tasks 6–7 |

**Placeholder scan:** No TBD/TODO in steps; obsolete test removal is explicit.

**Signature consistency:** `run_bootstrap(project_root, *, skip_skills: bool = False)` everywhere after Task 5.

---

### Task 1: Add skill markdown sources in repo

**Files:**
- Create: `docs/superpowers/skills/audit-embed.md`
- Create: `docs/superpowers/skills/carta-workflow.md`
- Create: `docs/superpowers/skills/carta-repair.md`

- [ ] **Step 1: Create directory and three files**

```bash
mkdir -p docs/superpowers/skills
```

**audit-embed.md** (minimal valid skill body; expand later if needed):

```markdown
---
name: audit-embed
description: Run and interpret Carta audit reports for embed pipeline consistency
---

# Carta audit and embed

Use this skill when the user runs `carta audit`, reviews `audit-report.json`, or needs to reconcile sidecars with Qdrant.

## When to use

- After `carta embed` or before a release, to find orphaned chunks, stale sidecars, or hash drift.
- When interpreting categories in the JSON report (see project docs).

## Commands

- `carta audit` — write structured report (default `audit-report.json` in repo root).
- `carta embed` — re-embed changed files after fixes.
```

**carta-workflow.md:**

```markdown
---
name: carta-workflow
description: General Carta workflow — scan, embed, search, and session memory
---

# Carta workflow

Guidance for `/doc-audit`, `/doc-embed`, `/doc-search`, and session memory using project config in `.carta/config.yaml`.
```

**carta-repair.md:**

```markdown
---
name: carta-repair
description: Interactive repair of audit findings (reserved for future carta repair flows)
---

# Carta repair (stub)

Reserved for future interactive repair workflows. Until then, use **audit-embed** and manual fixes from the audit report.
```

- [ ] **Step 2: Commit**

```bash
git add docs/superpowers/skills/
git commit -m "docs: add Carta Claude skill sources under docs/superpowers/skills"
```

---

### Task 2: Implement skill path helpers and `_install_skills`

**Files:**
- Modify: `carta/install/bootstrap.py`

- [ ] **Step 1: Add imports**

At the top of `bootstrap.py`, ensure `shutil` is already imported (it is). No new imports required beyond existing `shutil`, `sys`, `Path`.

- [ ] **Step 2: Add helpers and `_install_skills` (place after `_is_interactive` / `_prompt_user` block, before `run_bootstrap`)**

```python
def _skills_source_dir(project_root: Path) -> Path:
    """Directory containing Carta skill markdown files shipped with the repo checkout."""
    return project_root / "docs" / "superpowers" / "skills"


def _skills_destination_root(choice: str, project_root: Path) -> Path:
    """Root directory for Claude Code skills: global ~/.claude/skills or project .claude/skills."""
    if choice == "G":
        return Path.home() / ".claude" / "skills"
    if choice == "P":
        return project_root / ".claude" / "skills"
    raise ValueError(f"Invalid skills choice: {choice!r}")


def _prompt_skills_choice() -> str:
    """Interactive G/P/S for skill installation. Default G."""
    if not _is_interactive():
        return "G"
    try:
        print("Install Carta skills? [G]lobal/[P]roject/[S]kip [G]: ", end="", flush=True)
        line = input().strip().lower()
    except (EOFError, OSError):
        return "G"
    if not line or line in ("g", "global"):
        return "G"
    if line in ("p", "project"):
        return "P"
    if line in ("s", "skip"):
        return "S"
    return "G"


def _install_skills(choice: str, project_root: Path) -> tuple[int, int, str]:
    """Copy each docs/superpowers/skills/*.md into Claude skill layout. Idempotent per file.

    Returns:
        (copied_count, already_present_count, display_path): new copies, skips because file
        already existed, and a short path for messages (e.g. ~/.claude/skills or .claude/skills).
        display_path is "" if nothing to report (missing source / empty dir).
    """
    source_dir = _skills_source_dir(project_root)
    if not source_dir.is_dir():
        print(
            f"  Warning: skill sources not found ({source_dir}); skipping skill install.",
            file=sys.stderr,
        )
        return (0, 0, "")

    dest_root = _skills_destination_root(choice, project_root)
    md_files = sorted(source_dir.glob("*.md"))
    if not md_files:
        print(f"  Warning: no .md files in {source_dir}; skipping skill install.", file=sys.stderr)
        return (0, 0, "")

    copied = 0
    already = 0
    for src in md_files:
        stem = src.stem
        dest_dir = dest_root / stem
        dest_file = dest_dir / f"{stem}.md"
        if dest_file.is_file():
            already += 1
            continue
        try:
            dest_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest_file)
            copied += 1
        except OSError as e:
            print(f"  Warning: could not install skill {stem}: {e}", file=sys.stderr)

    if choice == "G":
        display = "~/.claude/skills"
    else:
        display = ".claude/skills"
    return (copied, already, display)
```

- [ ] **Step 3: Run pytest on bootstrap (expect no new tests yet; ensure no syntax errors)**

```bash
cd /Users/ian/dev/doc-audit-cc && python -m py_compile carta/install/bootstrap.py
```

Expected: exit code 0

- [ ] **Step 4: Commit**

```bash
git add carta/install/bootstrap.py
git commit -m "feat(bootstrap): add _install_skills and skill path helpers"
```

---

### Task 3: Wire `run_bootstrap` and remove obsolete test

**Files:**
- Modify: `carta/install/bootstrap.py` (`run_bootstrap` signature and body)
- Modify: `carta/install/tests/test_bootstrap.py` (delete `test_install_skills_removed`)

- [ ] **Step 1: Change `run_bootstrap` signature and add skills step at end of successful init**

Replace:

```python
def run_bootstrap(project_root: Path) -> None:
```

with:

```python
def run_bootstrap(project_root: Path, *, skip_skills: bool = False) -> None:
```

After `_create_agents_md(project_root, project_name)` (or after `_append_claude_md` / `_create_agents_md` block—keep order consistent with current file: after line 138 `_create_agents_md`), **before** the final `if collections_ok:` print block, insert:

```python
    if not skip_skills:
        if _is_interactive():
            sk_choice = _prompt_skills_choice()
            if sk_choice != "S":
                copied, already, display = _install_skills(sk_choice, project_root)
                if display and (copied > 0 or already > 0):
                    msg_parts = []
                    if copied:
                        msg_parts.append(f"{copied} installed")
                    if already:
                        msg_parts.append(f"{already} already present")
                    print(f"\n✓ Carta skills at {display}: {', '.join(msg_parts)}")
                    print("  (Reload Claude Code to activate)")
        else:
            copied, already, display = _install_skills("G", project_root)
            if display and (copied > 0 or already > 0):
                msg_parts = []
                if copied:
                    msg_parts.append(f"{copied} installed")
                if already:
                    msg_parts.append(f"{already} already present")
                print(f"\n✓ Carta skills at {display}: {', '.join(msg_parts)}")
                print("  (Reload Claude Code to activate)")
```

Adjust if your final success messages need to stay a single block—skills messages must not print when `count == 0` and `display == ""` (missing source).

- [ ] **Step 2: Remove obsolete test**

In `carta/install/tests/test_bootstrap.py`, **delete** the entire function `test_install_skills_removed` (lines asserting `_install_skills` must not exist).

- [ ] **Step 3: Run full install tests**

```bash
cd /Users/ian/dev/doc-audit-cc && pytest carta/install/tests/test_bootstrap.py -v
```

Expected: all tests pass (except any pre-existing skipped test).

- [ ] **Step 4: Commit**

```bash
git add carta/install/bootstrap.py carta/install/tests/test_bootstrap.py
git commit -m "feat(bootstrap): run skill install during init; drop obsolete anti-test"
```

---

### Task 4: CLI `--skip-skills` for `carta init`

**Files:**
- Modify: `carta/cli.py`

- [ ] **Step 1: Replace bare init subparser with one that has a flag**

Find:

```python
    sub.add_parser("init")
```

Replace with:

```python
    init_p = sub.add_parser("init", help="Initialize Carta in the current project")
    init_p.add_argument(
        "--skip-skills",
        action="store_true",
        help="Do not install Carta skills to ~/.claude/skills or .claude/skills",
    )
```

- [ ] **Step 2: Pass flag into `run_bootstrap`**

Replace `cmd_init`:

```python
def cmd_init(args):
    _check_path_conflict()
    from carta.install.bootstrap import run_bootstrap
    run_bootstrap(Path.cwd(), skip_skills=getattr(args, "skip_skills", False))
    _notify_if_update()
```

- [ ] **Step 3: Manual smoke (optional)**

```bash
cd /Users/ian/dev/doc-audit-cc && python -m carta init --help
```

Expected: `--skip-skills` listed under `init`.

- [ ] **Step 4: Commit**

```bash
git add carta/cli.py
git commit -m "feat(cli): add carta init --skip-skills"
```

---

### Task 5: Unit tests — `test_skills.py`

**Files:**
- Create: `carta/install/tests/test_skills.py`

- [ ] **Step 1: Add tests**

```python
"""Tests for Carta Claude skill installation during bootstrap."""
from pathlib import Path
from unittest.mock import patch

import pytest

from carta.install.bootstrap import (
    _install_skills,
    _skills_destination_root,
    _skills_source_dir,
)


def test_skills_source_dir_layout(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    d = _skills_source_dir(root)
    assert d == root / "docs" / "superpowers" / "skills"


def test_skills_destination_global():
    p = _skills_destination_root("G", Path("/tmp/fake"))
    assert p.parts[-3:] == (".claude", "skills")


def test_skills_destination_project(tmp_path):
    p = _skills_destination_root("P", tmp_path)
    assert p == tmp_path / ".claude" / "skills"


def test_install_skills_copies_to_global(tmp_path, monkeypatch):
    """Copies each *.md into ~/.claude/skills/{stem}/{stem}.md when choice is G."""
    home = tmp_path / "home"
    home.mkdir()
    project = tmp_path / "proj"
    src = project / "docs" / "superpowers" / "skills"
    src.mkdir(parents=True)
    (src / "audit-embed.md").write_text("---\nname: audit-embed\n---\nbody\n")

    monkeypatch.setattr("carta.install.bootstrap.Path.home", lambda: home)

    copied, already, display = _install_skills("G", project)
    assert display == "~/.claude/skills"
    dest = home / ".claude" / "skills" / "audit-embed" / "audit-embed.md"
    assert dest.is_file()
    assert "body" in dest.read_text()
    assert copied == 1 and already == 0


def test_install_skills_project_scope(tmp_path):
    project = tmp_path / "proj"
    src = project / "docs" / "superpowers" / "skills"
    src.mkdir(parents=True)
    (src / "carta-workflow.md").write_text("---\nname: carta-workflow\n---\n")

    copied, already, display = _install_skills("P", project)
    assert display == ".claude/skills"
    dest = project / ".claude" / "skills" / "carta-workflow" / "carta-workflow.md"
    assert dest.is_file()
    assert copied == 1 and already == 0


def test_install_skills_idempotent_skip_existing(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    project = tmp_path / "proj"
    src = project / "docs" / "superpowers" / "skills"
    src.mkdir(parents=True)
    (src / "audit-embed.md").write_text("new")
    dest_dir = home / ".claude" / "skills" / "audit-embed"
    dest_dir.mkdir(parents=True)
    existing = dest_dir / "audit-embed.md"
    existing.write_text("old")

    monkeypatch.setattr("carta.install.bootstrap.Path.home", lambda: home)

    copied, already, _ = _install_skills("G", project)
    assert existing.read_text() == "old"
    assert copied == 0 and already == 1


def test_install_skills_missing_source_warns(tmp_path, capsys):
    project = tmp_path / "proj"
    project.mkdir()
    copied, already, display = _install_skills("G", project)
    assert copied == 0 and already == 0
    assert display == ""
    err = capsys.readouterr().err
    assert "skill sources not found" in err


def test_install_skills_empty_dir_warns(tmp_path, capsys):
    project = tmp_path / "proj"
    src = project / "docs" / "superpowers" / "skills"
    src.mkdir(parents=True)
    copied, already, display = _install_skills("G", project)
    assert copied == 0 and already == 0
    assert display == ""
    assert "no .md files" in capsys.readouterr().err
```

- [ ] **Step 2: Run tests**

```bash
cd /Users/ian/dev/doc-audit-cc && pytest carta/install/tests/test_skills.py -v
```

Expected: all pass.

- [ ] **Step 3: Commit**

```bash
git add carta/install/tests/test_skills.py
git commit -m "test(install): cover skill copy, project scope, idempotency, missing source"
```

---

### Task 6: Full suite and bootstrap regression

**Files:**
- None (verification)

- [ ] **Step 1: Run full pytest**

```bash
cd /Users/ian/dev/doc-audit-cc && pytest
```

Expected: all pass (excluding known skipped tests).

- [ ] **Step 2: Commit only if fixes needed**

If any bootstrap test fails because `run_bootstrap` now prints skill lines or touches filesystem, patch `_install_skills` in those tests or add `skip_skills=True` to `run_bootstrap` calls in tests—prefer **`run_bootstrap(tmp_path, skip_skills=True)`** in `test_bootstrap.py` where full bootstrap is mocked partially, to avoid accidental global writes when `docs/superpowers/skills` exists in the real repo cwd… 

**Note:** Existing tests use `tmp_path` as `project_root` without `docs/superpowers/skills`; `_install_skills` will warn and return early—no writes to real `~`. So tests should still pass. If the repo **does** copy skills into tmp_path in a test that creates the full tree, verify behavior.

- [ ] **Step 3: Final commit if test bootstrap updates**

```bash
git add carta/install/tests/test_bootstrap.py
git commit -m "test(bootstrap): pass skip_skills where needed for isolation"
```

(Only if changes were required.)

---

### Task 7: Documentation touch (optional, one line)

**Files:**
- Modify: `docs/superpowers/specs/2026-04-07-skill-installation-design.md` — set `status: implemented` when done (optional)

- [ ] **Step 1: After verification, update spec frontmatter status**

```yaml
status: implemented
```

- [ ] **Step 2: Commit**

```bash
git add docs/superpowers/specs/2026-04-07-skill-installation-design.md
git commit -m "docs: mark skill installation spec implemented"
```

---

## Execution handoff

Plan complete and saved to `docs/superpowers/plans/2026-04-07-skill-installation.md`.

**Two execution options:**

1. **Subagent-Driven (recommended)** — Dispatch a fresh subagent per task, review between tasks, fast iteration. **REQUIRED SUB-SKILL:** superpowers:subagent-driven-development.

2. **Inline execution** — Execute tasks in this session using executing-plans with batch checkpoints. **REQUIRED SUB-SKILL:** superpowers:executing-plans.

**Which approach do you want?**
