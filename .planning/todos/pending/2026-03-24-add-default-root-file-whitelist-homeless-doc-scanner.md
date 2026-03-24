---
created: 2026-03-24T22:05:52.524Z
title: Add default root-file whitelist to homeless_doc scanner
area: general
priority: medium
files:
  - carta/scanner.py
  - carta/config.py
---

## Problem

CLAUDE.md and CHANGELOG.md are consistently flagged as `homeless_doc` issues across every repo. These are standard root-level convention files that should never require a home directory. Every user has to manually add them to `excluded_paths` to eliminate the noise.

## Solution

Add a built-in default whitelist to the homeless_doc scanner:
- README.md
- CHANGELOG.md
- CONTRIBUTING.md
- LICENSE.md
- CLAUDE.md
- AGENTS.md
- GEMINI.md
- .cursorrules
- CODEOWNERS

These should be excluded before user-configured `excluded_paths` are applied, with no config required.
