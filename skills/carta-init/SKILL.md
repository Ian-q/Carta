---
name: carta-init
description: Bootstrap a Carta knowledge-graph environment in the current repository.
---

# /carta-init Skill

Bootstrap a Carta knowledge-graph environment in the current repository.

---

## Step 1: Check for existing initialisation

Check whether `.carta/config.yaml` already exists.

- If it exists: print "Carta is already initialised in this repo." and stop — do not re-run init.
- If it does not exist: continue to Step 2.

---

## Step 2: Run carta init

Run the initialisation command:

```bash
carta init
```

If the `carta` command is not on your PATH, run from the repository root:

```bash
python -m carta.cli init
```

Wait for the command to complete. Capture stdout and stderr.

---

## Step 3: Confirm setup succeeded

After the command finishes, verify:

1. `.carta/config.yaml` now exists.
2. The Qdrant collections listed in the config were created (the CLI reports this in stdout).
3. Hook scripts were copied to `.carta/hooks/` with executable permissions. (Claude Code hook registration is handled plugin-natively — no `.claude/settings.json` changes are made.)
4. `CLAUDE.md` was updated with carta context (check that the file contains a `carta` section).

Report each check as passed or failed. If any check failed, surface the error and stop.

---

## Step 4: Offer to embed immediately

Ask the user:

> "Initialisation complete. Would you like to run `/doc-embed` now to index existing documents?"

If the user confirms, invoke the `/doc-embed` skill.
