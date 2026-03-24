# Carta Install & Setup Test — Agent Guide

**Purpose:** Validate the end-to-end Carta install flow in a real repository using the PyPI package. This is a test run — note anything that breaks, feels confusing, or requires steps not covered here. Report findings back to the carta-cc session when done.

**Target repo:** ET-embed (or any repo with a `docs/` folder and some markdown)
**Package:** `carta-cc` on PyPI
**Expected time:** ~10 minutes

---

## Pre-flight checks

Before starting, confirm:

- [ ] Python 3.10+ available: `python3 --version`
- [ ] `pip` or `uv` available: `pip --version` or `uv --version`
- [ ] Docker running (for Qdrant): `docker ps`
- [ ] Ollama running: `curl -s http://localhost:11434/api/tags | head -c 100`
- [ ] `nomic-embed-text` pulled: `ollama list | grep nomic`

If Qdrant isn't running, start it:
```bash
docker run -d -p 6333:6333 --name qdrant qdrant/qdrant
```

If `nomic-embed-text` isn't pulled:
```bash
ollama pull nomic-embed-text
```

If you want to skip embedding entirely (audit-only), that's fine — note it and continue. Set `modules.doc_embed: false` (and optionally `modules.doc_search: false`) in the config after init.

---

## Step 1: Install carta-cc

```bash
pip install carta-cc
```

Verify:
```bash
carta --help
```

Expected: usage output listing `init`, `scan`, `embed`, `search` subcommands.

**Note any errors here.**

---

## Step 2: Run carta init

From the root of the test repository:

```bash
carta init
```

Expected outcome:
- `.carta/` directory created
- `.carta/config.yaml` generated from template
- `.carta/carta/` contains the Python runtime (scanner + embed modules)
- Claude Code hooks registered in `.claude/settings.json` (`UserPromptSubmit` and `Stop`)
- Hook scripts copied to `.carta/hooks/` with executable permissions
- `.gitignore` updated to exclude `.carta/scan-results.json`

Check each:
```bash
ls .carta/
cat .carta/config.yaml
cat .claude/settings.json | python3 -m json.tool | grep -A2 hooks
ls -l .carta/hooks/
grep "scan-results" .gitignore
```

**Note anything missing or incorrect.**

---

## Step 3: Edit config

Open `.carta/config.yaml` and update:

```yaml
project_name: et-embed          # or whatever suits the repo
docs_root: docs/                # confirm this matches the actual docs directory
excluded_paths:
  - node_modules/
  - .venv/
  - "*.tmp"
  # add any repo-specific paths to exclude
```

Check that `docs_root` points to a real directory with markdown files:
```bash
ls docs/
```

If the docs directory is named differently, update `docs_root` accordingly.

---

## Step 4: Run the structural scanner

```bash
carta scan
```

Or directly:
```bash
python .carta/carta/cli.py scan
```

Expected: `.carta/scan-results.json` written with structural findings.

Inspect the output:
```bash
python3 -c "
import json
data = json.load(open('.carta/scan-results.json'))
print('Issues found:', len(data.get('issues', [])))
print('Stats:', json.dumps(data.get('stats', {}), indent=2))
"
```

**Note what issues were found. Note any errors or unexpected output.**

---

## Step 5: Run /doc-audit in Claude Code

Open a Claude Code session in the test repo and run:

```
/doc-audit
```

Expected:
- Claude reads `.carta/scan-results.json`
- Runs semantic contradiction check on recently changed docs
- Writes or updates `AUDIT_REPORT.md` with `AUDIT-NNN` issue IDs
- Appends actionable items to `docs/BACKLOG/TRIAGE.md` (creates it if missing)

**Note: does the skill trigger correctly? Does it find the scan results? Does the report look sensible for this repo?**

---

## Step 6: Run /doc-embed (optional — requires Qdrant + Ollama)

If you have PDFs or reference docs to embed, place them in `docs/reference/` and run:

```
/doc-embed
```

Or test with any existing PDF:
```bash
# Check if there are any PDFs in the repo
find . -name "*.pdf" | head -5
```

If no PDFs are available, skip this step and note it.

**Note: does embedding complete without errors? Check Qdrant for the collection:**
```bash
curl -s http://localhost:6333/collections | python3 -m json.tool | grep et-embed
```

---

## Step 7: Run /doc-search (optional — requires Step 6 to have run)

In Claude Code:
```
/doc-search what does the documentation say about [pick something relevant to this repo]
```

**Note: does it return cited results? Are they relevant?**

---

## Step 8: Test the Claude Code hooks

The hooks are registered in `.claude/settings.json` for `UserPromptSubmit` and `Stop` events (not as git pre-commit hooks). To verify they work, open a Claude Code session in the test repo and submit a prompt or end a session — the hook scripts in `.carta/hooks/` should execute.

Check the hooks are executable:
```bash
ls -l .carta/hooks/
```

Expected: `carta-prompt-hook.sh` and `carta-stop-hook.sh` listed with execute permissions (`-rwxr-xr-x`).

**Note: did the hooks execute during your Claude Code session? Any errors in Claude Code output?**

---

## Findings to report back

After running through the above, note:

1. **Install friction** — anything that required extra steps not in the guide
2. **Errors** — exact error messages and which step they occurred at
3. **Config confusion** — any fields that weren't obvious
4. **Skill behaviour** — anything unexpected in `/doc-audit`, `/doc-embed`, `/doc-search`
5. **Missing steps** — anything the guide should have covered but didn't
6. **What worked well** — so we don't accidentally break it

Bring this back to the `carta-cc` repo session for fixes.
