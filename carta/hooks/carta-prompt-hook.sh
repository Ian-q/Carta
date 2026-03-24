#!/usr/bin/env bash
# carta-prompt-hook.sh — UserPromptSubmit hook
# Plan 1: graceful stub. Plan 2 adds proactive recall logic.
set -euo pipefail

CONFIG="$(git rev-parse --show-toplevel 2>/dev/null)/.carta/config.yaml"
if [ ! -f "$CONFIG" ]; then
  exit 0  # Carta not initialised — exit silently
fi

ENABLED=$(grep -A1 'proactive_recall' "$CONFIG" 2>/dev/null | grep -q 'true' && echo "true" || echo "false")

if [ "$ENABLED" != "true" ]; then
  exit 0
fi

# Plan 2: proactive recall logic goes here
exit 0
