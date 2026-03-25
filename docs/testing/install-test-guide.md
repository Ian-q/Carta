# Carta Install & Setup Test — Agent Guide

**Purpose:** Validate the end-to-end Carta install flow in a real repository using the PyPI package. Note anything that breaks, feels confusing, or requires steps not covered here. Report findings back to the `carta-cc` session when done.

**Target repo:** `/Users/ian/School/Elementrailer/petsense/`
**Package:** `carta-cc` (latest) on PyPI
**Expected time:** ~10 minutes

---

## Pre-flight checks

Run these from anywhere. Confirm each before proceeding.

```bash
# Python 3.10+
python3 --version

# pip available
python3 -m pip --version

# Docker running (for Qdrant)
docker ps

# Ollama running
curl -s http://localhost:11434 | head -c 50

# nomic-embed-text pulled
ollama list | grep nomic
```

**If Qdrant isn't running:**
```bash
docker run -d -p 6333:6333 --name qdrant qdrant/qdrant
```

**If `nomic-embed-text` isn't pulled:**
```bash
ollama pull nomic-embed-text
```

> **Embed/search are optional.** If you want to test audit-only (no Qdrant, no Ollama),
> that's fine — continue through Step 4, then set `modules.doc_embed: false` and
> `modules.doc_search: false` in `.carta/config.yaml` after init.

---

## Step 1: Install carta-cc

> **PlatformIO conflict warning:** PlatformIO ships its own `carta` binary at `~/.platformio/penv/bin/carta`. If this is on your PATH before carta-cc, it will shadow the real `carta` command. Check with `which carta` — it should point to a pipx/uv/venv path, not `.platformio`.
>
> `carta init` will detect and print this conflict automatically, along with the fix. If you see the warning, run:
> ```bash
> export PATH="$HOME/.local/bin:$PATH"
> ```
> Add that line to your `~/.zshrc` or `~/.bashrc` and restart your terminal, then re-run `carta init`.

```bash
# Recommended on macOS (avoids PEP 668 "externally managed environment" errors)
pipx install carta-cc

# Or with uv
uv tool install carta-cc

# Or with pip in a venv (works everywhere)
python3 -m venv ~/.carta-venv
~/.carta-venv/bin/pip install carta-cc
# Add to PATH so `carta` resolves correctly:
export PATH="$HOME/.carta-venv/bin:$PATH"
# Add that export to your ~/.zshrc or ~/.bashrc to persist it
```

> **macOS note:** Homebrew Python 3.12+ enforces PEP 668, which blocks
> `pip install` into the system environment. Use `pipx`, `uv tool`, or
> create a venv first. If you see `externally-managed-environment`, that's why.

**After `pipx install`, ensure pipx's bin directory is on your PATH:**

```bash
pipx ensurepath
```

If it reports adding a path, **restart your terminal** (or run `source ~/.zshrc`) before continuing. This is the most common cause of "carta not found" after a successful pipx install.

**Verify the correct `carta` is on PATH before proceeding:**

```bash
which carta
```

Expected: a path containing `pipx`, `.local/bin`, or your venv — e.g. `/Users/you/.local/bin/carta`. If it shows `.platformio` or is missing, fix PATH first. `carta init` will also print a warning if it detects the wrong binary.

Verify the right version installed and the entry point works:

```bash
carta --version
carta --help
```

Expected:
- Version number (e.g. `carta <version>`)
- Help text listing `init`, `scan`, `embed`, `search` subcommands

**Note any errors.**

---

## Step 2: Run carta init

```bash
cd /Users/ian/School/Elementrailer/petsense
carta init
```

Expected output (roughly):
```
Initialising Carta for project: petsense
  Qdrant ready.
  Ollama ready.   (or: Warning: Ollama not reachable...)
  Registered 4 Carta skill(s) in global plugin cache (v<version>)
Carta ready. Collections: petsense_doc, petsense_session, petsense_quirk
Run /doc-embed to seed the knowledge store.
```

After init, verify each artifact was created:

```bash
# Config file
cat .carta/config.yaml

# Runtime copy
ls .carta/carta/

# Hook scripts with execute permissions
ls -l .carta/hooks/

# Claude Code hooks registered — should show git rev-parse wrapper, NOT absolute paths
cat .claude/settings.json

# Gitignore updated
grep "carta" .gitignore
```

**Checklist:**
- [ ] `.carta/config.yaml` exists with `project_name: petsense` at the top
- [ ] `.carta/carta/` contains Python runtime files (`cli.py`, `config.py`, etc.)
- [ ] `.carta/hooks/` contains `carta-prompt-hook.sh` and `carta-stop-hook.sh`, both executable (`-rwxr-xr-x`)
- [ ] Skills registered in global plugin cache — verify:
  ```bash
  python3 -c "import json; d=json.load(open('/Users/ian/.claude/plugins/installed_plugins.json')); print(json.dumps(d['plugins'].get('carta-cc@carta-cc'), indent=2))"
  ls ~/.claude/plugins/cache/carta-cc/carta-cc/<version>/skills/
  ```
  Expected: entry pointing to `<version>`, and `carta-init doc-audit doc-embed doc-search` in skills dir.
- [ ] `.claude/settings.json` `hooks` entries use array schema with `git rev-parse --show-toplevel`
- [ ] `.gitignore` includes `.carta/scan-results.json`, `.carta/carta/`, `.carta/hooks/`

> **⚠️ AGENT PAUSE — Action required before continuing:**
> `carta init` registers Carta's skills into the global plugin cache. Claude Code only loads
> skills at session start, so the `/doc-audit`, `/doc-embed`, and `/doc-search` skills won't
> be available until you restart.
>
> **Tell the user:** "Please restart Claude Code now (quit and reopen, or press Cmd+Q) so
> the Carta skills load into the new session. Then reopen this repo and resume from Step 3."
>
> **Do not continue with Steps 3–8 in this session.** The skills will not be found until
> after the restart.

**Note anything missing or incorrect.**

---

## Step 3: Review and adjust config

Open `.carta/config.yaml`. The most important fields to check:

```yaml
project_name: petsense       # used to namespace Qdrant collections
docs_root: docs/             # confirm this directory exists
qdrant_url: http://localhost:6333
```

Check that `docs_root` points to a real directory:
```bash
ls docs/
```

If the docs directory is named differently (e.g. `documentation/`, `doc/`), update `docs_root` in the config.

Add any repo-specific excluded paths:
```yaml
excluded_paths:
  - node_modules/
  - .venv/
  - "*.tmp"
```

**Note any config fields that are confusing or seem wrong.**

---

## Step 4: Run the structural scanner

```bash
carta scan
```

Expected: `.carta/scan-results.json` written.

Inspect the results:
```bash
python3 -c "
import json
data = json.load(open('.carta/scan-results.json'))
print('Issues found:', len(data.get('issues', [])))
print('Stats:', json.dumps(data.get('stats', {}), indent=2))
print()
print('Issue types:')
from collections import Counter
counts = Counter(i['type'] for i in data.get('issues', []))
for t, n in counts.most_common():
    print(f'  {t}: {n}')
print()
print('First 5 issues:')
for i in data.get('issues', [])[:5]:
    print(f'  [{i[\"type\"]}] {i.get(\"doc\", \"?\")} — {i.get(\"detail\", \"\")}')
"
```

Common issue types on a first scan of a repo without Carta frontmatter:
- `missing_frontmatter` — docs without `related:`/`last_reviewed:` (expected and fine)
- `homeless_doc` — markdown files outside `docs_root`
- `orphaned_doc` — docs with no cross-references and no siblings

**Note what issues were found. Note any errors or unexpected output.**

---

## Step 5: Run /doc-audit in Claude Code

Open a Claude Code session in the petsense repo and run:

```
/doc-audit
```

Expected:
- Carta reads `.carta/scan-results.json`
- Semantic contradiction check runs on recently changed docs
- `AUDIT_REPORT.md` written (or updated) with `AUDIT-NNN` issue IDs
- If issues need triage, `docs/BACKLOG/TRIAGE.md` created or updated

**Note: does the skill trigger correctly? Does it find the scan results? Does the report look sensible for this repo?**

> **First-run note:** On a brand-new repo with no prior audit history, `changed_since_last_audit`
> will be empty (no git baseline exists yet). This is expected — the semantic contradiction check
> is skipped and only the structural scanner results are written to `AUDIT_REPORT.md`. This is
> correct behaviour, not a bug.

If a skill isn't found, verify `~/.claude/plugins/installed_plugins.json` has a `carta-cc@carta-cc` entry pointing to `<version>`, and that `~/.claude/plugins/cache/carta-cc/carta-cc/<version>/skills/` contains the skill directories. Then restart the Claude Code session.

---

## Step 6: Test the hooks

The hooks fire inside Claude Code sessions (not as git hooks). To verify they're wired up:

1. Open Claude Code in the petsense repo
2. Submit any prompt — `UserPromptSubmit` hook should fire
3. End the session — `Stop` hook should fire

Both hooks are stubs (they check config and exit — no side effects yet), so no visible output is expected. The test is just that they don't *error*.

Check that the hook commands in `.claude/settings.json` use the correct format:
```bash
python3 -c "
import json
s = json.load(open('.claude/settings.json'))
hooks = s.get('hooks', {})
for name, entries in hooks.items():
    cmd = entries[0]['hooks'][0]['command']
    print(f'{name}: {cmd}')
"
```

Expected: each hook is an array of objects, and the command contains `git rev-parse --show-toplevel` — **not** a hardcoded `/Users/ian/...` path.

**Note: any hook errors in Claude Code output?**

---

## Step 7 (optional): Run /doc-embed

Requires Qdrant and Ollama running with `nomic-embed-text`.

If there are PDFs in the repo:
```bash
find . -name "*.pdf" | head -5
```

Or drop a test PDF into `docs/reference/` and run in Claude Code:
```
/doc-embed
```

Verify the collection appeared in Qdrant:
```bash
curl -s http://localhost:6333/collections | python3 -c "
import json, sys
data = json.load(sys.stdin)
for c in data['result']['collections']:
    print(c['name'])
"
```

Expected: `petsense_doc` (and `petsense_session`, `petsense_quirk` from init).

**Note: did embedding complete without errors? How long did it take per document?**

---

## Step 8 (optional): Run /doc-search

Requires Step 7 to have run.

In Claude Code:
```
/doc-search what does the documentation say about [pick something relevant to petsense]
```

**Note: does it return cited results? Are they relevant?**

---

## Cleanup (if desired)

To remove Carta from the test repo without leaving a mess:
```bash
# Remove runtime and config
rm -rf .carta/

# Remove hook registrations from Claude Code settings
# (edit .claude/settings.json and remove the UserPromptSubmit and Stop entries)

# Remove CLAUDE.md annotation (last line)
# Remove AUDIT_REPORT.md if created
```

---

## Findings to report back

After running through the above, note:

1. **Install friction** — anything that required extra steps not in the guide
2. **Errors** — exact error messages and which step they occurred at
3. **Config confusion** — any fields that weren't obvious or had wrong defaults for this repo
4. **Skill behaviour** — anything unexpected in `/doc-audit`, `/doc-embed`, `/doc-search`
5. **Hook behaviour** — did they fire? any errors?
6. **Missing steps** — anything the guide should have covered but didn't
7. **What worked well** — so we don't accidentally break it

Bring this back to the `carta-cc` repo session for fixes.
