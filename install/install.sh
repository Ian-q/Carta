#!/usr/bin/env bash
set -euo pipefail

REPO="https://github.com/carta-cc/carta-cc"
DEST="$HOME/.carta-install"

echo "Installing Carta..."
if command -v pip &>/dev/null; then
  pip install carta
  carta init
elif command -v uvx &>/dev/null; then
  uvx carta init
else
  echo "Error: pip or uvx required. Install Python first." >&2
  exit 1
fi
