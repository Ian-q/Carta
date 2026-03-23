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
python .carta/carta/cli.py init
```

If the `carta` command is globally installed you may also run:

```bash
carta init
```

Wait for the command to complete. Capture stdout and stderr.

---

## Step 3: Confirm setup succeeded

After the command finishes, verify:

1. `.carta/config.yaml` now exists.
2. The Qdrant collections listed in the config were created (the CLI reports this in stdout).
3. Git hooks were registered (look for "hooks registered" or similar in stdout).
4. `CLAUDE.md` was updated with carta context (check that the file contains a `carta` section).

Report each check as passed or failed. If any check failed, surface the error and stop.

---

## Step 4: Offer to embed immediately

Ask the user:

> "Initialisation complete. Would you like to run `/doc-embed` now to index existing documents?"

If the user confirms, invoke the `/doc-embed` skill.
