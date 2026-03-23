#!/usr/bin/env bash
set -euo pipefail

REPO="https://github.com/Ian-q/Carta"
DEST="$HOME/.carta-install"

echo "Installing Carta..."
if command -v pip &>/dev/null; then
  python3 -m pip install carta-cc
  carta init
elif command -v uvx &>/dev/null; then
  uvx carta-cc init
else
  echo "Error: pip or uvx required. Install Python first." >&2
  exit 1
fi
