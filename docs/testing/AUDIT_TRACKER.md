# Carta Codebase Audit Tracker

Comprehensive issue tracker from the March 2026 preemptive audit. Supersedes the previous `INSTALL_WORKFLOW_BUGS.md` (all items there are now **fixed**).

## Format

- **ID**: severity prefix + sequential number (C = critical, H = high, M = medium, L = low)
- **Severity**: impact on correctness, security, or user experience
- **Component**: area of the codebase affected
- **Symptom**: what breaks or degrades
- **Evidence**: file paths and code excerpts
- **Proposed fix**: recommended change
- **Status**: `open` / `in_progress` / `fixed` / `won't_fix`

---

## Critical — would cause failures for users

### C-1: Wrong PyPI dependency — `pypdf` declared but `fitz` (pymupdf) imported

- **Severity**: Critical (embed pipeline broken on install)
- **Component**: packaging / embed
- **Symptom**: After `pip install carta-cc`, running `carta embed` crashes with `ModuleNotFoundError: No module named 'fitz'`. Tests hide this because `extract_pdf_text` is always mocked.
- **Evidence**:
  - `pyproject.toml:14` declares `pypdf>=4.0`
  - `carta/embed/parse.py:13` imports `fitz` (PyMuPDF) — a completely different library
- **Proposed fix**: Change `pypdf>=4.0` to `pymupdf>=1.23` in `pyproject.toml`.
- **Status**: **fixed**

---

### C-2: README `uvx` command doesn't work

- **Severity**: Critical (first-run experience broken)
- **Component**: docs
- **Symptom**: `uvx carta-cc init` looks for a `carta-cc` executable which doesn't exist. The entry point is named `carta`.
- **Evidence**:
  - `README.md:77`: `uvx carta-cc init`
  - `pyproject.toml:18`: `carta = "carta.cli:main"`
- **Proposed fix**: Change to `uvx --from carta-cc carta init`.
- **Status**: **fixed**

---

### C-3: README curl install URL points to wrong path

- **Severity**: Critical (install fails)
- **Component**: docs
- **Symptom**: The curl URL references `main/install/install.sh` but the file lives at `carta/install/install.sh`.
- **Evidence**:
  - `README.md:87`: `https://raw.githubusercontent.com/carta-cc/carta-cc/main/install/install.sh`
  - Actual path: `carta/install/install.sh`
- **Proposed fix**: Change URL to `.../main/carta/install/install.sh`.
- **Status**: **fixed**

---

### C-4: `/carta-init` skill runs CLI from a path that doesn't exist yet

- **Severity**: Critical (skill fails on fresh project)
- **Component**: skills
- **Symptom**: Skill instructs `python .carta/carta/cli.py init`, but `.carta/` doesn't exist until init completes. This is a chicken-and-egg problem.
- **Evidence**:
  - `skills/carta-init/SKILL.md:21`: `python .carta/carta/cli.py init`
- **Proposed fix**: Change primary command to `carta init` (pip-installed) or `python -m carta.cli init` (from repo root). The `.carta/carta/cli.py` path only works after init.
- **Status**: **fixed**

---

### C-5: CI never runs tests — broken code can ship to PyPI

- **Severity**: Critical (quality gate missing)
- **Component**: CI
- **Symptom**: The only GitHub Actions workflow is `publish.yml`, which builds and publishes without running `pytest`. Regressions are not caught before release.
- **Evidence**:
  - `.github/workflows/publish.yml` — no `pytest` step, no test workflow
- **Proposed fix**: Add a `test.yml` workflow triggered on push/PR, or add a `pytest` step before `python -m build` in `publish.yml`.
- **Status**: **fixed**

---

## High — broken behavior or security risk

### H-1: Shell injection in both hook scripts

- **Severity**: High (security — arbitrary code execution)
- **Component**: hooks
- **Symptom**: `$CONFIG` is shell-interpolated directly into a Python string literal. A project path containing a single quote (e.g. `it's-here/`) breaks the Python code. A malicious directory name could inject arbitrary Python.
- **Evidence**:
  - `carta/hooks/carta-prompt-hook.sh:13`: `cfg = yaml.safe_load(open('$CONFIG'))`
  - `carta/hooks/carta-stop-hook.sh:13`: identical pattern
- **Proposed fix**: Pass the path via `sys.argv[1]` and quote the shell variable, or use an environment variable.
- **Status**: **fixed**

---

### H-2: `ConfigError` never caught in CLI — users see raw tracebacks

- **Severity**: High (poor user experience)
- **Component**: CLI
- **Symptom**: `cli.py` only catches `FileNotFoundError`. If `load_config()` raises `ConfigError` (missing field, invalid YAML), `RuntimeError`, or `KeyError`, users see a full Python traceback.
- **Evidence**:
  - `carta/cli.py:84-88`: only `except FileNotFoundError`
  - `carta/config.py:48-49`: defines `ConfigError` but nothing catches it at the CLI level
- **Proposed fix**: Catch `ConfigError`, `KeyError`, `RuntimeError` (and optionally `Exception`) in the top-level handler with a clean `print(..., file=sys.stderr)`.
- **Status**: **fixed**

---

### H-3: `_register_hooks` silently overwrites existing Claude Code hooks

- **Severity**: High (data loss)
- **Component**: bootstrap
- **Symptom**: If the user already has `UserPromptSubmit` or `Stop` hooks configured in `.claude/settings.json`, they are silently overwritten with no backup or warning.
- **Evidence**:
  - `carta/install/bootstrap.py:102-104`: unconditionally assigns hook paths
- **Proposed fix**: Check for existing hook values. If present, warn the user and either skip, merge, or create a backup of the original `settings.json`.
- **Status**: **fixed**

---

### H-4: Claude Code hooks format may not match expected schema

- **Severity**: High (hooks silently fail)
- **Component**: bootstrap
- **Symptom**: Hooks are written as simple string values (`"UserPromptSubmit": "/path/to/script.sh"`). Claude Code's settings schema may expect a structured format (array of objects with `type`/`command` fields). If the format is wrong, hooks register silently but never execute.
- **Evidence**:
  - `carta/install/bootstrap.py:103-104`
- **Proposed fix**: Verify against Claude Code's current hook specification and update the format accordingly.
- **Status**: **fixed** (format confirmed correct; added malformed-JSON handling)

---

### H-5: README config example shows non-existent `embed.enabled` key and omits required `qdrant_url`

- **Severity**: High (first-run confusion)
- **Component**: docs
- **Symptom**: The YAML example in the README shows `embed.enabled: true` (doesn't exist in schema) and omits `qdrant_url` (a required field). Users copying this will get `ConfigError`. BUG-005 was marked "fixed" but the README YAML block was not updated.
- **Evidence**:
  - `README.md:146-149`: shows `embed: enabled: true`
  - `carta/config.py:4`: `REQUIRED_FIELDS = ["project_name", "qdrant_url"]`
  - `carta/config.py:38-44`: toggle is `modules.doc_embed`, not `embed.enabled`
- **Proposed fix**: Replace the YAML example with correct keys including `qdrant_url` and `modules.doc_embed`.
- **Status**: **fixed**

---

## Medium — degraded behavior, footguns, fragility

### M-1: Hardcoded Ollama URL in bootstrap

- **Severity**: Medium
- **Component**: bootstrap
- **Symptom**: Qdrant URL reads from `CARTA_QDRANT_URL` env var, but Ollama URL is hardcoded to `http://localhost:11434`. Users on a non-default Ollama port/host have no way to configure this during init.
- **Evidence**:
  - `carta/install/bootstrap.py:27`: `_check_ollama("http://localhost:11434")`
  - Contrast with line 16: `qdrant_url = os.environ.get("CARTA_QDRANT_URL", ...)`
- **Proposed fix**: Use `os.environ.get("CARTA_OLLAMA_URL", DEFAULTS["embed"]["ollama_url"])`.
- **Status**: **fixed**

---

### M-2: Hardcoded vector dimension 768 in collection creation

- **Severity**: Medium
- **Component**: bootstrap
- **Symptom**: Collections are created with `size: 768`, which is specific to `nomic-embed-text`. If a user changes `embed.ollama_model` to a model with different dimensions (e.g. 1024 for `mxbai-embed-large`), inserts will fail with an opaque Qdrant error.
- **Evidence**:
  - `carta/install/bootstrap.py:114`: `"vectors": {"size": 768, "distance": "Cosine"}`
- **Proposed fix**: Make vector dimension configurable, or derive it from the model name via a lookup table.
- **Status**: **fixed**

---

### M-3: No response status check on Qdrant collection creation

- **Severity**: Medium
- **Component**: bootstrap
- **Symptom**: The HTTP response status from `requests.put()` is never checked. A 400 Bad Request (e.g. collection exists with different params) is silently treated as success. Only network-level exceptions are caught.
- **Evidence**:
  - `carta/install/bootstrap.py:111-118`
- **Proposed fix**: Check `r.status_code`, distinguish "already exists" (benign, 409) from real errors, log appropriately.
- **Status**: **fixed**

---

### M-4: `shutil.rmtree` follows symlinks — could destroy source code

- **Severity**: Medium
- **Component**: bootstrap
- **Symptom**: If `.carta/carta` is a symlink (common in development), `shutil.rmtree` follows it and recursively deletes the *target* directory.
- **Evidence**:
  - `carta/install/bootstrap.py:38-39`
- **Proposed fix**: Check `runtime_dest.is_symlink()` first and use `unlink()` for symlinks.
- **Status**: **fixed**

---

### M-5: `_deep_merge` shallow copy can corrupt `DEFAULTS` dict

- **Severity**: Medium (latent data corruption)
- **Component**: config
- **Symptom**: `dict(base)` is a shallow copy. Un-overridden nested sub-dicts still reference the original `DEFAULTS` objects. Any downstream mutation (e.g. `cfg["embed"]["chunking"]["max_tokens"] = 500`) corrupts `DEFAULTS` for all future calls in the same process.
- **Evidence**:
  - `carta/config.py:71-72`
- **Proposed fix**: Use `copy.deepcopy(base)` instead of `dict(base)`.
- **Status**: **fixed**

---

### M-6: No type/schema validation on config after merge

- **Severity**: Medium
- **Component**: config
- **Symptom**: After `_deep_merge`, there's no validation of the result shape. A user writing `embed: "oops"` (string instead of dict) passes config loading, then crashes later with a confusing `TypeError` on `cfg["embed"]["ollama_url"]`.
- **Evidence**:
  - `carta/config.py:60-64` — only checks `field in raw`, never validates types
- **Proposed fix**: Add basic type checks for critical nested dicts (`embed`, `modules`, `search` must be dicts; `project_name` and `qdrant_url` must be non-empty strings).
- **Status**: **fixed**

---

### M-7: `cmd_embed` and `cmd_scan` don't check if their module is enabled

- **Severity**: Medium
- **Component**: CLI
- **Symptom**: `cmd_search` checks `cfg["modules"].get("doc_search")` before proceeding, but `cmd_embed` and `cmd_scan` skip this check. A user who disables `doc_embed` or `doc_audit` in config can still run those commands.
- **Evidence**:
  - `carta/cli.py:38-47` (`cmd_embed`): no module check
  - `carta/cli.py:26-36` (`cmd_scan`): no module check
  - `carta/cli.py:52-53` (`cmd_search`): has module check
- **Proposed fix**: Add `if not cfg["modules"].get("doc_embed"):` / `"doc_audit"` guards to match `cmd_search`.
- **Status**: **fixed**

---

### M-8: `find_config` doesn't walk up parent directories

- **Severity**: Medium
- **Component**: CLI
- **Symptom**: Unlike `git` (which walks up ancestors), `find_config` only checks CWD. Running `carta scan` from a subdirectory (e.g. `cd src/ && carta scan`) fails with a confusing "not found" error.
- **Evidence**:
  - `carta/cli.py:17-24`
- **Proposed fix**: Walk up parent directories until `.carta/config.yaml` is found or filesystem root is reached.
- **Status**: **fixed**

---

### M-9: Hook scripts depend on system Python having `pyyaml`

- **Severity**: Medium
- **Component**: hooks
- **Symptom**: Both hooks run `python3 -c "import yaml, ..."`. If `python3` resolves to a different interpreter than where `carta-cc` is installed (common with `pyenv`, `conda`), `import yaml` fails. The `|| echo "false"` fallback silently disables modules that are actually enabled — with no diagnostic.
- **Evidence**:
  - `carta/hooks/carta-prompt-hook.sh:11-15`
  - `carta/hooks/carta-stop-hook.sh:11-15`
- **Proposed fix**: Use a flag file (`.carta/hooks-enabled`) instead of parsing YAML, or record the specific Python interpreter path during init.
- **Status**: **fixed** (replaced python3/pyyaml with pure-shell grep)

---

### M-10: Absolute hook paths in settings make config non-portable

- **Severity**: Medium
- **Component**: bootstrap
- **Symptom**: Hook paths are stored as absolute paths. If the project is moved, cloned by another user, or used in CI, the paths break.
- **Evidence**:
  - `carta/install/bootstrap.py:103-104`
- **Proposed fix**: Use paths relative to the project root.
- **Status**: **fixed** (hook commands use `git rev-parse --show-toplevel` wrapper for portable, CWD-drift-immune resolution)

---

### M-11: Uncaught `json.JSONDecodeError` in `_register_hooks`

- **Severity**: Medium
- **Component**: bootstrap
- **Symptom**: Corrupted `.claude/settings.json` (trailing commas, comments, empty file with whitespace) crashes the entire init with an unhelpful traceback.
- **Evidence**:
  - `carta/install/bootstrap.py:101`
- **Proposed fix**: Wrap in try/except, warn and start from empty dict if parse fails.
- **Status**: **fixed** (addressed as part of H-3/H-4 fix)

---

### M-12: `config.yaml.example` says "Copy this to carta.yaml" (wrong filename)

- **Severity**: Medium
- **Component**: docs / install
- **Symptom**: All code references `.carta/config.yaml`, not `carta.yaml`. A user following this instruction creates the file in the wrong location.
- **Evidence**:
  - `carta/install/config.yaml.example:3`
- **Proposed fix**: Change to "This template is used by `carta init` to generate `.carta/config.yaml`."
- **Status**: **fixed**

---

### M-13: `plugin.json` version drift — still at 0.1.0

- **Severity**: Medium
- **Component**: packaging
- **Symptom**: Both `plugin.json` (root) and `.claude-plugin/plugin.json` say `0.1.0`, while `pyproject.toml` and `__init__.py` say `0.1.2`. BUG-007 was marked fixed but only aligned the Python version sources.
- **Evidence**:
  - `plugin.json:3` and `.claude-plugin/plugin.json:3`: `"version": "0.1.0"`
  - `pyproject.toml:7` and `carta/__init__.py:1`: `0.1.2`
- **Proposed fix**: Update both `plugin.json` files to `0.1.2`.
- **Status**: **fixed**

---

### M-14: Broken reference in README to `docs/superpowers/specs/`

- **Severity**: Medium
- **Component**: docs
- **Symptom**: README references a design spec directory that does not exist.
- **Evidence**:
  - `README.md:176`: `See docs/superpowers/specs/ for the full design spec.`
- **Proposed fix**: Remove the reference or create the directory with the spec.
- **Status**: **fixed**

---

### M-15: `doc-embed` skill sidecar schema doesn't match actual code

- **Severity**: Medium
- **Component**: skills / docs
- **Symptom**: Skill shows sidecar fields `title` and `used_in`, but actual `generate_sidecar_stub()` produces `slug`, `indexed_at`, `chunk_count`, `collection`, `notes` — different field names entirely.
- **Evidence**:
  - `skills/doc-embed/SKILL.md:29-35`
  - `carta/embed/induct.py` — `generate_sidecar_stub()`
- **Proposed fix**: Update the skill to show the actual sidecar fields.
- **Status**: **fixed**

---

## Low — cleanup, hardening, best practices

### L-1: `copytree` copies `install/` into `.carta/carta/`

- **Severity**: Low
- **Component**: bootstrap
- **Symptom**: `ignore_patterns` omits `tests` and `__pycache__` but not `install/`. The install directory (including `bootstrap.py`, `install.sh`, `config.yaml.example`) ends up in the runtime copy unnecessarily.
- **Evidence**: `carta/install/bootstrap.py:40-41`
- **Proposed fix**: Add `"install"` to `ignore_patterns`.
- **Status**: **fixed**

---

### L-2: `.gitignore` too narrow — only adds `scan-results.json`

- **Severity**: Low
- **Component**: bootstrap
- **Symptom**: `.carta/carta/`, `.carta/hooks/`, and other generated content are not gitignored.
- **Evidence**: `carta/install/bootstrap.py:123`
- **Proposed fix**: Gitignore `.carta/` entirely, or add entries for each generated path.
- **Status**: **fixed**

---

### L-3: Substring match for gitignore idempotency check

- **Severity**: Low
- **Component**: bootstrap
- **Symptom**: `if entry in gitignore.read_text()` matches comments containing the entry string, causing the actual entry to never be added.
- **Evidence**: `carta/install/bootstrap.py:125`
- **Proposed fix**: Check line-by-line with `entry in gitignore.read_text().splitlines()`.
- **Status**: **fixed**

---

### L-4: `_detect_project_name` can return empty string

- **Severity**: Low
- **Component**: bootstrap
- **Symptom**: If `root` is `/` or git returns empty output, `Path("").name` / `Path("/").name` is `""`. Collection names become `:doc`, `:session`, `:quirk`.
- **Evidence**: `carta/install/bootstrap.py:49-59`
- **Proposed fix**: Validate non-empty and raise if empty.
- **Status**: **fixed** (falls back to "carta-project")

---

### L-5: `setuptools>=42` lower bound predates PEP 621 support

- **Severity**: Low
- **Component**: packaging
- **Symptom**: PEP 621 `[project]` table requires setuptools >= 61.0. A user with setuptools 42–60 would get a broken build.
- **Evidence**: `pyproject.toml:2`
- **Proposed fix**: Change to `setuptools>=61.0`.
- **Status**: **fixed**

---

### L-6: Dual version sources — manual sync required

- **Severity**: Low
- **Component**: packaging
- **Symptom**: Version is maintained in both `pyproject.toml:7` and `carta/__init__.py:1`. Must be kept in sync manually.
- **Proposed fix**: Use `[tool.setuptools.dynamic] version = {attr = "carta.__version__"}` and remove the static version from `[project]`.
- **Status**: **fixed**

---

### L-7: No dev/test dependencies declared

- **Severity**: Low
- **Component**: packaging
- **Symptom**: No `[project.optional-dependencies]` for dev/test extras. `pytest` is not declared anywhere.
- **Proposed fix**: Add `[project.optional-dependencies] dev = ["pytest>=7.0"]`.
- **Status**: **fixed**

---

### L-8: No `conftest.py` — shared test fixtures duplicated

- **Severity**: Low
- **Component**: tests
- **Symptom**: `MINIMAL_CONFIG` / `MINIMAL_CFG` / `_minimal_cfg()` are duplicated across three test files with different shapes. Config schema drift will cause silent inconsistencies.
- **Evidence**: `carta/tests/test_config.py`, `carta/embed/tests/test_embed.py`, `carta/scanner/tests/test_scanner.py`
- **Proposed fix**: Create a shared `conftest.py` with a canonical minimal config fixture.
- **Status**: **fixed**

---

### L-9: Missing direct tests for core functions

- **Severity**: Low
- **Component**: tests
- **Symptom**: Several functions are always mocked and never directly tested: `extract_pdf_text`, `get_embedding` error path, `ensure_collection` create branch, sidecar CRUD functions, `check_prototype_doc`.
- **Proposed fix**: Add unit tests that exercise these functions directly.
- **Status**: **fixed** (11 new tests: sidecar CRUD round-trip, get_embedding error path, ensure_collection create/skip, check_prototype_doc, extract_pdf_text with real pymupdf-generated PDFs)

---

### L-10: File I/O without explicit `encoding="utf-8"`

- **Severity**: Low
- **Component**: config / bootstrap
- **Symptom**: `write_text()` and `open()` calls rely on platform default encoding. On some Windows systems this could be `cp1252`.
- **Evidence**: `carta/config.py:55`, `carta/install/bootstrap.py:86`
- **Proposed fix**: Pass `encoding="utf-8"` explicitly.
- **Status**: **fixed** (addressed in M-6 config.py fix)

---

### L-11: `install.sh` dead code and pip check mismatch

- **Severity**: Low
- **Component**: install script
- **Symptom**: `REPO` and `DEST` variables are defined but never used. Script checks `command -v pip` but then runs `python3 -m pip` — these may reference different Pythons.
- **Evidence**: `carta/install/install.sh:4-5`, `carta/install/install.sh:8-9`
- **Proposed fix**: Remove dead variables. Change check to `python3 -m pip --version`.
- **Status**: **fixed**

---

### L-12: `doc-embed` skill sidecar schema shows wrong field names

- **Severity**: Low
- **Component**: skills
- **Symptom**: Skill shows `title` and `used_in` fields; actual code produces `slug`, `indexed_at`, `chunk_count`, `collection`, `notes`.
- **Evidence**: `skills/doc-embed/SKILL.md:29-35` vs `carta/embed/induct.py`
- **Proposed fix**: Update skill to reflect actual sidecar schema.
- **Status**: **fixed** (addressed in M-15 fix)

---

## Field Test — v0.1.2 install on `petsense` repo (2026-03-24)

Issues discovered during end-to-end install test on a real project.

### FT-1: Qdrant rejects `:` in collection names

- **Severity**: Critical (all collections fail to create)
- **Component**: config / bootstrap
- **Symptom**: Qdrant returns HTTP 422: `collection name cannot contain ":" char`. All three collections (`petsense:doc`, `petsense:session`, `petsense:quirk`) fail. `carta init` still prints "Carta ready" despite the failures.
- **Evidence**: Field test log — `Qdrant returned 422 for collection petsense:doc`
- **Proposed fix**: Change `collection_name()` separator from `:` to `_`. Also make `_create_qdrant_collections` fail loudly (or at least not print "Carta ready") when all collections fail.
- **Status**: **fixed** (separator changed to `_`; `_create_qdrant_collections` returns bool; `run_bootstrap` prints failure message when collections fail)

---

### FT-2: Skills not bundled in package

- **Severity**: High (core workflows broken)
- **Component**: packaging / skills
- **Symptom**: `/doc-audit`, `/doc-embed`, `/doc-search` skills are not included in the pip package or copied during `carta init`. The runtime code exists but there's no way to invoke the skills from Claude Code.
- **Evidence**: `find .carta -name "*.md"` returns nothing; `Skill(doc-audit)` → "Unknown skill"
- **Proposed fix**: Ensure skill files are bundled in the package and `carta init` installs them into `.claude/skills/` so Claude Code discovers `/doc-audit`, `/doc-embed`, and `/doc-search`.
- **Status**: **fixed** (skills are now packaged under `carta/skills/*/SKILL.md` and installed to `.claude/skills/` during `carta init`)

---

### FT-3: `pip install` fails on macOS with PEP 668

- **Severity**: Medium (install friction on default macOS Python)
- **Component**: docs
- **Symptom**: `python3 -m pip install carta-cc` fails on Homebrew Python 3.14 with `externally-managed-environment` error. This is now the default macOS experience.
- **Evidence**: Field test log — PEP 668 error from Homebrew Python
- **Proposed fix**: Update README and install guide to recommend `pipx install carta-cc` as the primary macOS install path. Add `uv tool install` as alternative.
- **Status**: **fixed** (README and install guide updated with pipx/uv recommendations and PEP 668 note)

---

### FT-4: Stop hook references nonexistent `/carta-save` skill

- **Severity**: Low (confusing output)
- **Component**: hooks
- **Symptom**: Stop hook prints "Use /carta-save to save this session to Carta memory." but `/carta-save` does not exist as a skill.
- **Evidence**: Field test log — hook output references unimplemented feature
- **Proposed fix**: Remove the message until the skill is implemented, or replace with a generic reminder.
- **Status**: **fixed** (removed premature `/carta-save` reference from stop hook)

---

## Field Test — v0.1.10 install on `petsense` repo (2026-03-24)

Issues discovered during end-to-end install test walkthrough of `install-test-guide.md`. Full findings at `docs/testing/install-test-findings-v0.1.10.md`.

### FT-5: `carta embed` has no concurrency lock — machine crash risk

- **Severity**: High (crash risk)
- **Component**: embed
- **Symptom**: `carta embed` has no lock file or mutex. Multiple simultaneous invocations each load PDFs and call Ollama independently. During testing, 5–6 concurrent processes exhausted ~180GB RAM and crashed the host machine.
- **Root cause**: An install agent spawned parallel sub-agents to run the embed skill; each sub-agent independently launched `carta embed`.
- **Proposed fix**: Add a lock file (e.g. `.carta/embed.lock`) at pipeline start. If lock exists, print "carta embed is already running (PID: X). Exiting." and exit non-zero. Remove lock on exit via trap.
- **Status**: open

---

### FT-6: `carta embed` hangs indefinitely when Qdrant is unreachable

- **Severity**: High (blocking)
- **Component**: embed
- **Symptom**: `carta embed` hangs with no stdout or stderr — not even a startup message. 120s, 180s, and 600s timeouts all expire. All isolated components (imports, fitz, rglob, Ollama calls) work fine individually.
- **Likely root cause**: Docker was not running at test time, so the Qdrant container was down. QdrantClient initialization appears to block indefinitely (no timeout) when the host is unreachable, rather than failing fast. The machine crash-and-restart from FT-5 is the probable reason Docker was not running.
- **Proposed fix** (two parts):
  1. Add a pre-flight Qdrant connectivity check at the top of `cmd_embed` — same pattern as `carta init` readiness checks — and exit with a clear error if Qdrant is unreachable.
  2. Add `flush=True` progress prints at each stage of `run_embed` so any future hang is immediately locatable.
- **Note**: The hang may be fully explained by Qdrant being down. Re-test with Docker running before deeper investigation.
- **Status**: open

---

### FT-7: `carta embed` produces zero progress output

- **Severity**: Medium (UX / diagnosability)
- **Component**: embed
- **Symptom**: `run_embed` prints nothing until the entire pipeline completes, then prints one line. Large repos show a blank terminal for minutes with no indication of progress or hang. This also made FT-6 undiagnosable.
- **Proposed fix**: Add per-file progress lines and per-file chunk counts to `run_embed` and `upsert_chunks`. E.g. `Embedding docs/reference/foo.pdf (72 chunks)... ✓ 72 chunks in 34s`.
- **Status**: open

---

### FT-8: `pipx upgrade carta-cc` stops at 0.1.9, does not reach 0.1.10

- **Severity**: Medium
- **Component**: packaging
- **Symptom**: `pipx upgrade carta-cc` from 0.1.7 upgrades to 0.1.9, not 0.1.10 (latest on PyPI). Confirmed 0.1.10 was available. Required `pipx install carta-cc==0.1.10 --force`.
- **Proposed fix**: Investigate whether 0.1.10 package metadata causes pipx to skip it — check `python_requires`, yanked flag, or pre-release marker misdetection.
- **Status**: open

---

### FT-9: Skills load from old cached version after upgrade

- **Severity**: Medium
- **Component**: skills / packaging
- **Symptom**: After `pipx install carta-cc==0.1.10 --force` and `carta init` (which reported "Registered 4 Carta skill(s)… v0.1.10"), both `/doc-audit` and `/doc-embed` loaded from the 0.1.6 cache directory. Stale skill logic used silently.
- **Proposed fix**: `carta init` should verify the skill path in `installed_plugins.json` matches the installed version and warn/overwrite if stale. Clean up old version directories during init.
- **Status**: open

---

### FT-10: `carta init` fires a false PATH warning for valid pipx binary

- **Severity**: Low
- **Component**: bootstrap
- **Symptom**: `carta init` warns "carta found on PATH at /Users/ian/.local/bin/carta does not match the running interpreter" — but that binary IS the correct pipx-installed carta. The real PlatformIO conflict was correctly detected at install time.
- **Proposed fix**: Tighten PATH conflict check to only warn when the resolved `carta` binary points to a known-bad path (e.g. `.platformio`), not whenever the pipx venv Python and the resolved binary differ.
- **Status**: open

---

### FT-11 (UX): Agent install guide pause scope is too broad

- **Severity**: Low (friction)
- **Component**: docs / install-test-guide
- **Symptom**: The `⚠️ AGENT PAUSE` note after Step 2 says "Do not continue with Steps 3–8." But Steps 3 (config review) and 4 (`carta scan`) require no skills and work fine in the same session. Only Steps 5–8 need newly registered skills.
- **Proposed fix**: Change pause scope to "Do not continue with Steps 5–8 in this session."
- **Status**: open

---

### FT-12 (UX): 20 `missing_frontmatter` TRIAGE entries flood backlog on fresh repo

- **Severity**: Low (friction)
- **Component**: doc-audit skill
- **Symptom**: On a repo with no Carta frontmatter, `/doc-audit` creates one TRIAGE entry per doc (20 entries on first run). This overwhelms the backlog and makes actionable issues (homeless_doc, embed_induction_needed) hard to find.
- **Proposed fix**: Group `missing_frontmatter` issues into a single TRIAGE entry with a doc list, or mark them lower priority / bulk-actionable.
- **Status**: open
