# Stale-reference diff-range scan — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a local, on-demand `carta hook check --diff [range]` mode that scans every doc changed across a git range (whole branch vs base) against the knowledge graph, reusing the slice-1 stale-scan core, with a `--fail-on-stale` opt-in exit code.

**Architecture:** Factor the file-collection inner loop out of `collect_pushed` into a shared `_collect_from_ranges` helper; add a sibling `collect_range(repo_root, cfg, range_spec)` collector (deriving the range tip via `_range_tip`). Wire two new flags (`--diff`, `--fail-on-stale`) onto the existing `carta hook check` subcommand and branch the collection logic accordingly. Everything else (scan core, judge, config, plain reporter) is reused unchanged; fail-open is preserved everywhere.

**Tech Stack:** Python 3.10+, argparse, `subprocess` (git), pytest + `unittest.mock`.

**Spec:** `docs/superpowers/specs/2026-06-16-stale-reference-diff-scan-design.md`

---

## File structure

| File | Responsibility |
|------|----------------|
| `carta/hook/stale_scan.py` (modify) | Extract `_collect_from_ranges`; add `_range_tip` + `collect_range`; rewire `collect_pushed` to the shared helper |
| `carta/hook/tests/test_stale_scan.py` (append) | Tests for `_range_tip` and `collect_range` (real temp git repo) |
| `carta/cli.py` (modify) | `--diff` / `--fail-on-stale` on the `check` subparser; diff-aware collection + exit in `cmd_hook` |
| `carta/tests/test_cli.py` (append) | Tests for `cmd_hook` `--diff` collection + exit codes |
| `CLAUDE.md` (modify) | Note the `--diff` diff-range audit on the hook surface |
| `docs/superpowers/specs/2026-06-16-stale-reference-diff-scan-design.md` (modify) | `status: draft` → `status: shipped` |

**Run the whole suite with:** `python -m pytest carta/ -q`

---

## Task 1: `collect_range` + shared `_collect_from_ranges` refactor

The slice-1 `collect_pushed` (in `carta/hook/stale_scan.py`) already contains the exact
inner loop we need: diff a range, scope-filter, read each path at the range tip via
`git show <tip>:<path>`, dedupe by path. Extract that loop into a shared helper, rewire
`collect_pushed` to call it (behavior-preserving), then add `_range_tip` + `collect_range`.

**Files:**
- Modify: `carta/hook/stale_scan.py` (the collectors region, ~lines 134-167)
- Test: `carta/hook/tests/test_stale_scan.py` (append)

- [ ] **Step 1: Write the failing tests**

Append to `carta/hook/tests/test_stale_scan.py`:

```python
from carta.hook.stale_scan import _range_tip, collect_range


def test_range_tip_parses_two_and_three_dot():
    assert _range_tip("a..b") == "b"
    assert _range_tip("a...b") == "b"
    assert _range_tip("main...HEAD") == "HEAD"
    assert _range_tip("main...") == "HEAD"      # trailing-empty right side
    assert _range_tip("origin/main..feature") == "feature"


def test_collect_range_returns_scoped_docs_at_tip(repo):
    # repo fixture (defined earlier in this file) is a fresh git repo with docs/.
    (repo / "docs" / "a.md").write_text("v1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "c1")
    base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True).stdout.strip()
    (repo / "docs" / "a.md").write_text("## A\nv2 changed\n")
    (repo / "docs" / "b.md").write_text("## B\nbrand new\n")
    (repo / "src.py").write_text("print()\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "c2")
    cfg = {"docs_root": "docs/", "excluded_paths": []}
    docs = collect_range(repo, cfg, f"{base}...HEAD")
    paths = sorted(d.path for d in docs)
    assert paths == ["docs/a.md", "docs/b.md"]     # src.py excluded by scope
    by_path = {d.path: d.text for d in docs}
    assert "v2 changed" in by_path["docs/a.md"]     # tip content, not v1
    assert "brand new" in by_path["docs/b.md"]


def test_collect_range_two_dot_range(repo):
    (repo / "docs" / "a.md").write_text("v1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "c1")
    base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True).stdout.strip()
    (repo / "docs" / "a.md").write_text("## A\nv2\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "c2")
    cfg = {"docs_root": "docs/", "excluded_paths": []}
    docs = collect_range(repo, cfg, f"{base}..HEAD")
    assert [d.path for d in docs] == ["docs/a.md"]
    assert "v2" in docs[0].text
```

> These tests reuse the `repo` pytest fixture and the `_git` helper already defined in
> `test_stale_scan.py` by the slice-1 collector tests (Task 7). Do NOT redefine them.
> `subprocess` is already imported at the top of the test file.

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest carta/hook/tests/test_stale_scan.py -k "range_tip or collect_range" -v`
Expected: FAIL with `ImportError: cannot import name '_range_tip'` (or `collect_range`).

- [ ] **Step 3: Refactor + implement**

In `carta/hook/stale_scan.py`, replace the body of `collect_pushed` (currently lines
~134-167) so its final collection loop is extracted into a shared helper, and add the
new `_range_tip` + `collect_range`. The full replacement for the region from
`def collect_pushed` through the end of that function:

```python
def collect_pushed(repo_root: Path, cfg: dict, stdin_lines: list[str]) -> list[ChangedDoc]:
    ranges: list[tuple[str, str]] = []  # (range_spec, tip_sha)
    for line in stdin_lines:
        parts = line.split()
        if len(parts) < 4:
            continue
        local_sha, remote_sha = parts[1], parts[3]
        if set(local_sha) == {"0"}:
            continue  # branch deletion — nothing to scan
        if set(remote_sha) == {"0"}:
            rng = _new_branch_range(repo_root, local_sha)
        else:
            rng = f"{remote_sha}..{local_sha}"
        ranges.append((rng, local_sha))

    if not stdin_lines:  # manual invocation — scan default-branch..HEAD
        ranges = [(f"{_default_branch(repo_root)}..HEAD", "HEAD")]

    return _collect_from_ranges(repo_root, cfg, ranges)


def _collect_from_ranges(
    repo_root: Path, cfg: dict, ranges: list[tuple[str, str]]
) -> list[ChangedDoc]:
    """Collect in-scope changed docs across one or more (range_spec, tip_sha) pairs.

    For each range, list ACM-changed paths, keep only in-scope docs, and read each
    doc's content at that range's tip via `git show <tip>:<path>`. Deduped by path
    (first range wins). Fails open per range and per file."""
    seen: dict[str, ChangedDoc] = {}
    for rng, tip in ranges:
        try:
            out = _git(repo_root, "diff", "--name-only", "--diff-filter=ACM", rng)
        except subprocess.CalledProcessError:
            continue
        for rel in out.splitlines():
            rel = rel.strip()
            if not rel or rel in seen or not _in_doc_scope(rel, cfg, repo_root):
                continue
            try:
                text = _git(repo_root, "show", f"{tip}:{rel}")
            except subprocess.CalledProcessError:
                continue
            seen[rel] = ChangedDoc(path=rel, text=text)
    return list(seen.values())


def _range_tip(range_spec: str) -> str:
    """Right operand of a git range (`A..B` / `A...B` → `B`); empty right side or a
    bare ref → `HEAD`. Used to read changed-file content at the tip of the range."""
    for sep in ("...", ".."):
        if sep in range_spec:
            right = range_spec.split(sep, 1)[1].strip()
            return right or "HEAD"
    return range_spec.strip() or "HEAD"


def collect_range(repo_root: Path, cfg: dict, range_spec: str) -> list[ChangedDoc]:
    """Collect in-scope docs changed across an explicit git range, read at the range
    tip. Used by the local on-demand pre-PR diff scan (`carta hook check --diff`)."""
    return _collect_from_ranges(repo_root, cfg, [(range_spec, _range_tip(range_spec))])
```

- [ ] **Step 4: Run tests to verify they pass (new + existing collectors)**

Run: `python -m pytest carta/hook/tests/test_stale_scan.py -v`
Expected: PASS — the new `_range_tip`/`collect_range` tests pass AND every existing
slice-1 test (including the three `collect_pushed`/`collect_staged` tests) stays green,
proving the refactor is behavior-preserving.

- [ ] **Step 5: Commit**

```bash
git add carta/hook/stale_scan.py carta/hook/tests/test_stale_scan.py
git commit -m "feat(hook): collect_range + shared range-collection helper (#10 slice 2)"
```

End the commit message with a blank line then:
`Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`

---

## Task 2: CLI `--diff` + `--fail-on-stale`

**Files:**
- Modify: `carta/cli.py` (the `check` subparser registration; the `action == "check"` branch of `cmd_hook`, ~lines 846-878)
- Test: `carta/tests/test_cli.py` (append)

- [ ] **Step 1: Write the failing tests**

Append to `carta/tests/test_cli.py` (the file already has `import pytest` and the
slice-1 cmd_hook tests):

```python
def test_cmd_hook_check_diff_uses_collect_range(tmp_path, monkeypatch, capsys):
    import carta.cli as cli
    from carta.hook.stale_scan import StaleFinding, StaleScanResult

    cfg_path = tmp_path / ".carta" / "config.yaml"
    cfg_path.parent.mkdir(parents=True)
    cfg_path.write_text("")
    cfg = {"hooks": {"stale_scan": {"enabled": True, "block_on_stale": False}}}
    monkeypatch.setattr(cli, "find_config", lambda: cfg_path)
    monkeypatch.setattr("carta.config.load_config", lambda p: cfg)

    captured = {}
    def fake_collect_range(repo_root, c, range_spec):
        captured["range_spec"] = range_spec
        return [object()]
    monkeypatch.setattr("carta.hook.stale_scan.collect_range", fake_collect_range)
    # If collect_range is wrongly bypassed, these would blow up the test:
    monkeypatch.setattr("carta.hook.stale_scan.collect_staged", lambda r, c: (_ for _ in ()).throw(AssertionError("collect_staged should not be called")))
    monkeypatch.setattr("carta.hook.stale_scan.collect_pushed", lambda r, c, s: (_ for _ in ()).throw(AssertionError("collect_pushed should not be called")))
    result = StaleScanResult(findings=[StaleFinding("docs/a.md", "## A", "snip", "docs/b.md", 0.9)], scanned=1)
    monkeypatch.setattr("carta.hook.stale_scan.run_stale_scan", lambda r, c, d: result)

    args = type("A", (), {"command": "hook", "hook_action": "check", "stage": "pre-push",
                          "diff": "origin/main...HEAD", "fail_on_stale": False})()
    with pytest.raises(SystemExit) as exc:
        cli.cmd_hook(args)
    assert exc.value.code == 0                         # warn-only default
    assert captured["range_spec"] == "origin/main...HEAD"
    assert "may be stale" in capsys.readouterr().err


def test_cmd_hook_check_diff_default_range(tmp_path, monkeypatch):
    import carta.cli as cli
    from carta.hook.stale_scan import StaleScanResult

    cfg_path = tmp_path / ".carta" / "config.yaml"
    cfg_path.parent.mkdir(parents=True)
    cfg_path.write_text("")
    cfg = {"hooks": {"stale_scan": {"enabled": True, "block_on_stale": False}}}
    monkeypatch.setattr(cli, "find_config", lambda: cfg_path)
    monkeypatch.setattr("carta.config.load_config", lambda p: cfg)

    captured = {}
    def fake_collect_range(repo_root, c, range_spec):
        captured["range_spec"] = range_spec
        return [object()]
    monkeypatch.setattr("carta.hook.stale_scan.collect_range", fake_collect_range)
    monkeypatch.setattr("carta.hook.stale_scan.run_stale_scan", lambda r, c, d: StaleScanResult(scanned=1))

    # bare --diff arrives as "" (argparse const); handler must resolve the default range.
    args = type("A", (), {"command": "hook", "hook_action": "check", "stage": "pre-push",
                          "diff": "", "fail_on_stale": False})()
    with pytest.raises(SystemExit) as exc:
        cli.cmd_hook(args)
    assert exc.value.code == 0
    # repo_root (tmp_path) is not a git repo → _default_branch falls back to "main"
    assert captured["range_spec"] == "main...HEAD"


def test_cmd_hook_check_diff_fail_on_stale_exits_one(tmp_path, monkeypatch):
    import carta.cli as cli
    from carta.hook.stale_scan import StaleFinding, StaleScanResult

    cfg_path = tmp_path / ".carta" / "config.yaml"
    cfg_path.parent.mkdir(parents=True)
    cfg_path.write_text("")
    cfg = {"hooks": {"stale_scan": {"enabled": True, "block_on_stale": False}}}
    monkeypatch.setattr(cli, "find_config", lambda: cfg_path)
    monkeypatch.setattr("carta.config.load_config", lambda p: cfg)
    monkeypatch.setattr("carta.hook.stale_scan.collect_range", lambda r, c, rng: [object()])
    result = StaleScanResult(findings=[StaleFinding("docs/a.md", "## A", "s", "docs/b.md", 0.9)], scanned=1)
    monkeypatch.setattr("carta.hook.stale_scan.run_stale_scan", lambda r, c, d: result)

    args = type("A", (), {"command": "hook", "hook_action": "check", "stage": "pre-push",
                          "diff": "origin/main...HEAD", "fail_on_stale": True})()
    with pytest.raises(SystemExit) as exc:
        cli.cmd_hook(args)
    assert exc.value.code == 1                          # --fail-on-stale + findings


def test_cmd_hook_check_diff_no_findings_exits_zero(tmp_path, monkeypatch):
    import carta.cli as cli
    from carta.hook.stale_scan import StaleScanResult

    cfg_path = tmp_path / ".carta" / "config.yaml"
    cfg_path.parent.mkdir(parents=True)
    cfg_path.write_text("")
    cfg = {"hooks": {"stale_scan": {"enabled": True, "block_on_stale": False}}}
    monkeypatch.setattr(cli, "find_config", lambda: cfg_path)
    monkeypatch.setattr("carta.config.load_config", lambda p: cfg)
    monkeypatch.setattr("carta.hook.stale_scan.collect_range", lambda r, c, rng: [object()])
    monkeypatch.setattr("carta.hook.stale_scan.run_stale_scan", lambda r, c, d: StaleScanResult(findings=[], scanned=1))

    args = type("A", (), {"command": "hook", "hook_action": "check", "stage": "pre-push",
                          "diff": "origin/main...HEAD", "fail_on_stale": True})()
    with pytest.raises(SystemExit) as exc:
        cli.cmd_hook(args)
    assert exc.value.code == 0                          # no findings → exit 0 even with --fail-on-stale
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest carta/tests/test_cli.py -k "cmd_hook_check_diff" -v`
Expected: FAIL — `AttributeError`/`TypeError` because `cmd_hook` doesn't yet read
`args.diff` / `args.fail_on_stale` and still calls `collect_staged`/`collect_pushed`.

- [ ] **Step 3: Add the two subparser flags**

In `carta/cli.py`, find the `check` subparser registration (the line
`hook_check = hook_sub.add_parser("check", ...)` followed by its
`hook_check.add_argument("--stage", ...)`). Add immediately after the `--stage` line:

```python
    hook_check.add_argument(
        "--diff", nargs="?", const="", default=None, metavar="RANGE",
        help="Scan docs changed across a git range instead of staged/pushed "
             "(bare --diff uses <default-branch>...HEAD)",
    )
    hook_check.add_argument(
        "--fail-on-stale", action="store_true",
        help="Exit 1 if any stale finding (default: warn-only, exit 0)",
    )
```

- [ ] **Step 4: Branch the collection + exit logic in `cmd_hook`**

In the `action == "check"` branch of `cmd_hook`, replace the collection `try/except`
block AND the final exit block. Replace this current code:

```python
        stage = args.stage
        try:
            if stage == "pre-commit":
                docs = stale_scan.collect_staged(repo_root, cfg)
            else:
                stdin_lines = [] if sys.stdin.isatty() else sys.stdin.read().splitlines()
                docs = stale_scan.collect_pushed(repo_root, cfg, stdin_lines)
        except Exception as e:
            print(f"carta stale-scan: collection error (fail-open): {e}", file=sys.stderr)
            sys.exit(0)
        if not docs:
            sys.exit(0)
        try:
            result = stale_scan.run_stale_scan(repo_root, cfg, docs)
        except Exception as e:
            print(f"carta stale-scan: scan error (fail-open): {e}", file=sys.stderr)
            sys.exit(0)
        _print_stale_result(result, scfg)
        if result.findings and scfg.get("block_on_stale", False):
            sys.exit(1)
        sys.exit(0)
```

with:

```python
        stage = args.stage
        diff = getattr(args, "diff", None)
        try:
            if diff is not None:
                range_spec = diff or f"{stale_scan._default_branch(repo_root)}...HEAD"
                docs = stale_scan.collect_range(repo_root, cfg, range_spec)
            elif stage == "pre-commit":
                docs = stale_scan.collect_staged(repo_root, cfg)
            else:
                stdin_lines = [] if sys.stdin.isatty() else sys.stdin.read().splitlines()
                docs = stale_scan.collect_pushed(repo_root, cfg, stdin_lines)
        except Exception as e:
            print(f"carta stale-scan: collection error (fail-open): {e}", file=sys.stderr)
            sys.exit(0)
        if not docs:
            sys.exit(0)
        try:
            result = stale_scan.run_stale_scan(repo_root, cfg, docs)
        except Exception as e:
            print(f"carta stale-scan: scan error (fail-open): {e}", file=sys.stderr)
            sys.exit(0)
        _print_stale_result(result, scfg)
        fail = getattr(args, "fail_on_stale", False) or scfg.get("block_on_stale", False)
        if result.findings and fail:
            sys.exit(1)
        sys.exit(0)
```

> `diff or <default>` resolves bare `--diff` (argparse `const=""`, falsy) to the default
> range while keeping an explicit value. `stale_scan._default_branch` is module-qualified
> (same module already imported as `stale_scan`). Collectors stay module-qualified so the
> tests' monkeypatches bind.

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest carta/tests/test_cli.py -k "cmd_hook" -v`
Expected: PASS — the 4 new diff tests AND the slice-1 cmd_hook tests (no regression).

- [ ] **Step 6: Confirm argparse wiring**

Run: `python -m carta hook check --help`
Expected: help text lists `--diff [RANGE]` and `--fail-on-stale`.

- [ ] **Step 7: Commit**

```bash
git add carta/cli.py carta/tests/test_cli.py
git commit -m "feat(cli): carta hook check --diff / --fail-on-stale (#10 slice 2)"
```

End the commit message with a blank line then:
`Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`

---

## Task 3: Docs + full-suite verification + smoke

**Files:**
- Modify: `CLAUDE.md` (Hook section)
- Modify: `docs/superpowers/specs/2026-06-16-stale-reference-diff-scan-design.md` (status → shipped)

- [ ] **Step 1: Update CLAUDE.md hook surface**

In `CLAUDE.md`, find the paragraph (added in slice 1) that begins "A second, opt-in hook
— `carta hook` —" and append this sentence to the end of that paragraph:

```markdown
Run on demand as a whole-branch pre-PR audit with `carta hook check --diff [range]`
(default range `<default-branch>...HEAD`; `--fail-on-stale` to exit non-zero).
```

- [ ] **Step 2: Mark the spec shipped**

In `docs/superpowers/specs/2026-06-16-stale-reference-diff-scan-design.md`, change the
frontmatter `status: draft` to `status: shipped`.

- [ ] **Step 3: Run the FULL suite**

Run: `python -m pytest carta/ -q`
Expected: PASS — slice-1 baseline was 970 passed / 1 skipped; expect 970 + the new
Task 1 & 2 tests, 0 failures, 1 skip.

- [ ] **Step 4: Manual smoke (no graph required)**

Run:
```bash
cd /tmp && rm -rf cartadifftest && mkdir cartadifftest && cd cartadifftest && git init -q
# In a non-Carta repo, --diff check must fail open (exit 0):
carta hook check --diff origin/main...HEAD; echo "exit: $?"
```
Expected: exit 0 (no Carta config → fail-open). `carta hook check --help` shows the new
flags. (The real scan path needs a Carta-initialised repo with Qdrant/Ollama; that is
covered by the unit tests and is the maintainer's corpus validation, not this commit.)

- [ ] **Step 5: Commit**

```bash
git add CLAUDE.md docs/superpowers/specs/2026-06-16-stale-reference-diff-scan-design.md
git commit -m "docs(hook): document carta hook check --diff; mark spec shipped (#10 slice 2)"
```

End the commit message with a blank line then:
`Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`

---

## Self-review notes (for the implementer)

- **Spec coverage:** `--diff [range]` default-range resolution (Task 2 Steps 3-4); `collect_range` + shared loop (Task 1); `--fail-on-stale` / `block_on_stale` exit (Task 2 Step 4); no-regression for non-`--diff` `check` (slice-1 cmd_hook tests stay green); fail-open preserved (the collection/scan try/excepts are untouched in structure); docs + spec-shipped (Task 3). All spec sections map to a task.
- **Type/interface consistency:** `collect_range(repo_root, cfg, range_spec)`; `_collect_from_ranges(repo_root, cfg, ranges: list[(rng, tip)])`; `_range_tip(range_spec) -> str`. `cmd_hook` calls `stale_scan.collect_range(repo_root, cfg, range_spec)` and `stale_scan._default_branch(repo_root)` — both real symbols in `stale_scan.py`.
- **No core changes:** `run_stale_scan`, `_stale_judge`, `sections_from_markdown`, `chunk_text`, `StaleFinding`, `_print_stale_result`, and the `hooks.stale_scan` config block are all untouched.
- **Monkeypatch binding:** all collectors/scan are referenced module-qualified (`stale_scan.X`) so the Task 2 tests bind, consistent with slice 1.
