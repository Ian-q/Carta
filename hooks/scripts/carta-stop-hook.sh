#!/usr/bin/env bash
# carta-stop-hook.sh — Stop hook (plugin-native)
set -euo pipefail

# Guard: carta-hook binary must be on PATH
if ! command -v carta-hook &>/dev/null; then
  exit 0
fi

CONFIG="$(git rev-parse --show-toplevel 2>/dev/null)/.carta/config.yaml"
if [ ! -f "$CONFIG" ]; then
  exit 0
fi

ENABLED=$(python3 -c "import yaml, sys; cfg=yaml.safe_load(open('$CONFIG')); print(cfg.get('modules', {}).get('session_memory', False))" 2>/dev/null || echo "False")
ENABLED=$([ "$ENABLED" = "True" ] && echo "true" || echo "false")

if [ "$ENABLED" != "true" ]; then
  exit 0
fi

# Session save logic placeholder for future Plan 2 work
exit 0
