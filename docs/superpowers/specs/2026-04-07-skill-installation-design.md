---
title: Skill Installation for Carta Init
date: 2026-04-07
status: implemented
---

# Skill Installation Design

## Overview

Extend `carta init` to install Carta skills to Claude Code, giving users structured guidance on when/how to use Carta audit, search, and embedding workflows.

Skills are stored in the repo (`docs/superpowers/skills/`) and installed to either global scope (`~/.claude/skills/`) or project scope (`.claude/skills/` at the repo root) at user choice.

## Requirements

- Prompt during `carta init`: "Install Carta skills? [G]lobal/[P]roject/[S]kip"
- Copy all `.md` files from `docs/superpowers/skills/*` to destination
- Handle conflicts gracefully (skip existing, don't overwrite)
- Support three skills initially: `audit-embed`, `carta-workflow`, `carta-repair`
- Allow `--skip-skills` flag to skip installation entirely
- Idempotent (running `carta init` twice doesn't error)

## Architecture

### Skill Storage in Repo

Skills live in `docs/superpowers/skills/` as individual Markdown files:
- `docs/superpowers/skills/audit-embed.md` — audit report interpretation and usage
- `docs/superpowers/skills/carta-workflow.md` — general Carta workflow guidance
- `docs/superpowers/skills/carta-repair.md` — interactive repair flow (future)

Each file has YAML frontmatter:
```yaml
---
name: audit-embed
description: Run and interpret Carta audit reports for data consistency
---
```

### Installation Destinations

**Global scope:** `~/.claude/skills/`
```
~/.claude/skills/
  ├─ audit-embed/
  │  └─ audit-embed.md
  ├─ carta-workflow/
  │  └─ carta-workflow.md
  └─ carta-repair/
     └─ carta-repair.md
```

**Project scope:** `.claude/skills/` (same layout as global; lives beside other project Claude Code config)

```
.claude/skills/
  ├─ audit-embed/
  │  └─ audit-embed.md
  ├─ carta-workflow/
  │  └─ carta-workflow.md
  └─ carta-repair/
     └─ carta-repair.md
```

Whether to commit `.claude/skills/` is a project choice (many teams commit `.claude/` for shared agent settings).

### Installation Flow

**Prompt (interactive only):**
```
Install Carta skills? [G]lobal/[P]roject/[S]kip [G]: 
```

- `G`: Install to `~/.claude/skills/` (shared across all projects on machine)
- `P`: Install to `.claude/skills/` (this repo only; Claude Code’s project skill location)
- `S`: Skip (user installs manually or defers)

**Default:** `G` (global is recommended, easier to maintain)

**Non-interactive mode:** Defaults to global if not running in a terminal.

**Output:**
```
✓ Installed 3 Carta skills to ~/.claude/skills/
  (Reload Claude Code to activate)
```

Or if project scope:
```
✓ Installed 3 Carta skills to .claude/skills/
  (Reload Claude Code to activate)
```

### Implementation Details

**Location:** `carta/install/bootstrap.py`

**New function:** `_install_skills(choice: str, repo_root: Path, project_root: Path)`
- Scans `docs/superpowers/skills/` for all `.md` files
- Creates destination dirs (e.g., `~/.claude/skills/audit-embed/` or `{project_root}/.claude/skills/audit-embed/`)
- Copies each `.md` file preserving name
- Returns count of installed skills

**Integration point:** In `run_bootstrap()`, after config creation and hook registration:
```python
if _is_interactive():
    choice = _prompt_user_skills()  # Prompt G/P/S
    if choice != "S":
        _install_skills(choice, repo_root, project_root)
else:
    # Non-interactive: default to global (silent)
    _install_skills("G", repo_root, project_root)
```

**Idempotency:**
- Check if destination dir exists before copying
- If skill already present, skip (no error, just count as installed)
- Allows re-running `carta init` safely

**CLI flag (optional for v1):**
- `--skip-skills` flag to bypass prompt and skip installation
- Useful for automation/testing

### Error Handling

- **Source dir missing:** Warn but don't fail (allow graceful degradation if repo is incomplete)
- **Destination permission denied:** Warn and skip that skill; continue with others
- **Non-interactive + no destination perms:** Warn and continue (don't block init)

### Testing

**Unit tests:**
- `test_install_skills_global()` — mock global dest dir, verify copy
- `test_install_skills_project()` — mock project dest dir, verify copy
- `test_install_skills_skip()` — verify no-op when skipped
- `test_install_skills_idempotent()` — verify second run doesn't error

**Integration test:**
- Full init with skill installation (mock filesystem)

### Files to Create/Modify

**New files:**
- `docs/superpowers/skills/audit-embed.md` (existing content from plan)
- `docs/superpowers/skills/carta-workflow.md` (new)
- `docs/superpowers/skills/carta-repair.md` (new, stub for future)
- `carta/install/tests/test_skills.py` (unit tests)

**Modified files:**
- `carta/install/bootstrap.py` — add `_install_skills()` and prompt logic
- `carta/cli.py` — add `--skip-skills` flag to `carta init` (optional)

## Success Criteria

✓ Skills install to user-selected scope (global or project)
✓ Non-interactive mode defaults to global without prompting
✓ Installation is idempotent (re-running init doesn't break)
✓ Users can skip installation with prompt or flag
✓ All 3 initial skills copy successfully
✓ Claude Code picks up skills after reload (user-verified)
✓ Tests cover all code paths

## Future Extensions

- **Skill versioning:** Track installed skill versions, warn on mismatches
- **Repair flow:** `carta repair --interactive` with Claude guidance
- **Auto-update:** Check for newer skill versions on startup
- **Custom skills:** Allow projects to define additional local skills under `.claude/skills/` (or document conventions alongside Carta-installed files)
