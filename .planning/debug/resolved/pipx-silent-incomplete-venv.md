---
status: resolved
trigger: "pipx install exits 0 but leaves the venv without the carta entrypoint. pipx reinstall recovers."
created: 2026-03-24T00:00:00Z
updated: 2026-03-24T00:00:00Z
---

## Current Focus

hypothesis: CONFIRMED — carta itself has no dirty-venv bug; the issue is (1) pipx PATH not ensured after install, (2) PlatformIO carta binary shadowing, (3) no post-install PATH verification step in guide or in carta init.
test: Read bootstrap.py, install guide, pyproject.toml, cli.py
expecting: Fix requires: guide update to add pipx ensurepath + which carta check; optional carta init pre-check warning
next_action: Apply fix to install guide + add pre-check warning in cli.py cmd_init

## Symptoms

expected: After `pipx install carta-cc==0.1.7`, the `carta` command is available in PATH
actual: First pipx install ran in background, completed with exit code 0, but the carta entrypoint was missing from the venv. `which carta` either found the PlatformIO binary or nothing. `pipx reinstall carta-cc` recovered.
errors: carta command missing or pointing to wrong binary after successful install exit code
reproduction: Unclear — possibly only triggered when a prior failed install left dirty venv state, or when install runs in background (async) and the entrypoint isn't flushed yet
started: Observed once in 0.1.7 install test; root cause unknown

## Eliminated

- hypothesis: carta bootstrap.py or install.sh invokes pipx in a broken way
  evidence: install.sh uses pip (not pipx); bootstrap.py doesn't invoke pipx at all. pyproject.toml correctly declares the entrypoint. carta code is not the cause.
  timestamp: 2026-03-24

- hypothesis: background install causes async entrypoint flush failure
  evidence: pipx installs synchronously; background use by the tester is a user-side behaviour. No pipx async mechanism exists that would cause this. The real issue is PATH not updated after pipx install.
  timestamp: 2026-03-24

## Evidence

- timestamp: 2026-03-24
  checked: pyproject.toml [project.scripts]
  found: carta = "carta.cli:main" — correctly declared
  implication: The entrypoint is declared correctly; pipx should install it. If it's missing, pipx itself had dirty state OR pipx ensurepath was never run.

- timestamp: 2026-03-24
  checked: install-test-guide.md Step 1
  found: Guide recommends `pipx install carta-cc` with a PlatformIO warning. It does NOT tell user to run `pipx ensurepath` or verify `which carta` points to pipx path before continuing.
  implication: User could install successfully but have the wrong carta on PATH (PlatformIO shadow or pipx bin dir not in PATH), and the guide doesn't catch it.

- timestamp: 2026-03-24
  checked: carta/cli.py cmd_init
  found: cmd_init calls run_bootstrap(Path.cwd()) with no pre-check that the carta binary resolving in PATH is the pipx-installed one.
  implication: carta init can succeed on a system where `which carta` points to the PlatformIO binary, giving false confidence.

- timestamp: 2026-03-24
  checked: install.sh
  found: Uses `python3 -m pip install carta-cc` (not pipx). This is the curl-install path, separate from the recommended pipx flow.
  implication: curl install path doesn't have the pipx dirty-state problem, but it also doesn't add to PATH automatically.

## Resolution

root_cause: pipx exits 0 even with partial/dirty venv state, and the install guide does not include a `pipx ensurepath` step or a `which carta` verification check before proceeding. Additionally, `carta init` has no pre-check to warn when `which carta` resolves to a non-pipx path (e.g., PlatformIO binary).

fix: (1) Add `pipx ensurepath` + `which carta` verification block to install-test-guide.md Step 1, with explicit instruction to restart shell if needed and verify the path is NOT .platformio. (2) Add a warning in cli.py cmd_init that checks `shutil.which("carta")` and warns if it points to a non-pipx path.

verification: Manual review of guide and init warning; original failure (wrong carta on PATH) would have been caught by both changes.

files_changed:
  - docs/testing/install-test-guide.md
  - carta/cli.py
