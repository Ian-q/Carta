---
created: 2026-03-24T22:05:52.524Z
title: Docs — add venv bin PATH setup step after install
area: docs
priority: low
files:
  - docs/install.md
---

## Problem

The install guide recommends `pipx` first but doesn't mention it needs to be installed. When users fall back to the venv path, `carta` requires the full venv path for every invocation. The guide doesn't include a convenience step for adding the venv's bin to PATH.

## Solution

In the install guide, after the venv fallback section, add:
```bash
# Add to your shell profile (~/.zshrc or ~/.bashrc)
export PATH="$HOME/.venv/carta/bin:$PATH"  # adjust path to your venv location
```

Also add a note: "If pipx is not installed, you can install it with `brew install pipx` (macOS) or `pip install --user pipx`."
