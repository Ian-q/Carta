#!/usr/bin/env bash
set -euo pipefail

echo "Installing Carta..."
if python3 -m pip --version &>/dev/null; then
  python3 -m pip install carta-cc && carta init
elif command -v uvx &>/dev/null; then
  uvx --from carta-cc carta init
else
  echo "Error: pip or uvx required. Install Python 3 first." >&2
  exit 1
fi
