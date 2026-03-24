---
status: resolved
trigger: "pipx-path-conflict-actionable-warning: When pipx detects a PATH conflict, it warns but gives no actionable fix. Users hit ModuleNotFoundError immediately after."
created: 2026-03-24T00:00:00Z
updated: 2026-03-24T00:00:00Z
---

## Current Focus

hypothesis: PATH conflict detection is entirely absent from the codebase; pipx's own warning fires but carta never adds an actionable follow-up message pointing to the fix.
test: Read cli.py cmd_init and bootstrap.py run_bootstrap for any PATH-checking logic.
expecting: No such logic found → need to add it in cmd_init before run_bootstrap runs.
next_action: Add _check_path_conflict() call in cmd_init (cli.py) and append note to docs/testing/install-test-guide.md.

## Symptoms

expected: When carta init detects a PATH conflict, it prints both the warning AND the fix: "Add export PATH=\"$HOME/.local/bin:$PATH\" to your ~/.zshrc or ~/.bashrc"
actual: pipx warns "carta was already on your PATH at .platformio/penv/bin/carta" but gives no instruction on how to fix it. Users immediately hit ModuleNotFoundError: No module named 'carta' when running the PlatformIO carta binary.
errors: ModuleNotFoundError: No module named 'carta' when running the PlatformIO carta binary
reproduction: Install carta-cc via pipx on a machine with PlatformIO installed (which puts its own carta binary at ~/.platformio/penv/bin/carta earlier in PATH)
started: Discovered in 0.1.7 install test

## Eliminated

- hypothesis: Conflict detection exists but doesn't print fix instructions
  evidence: Searched all .py files for PATH, pipx, local.bin — no conflict detection code anywhere
  timestamp: 2026-03-24T00:00:00Z

## Evidence

- timestamp: 2026-03-24T00:00:00Z
  checked: carta/cli.py cmd_init
  found: cmd_init simply calls run_bootstrap(Path.cwd()) with no PATH validation
  implication: PATH conflict detection must be added here

- timestamp: 2026-03-24T00:00:00Z
  checked: carta/install/bootstrap.py run_bootstrap
  found: No PATH-checking logic anywhere in bootstrap
  implication: The right place to add detection is in cli.py cmd_init, before bootstrap runs

- timestamp: 2026-03-24T00:00:00Z
  checked: docs/testing/install-test-guide.md line 50
  found: Manual note about PlatformIO conflict already exists in install guide, but only as a preflight reminder — not printed at runtime
  implication: Runtime detection in cmd_init will reinforce what the doc already says

## Resolution

root_cause: cmd_init in cli.py never inspects sys.executable or PATH to detect whether the running carta binary is the correct pipx-installed one. When PlatformIO's carta binary is resolved first, Python's import system finds PlatformIO's environment, not carta-cc's, causing ModuleNotFoundError. No actionable fix is ever printed.
fix: Add _check_path_conflict() in cli.py called at the top of cmd_init. It detects when the resolved `carta` binary on PATH differs from the executable currently running, and when the conflicting path matches known patterns (.platformio), prints both the warning and the actionable export fix.
verification: confirmed by human — reviewed carta/cli.py directly. _check_path_conflict() correctly uses shutil.which, checks against sys.prefix, handles PlatformIO-specific note, and prints the actionable export PATH fix.
files_changed: [carta/cli.py, docs/testing/install-test-guide.md]
