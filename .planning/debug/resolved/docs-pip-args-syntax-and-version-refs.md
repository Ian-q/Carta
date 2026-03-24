---
status: resolved
trigger: "Fix: docs-pip-args-syntax-and-version-refs"
created: 2026-03-24T00:00:00Z
updated: 2026-03-24T00:00:00Z
---

## Current Focus

hypothesis: install-test-guide.md contains hard-coded version strings (0.1.5, 0.1.4, 0.1.2) and no --pip-args occurrences in docs (syntax issue is referenced in latest-log.txt but not present in current docs)
test: Replacing version strings in install-test-guide.md with <version> placeholders
expecting: Guide will survive future releases without going stale
next_action: Apply version placeholder replacements

## Symptoms

expected: Guide uses `--pip-args "--no-cache-dir"` (space-separated, quoted). Version numbers use `<version>` placeholder or are removed from expected-output examples.
actual: Guide uses hard-coded version strings like "0.1.5" in expected output and cache paths. No --pip-args occurrences found in docs (may have been in a version that was not committed).
errors: `error: unrecognized arguments: --pip-args=--no-cache-dir`
reproduction: Follow the install guide as written on a fresh machine
started: Present since guide was written

## Eliminated

- hypothesis: --pip-args=--no-cache-dir syntax present in docs/install.md
  evidence: docs/install.md does not exist; no --pip-args occurrences in any docs files
  timestamp: 2026-03-24

- hypothesis: Scripts in carta/install/ use wrong --pip-args syntax
  evidence: Grep of carta/install/install.sh found no pip-args occurrences
  timestamp: 2026-03-24

## Evidence

- timestamp: 2026-03-24
  checked: docs/testing/install-test-guide.md
  found: Hard-coded "0.1.5" at lines 6, 79, 98, 129, 131, 219; "0.1.4" at line 219; "0.1.2" at line 231
  implication: Guide will show wrong version each release

- timestamp: 2026-03-24
  checked: carta/install/install.sh and all carta/ scripts
  found: No pip-args usage
  implication: No script fixes needed

- timestamp: 2026-03-24
  checked: Grep for pip-args across all docs
  found: No occurrences in docs/ directory
  implication: The --pip-args fix is not needed in docs (was likely in a draft or earlier version)

## Resolution

root_cause: install-test-guide.md hard-codes version "0.1.5" (and 0.1.4, 0.1.2) in expected output blocks and cache paths. These go stale with every release. The --pip-args syntax issue was confirmed in latest-log.txt but is not present in current committed docs.
fix: Replace all hard-coded version strings in install-test-guide.md with <version> placeholders
verification: Grep confirms zero occurrences of 0.1.x version strings or pip-args in docs/testing/install-test-guide.md after edits
files_changed: [docs/testing/install-test-guide.md]
