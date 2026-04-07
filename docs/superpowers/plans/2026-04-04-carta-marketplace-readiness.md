# Carta Marketplace Readiness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the Carta Claude Code plugin install bulletproof so hooks and MCP server work immediately after marketplace install — before `carta init` is ever run.

**Architecture:** Migrate hook and MCP registration from runtime (`carta init`) into plugin-native declarations (`hooks/hooks.json`, `.mcp.json`) so Claude Code wires them automatically on plugin enable. Bootstrap retains graceful degradation and optional deep-init; it stops being a prerequisite for basic functionality.

**Tech Stack:** Bash (hook scripts), JSON (plugin manifests), Python (bootstrap.py), YAML (CI workflow)

---

## File Map

| Action | Path | Responsibility |
|---|---|---|
| Create | `hooks/hooks.json` | Plugin-native hook declarations (UserPromptSubmit, Stop, SessionStart) |
| Create | `hooks/scripts/carta-prompt-hook.sh` | UserPromptSubmit hook with binary guard |
| Create | `hooks/scripts/carta-stop-hook.sh` | Stop hook with binary guard |
| Create | `hooks/scripts/carta-check-deps.sh` | SessionStart guard — checks carta-mcp on PATH |
| Create | `.mcp.json` | Plugin-root MCP server config passing userConfig env vars |
| Delete | `plugin.json` (repo root) | Stale conflicting manifest |
| Update | `.claude-plugin/plugin.json` | Fix URLs, add userConfig, hooks ref, bump version to 0.3.0 |
| Update | `.claude-plugin/marketplace.json` | Sync version, update owner metadata |
| Update | `carta/install/bootstrap.py` | Graceful Qdrant degradation; remove Claude Code MCP/hook registration |
| Update | `.github/workflows/test.yml` | Add Python 3.10 to matrix; add `claude plugin validate` step |
| Update | `pyproject.toml` | Remove `carta/hooks/*.sh` from package-data |

---

## Task 1: Create plugin-native hook scripts

**Files:**
- Create: `hooks/scripts/carta-prompt-hook.sh`
- Create: `hooks/scripts/carta-stop-hook.sh`

- [ ] **Step 1: Create `hooks/scripts/` directory**

```bash
mkdir -p hooks/scripts
```

- [ ] **Step 2: Write `hooks/scripts/carta-prompt-hook.sh`**

```bash
#!/usr/bin/env bash
# carta-prompt-hook.sh — UserPromptSubmit hook (plugin-native)
set -euo pipefail

# Guard: carta-hook binary must be on PATH
if ! command -v carta-hook &>/dev/null; then
  echo '{"hookOutput": {"type": "warning", "message": "carta-hook not found. Install with: pipx install carta-cc"}}'
  exit 0
fi

CONFIG="$(git rev-parse --show-toplevel 2>/dev/null)/.carta/config.yaml"
if [ ! -f "$CONFIG" ]; then
  exit 0  # Carta not initialised — exit silently
fi

ENABLED=$(python3 -c "import yaml, sys; cfg=yaml.safe_load(open('$CONFIG')); print(cfg.get('modules', {}).get('proactive_recall', False))" 2>/dev/null || echo "False")
ENABLED=$([ "$ENABLED" = "True" ] && echo "true" || echo "false")

if [ "$ENABLED" != "true" ]; then
  exit 0
fi

exec carta-hook
```

- [ ] **Step 3: Write `hooks/scripts/carta-stop-hook.sh`**

```bash
#!/usr/bin/env bash
# carta-stop-hook.sh — Stop hook (plugin-native)
set -euo pipefail

# Guard: carta-hook binary must be on PATH
if ! command -v carta-hook &>/dev/null; then
  exit 0
fi

CONFIG="$(git rev-parse --show-toplevel 2>/dev/null)/.carta/config.yaml"
if [ ! -f "$CONFIG" ]; then
  exit 0
fi

ENABLED=$(python3 -c "import yaml, sys; cfg=yaml.safe_load(open('$CONFIG')); print(cfg.get('modules', {}).get('session_memory', False))" 2>/dev/null || echo "False")
ENABLED=$([ "$ENABLED" = "True" ] && echo "true" || echo "false")

if [ "$ENABLED" != "true" ]; then
  exit 0
fi

# Session save logic placeholder for future Plan 2 work
exit 0
```

- [ ] **Step 4: Make scripts executable**

```bash
chmod +x hooks/scripts/carta-prompt-hook.sh
chmod +x hooks/scripts/carta-stop-hook.sh
```

- [ ] **Step 5: Verify scripts exist and are executable**

```bash
ls -la hooks/scripts/
```

Expected: both `.sh` files with `-rwxr-xr-x` permissions.

- [ ] **Step 6: Commit**

```bash
git add hooks/scripts/carta-prompt-hook.sh hooks/scripts/carta-stop-hook.sh
git commit -m "feat: add plugin-native hook scripts with binary existence guard"
```

---

## Task 2: Create SessionStart dependency check script

**Files:**
- Create: `hooks/scripts/carta-check-deps.sh`

- [ ] **Step 1: Write `hooks/scripts/carta-check-deps.sh`**

```bash
#!/usr/bin/env bash
# carta-check-deps.sh — SessionStart hook
# Checks that Python entry points are on PATH and warns if not.
set -euo pipefail

MISSING=()

if ! command -v carta-mcp &>/dev/null; then
  MISSING+=("carta-mcp")
fi

if ! command -v carta-hook &>/dev/null; then
  MISSING+=("carta-hook")
fi

if [ ${#MISSING[@]} -gt 0 ]; then
  echo "{\"hookOutput\": {\"type\": \"warning\", \"message\": \"Carta: ${MISSING[*]} not found on PATH. Run: pipx install carta-cc\"}}"
fi

exit 0
```

- [ ] **Step 2: Make executable**

```bash
chmod +x hooks/scripts/carta-check-deps.sh
```

- [ ] **Step 3: Verify**

```bash
bash hooks/scripts/carta-check-deps.sh
```

Expected: either no output (if carta-mcp and carta-hook are installed) or a JSON warning object.

- [ ] **Step 4: Commit**

```bash
git add hooks/scripts/carta-check-deps.sh
git commit -m "feat: add SessionStart dependency check hook script"
```

---

## Task 3: Create `hooks/hooks.json`

**Files:**
- Create: `hooks/hooks.json`

- [ ] **Step 1: Write `hooks/hooks.json`**

```json
{
  "hooks": {
    "SessionStart": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "${CLAUDE_PLUGIN_ROOT}/hooks/scripts/carta-check-deps.sh"
          }
        ]
      }
    ],
    "UserPromptSubmit": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "${CLAUDE_PLUGIN_ROOT}/hooks/scripts/carta-prompt-hook.sh"
          }
        ]
      }
    ],
    "Stop": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "${CLAUDE_PLUGIN_ROOT}/hooks/scripts/carta-stop-hook.sh"
          }
        ]
      }
    ]
  }
}
```

- [ ] **Step 2: Validate JSON is well-formed**

```bash
python3 -c "import json; json.load(open('hooks/hooks.json')); print('OK')"
```

Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add hooks/hooks.json
git commit -m "feat: add plugin-native hooks/hooks.json for automatic hook registration"
```

---

## Task 4: Create plugin-root `.mcp.json`

**Files:**
- Create: `.mcp.json`

- [ ] **Step 1: Write `.mcp.json`**

```json
{
  "mcpServers": {
    "carta": {
      "command": "carta-mcp",
      "args": [],
      "env": {
        "CARTA_QDRANT_URL": "${user_config.qdrant_url}",
        "CARTA_OLLAMA_URL": "${user_config.ollama_url}"
      }
    }
  }
}
```

- [ ] **Step 2: Validate JSON is well-formed**

```bash
python3 -c "import json; json.load(open('.mcp.json')); print('OK')"
```

Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add .mcp.json
git commit -m "feat: add plugin-root .mcp.json for automatic MCP server registration"
```

---

## Task 5: Update `.claude-plugin/plugin.json`

**Files:**
- Modify: `.claude-plugin/plugin.json`

- [ ] **Step 1: Replace file contents**

Write the full updated manifest:

```json
{
  "name": "carta-cc",
  "version": "0.3.0",
  "description": "Maps, connects, and remembers your documentation. Structural scanning, LLM semantic audit, Qdrant embedding, and semantic search.",
  "author": {
    "name": "Ian",
    "url": "https://github.com/Ian-q"
  },
  "homepage": "https://github.com/Ian-q/Carta",
  "repository": "https://github.com/Ian-q/Carta",
  "license": "MIT",
  "keywords": ["documentation", "audit", "semantic-search", "qdrant", "ollama", "embedding", "knowledge-base"],
  "skills": "./skills/",
  "hooks": "./hooks/hooks.json",
  "userConfig": {
    "qdrant_url": {
      "description": "Qdrant URL (default: http://localhost:6333 — leave blank to use default)",
      "sensitive": false
    },
    "ollama_url": {
      "description": "Ollama URL for proactive recall (default: http://localhost:11434 — leave blank to disable)",
      "sensitive": false
    }
  }
}
```

- [ ] **Step 2: Validate JSON**

```bash
python3 -c "import json; d=json.load(open('.claude-plugin/plugin.json')); assert d['version'] == '0.3.0'; assert 'userConfig' in d; assert 'hooks' in d; print('OK')"
```

Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add .claude-plugin/plugin.json
git commit -m "fix: update plugin.json — fix URLs, add userConfig and hooks ref, bump version to 0.3.0"
```

---

## Task 6: Update `.claude-plugin/marketplace.json`

**Files:**
- Modify: `.claude-plugin/marketplace.json`

- [ ] **Step 1: Replace file contents**

```json
{
  "name": "carta-cc",
  "owner": {
    "name": "Ian-q"
  },
  "metadata": {
    "description": "Carta — documentation auditor, embedder, and semantic search for Claude Code projects",
    "version": "0.3.0"
  },
  "plugins": [
    {
      "name": "carta-cc",
      "source": "./",
      "description": "Maps, connects, and remembers your documentation. Structural scanning, LLM semantic audit, Qdrant embedding, and /doc-search recall.",
      "version": "0.3.0",
      "author": {
        "name": "Ian-q"
      },
      "category": "documentation",
      "keywords": ["documentation", "audit", "semantic-search", "qdrant", "ollama", "embedding", "knowledge-base"]
    }
  ]
}
```

- [ ] **Step 2: Validate JSON**

```bash
python3 -c "import json; d=json.load(open('.claude-plugin/marketplace.json')); assert d['metadata']['version'] == '0.3.0'; assert d['owner']['name'] == 'Ian-q'; print('OK')"
```

Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add .claude-plugin/marketplace.json
git commit -m "fix: sync marketplace.json version to 0.3.0 and update owner to Ian-q"
```

---

## Task 7: Delete stale root `plugin.json`

**Files:**
- Delete: `plugin.json` (repo root)

- [ ] **Step 1: Confirm it exists and is the stale one**

```bash
python3 -c "import json; d=json.load(open('plugin.json')); print(d.get('version'), d.get('name'))"
```

Expected: `0.1.3 carta-cc` (the old stale version).

- [ ] **Step 2: Delete it**

```bash
git rm plugin.json
```

- [ ] **Step 3: Commit**

```bash
git commit -m "chore: remove stale root plugin.json (canonical manifest is .claude-plugin/plugin.json)"
```

---

## Task 8: Fix bootstrap.py — graceful Qdrant degradation

**Files:**
- Modify: `carta/install/bootstrap.py`

- [ ] **Step 1: Write a failing test for graceful degradation**

In `carta/tests/test_bootstrap.py`, add:

```python
from unittest.mock import patch
from carta.install.bootstrap import run_bootstrap
from pathlib import Path
import tempfile, os

def test_bootstrap_continues_when_qdrant_unreachable(tmp_path):
    """bootstrap should not sys.exit when Qdrant is down — should warn and disable embed/search modules."""
    project_root = tmp_path
    (project_root / ".git").mkdir()

    with patch("carta.install.bootstrap._check_qdrant", return_value=False), \
         patch("carta.install.bootstrap._detect_project_name", return_value="test-proj"), \
         patch("carta.install.bootstrap._check_plugin_cache_residue", return_value=False), \
         patch("carta.install.bootstrap._create_qdrant_collections", return_value=True), \
         patch("carta.install.bootstrap._update_gitignore"), \
         patch("carta.install.bootstrap._create_mcp_configs"), \
         patch("carta.install.bootstrap._write_config"):
        # Should NOT raise SystemExit
        try:
            run_bootstrap(project_root)
        except SystemExit as e:
            raise AssertionError(f"bootstrap exited with code {e.code} when Qdrant was unreachable") from e
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
pytest carta/tests/test_bootstrap.py::test_bootstrap_continues_when_qdrant_unreachable -v
```

Expected: FAIL — `SystemExit` raised.

- [ ] **Step 3: Find and replace the hard-exit block in `carta/install/bootstrap.py`**

Replace:
```python
    if not _check_qdrant(qdrant_url):
        print(f"  Qdrant not reachable at {qdrant_url}.")
        print("  Start it with: docker run -p 6333:6333 qdrant/qdrant")
        sys.exit(1)
    print(f"  Qdrant ready at {qdrant_url}")

    modules = {
        "doc_audit": True, "doc_embed": True, "doc_search": True,
```

With:
```python
    qdrant_ok = _check_qdrant(qdrant_url)
    if not qdrant_ok:
        print(f"  Warning: Qdrant not reachable at {qdrant_url}.")
        print("  Structural audit (/doc-audit) will work without Qdrant.")
        print("  For embedding and search: docker run -p 6333:6333 qdrant/qdrant")
    else:
        print(f"  Qdrant ready at {qdrant_url}")

    modules = {
        "doc_audit": True, "doc_embed": qdrant_ok, "doc_search": qdrant_ok,
```

- [ ] **Step 4: Run the test to verify it passes**

```bash
pytest carta/tests/test_bootstrap.py::test_bootstrap_continues_when_qdrant_unreachable -v
```

Expected: PASS

- [ ] **Step 5: Run full test suite to check for regressions**

```bash
pytest carta/ -v
```

Expected: all previously passing tests still pass.

- [ ] **Step 6: Commit**

```bash
git add carta/install/bootstrap.py carta/tests/test_bootstrap.py
git commit -m "fix: bootstrap.py degrades gracefully when Qdrant unreachable instead of sys.exit(1)"
```

---

## Task 9: Update CI workflow — Python 3.10 matrix + plugin validate

**Files:**
- Modify: `.github/workflows/test.yml`

- [ ] **Step 1: Replace file contents**

```yaml
name: Test

on:
  push:
    branches: [main]
  pull_request:
    branches: [main]

jobs:
  test:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        python-version: ["3.10", "3.11", "3.12"]

    steps:
      - uses: actions/checkout@v4

      - name: Set up Python ${{ matrix.python-version }}
        uses: actions/setup-python@v5
        with:
          python-version: ${{ matrix.python-version }}

      - name: Install package and pytest
        run: |
          python -m pip install --upgrade pip
          pip install -e .
          pip install pytest

      - name: Run tests
        run: pytest carta/ -v

  validate-plugin:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - name: Install Claude Code CLI
        run: npm install -g @anthropic-ai/claude-code

      - name: Validate Claude plugin
        run: claude plugin validate
```

- [ ] **Step 2: Validate YAML is well-formed**

```bash
python3 -c "import yaml; yaml.safe_load(open('.github/workflows/test.yml')); print('OK')"
```

Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add .github/workflows/test.yml
git commit -m "ci: add Python 3.10 to test matrix and add claude plugin validate job"
```

---

## Task 10: Update pyproject.toml — remove stale carta/hooks package-data

**Files:**
- Modify: `pyproject.toml`

- [ ] **Step 1: Check current package-data in pyproject.toml**

```bash
grep -A5 "package-data\|carta.*hooks" pyproject.toml
```

Expected: a line like `"carta" = ["hooks/*.sh", "skills/*/SKILL.md"]` or similar.

- [ ] **Step 2: Remove `hooks/*.sh` from the package-data entry**

Find the line in `[tool.setuptools.package-data]` that includes `carta/hooks/*.sh` and remove just the hooks entry. Leave any skills entries intact.

If the entry looks like:
```toml
[tool.setuptools.package-data]
"carta" = ["hooks/*.sh", "skills/*/SKILL.md"]
```

Change it to:
```toml
[tool.setuptools.package-data]
"carta" = ["skills/*/SKILL.md"]
```

If `carta/skills/` is confirmed as a duplicate of `skills/` (plugin root), remove the entire package-data entry for skills too and leave it empty or remove the section.

- [ ] **Step 3: Verify package still installs cleanly**

```bash
pip install -e . && python3 -c "import carta; print(carta.__version__)"
```

Expected: `0.3.0`

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml
git commit -m "chore: remove carta/hooks/*.sh from package-data (hooks now live at plugin root)"
```

---

## Task 11: Clarify skills/ ownership — remove carta/skills/ duplicate

**Files:**
- Possibly delete: `carta/skills/` (after confirming it's a duplicate)

- [ ] **Step 1: Confirm both directories exist and compare**

```bash
diff -r skills/ carta/skills/ && echo "IDENTICAL" || echo "DIFFERS"
```

If `IDENTICAL`: proceed to delete `carta/skills/`. If `DIFFERS`: manually review which files are unique, merge to `skills/`, then delete `carta/skills/`.

- [ ] **Step 2: If identical, delete the duplicate**

```bash
git rm -r carta/skills/
```

- [ ] **Step 3: Verify `skills/` at plugin root still contains all skill files**

```bash
ls skills/
```

Expected: all four skill directories present.

- [ ] **Step 4: Commit**

```bash
git commit -m "chore: remove duplicate carta/skills/ — skills/ at plugin root is canonical"
```

---

## Task 12: Move conftest.py to repo root

**Files:**
- Move: `carta/conftest.py` → `conftest.py`

- [ ] **Step 1: Run existing tests to establish baseline**

```bash
pytest carta/ -v 2>&1 | tail -5
```

Note the number of passed/failed tests.

- [ ] **Step 2: Move conftest.py**

```bash
git mv carta/conftest.py conftest.py
```

- [ ] **Step 3: Run tests to verify nothing broke**

```bash
pytest carta/ -v 2>&1 | tail -5
```

Expected: same pass count as Step 1.

- [ ] **Step 4: Commit**

```bash
git commit -m "chore: move conftest.py from carta/ to repo root for standard pytest layout"
```

---

## Task 13: Remove old hook registration from bootstrap.py

**Files:**
- Modify: `carta/install/bootstrap.py`

> **Context:** Now that hooks and MCP are declared plugin-natively, `bootstrap.py` should no longer write `.mcp.json` for Claude Code or register hooks in `.claude/settings.json`. It can retain OpenCode `.opencode.json` generation if that's still needed.

- [ ] **Step 1: Find the hook and MCP registration code**

```bash
grep -n "_create_mcp_configs\|settings.json\|UserPromptSubmit\|hooks.*claude" carta/install/bootstrap.py
```

Note the line numbers of Claude Code-specific registration logic.

- [ ] **Step 2: Write a test that confirms bootstrap no longer writes `.claude/settings.json` hooks**

In `carta/tests/test_bootstrap.py`, add:

```python
def test_bootstrap_does_not_write_claude_settings_hooks(tmp_path):
    """bootstrap should not mutate .claude/settings.json for hooks (plugin-native now handles this)."""
    project_root = tmp_path
    (project_root / ".git").mkdir()
    claude_settings = project_root / ".claude" / "settings.json"

    with patch("carta.install.bootstrap._check_qdrant", return_value=True), \
         patch("carta.install.bootstrap._detect_project_name", return_value="test-proj"), \
         patch("carta.install.bootstrap._check_plugin_cache_residue", return_value=False), \
         patch("carta.install.bootstrap._create_qdrant_collections", return_value=True), \
         patch("carta.install.bootstrap._update_gitignore"), \
         patch("carta.install.bootstrap._write_config"):
        run_bootstrap(project_root)

    assert not claude_settings.exists(), ".claude/settings.json should not be written by bootstrap"
```

- [ ] **Step 3: Run to verify it fails (if bootstrap currently writes settings.json)**

```bash
pytest carta/tests/test_bootstrap.py::test_bootstrap_does_not_write_claude_settings_hooks -v
```

Expected: FAIL if the code writes `.claude/settings.json`.

- [ ] **Step 4: Remove Claude Code hook registration from `_create_mcp_configs` or dedicated function**

In `carta/install/bootstrap.py`, find the block that writes hook entries to `.claude/settings.json` and remove it. Keep any code that writes `.opencode.json` for OpenCode compatibility.

The `.mcp.json` write for Claude Code should also be removed from `_create_mcp_configs` since `.mcp.json` is now at plugin root. If `_create_mcp_configs` only does Claude Code things, delete the function call from `run_bootstrap`. If it also writes `.opencode.json`, keep that part.

- [ ] **Step 5: Run both bootstrap tests**

```bash
pytest carta/tests/test_bootstrap.py -v
```

Expected: both tests pass.

- [ ] **Step 6: Run full suite**

```bash
pytest carta/ -v
```

Expected: all previously passing tests still pass.

- [ ] **Step 7: Commit**

```bash
git add carta/install/bootstrap.py carta/tests/test_bootstrap.py
git commit -m "fix: remove Claude Code hook/MCP registration from bootstrap.py (now plugin-native)"
```

---

## Self-Review

### Spec Coverage Check

| Spec Issue | Task |
|---|---|
| #1 No hooks/hooks.json | Task 3 |
| #2 No .mcp.json at plugin root | Task 4 |
| #3 bootstrap hard-exits on Qdrant failure | Task 8 |
| #4 Version out of sync | Tasks 5, 6 |
| #5 Hook scripts no binary guard | Task 1 |
| #6 Stale root plugin.json | Task 7 |
| #7 Wrong repo URLs | Task 5 |
| #8 No userConfig | Task 5 |
| #9 No SessionStart hook | Tasks 2, 3 |
| #10 No claude plugin validate in CI | Task 9 |
| #11 marketplace.json sparse owner | Task 6 |
| #12 CI missing Python 3.10 | Task 9 |
| #13 carta/skills/ duplicate | Task 11 |
| #14 conftest.py in wrong place | Task 12 |
| bootstrap hook deregistration | Task 13 |

All 15 spec requirements are covered.

### Execution Order

Tasks 1–7 are fully independent of each other. Tasks 8 and 13 both modify `bootstrap.py` — run Task 8 first, then Task 13 in the same file. Tasks 11 and 12 are cleanup and can go last.

Recommended order: 7 → 1 → 2 → 3 → 4 → 5 → 6 → 9 → 10 → 8 → 13 → 11 → 12
