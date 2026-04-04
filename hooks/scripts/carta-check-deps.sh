#!/usr/bin/env bash
# carta-check-deps.sh — SessionStart hook
# Checks that Python entry points are on PATH and warns if not.
set -euo pipefail

MISSING=()

if ! command -v carta-mcp &>/dev/null; then
  MISSING+=("carta-mcp")
fi

if ! command -v carta-hook &>/dev/null; then
  MISSING+=("carta-hook")
fi

if [ ${#MISSING[@]} -gt 0 ]; then
  echo "{\"hookOutput\": {\"type\": \"warning\", \"message\": \"Carta: ${MISSING[*]} not found on PATH. Run: pipx install carta-cc\"}}"
fi

exit 0
