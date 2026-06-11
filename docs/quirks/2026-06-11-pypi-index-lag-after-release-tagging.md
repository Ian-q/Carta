---
doc_type: quirk
title: PyPI index lag after release tagging
created: '2026-06-11'
tags:
- release
- pypi
- pipx
---

Right after a release is tagged, PyPI's simple index can lag for a minute — 'pipx upgrade carta-cc' may report 'already latest' while the GitHub Release and pypi.org JSON already show the new version. Use 'pipx install --force carta-cc==X.Y.Z' to pin the explicit version instead of waiting.
