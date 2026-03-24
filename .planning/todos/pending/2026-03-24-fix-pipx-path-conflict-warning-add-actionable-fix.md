---
created: 2026-03-24T22:30:00.000Z
title: Add actionable fix to pipx PATH conflict warning
area: docs
priority: medium
files:
  - docs/install.md
  - carta/install/bootstrap.py
---

## Problem

pipx correctly warns `⚠️ carta was already on your PATH at .platformio/penv/bin/carta` but doesn't tell the user what to do. Users immediately hit `ModuleNotFoundError: No module named 'carta'` when running the PlatformIO binary. There's no path forward without knowing to add `~/.local/bin` to PATH.

## Solution

In the install guide and/or the `carta init` PATH collision warning, add:
```
To fix: add export PATH="$HOME/.local/bin:$PATH" to your ~/.zshrc or ~/.bashrc
then restart your terminal or run: source ~/.zshrc
```
