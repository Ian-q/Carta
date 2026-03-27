#!/usr/bin/env bash
# carta-prompt-hook.sh — UserPromptSubmit hook
set -euo pipefail

CONFIG="$(git rev-parse --show-toplevel 2>/dev/null)/.carta/config.yaml"
if [ ! -f "$CONFIG" ]; then
  exit 0  # Carta not initialised — exit silently
fi

ENABLED=$(python3 -c "import yaml, sys; cfg=yaml.safe_load(open('$CONFIG')); print(cfg.get('modules', {}).get('proactive_recall', False))" 2>/dev/null || echo "False")
ENABLED=$([ "$ENABLED" = "True" ] && echo "true" || echo "false")

if [ "$ENABLED" != "true" ]; then
  exit 0
fi

# Pass the Claude Code hook payload to the Python module via stdin.
# carta-hook reads JSON from stdin, queries Qdrant, and writes context JSON to stdout.
exec carta-hook
