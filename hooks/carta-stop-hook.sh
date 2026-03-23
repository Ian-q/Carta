#!/usr/bin/env bash
# carta-stop-hook.sh — Stop hook
# Plan 1: graceful stub. Plan 2 adds session save logic.
set -euo pipefail

CONFIG="$(git rev-parse --show-toplevel 2>/dev/null)/.carta/config.yaml"
if [ ! -f "$CONFIG" ]; then
  exit 0
fi

ENABLED=$(python3 -c "
import yaml, sys
cfg = yaml.safe_load(open('$CONFIG'))
print('true' if cfg.get('modules', {}).get('session_memory', False) else 'false')
" 2>/dev/null || echo "false")

if [ "$ENABLED" != "true" ]; then
  exit 0
fi

# Plan 2: session save logic goes here.
# For now, print a reminder that /carta-save is available.
echo "Session ended. Use /carta-save to save this session to Carta memory."
exit 0
