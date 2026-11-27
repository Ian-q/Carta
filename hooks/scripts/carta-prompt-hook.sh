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

exec carta-hook
