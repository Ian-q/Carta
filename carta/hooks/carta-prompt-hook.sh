#!/usr/bin/env bash
# carta-prompt-hook.sh — UserPromptSubmit hook
set -euo pipefail

CONFIG="$(git rev-parse --show-toplevel 2>/dev/null)/.carta/config.yaml"
if [ ! -f "$CONFIG" ]; then
  exit 0  # Carta not initialised — exit silently
fi

# Cheap early-out for projects that switched recall off. ONLY an explicit "False"
# exits here: if this python3 cannot answer (no PyYAML on it, a different python3
# first on PATH, a parse error) fall through to carta-hook, which runs in Carta's
# own environment and checks modules.proactive_recall itself. Treating "could not
# check" as "disabled" made the hook silently never run on a machine whose python3
# had no PyYAML (#138).
ENABLED=$(python3 -c "import yaml, sys; cfg=yaml.safe_load(open('$CONFIG')); print(cfg.get('modules', {}).get('proactive_recall', False))" 2>/dev/null || echo "unknown")

if [ "$ENABLED" = "False" ]; then
  exit 0
fi

# Without carta-hook on PATH, `exec` would fail with status 127 and Claude Code
# would report a hook error on every prompt. Exit 0 with one stderr line instead;
# `carta doctor` reports the missing entry point as an error.
if ! command -v carta-hook >/dev/null 2>&1; then
  echo "carta-prompt-hook: carta-hook not on PATH, recall skipped (run: carta doctor)" >&2
  exit 0
fi

# Pass the Claude Code hook payload to the Python module via stdin.
# carta-hook reads JSON from stdin, queries Qdrant, and writes context JSON to stdout.
exec carta-hook
