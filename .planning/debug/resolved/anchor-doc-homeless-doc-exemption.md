---
status: resolved
trigger: "Files listed as anchor_doc in carta config are still flagged as homeless_doc by the scanner."
created: 2026-03-24T00:00:00
updated: 2026-03-24T00:00:00
---

## Current Focus

hypothesis: check_homeless_docs does not read anchor_doc from cfg before flagging files
test: read check_homeless_docs in carta/scanner/scanner.py
expecting: missing anchor_doc exemption logic
next_action: add anchor_doc exemption before the issue append

## Symptoms

expected: Files configured as anchor_doc (e.g. CLAUDE.md) are automatically exempted from the homeless_doc scanner check
actual: CLAUDE.md is flagged as a homeless_doc issue even though anchor_doc: CLAUDE.md is set in the carta config
errors: False-positive audit issue on every repo that uses anchor_doc
reproduction: Set anchor_doc: CLAUDE.md in carta config, run carta scan, observe CLAUDE.md flagged as homeless_doc
started: Present since homeless_doc check was introduced; discovered in 0.1.7 install test

## Eliminated

- hypothesis: anchor_doc check exists but has wrong field name
  evidence: check_homeless_docs has no reference to anchor_doc at all
  timestamp: 2026-03-24T00:00:00

## Evidence

- timestamp: 2026-03-24T00:00:00
  checked: carta/scanner/scanner.py check_homeless_docs (lines 75-96)
  found: function skips README.md and excluded_paths but has no logic to read anchor_doc from cfg
  implication: any file set as anchor_doc will be flagged as homeless_doc regardless of config

## Resolution

root_cause: check_homeless_docs does not check cfg.get("anchor_doc") before appending an issue. anchor_doc is a scalar string in config (and potentially a list for anchor_docs). Files whose basename matches the anchor_doc value are legitimate top-level docs and should be exempt.
fix: After the README.md skip and is_excluded check, add a check: if p.name matches cfg.get("anchor_doc") or is in cfg.get("anchor_docs", []), skip the file.
verification: 38/38 tests pass including new regression test test_anchor_doc_exempt_from_homeless
files_changed: [carta/scanner/scanner.py]
