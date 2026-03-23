#!/usr/bin/env bash
# carta-prompt-hook.sh — UserPromptSubmit hook
# Plan 1: graceful stub. Plan 2 adds proactive recall logic.
set -euo pipefail

CONFIG="$(git rev-parse --show-toplevel 2>/dev/null)/.carta/config.yaml"
if [ ! -f "$CONFIG" ]; then
  exit 0  # Carta not initialised — exit silently
fi

ENABLED=$(python3 -c "
import yaml, sys
cfg = yaml.safe_load(open('$CONFIG'))
print('true' if cfg.get('modules', {}).get('proactive_recall', False) else 'false')
" 2>/dev/null || echo "false")

if [ "$ENABLED" != "true" ]; then
  exit 0
fi

# Plan 2: proactive recall logic goes here
exit 0
