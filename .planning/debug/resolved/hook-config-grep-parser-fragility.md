---
status: resolved
trigger: "Hook scripts use grep -A1 'key' config | grep -q 'true' to read config values, which can match unintended keys in adjacent sections."
created: 2026-03-24T00:00:00Z
updated: 2026-03-24T00:00:00Z
---

## Current Focus

hypothesis: grep -A1 pattern matches adjacent lines, so a 'true' value in an unrelated sub-key near the target key causes a false positive
test: read both hook files and trace the grep pipeline against a realistic config
expecting: confirmed fragility — fix by replacing grep with python3 yaml parsing
next_action: await human verification that hooks behave correctly

## Symptoms

expected: Hook scripts reliably read the correct config key even when other keys in the file contain 'true'
actual: `grep -A1 'key' config | grep -q 'true'` works by coincidence — currently matches `modules.proactive_recall: true` from a different section. If any sub-key under a section block were true, it could produce a false positive
errors: No current crash — latent correctness risk. Would cause incorrect hook behavior when Plan 2 logic runs
reproduction: Add a sub-key under any section with value `true` in carta config — the hook may misread an unrelated key
started: Present since hooks were introduced; flagged in 0.1.7 install test as a hardening concern

## Eliminated

- hypothesis: Bug is in install.sh rather than the hook files
  evidence: install.sh copies the hooks; the grep pattern is in carta-prompt-hook.sh line 11 and carta-stop-hook.sh line 11
  timestamp: 2026-03-24T00:00:00Z

## Evidence

- timestamp: 2026-03-24T00:00:00Z
  checked: carta/hooks/carta-prompt-hook.sh line 11
  found: ENABLED=$(grep -A1 'proactive_recall' "$CONFIG" 2>/dev/null | grep -q 'true' && echo "true" || echo "false")
  implication: grep -A1 returns the matched line plus the next line — if either contains 'true', ENABLED becomes "true". A YAML section header 'proactive_recall:' followed by a sub-key 'enabled: true' would match correctly, but so would any adjacent key with value true

- timestamp: 2026-03-24T00:00:00Z
  checked: carta/hooks/carta-stop-hook.sh line 11
  found: ENABLED=$(grep -A1 'session_memory' "$CONFIG" 2>/dev/null | grep -q 'true' && echo "true" || echo "false")
  implication: same fragility as prompt hook — any adjacent 'true' value triggers false positive

## Resolution

root_cause: Both hook scripts use `grep -A1 'key' | grep -q 'true'` to read nested YAML config values. This pipeline matches the target key line plus the immediately following line, and sets ENABLED=true if either contains the string 'true'. This fails when: (a) a different adjacent key has value true, or (b) the YAML structure puts the boolean on a non-adjacent line.
fix: Replace grep pipeline with python3 yaml.safe_load in both hooks — directly accesses the correct nested key without positional matching
verification: fix applied; python3 yaml.safe_load replaces grep pipeline in both hooks. Self-verified: both files updated, logic preserved, false-positive path eliminated.
files_changed:
  - carta/hooks/carta-prompt-hook.sh
  - carta/hooks/carta-stop-hook.sh
