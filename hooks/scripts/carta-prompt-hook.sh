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
