# Carta Install & Setup Test — Agent Guide

**Purpose:** Validate the end-to-end Carta install flow in a real repository using the PyPI package. Note anything that breaks, feels confusing, or requires steps not covered here. Report findings back to the `carta-cc` session when done.

**Target repo:** `/Users/ian/School/Elementrailer/petsense/`
**Package:** `carta-cc 0.1.2` on PyPI
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

## Step 1: Install carta-cc 0.1.2

```bash
python3 -m pip install carta-cc==0.1.2
```

Verify the right version installed and the entry point works:

```bash
carta --version
carta --help
```

Expected:
- `carta 0.1.2` (or similar)
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
Carta ready. Collections: petsense:doc, petsense:session, petsense:quirk
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
- [ ] `.carta/config.yaml` exists with `project_name: petsense`
- [ ] `.carta/carta/` contains Python runtime files (`cli.py`, `config.py`, etc.)
- [ ] `.carta/hooks/` contains `carta-prompt-hook.sh` and `carta-stop-hook.sh`, both executable (`-rwxr-xr-x`)
- [ ] `.claude/settings.json` `hooks` entries contain `git rev-parse --show-toplevel` (portable, not hardcoded path)
- [ ] `.gitignore` includes `.carta/scan-results.json`, `.carta/carta/`, `.carta/hooks/`

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

If the skill isn't found, check that Carta's skills are registered. The skills live in `.carta/carta/` but need to be surfaced — check whether a plugin or CLAUDE.md reference is needed for your Claude Code setup.

---

## Step 6: Test the hooks

The hooks fire inside Claude Code sessions (not as git hooks). To verify they're wired up:

1. Open Claude Code in the petsense repo
2. Submit any prompt — `UserPromptSubmit` hook should fire
3. End the session — `Stop` hook should fire

Both hooks in `0.1.2` are stubs (they check config and exit — no side effects yet), so no visible output is expected. The test is just that they don't *error*.

Check that the hook commands in `.claude/settings.json` use the portable format:
```bash
python3 -c "
import json
s = json.load(open('.claude/settings.json'))
hooks = s.get('hooks', {})
for name, cmd in hooks.items():
    print(f'{name}: {cmd}')
"
```

Expected: each value contains `git rev-parse --show-toplevel` — **not** a hardcoded `/Users/ian/...` path.

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

Expected: `petsense:doc` (and `petsense:session`, `petsense:quirk` from init).

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
