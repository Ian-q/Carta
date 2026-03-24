# Carta Bug Tracker

Bugs identified during install testing of `carta-cc` in the `petsense` repo (2026-03-24).
All issues discovered on macOS Sonoma, Homebrew Python 3.14, framework Python 3.12, Qdrant latest via Docker.

---

## BUG-001 — Skills not resolvable via the Skill tool [CRITICAL]

**Symptom:** After `carta init` reports "Installed 4 Carta skill(s) into .claude/skills/",
running `/doc-audit` (or `Skill("doc-audit")`) in Claude Code returns `Unknown skill: doc-audit`.
Neither `doc-audit` nor `carta-cc:doc-audit` works.

**Root cause:** Two separate carta-cc distributions exist and are out of sync:

1. The **Claude Code plugin** (`carta-cc@carta-cc`) is globally registered in
   `~/.claude/plugins/installed_plugins.json` at **v0.1.0**. Its skill files exist in
   `~/.claude/plugins/cache/carta-cc/carta-cc/0.1.0/skills/` but are not surfaced in
   session-start skill lists — likely because the plugin was installed from a git commit
   that predates the current skill format, and hasn't been refreshed.

2. The **PyPI package** (`carta init`) copies skills to `.claude/skills/` in the project
   directory. This path is **not read by Claude Code's Skill tool** — skills are only
   resolved from the global plugin cache (`~/.claude/plugins/cache/`), not from
   project-local `.claude/skills/`.

**Result:** Skills exist in two places, neither of which the Skill tool can use in practice.

**Proposed fix (option A — simpler):**
Have `carta init` update the global plugin cache entry to point at the current package's
skills directory, and write a valid `installed_plugins.json` entry for the current version:

```python
# In _install_skills(), after copying skills to .claude/skills/:
_register_plugin_globally(skills_src, version)

def _register_plugin_globally(skills_src: Path, version: str) -> None:
    import json, datetime
    plugins_json = Path.home() / ".claude/plugins/installed_plugins.json"
    cache_dest = Path.home() / f".claude/plugins/cache/carta-cc/carta-cc/{version}"
    skills_cache = cache_dest / "skills"
    skills_cache.mkdir(parents=True, exist_ok=True)
    for skill_file in skills_src.glob("*/SKILL.md"):
        dest = skills_cache / skill_file.parent.name
        dest.mkdir(exist_ok=True)
        shutil.copy2(skill_file, dest / "SKILL.md")
    # Write plugin.json into cache
    (cache_dest / ".claude-plugin").mkdir(exist_ok=True)
    plugin_meta = {"name": "carta-cc", "version": version, "skills": "./skills/"}
    (cache_dest / ".claude-plugin" / "plugin.json").write_text(json.dumps(plugin_meta))
    # Update installed_plugins.json
    data = json.loads(plugins_json.read_text()) if plugins_json.exists() else {"version": 2, "plugins": {}}
    data["plugins"]["carta-cc@carta-cc"] = [{
        "scope": "user",
        "installPath": str(cache_dest),
        "version": version,
        "installedAt": datetime.datetime.utcnow().isoformat() + "Z",
        "lastUpdated": datetime.datetime.utcnow().isoformat() + "Z",
    }]
    plugins_json.write_text(json.dumps(data, indent=2))
```

**Proposed fix (option B — investigate first):**
Confirm whether Claude Code will pick up `.claude/skills/` on session restart (it may be
supported but requires a new session to load). If it does, the fix is simply to update the
install guide: *"Restart Claude Code after running `carta init` to activate skills."*
This is worth testing before implementing option A.

---

## BUG-002 — Qdrant rejects collection names containing `:` [FIXED in 0.1.3]

**Symptom (0.1.2):** `carta init` printed warnings for all three collections:
```
Warning: Qdrant returned 422: collection name cannot contain ":" char
```
Then printed "Carta ready" anyway — silently broken state.

**Fix shipped in 0.1.3:** Collection names now use `_` separator (`petsense_doc` etc.).
Bootstrap `_create_qdrant_collections()` now handles the 409 (already-exists) status code
gracefully and returns `False` on failures, triggering a clear error message instead of
false success.

**Verification:** ✅ All three collections created successfully in 0.1.3 test.

---

## BUG-003 — `python3 -m pip install` fails on modern macOS [DOCUMENTATION]

**Symptom:** Running `python3 -m pip install carta-cc==0.1.3` on macOS with Homebrew
Python 3.14 fails with PEP 668 error:
```
error: externally-managed-environment
```

**Root cause:** PEP 668 (Python 3.12+) blocks pip installs into system/Homebrew-managed
environments. This is now the default on macOS with Homebrew Python.

**Impact:** Install guide step 1 silently fails for most modern macOS users.

**Proposed fix:** The right tool for installing CLI apps is `pipx`. Update the install
guide Step 1:

```markdown
## Step 1: Install carta-cc

**macOS (recommended):**
```bash
pipx install carta-cc
```

**Other / venv:**
```bash
python3 -m pip install carta-cc
```

If you get an `externally-managed-environment` error, use `pipx install carta-cc` instead.
`pipx` is the standard tool for installing Python CLI apps in isolation.
Install pipx: `brew install pipx && pipx ensurepath`
```

---

## BUG-004 — `.claude/skills/` not added to `.gitignore` [MINOR]

**Symptom:** `_install_skills()` writes auto-generated skill files to `.claude/skills/`,
but `_update_gitignore()` only adds `.carta/` paths — not `.claude/skills/`.
Users will see untracked skill files in `git status` and may accidentally commit them.

**Current gitignore entries added by carta:**
```
.carta/scan-results.json
.carta/carta/
.carta/hooks/
```

**Proposed fix:** Add `.claude/skills/` to the gitignore entries list in `_update_gitignore()`:

```python
# bootstrap.py _update_gitignore()
entries = [
    ".carta/scan-results.json",
    ".carta/carta/",
    ".carta/hooks/",
    ".claude/skills/",   # ← add this
]
```

---

## BUG-005 — `project_name` written to bottom of config.yaml [MINOR UX]

**Symptom:** The install guide highlights `project_name` as one of the first fields to
verify after `carta init`, but `_deep_merge()` in `_write_config()` appends override
keys after the defaults — so `project_name` and `qdrant_url` appear at the very bottom
of the generated config file, after ~30 lines of nested defaults.

**Proposed fix:** In `_write_config()`, manually place priority fields at the top before
the rest of the defaults:

```python
def _write_config(carta_dir, project_name, qdrant_url, modules):
    from carta.config import DEFAULTS, _deep_merge
    base = _deep_merge(DEFAULTS, {"modules": modules})
    # Hoist identity fields to the top for readability
    ordered = {
        "project_name": project_name,
        "qdrant_url": qdrant_url,
        "docs_root": base.pop("docs_root", "docs/"),
        **base,
    }
    (carta_dir / "config.yaml").write_text(
        yaml.dump(ordered, default_flow_style=False, sort_keys=False)
    )
```

---

## BUG-006 — Hooks written in wrong format [BLOCKING — causes settings error on startup]

**Symptom:** On Claude Code session start, a settings error popup appears:
```
hooks
├ Stop: Expected array, but received string
└ UserPromptSubmit: Expected array, but received string
```
The entire `.claude/settings.json` is skipped — hooks don't fire at all.

**Root cause:** `_register_hooks()` writes hook values as plain strings:
```python
hooks[hook_name] = "bash -c '...'"
```
Claude Code's current hook schema requires an array of `{matcher, hooks}` objects:
```json
[{"matcher": "", "hooks": [{"type": "command", "command": "..."}]}]
```

**Fix (applied to bootstrap.py):**
```python
cmd = f"""bash -c '"$(git rev-parse --show-toplevel)/.carta/hooks/{script_name}"'"""
hooks[hook_name] = [{"matcher": "", "hooks": [{"type": "command", "command": cmd}]}]
```

---

## Summary

| ID | Severity | Status | Title |
|----|----------|--------|-------|
| BUG-001 | Critical blocker | ✅ Fixed (bootstrap.py) | Skills not resolvable via Skill tool |
| BUG-002 | Critical blocker | ✅ Fixed (0.1.3) | Qdrant rejects `:` in collection names |
| BUG-003 | High (most users) | ✅ Already fixed (docs) | `pip install` fails on modern macOS — needs `pipx` note |
| BUG-004 | Minor | ✅ N/A (resolved by BUG-001 fix) | `.claude/skills/` not gitignored |
| BUG-005 | Minor UX | ✅ Fixed (bootstrap.py) | `project_name` appears at bottom of config.yaml |
| BUG-006 | Critical blocker | ✅ Fixed (bootstrap.py) | Hooks written as strings instead of array schema |

**Priority:** BUG-001 is the main remaining blocker — everything else works. The quickest
path to unblocking it is to test whether a session restart picks up `.claude/skills/`
(option B). If not, implement option A (update global plugin cache from `carta init`).
