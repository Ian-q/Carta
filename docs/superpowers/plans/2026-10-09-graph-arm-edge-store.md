# Graph Arm: Edge Store and Mention Edges — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give Carta a deterministic second arm: a SQLite edge store, a pure doc→source mention extractor, `carta impact` (CLI + MCP), a `carta scan` finding for mentions of vanished paths, and a leak-free PR-replay eval with two baselines (#129), on a schema that #130 and #131 can write into unchanged.

**Spec (read it first, it is the contract):** [`../specs/2026-10-09-graph-arm-edge-store-design.md`](../specs/2026-10-09-graph-arm-edge-store-design.md). Rule numbers (N1–N14), the SQL, the ranking formula and every JSON shape are defined there and are not repeated here.

**Architecture:** New package `carta/graph/`. Extraction is two pure stages (`scan_text` over text, `resolve_candidates` against a path index) composed by `extract_mentions`. `EdgeStore` wraps one SQLite file. `rank_impact` is a pure function over an in-memory `ImpactIndex`, used by both the CLI and the eval. The eval reads history through git plumbing only.

**Tech stack:** Python 3.10+, stdlib (`sqlite3`, `subprocess`, `re`, `json`, `hashlib`, `pathlib`, `posixpath`, `random`), PyYAML, pytest.

## Amendments after review (these override the task text below)

The spec was amended after an adversarial review (its *Review amendments* table, A1 to A18).
Where a task below disagrees with the spec, the spec wins. The differences that change a task:

- **Imports (Tasks 0, 2, 8):** `carta.scanner.scanner` is NOT an allowed import; `is_excluded_rel` is reimplemented in `paths.py` and parity-tested. `carta.registry` is allowed.
- **Model and store (Tasks 0, 6):** `Edge.key`; `UNIQUE (kind, src, dst, line, key)`; `sources.meta`; `MENTION_EXTRACTOR_VERSION` (per extractor); `edges_digest(extractors)`; a newer schema raises `StoreVersionError` instead of being deleted; `isolation_level=None`. The contract test inserts two `symbol` rows on one doc line to one file.
- **Paths (Task 2):** `PathIndex` is mutable (`add`, `remove`) and dict-indexed.
- **Extractor (Tasks 3 to 5):** call spans with arguments are single candidates; N1 percent-decodes links; N2 cuts `::name`; N8 tries the doc's directory and its ancestors; N13 no longer needs a known top-level directory and matches placeholders as whole segments. `nope/x.py` now gives a `missing` edge.
- **Build (Task 8):** nested-repo entries and `.carta/` are never in the path set; the budget test uses realistic doc sizes.
- **Query and CLI (Tasks 9, 10):** per-target aggregation of dir/glob matches; `PATH` and `--diff` are mutually exclusive.
- **Scan (Task 12):** gated `broken_link`; directory evidence; `--relative`.
- **Eval (Tasks 13 to 15, 17):** hubs over the eligible population and removed before truncation; `basename_idf` baseline; stateful providers with `observe`; doc-side-only exclusion; cache under the carta home with a tokenizer version; `--last 0` default; minimum population and "inconclusive"; a second public repository for headline numbers.

## Global constraints

- **Work only in this worktree**, with a Python that has an editable install of it plus pytest. Full suite:
  `python -m pytest carta/ -q -p no:cacheprovider` (baseline 1481 passed, 2 skipped). It stays green after every task.
- **TDD.** Each task: write the listed tests, run them and see them fail for the right reason, implement, see them pass, run the package's tests, commit. Do not write code that no test asked for.
- **No new dependencies. Python 3.10 only:** no `tomllib`, `typing.Self`, `datetime.UTC`, `itertools.batched`, `except*`, or parenthesised-context-manager-only idioms that 3.10 rejects.
- **Import hygiene.** Nothing under `carta/graph/` may import `qdrant_client`, `fastembed`, `torch`, `requests`, `carta.embed.*` or `carta.search.*`. Allowed inside the package: stdlib, `yaml`, `carta.config`, `carta.registry`. (Test modules may import more when a test is about parity.)
- **Determinism.** Every list returned or written is explicitly sorted. No `set` iteration order reaches output. No timestamps, absolute paths, hostnames or mtimes in stored rows or reports. Floats rounded only at output.
- **Tests are offline.** No network, Qdrant, Ollama, or model. Git tests use the `git_repo` fixture from Task 0 and are skipped when `git` is not on PATH.
- **Public-repo hygiene.** Fixtures are invented and generic (`src/parser/frame.py`, `docs/design/parser.md`). No content, names or paths from any private project, in code, tests, docs or commit messages.
- **Style.** Match the codebase: `snake_case`, type hints on signatures, docstrings on public functions, errors to stderr, `Path` for filesystem paths and `str` for repo-relative POSIX paths.
- **Commits.** Explicit paths only (never `git add -A` or `git add .`). Conventional-commit subjects. End every message with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`. If a commit hangs on a hook, retry with `--no-verify`. Do not push; do not open a PR.

## File structure

Create:

```
carta/graph/__init__.py   model.py   settings.py   paths.py   markdown.py   idents.py
carta/graph/mentions.py   store.py   gitio.py      build.py   query.py      scan.py
carta/graph/eval/__init__.py   metrics.py   baselines.py   replay.py
carta/graph/tests/__init__.py  conftest.py
carta/graph/tests/test_import_hygiene.py  test_settings.py  test_paths.py  test_markdown.py
carta/graph/tests/test_idents.py  test_mentions.py  test_store.py  test_gitio.py  test_build.py
carta/graph/tests/test_query.py  test_cli_graph.py  test_scan_missing.py
carta/graph/tests/test_metrics.py  test_baselines.py  test_replay.py
```

Modify: `carta/config.py`, `carta/cli.py`, `carta/mcp/server.py`, `carta/scanner/scanner.py`,
`carta/install/bootstrap.py` (gitignore line), `.gitignore`, `README.md`, `CHANGELOG.md`,
`docs/ROADMAP.md`, `CLAUDE.md` (surface tables), `carta/tests/test_config.py`,
`carta/tests/test_mcp_server.py`.

---

## Phase 0 — Scaffolding

### Task 0: Package, constants, git fixture, import-hygiene guard

**Files:** `carta/graph/__init__.py`, `carta/graph/model.py`, `carta/graph/eval/__init__.py`, `carta/graph/tests/__init__.py`, `carta/graph/tests/conftest.py`, `carta/graph/tests/test_import_hygiene.py`

**Interfaces produced:**
- `model.py`: `SCHEMA_VERSION = 1`, `EXTRACTOR_VERSION = 1`, `KINDS = ("mention", "ident", "pattern", "cochange", "symbol", "related")`, `DST_TYPES = ("file", "doc", "dir", "glob", "missing", "token", "pattern")`, `canonical_meta(d: dict) -> str`, and the frozen dataclasses `Edge` and `Candidate` exactly as in the spec.
- `conftest.py`: fixture `git_repo(tmp_path)` returning a helper with `.root`, `.write(rel, text)`, `.rm(rel)`, `.commit(message, date_index) -> sha`, `.git(*args) -> str`. It runs git with `GIT_AUTHOR_NAME/EMAIL`, `GIT_COMMITTER_NAME/EMAIL` fixed, `GIT_AUTHOR_DATE` and `GIT_COMMITTER_DATE` set to `2020-01-01T00:00:00Z` plus `date_index` days, `GIT_CONFIG_GLOBAL` and `GIT_CONFIG_SYSTEM` pointing at an empty file, `init -q -b main`, and `-c commit.gpgsign=false -c core.hooksPath=/dev/null`. Module-level `requires_git = pytest.mark.skipif(shutil.which("git") is None, ...)`.

**Tests (`test_import_hygiene.py`):**
- `test_importing_graph_package_pulls_no_vector_stack`: run `sys.executable -c` in a subprocess that imports every `carta.graph` submodule that exists (walk with `pkgutil`), then prints the sorted intersection of `sys.modules` with the forbidden roots `qdrant_client`, `fastembed`, `torch`, `requests`, `carta.embed`, `carta.search`; assert it is empty.
- `test_edge_is_ordered_and_hashable`, `test_canonical_meta_is_sorted_and_compact`.
- `test_git_repo_fixture_is_reproducible` (`@requires_git`): two repos built with the same writes and commits have the same HEAD sha.

- [ ] Write tests, watch them fail (package missing)
- [ ] Implement `model.py`, empty `__init__` modules, `conftest.py`
- [ ] Tests pass; full suite green
- [ ] Commit: `feat(graph): package skeleton, edge model and import-hygiene guard (#129)`

The hygiene test is extended in Tasks 6 and 8 to also run a build and a query in the subprocess.

### Task 1: The `graph` config section

**Files:** `carta/config.py`, `carta/graph/settings.py`, `carta/tests/test_config.py`, `carta/graph/tests/test_settings.py`

**Interfaces produced:**
- `DEFAULTS["graph"]` exactly as in the spec's *Configuration*; `"graph"` added to the mapping-typed keys checked in `load_config`.
- `settings.py`: `class GraphConfigError(ConfigError)`; frozen dataclasses `PatternSpec(name, regex, where, compiled)` and `GraphSettings(enabled, doc_globs, excluded_paths, ident_enabled, ident_min_length, ident_stoplist, patterns, impact_top, impact_min_score, impact_kinds, impact_weights, scan_missing_paths, scan_ignore_docs, scan_history_limit, replay_hub_share, replay_max_files, replay_popularity_window)` (tuples and frozensets, never lists); `graph_settings(cfg: dict) -> GraphSettings`; `GraphSettings.fingerprint() -> str` (sha256 of canonical JSON of `doc_globs`, identifier settings, pattern `(name, regex, where)` triples, `excluded_paths`, `EXTRACTOR_VERSION`); `DEFAULT_SETTINGS = graph_settings({})`.

**Tests:**
- `test_defaults_have_graph_section_and_search_graph_is_untouched` (in `test_config.py`): `DEFAULTS["graph"]["enabled"] is True`; `DEFAULTS["search"]["graph"]` still has exactly its four keys.
- `test_load_config_rejects_non_mapping_graph`.
- `test_settings_from_empty_cfg_equals_defaults`; `test_user_values_override_and_merge` (setting only `graph.impact.top` keeps the default weights).
- `test_pattern_validation`: bad regex, duplicate name, name with spaces, unknown `where` each raise `GraphConfigError` naming the pattern.
- `test_fingerprint_changes_with_extraction_inputs_only`: changing `doc_globs`, a pattern, `excluded_paths` or the identifier stoplist changes it; changing `impact.top` or `replay.hub_share` does not.
- `test_fingerprint_is_stable_across_calls_and_key_order`.

- [ ] Tests fail → implement → pass → suite green
- [ ] Commit: `feat(graph): graph config section and validated settings (#129)`

---

## Phase 1 — The pure extractor

### Task 2: Globs, the path index, doc and exclusion predicates

**Files:** `carta/graph/paths.py`, `carta/graph/tests/test_paths.py`

**Interfaces produced:**
- `glob_to_regex(glob: str) -> re.Pattern` (full match)
- `expand_braces(s: str, limit: int = 16) -> list[str] | None` (`None` = reject)
- `is_excluded_rel(rel: str, excluded_paths: Sequence[str]) -> bool` (delegates to `carta.scanner.scanner.is_excluded` with `PurePosixPath` under a dummy root; no filesystem access)
- `is_doc_path(rel: str, settings: GraphSettings) -> bool` (matches a doc glob and is not excluded)
- `class PathIndex`: `__init__(paths: Iterable[str])`; `files: frozenset[str]`; `has_file(p)`, `has_dir(p)`, `top_level_dirs: frozenset[str]`, `by_basename(name) -> tuple[str, ...]` (sorted), `suffix_matches(cand) -> tuple[str, ...]` (files and dirs ending with `/cand` on a segment boundary, sorted), `glob_matches(pattern) -> int`, `fingerprint() -> str` (sha256 over sorted paths joined by `\n`).

**Tests** (parametrised tables):
- `test_glob_to_regex`: `src/*.py` matches `src/a.py` not `src/x/a.py`; `src/**/*.py` matches both; `**/*.md` matches `a.md` and `x/y/a.md`; `fsm.[ch]` matches `fsm.c`, `fsm.h`, not `fsm.o`; `?` does not match `/`; regex metacharacters in the glob (`+`, `(`, `.`) are literal.
- `test_expand_braces`: `a.{c,h}` gives two; nested or unbalanced gives `None`; 17 alternatives gives `None`; no braces gives the input alone.
- `test_is_excluded_rel_matches_scanner_semantics`: a table of (path, patterns) checked against `carta.scanner.scanner.is_excluded` on a real `tmp_path`, covering dir patterns, nested always-excluded dirs (`pkg/node_modules/x.md`), and filename globs.
- `test_is_doc_path`: default globs accept `README.md`, `docs/a/b.md`; reject `src/a.py`, `.venv/x.md`.
- `test_path_index_dirs_and_top_level`, `test_by_basename_sorted_and_complete`, `test_suffix_matches_respects_segment_boundary` (`parser/frame.py` matches `src/parser/frame.py`, not `src/xparser/frame.py`), `test_fingerprint_independent_of_input_order`.

- [ ] Tests fail → implement → pass → commit: `feat(graph): path index and glob primitives (#129)`

### Task 3: Stage 1 — `scan_text`

**Files:** `carta/graph/markdown.py`, `carta/graph/tests/test_markdown.py`

**Interface produced:** `scan_text(text: str, patterns: Sequence[PatternSpec] = ()) -> list[Candidate]`, ordered by `(line, column of first appearance)`; module helper `collapse_evidence(line: str, limit: int = 240) -> str`.

**Tests** (each asserts the exact `(raw, ctx, line, single)` tuples):
- `test_inline_code_single_token`; `test_inline_code_command_yields_only_slash_tokens` (`` `python tools/gen.py --out build/` `` gives `tools/gen.py` and `build/`, both `single=False`).
- `test_double_backtick_spans`; `test_unclosed_backtick_yields_nothing`.
- `test_links_inline_image_and_reference_definition`; `test_link_with_title_and_angle_brackets`; `test_link_inside_code_span_is_code_only`.
- `test_fenced_block_backticks_and_tildes`; `test_fence_closed_only_by_long_enough_run`; `test_fence_tokens_need_a_slash`; `test_unterminated_fence_runs_to_eof`.
- `test_frontmatter_tokens_and_line_numbers` (list items, inline lists, quoted values); `test_no_frontmatter_when_first_line_is_not_dashes`; `test_frontmatter_never_closed_is_treated_as_body`.
- `test_line_numbers_with_crlf_and_bom`.
- `test_evidence_is_collapsed_and_truncated` (a 400-character line ends with `…` at 240).
- `test_patterns_scopes`: one regex with `where` = `all`, `prose`, `code` against the same text; group 1 preferred over the whole match.
- `test_plain_prose_paths_are_not_candidates`.
- `test_scan_text_is_deterministic` (two calls, equal lists) and `test_empty_and_whitespace_text`.

- [ ] Tests fail → implement → pass → commit: `feat(graph): markdown candidate scanner (stage 1) (#129)`

### Task 4: The identifier shape filter

**Files:** `carta/graph/idents.py`, `carta/graph/tests/test_idents.py`

**Interface produced:** `identifier_token(span: str, *, min_length: int = 3, stoplist: frozenset[str] = frozenset()) -> Optional[str]`; module constants `KEYWORD_STOPLIST`, `FILE_EXTENSIONS`, `TLDS`.

**Tests** (two parametrised tables, plus edge cases):
- `test_accepts`: `parse_frame`→same; `MAX_RETRIES`; `parseFrame`; `EdgeStore`; `store.get_meta`; `Parser::next`; `node->next`; `main()`→`main`; `init(self, x)`→`init`; `&ctx_ptr`→`ctx_ptr`; `CARTA_OLLAMA_KEEP_ALIVE`; `search.graph.enabled`.
- `test_rejects`: `git`, `pip`, `main`, `string`, `true`, `None`, `HTTP`, `TODO`, `--force`, `-v`, `KEY=value`, `$HOME`, `git commit -m`, `v1.2.3`, `3.10`, `e.g`, `i.e`, `config.yaml`, `example.com`, `ab` (too short), `x`, an 81-character token, `foo/bar`, `<name>`, `{placeholder}`, `self.this`, empty string.
- `test_min_length_and_stoplist_are_honoured`.
- `test_returns_none_never_raises_on_odd_input` (unicode, lone punctuation, unbalanced parentheses).

- [ ] Tests fail → implement → pass → commit: `feat(graph): identifier shape filter (#129)`

### Task 5: Stage 2 and composition — `resolve_candidates`, `extract_mentions`

**Files:** `carta/graph/mentions.py`, `carta/graph/tests/test_mentions.py`

**Interfaces produced:** `resolve_candidates(doc_path, candidates, index, settings) -> list[Edge]`; `extract_mentions(doc_path, text, repo_paths, settings=DEFAULT_SETTINGS) -> list[Edge]`; internal `normalise_candidate(raw: str, ctx: str) -> list[str] | None` (N1–N7, pure string work).

Use one shared fixture path set in the test module, e.g. `README.md`, `docs/design/parser.md`, `docs/design/store.md`, `docs/guide/setup.md`, `src/parser/frame.py`, `src/parser/checksum.py`, `src/parser/tests/test_frame.py`, `src/store/db.py`, `src/store/frame.py`, `tools/gen_tables.py`, `Makefile`, `assets/logo.png`.

**Tests, one per rule so a failure names the rule:**
- N1/N2: trailing punctuation; `src/parser/frame.py:42`, `...frame.py#L10-L20`, `?raw=1`; `#section` alone gives nothing.
- N3/N4: URLs (`https://…`, `mailto:`, `//cdn…`, `www.…`), `~/x`, `@scope/pkg`, e-mail, backslashes, `$VAR/x`, `<dir>/x`, `a/.../b` all give no edge.
- N5: `[x](/src/parser/frame.py)` resolves; `` `/usr/lib/x.so` `` gives nothing.
- N6: `` `src/parser/{frame,checksum}.py` `` gives two file edges; `` `src/{name}.py` `` gives none.
- N7: `` `src/parser/*.py` `` gives one `glob` edge with `meta.matches == 2`; a glob matching nothing gives no edge and no `missing` edge; `` `*.py` `` allowed, bare `*` not.
- N8: from `docs/design/parser.md`, link `../../src/parser/frame.py` resolves; link `store.md` resolves to the sibling; code `./store.md` resolves to the sibling; `../../../etc/passwd` gives nothing.
- N9: target `docs/design/store.md` has `dst_type='doc'`; `src/parser/frame.py` has `'file'`.
- N10: `` `src/parser/` `` and `` `src/parser` `` give `dir` edge `src/parser/`; `` `docs` `` and `` `tests` `` give nothing; `` `docs/` `` gives a `dir` edge.
- N11: `` `parser/checksum.py` `` resolves with `via='suffix'`; `` `tests/test_frame.py` `` resolves; a suffix with two matches gives nothing.
- N12: `` `checksum.py` `` resolves with `via='basename'`; `` `frame.py` `` (two files) gives nothing; `` `Makefile` `` (no extension) gives no path edge; `` `README.md` `` from a nested doc resolves to the root file; `` `1.5` `` and `` `v2.0` `` are not filenames; a bare filename inside a multi-token span or a fence gives nothing.
- N13: `` `src/parser/old_frame.py` `` gives a `missing` edge; `` `path/to/file.py` ``, `` `foo/bar.py` ``, `` `nope/x.py` `` (unknown top-level dir), `` `src/parser/thing` `` (no extension, no slash) give nothing; the same missing path inside a fence gives nothing; a relative link to a file that does not exist gives a `missing` edge.
- N14: a doc linking to itself gives nothing.
- `test_fence_mentions_have_half_weight`; `test_same_path_twice_on_one_line_is_one_edge`; `test_same_path_on_two_lines_is_two_edges`; `test_link_and_fence_on_same_line_keeps_higher_weight`.
- `test_unresolved_single_token_falls_through_to_identifier`: `` `parse_frame()` `` gives one `ident` edge with `dst='parse_frame'`, `dst_type='token'`; identifiers disabled in settings gives none.
- `test_pattern_edges`: a configured pattern gives `kind='pattern'`, `dst='message:MSG_START'`.
- `test_target_under_excluded_path_is_still_an_edge`.
- `test_output_sorted_and_deduplicated`; `test_extract_is_pure`: called twice, and with `repo_paths` as a list, a set and a `PathIndex`, results are equal; patch `builtins.open`, `os.stat` and `subprocess.run` to raise and the call still succeeds.
- `test_composition_equals_stages`: `extract_mentions` equals `resolve_candidates(scan_text(...))`.

- [ ] Tests fail → implement → pass
- [ ] Run the whole `carta/graph/tests` directory; full suite green
- [ ] Commit: `feat(graph): pure mention extractor with path normalisation (#129)`

---

## Phase 2 — Store and build

### Task 6: `EdgeStore`

**Files:** `carta/graph/store.py`, `carta/graph/tests/test_store.py`

**Interface produced:** `class EdgeStore` exactly as in the spec's *Store API*; module function `store_path(repo_root: Path) -> Path` (`<repo>/.carta/graph/edges.db`).

**Tests:**
- `test_creates_schema_dir_and_self_ignoring_gitignore` (`.carta/graph/.gitignore` contains `*`; `PRAGMA user_version` is 1; the three tables and three indexes exist).
- `test_replace_source_is_idempotent_and_replaces` (write edges A, then B: only B remain; `sources` has one row).
- `test_replace_source_only_touches_its_own_extractor` (a `cochange` row with the same `src` survives a `mention` replace).
- `test_remove_source_and_clear_extractor`.
- `test_unique_key_rejects_duplicate_within_one_write` or de-duplicates, whichever the implementation documents; assert no crash and one row.
- `test_iter_edges_order_and_filters` (by kinds, by dst types).
- `test_roundtrip_preserves_every_field` (unicode evidence, meta JSON, weight 0.5).
- `test_schema_accepts_later_kinds_without_migration`: insert a `cochange` row (`src_type='file'`, `dst_type='file'`, `line=0`, `weight=0.37`, meta with `support`) and a `symbol` row (meta with `name`, `def_line`); read them back equal. This test is the contract #130 and #131 rely on.
- `test_digest_ignores_insert_order_and_row_ids` (same edges inserted in two orders, across two stores: equal digests); `test_digest_changes_with_any_field`.
- `test_version_mismatch_rebuilds_empty` (set `user_version` to 99, reopen: tables empty, version 1).
- `test_corrupt_file_is_replaced` (write garbage bytes to the path, open: a working empty store).
- `test_transaction_rolls_back_on_exception`.
- `test_meta_get_set`.

- [ ] Tests fail → implement → pass → commit: `feat(graph): sqlite edge store with canonical digest (#129)`

### Task 7: Git plumbing

**Files:** `carta/graph/gitio.py`, `carta/graph/tests/test_gitio.py` (all `@requires_git`)

**Interfaces produced:**
- `class GitError(RuntimeError)`; `run_git(repo: Path, *args: str, input_bytes: bytes | None = None) -> bytes` (raises `GitError` on a non-zero exit or missing binary, with stderr in the message; always passes `-c core.quotepath=off`).
- `is_work_tree(path: Path) -> bool`; `toplevel(path: Path) -> Path | None`; `default_branch(repo: Path) -> str` (same logic as `carta.hook.stale_scan._default_branch`, reimplemented here to keep imports clean).
- `ls_files(repo: Path) -> list[str]` (`-z --cached --others --exclude-standard`, sorted)
- `diff_names(repo: Path, range_spec: str) -> list[str]` (`diff --name-only --no-renames --relative -z`)
- `rev_list_first_parent(repo: Path, range_spec: str | None, last: int | None) -> list[tuple[str, tuple[str, ...]]]` (oldest first; each commit with its parents)
- `diff_tree(repo: Path, parent: str, commit: str) -> list[tuple[str, str]]` (`(status, path)`, `--no-renames`, sorted by path)
- `ls_tree(repo: Path, commit: str) -> dict[str, str]` (path → blob id; gitlinks skipped)
- `class BlobReader`: context manager around one `git cat-file --batch`; `read(blob_id: str) -> bytes`
- `decode_text(data: bytes) -> str` (UTF-8 with replacement, BOM stripped, CRLF folded)
- `deleted_and_renamed(repo: Path, limit: int) -> tuple[frozenset[str], dict[str, str]]` (deleted paths; old → new for renames; newest record wins)

**Tests:** `test_ls_files_includes_untracked_excludes_ignored`; `test_ls_files_relative_to_subdirectory_root`; `test_paths_with_spaces_and_unicode_roundtrip`; `test_rev_list_first_parent_order_and_last_n`; `test_rev_list_follows_first_parent_through_a_merge` (build a merge commit; the side branch's commits are absent); `test_diff_tree_statuses_no_renames` (a `git mv` shows as `D` + `A`); `test_ls_tree_and_blob_reader_roundtrip` (many blobs through one process, including an empty file and a binary one); `test_blob_reader_unknown_id_raises_giterror`; `test_deleted_and_renamed`; `test_not_a_repo_raises_giterror`; `test_decode_text`.

- [ ] Tests fail → implement → pass → commit: `feat(graph): git plumbing helpers (#129)`

### Task 8: `build_graph`

**Files:** `carta/graph/build.py`, `carta/graph/tests/test_build.py`

**Interfaces produced:** `content_sha256(data: bytes) -> str`; `list_repo_paths(repo_root: Path) -> list[str]`; `build_graph(repo_root: Path, cfg: dict, *, force: bool = False) -> dict` (report shape in the spec); `graph_status(repo_root: Path, cfg: dict) -> dict`.

**Tests** (use a plain `tmp_path` tree unless git is the point):
- `test_content_sha256_matches_embed_lifecycle_for_text` (CRLF and LF variants hash equal; equals `carta.embed.lifecycle.compute_file_hash` on a `.md` file. This test imports the embed module; the package does not).
- `test_list_repo_paths_git_and_non_git` (`@requires_git` half: ignored files absent, untracked present, a tracked-but-deleted file absent; non-git half: walk fallback skips `.git` and `node_modules`).
- `test_first_build_extracts_every_doc_and_reports_counts`.
- `test_second_build_is_a_noop`: `extracted == 0`, digest unchanged.
- `test_editing_one_doc_re_extracts_only_it` (`extracted == 1`, `full is False`).
- `test_adding_a_source_file_triggers_full_pass_and_resolves_a_missing_edge` (a `missing` edge becomes a `file` edge).
- `test_deleting_a_source_file_turns_edge_into_missing`.
- `test_deleting_a_doc_removes_its_source_and_edges`.
- `test_excluded_docs_are_not_sources_but_can_be_targets`.
- `test_settings_change_triggers_full_pass` (add a pattern).
- `test_force_rebuilds_everything`.
- **`test_incremental_equals_fresh`**: apply a scripted sequence of edits, adds and deletes with a build after each; then delete the store and build once; the two digests are equal. Parametrise over three different edit orders.
- `test_delete_db_and_rebuild_reproduces_digest`.
- `test_build_when_graph_disabled_returns_disabled_report_and_writes_nothing`.
- `test_unreadable_or_binary_doc_is_skipped_not_fatal` (invalid UTF-8 bytes in a `.md`).
- `test_graph_status_reports_stale_reasons` (missing store; then fresh; then after a doc edit `stale` with reason `docs_changed`; after a file add `paths_changed`).
- `test_build_budget`: 500 generated docs of about 4 kB each with 20 mentions; a full build finishes in under 5 s. Mark it so it can be deselected, but run it by default.
- Extend `test_import_hygiene.py`: the subprocess also creates a temp tree, runs `build_graph`, and re-checks `sys.modules`.

- [ ] Tests fail → implement → pass → full suite green
- [ ] Commit: `feat(graph): incremental graph build keyed by content hash (#129)`

---

## Phase 3 — Query surface

### Task 9: `rank_impact` and `run_impact`

**Files:** `carta/graph/query.py`, `carta/graph/tests/test_query.py`

**Interfaces produced:** `DEFAULT_KIND_WEIGHTS`; `class ImpactIndex`; `rank_impact(...)` as in the spec; `normalise_query_paths(repo_root, cwd, raw_paths) -> list[str]`; `run_impact(repo_root: Path, cfg: dict, *, paths: Sequence[str] = (), diff: str | None = None, top: int | None = None, kinds: Sequence[str] | None = None, include_docs: bool = False, min_score: float | None = None, build: bool = True) -> dict` (the JSON object of the spec); `format_impact(result: dict) -> str`.

**Tests, pure ranking first (hand-built `Edge` lists, no filesystem):**
- `test_exact_match_scores_formula`: one doc, one exact mention, `N=10`, `df=1`: score equals `ln(11)` to 1e-9.
- `test_rarer_target_ranks_higher`: doc A names a file 2 docs name, doc B names a file 8 docs name; query both files' owners separately and together; A outranks B.
- `test_dir_match_discounted_by_depth` (direct child 1/2, grandchild 1/3); `test_exact_beats_dir_for_same_doc_and_is_not_double_counted` (the contribution is the max, not the sum).
- `test_glob_match_half_weight`; `test_query_directory_matches_targets_under_it`.
- `test_missing_edge_matches_exact_query`.
- `test_repeated_mentions_grow_logarithmically` (`1 + ln(3)` for three lines).
- `test_fence_only_mention_is_half`.
- `test_score_sums_across_query_paths`.
- `test_ident_and_pattern_edges_never_match_paths`.
- `test_kinds_filter_and_kind_weights` (a synthetic `cochange` edge contributes `w * weight` with no idf; excluded when not in `kinds`).
- `test_tie_break_is_path_ascending`; `test_top_and_min_score`; `test_exclude_docs`.
- `test_evidence_lines_sorted_and_complete`; `test_df_reported_per_match`.
- `test_no_edges_and_empty_query_return_empty`.

**Then `run_impact` (tmp tree; `@requires_git` for diff cases):**
- `test_run_impact_builds_then_queries`; `test_no_build_uses_existing_store_and_errors_clearly_when_absent`.
- `test_paths_relative_to_cwd_are_normalised`; `test_path_outside_repo_is_reported_in_skipped`.
- `test_diff_mode_queries_changed_non_docs_and_marks_in_diff` ; `test_include_docs_adds_changed_docs_to_query`; `test_bare_diff_uses_default_branch_range`; `test_bad_range_raises_clear_error`.
- `test_unmatched_paths_listed`.
- `test_json_shape_is_exactly_the_spec` (assert the key sets at each level).
- `test_result_is_json_serialisable_and_stable` (two runs, `json.dumps(..., sort_keys=True)` equal).
- `test_format_impact_snapshot` (the human text for a small fixture, compared whole).

- [ ] Tests fail → implement → pass → commit: `feat(graph): impact ranking and query (#129)`

### Task 10: CLI — `carta impact`, `carta graph build|status`

**Files:** `carta/cli.py`, `carta/graph/tests/test_cli_graph.py`

**Interfaces produced:** `cmd_impact(args)`, `cmd_graph(args)`; parsers `impact` and `graph` (subcommands `build`, `status`; `replay` is added in Task 15) registered in `main()` and its `dispatch` table. All graph imports are inside the handlers, like the other commands.

**Tests** (call `carta.cli.main()` with patched `sys.argv` and `monkeypatch.chdir` into a tmp project that has a minimal `.carta/config.yaml`; capture with `capsys`):
- `test_graph_build_prints_summary_and_json`; `test_graph_build_force`.
- `test_graph_status_before_and_after_build` (never creates the store).
- `test_impact_paths_human_and_json` (`--json` output parses and has the spec's keys; nothing else on stdout).
- `test_impact_requires_a_path_or_diff` (exit 1, message on stderr).
- `test_impact_diff` (`@requires_git`).
- `test_impact_top_kinds_min_score_flags_reach_the_query`.
- `test_impact_works_with_unreachable_qdrant_and_ollama`: config points both URLs at `http://127.0.0.1:9` and a socket guard (patch `socket.socket.connect` to raise) proves no connection is attempted.
- `test_commands_without_config_fail_with_the_standard_message`.

- [ ] Tests fail → implement → pass → full suite green
- [ ] Commit: `feat(cli): carta impact and carta graph build/status (#129)`

### Task 11: MCP — `carta_impact`

**Files:** `carta/mcp/server.py`, `carta/tests/test_mcp_server.py`

**Interface produced:** `_impact(paths, diff, top_k, kinds, include_docs) -> dict` (plain function, kept undecorated for tests, following `_remember`) and the decorated `carta_impact` tool with a docstring that tells the calling agent when to use it ("before or after changing source files, to find the docs that describe them") and what comes back.

**Tests** (follow the existing patterns in `test_mcp_server.py` for patching `_load_cfg` and `_repo_root_from_cfg`):
- `test_carta_impact_returns_cli_json_shape`.
- `test_carta_impact_no_paths_and_no_diff_is_invalid_request`.
- `test_carta_impact_config_missing_is_service_unavailable`.
- `test_carta_impact_unexpected_error_is_caught_and_never_prints` (patch `run_impact` to raise; `capsys` shows empty stdout).
- `test_carta_impact_is_registered_as_a_tool`.

- [ ] Tests fail → implement → pass → commit: `feat(mcp): carta_impact tool (#129)`

### Task 12: Scan finding — `missing_path_mention`

**Files:** `carta/graph/scan.py`, `carta/scanner/scanner.py`, `carta/graph/tests/test_scan_missing.py`

**Interface produced:** `missing_path_findings(repo_root: Path, cfg: dict) -> list[dict]`; `run_scan` calls it (lazy import, wrapped in `try/except Exception`) after the per-doc structural checks, and adds `stats["missing_path_mentions"]`.

Before writing code, read `carta/scanner/tests/test_scanner.py` for `run_scan` tests that assert on the whole `issues` list or on totals, and confirm their fixtures contain no relative markdown link to a missing file. If one does, the new finding must not break it: prefer fixing the design (it is the precision contract) over editing an old test, and record what was found in the commit body.

**Tests** (`@requires_git` where history is needed):
- `test_deleted_file_mentioned_in_backticks_is_reported` (`reason='deleted'`, severity `warning`, fields `path`, `line`).
- `test_renamed_file_reports_new_path` (`reason='renamed'`, `renamed_to` set); `test_renamed_then_new_path_deleted_reports_no_renamed_to`.
- `test_broken_relative_link_is_reported_without_history` (`reason='broken_link'`, works in a non-git tree).
- **Precision table, each must give no finding:** a URL; `path/to/file.py`; `foo/bar.py`; an absolute path; `../../outside/x.py`; a never-existed backticked path under a real directory; a path inside a fenced block; a path that is gitignored but present on disk; a mention in a doc matched by `graph.scan.ignore_docs`; a mention in an excluded doc; a glob; a deleted path that has since been re-created.
- `test_one_finding_per_doc_and_path_on_first_line`; `test_findings_sorted`.
- `test_disabled_by_config` (`graph.enabled: false`, and `graph.scan.missing_paths: false`).
- `test_run_scan_includes_finding_and_stat` (through `run_scan`, with `suggest_related_for_all` patched as the neighbouring tests do).
- `test_run_scan_survives_graph_failure` (patch `missing_path_findings` to raise: scan completes, no finding, no exception).
- `test_existing_scanner_suite_untouched`: not a test to write; run `carta/scanner/tests` and `carta/tests` and confirm no change.

- [ ] Tests fail → implement → pass → full suite green
- [ ] Commit: `feat(scan): missing_path_mention finding from the edge store (#129)`

---

## Phase 4 — The replay eval

### Task 13: Metrics

**Files:** `carta/graph/eval/metrics.py`, `carta/graph/tests/test_metrics.py`

**Interfaces produced:** `score_ranking(ranked: Sequence[str], gold: Collection[str], ks: Sequence[int]) -> dict` (`recall_at`, `precision_at`, `any_hit_at`, `rr`, `first_rank`, `returned`); `aggregate(rows: Sequence[dict], ks) -> dict` (macro means, `n`); `bootstrap_ci(values: Sequence[float], *, resamples: int = 2000, seed: int = 0) -> tuple[float, float]`; `paired_bootstrap(a: Sequence[float], b: Sequence[float], *, resamples: int = 2000, seed: int = 0) -> dict` (`delta`, `ci95`, `better`, `worse`, `tied`).

**Tests:** hand-computed cases: `test_perfect_ranking`; `test_gold_at_rank_3` (R@1 0, R@3 1, P@3 1/3, rr 1/3); `test_two_gold_one_found` (R@5 0.5); `test_precision_divides_by_k_not_returned`; `test_empty_ranking`; `test_rr_zero_beyond_max_k`; `test_duplicate_docs_in_ranking_are_an_error_or_deduplicated` (document which); `test_aggregate_macro_average`; `test_aggregate_empty_population_is_zeros_with_n_zero`; `test_bootstrap_is_deterministic_and_brackets_the_mean`; `test_paired_bootstrap_counts_and_sign`; `test_paired_requires_equal_lengths`.

- [ ] Tests fail → implement → pass → commit: `feat(graph-eval): ranking metrics and seeded bootstrap (#129)`

### Task 14: Baselines

**Files:** `carta/graph/eval/baselines.py`, `carta/graph/tests/test_baselines.py`

**Interfaces produced:** `popularity_ranking(prior_doc_edits: Sequence[Collection[str]], docs_at_parent: Collection[str], window: int, top: int) -> list[str]`; `basename_tokens(text: str) -> dict[str, int]`; `basename_ranking(changed_paths: Sequence[str], doc_tokens: Mapping[str, Mapping[str, int]], top: int) -> list[str]`.

**Tests:** `test_popularity_counts_only_the_window_and_only_prior_commits`; `test_popularity_drops_docs_absent_at_parent`; `test_popularity_ties_by_path`; `test_popularity_with_no_history_is_empty`; `test_basename_tokens_whole_token_only` (`frame.py` found in "see frame.py." and in `` `src/parser/frame.py` ``, not in `subframe.pyc`); `test_basename_ranking_orders_by_distinct_then_total_then_path`; `test_basename_is_case_sensitive`; `test_basename_ignores_directory_part`; `test_basename_no_match_is_empty`.

- [ ] Tests fail → implement → pass → commit: `feat(graph-eval): popularity and basename baselines (#129)`

### Task 15: `run_replay`, the cache, and the CLI entry

**Files:** `carta/graph/eval/replay.py`, `carta/cli.py` (the `graph replay` subcommand), `carta/graph/tests/test_replay.py` (all `@requires_git`)

**Interfaces produced:**
- `@dataclass(frozen=True) class Snapshot` (`parent`, `paths: PathIndex`, `blobs: Mapping[str, str]`, `docs: tuple[str, ...]`, `read_text: Callable[[str], str]`, `prior_commits: tuple[str, ...]`)
- `class EdgeProvider(Protocol)`; `PROVIDERS: dict[str, EdgeProvider]` with `mention` registered; `register_provider(provider) -> None`
- `class ReplayCache` (`get_scan`, `put_scan`, `get_tokens`, `put_tokens`; in-memory when `path is None`)
- `default_cache_path(repo: Path) -> Path`
- `classify_commit(changes, parent_exists, settings, max_files) -> tuple[str | None, list[str], list[str], int]` (skip reason or `None`, query, gold, count of created docs), a pure function
- `run_replay(repo: Path, *, range_spec: str | None = None, last: int | None = 300, arms: Mapping[str, Sequence[str]] | None = None, ks: Sequence[int] = (1, 3, 5, 10), settings: GraphSettings | None = None, hub_share: float | None = None, max_files: int | None = None, popularity_window: int | None = None, cache_path: Path | None = None, use_disk_cache: bool = True, per_commit: bool = False) -> dict`
- `format_replay(report: dict) -> str`; `main(argv: list[str] | None = None) -> int`; `if __name__ == "__main__"` guard

**The synthetic history every test shares** (a fixture building about 14 commits): docs `README.md`, `CHANGELOG.md`, `docs/parser.md` (names `src/parser/frame.py`), `docs/store.md` (names `src/store/`), `docs/misc.md` (names nothing). Commits: root; source+its doc (several); source+`CHANGELOG.md` only; source only; docs only; a commit that creates a new doc alongside source; a bulk commit over the limit; a `git mv` of a source file with its doc edited; a merge commit from a side branch that edits source and a doc; and one commit where the doc starts naming a file **in the same commit** that edits it (the leakage trap).

**Tests:**
- `test_classify_commit_each_skip_reason_in_priority_order` (pure, table-driven).
- `test_population_counts_add_up`: `commits_scanned == eligible + sum(skipped.values())`, and each reason has the hand-counted value.
- **`test_no_leakage_from_the_commit_itself`**: for the trap commit, the graph arm does *not* rank the doc on the strength of the mention added in that commit (assert its rank is `None` or that the evidence line does not exist at the parent). Then a second variant: mutate the working tree and the index (edit and `git add` a doc naming every source file) and assert the report is byte-identical to the clean run.
- `test_never_reads_worktree_or_store`: run with the working tree files deleted (`git checkout` of an empty orphan is overkill; simply remove every tracked file from disk) and get the same report; assert `.carta/graph/edges.db` does not exist afterwards.
- `test_graph_arm_finds_the_named_doc` (the source+doc commits score `any_hit@1 == 1` for `graph[mention]`).
- `test_created_docs_are_not_gold_and_are_counted`.
- `test_merge_commit_is_scored_against_first_parent`.
- `test_rename_is_delete_plus_add`.
- `test_baselines_scored_on_identical_population` (`n` equal across all methods, in both variants).
- `test_hub_variant_removes_hub_from_gold_and_rankings_and_shrinks_population` (`CHANGELOG.md` is the hub; the changelog-only commit leaves the no-hub population).
- `test_alarm_section_counts_source_only_commits`.
- `test_incremental_resolution_equals_from_scratch`: run once normally and once with the reuse optimisation disabled (a module flag or parameter); reports equal.
- `test_cache_hit_reads_no_blobs_second_time` (count `BlobReader.read` calls: second run with the disk cache makes zero doc reads) and `test_cache_miss_on_fingerprint_change`.
- `test_report_is_deterministic` (two runs, with and without the cache: `json.dumps(sort_keys=True)` equal) and `test_report_has_no_timestamps_or_absolute_paths` (the tmp path string does not appear in the JSON).
- `test_arms_unknown_kind_is_a_clear_error` (`--arm x=cochange` names the kind); `test_custom_provider_can_be_registered_and_measured` (register a toy provider in the test, run two arms, both appear in `methods` and in `paired`). This is the hook #130 and #131 use.
- `test_range_and_last_selection`; `test_shallow_or_tiny_history_degrades_to_empty_population_not_error` (a one-commit repo: `eligible == 0`, exit 0).
- `test_works_without_carta_config` (no `.carta/` in the repo).
- `test_json_shape_is_the_spec` (key sets); `test_format_replay_states_population_first` (the first non-header line starts with `Population:` and the skip reasons appear before any metric table).
- `test_cli_graph_replay_json_and_text` (through `carta.cli.main`), `test_module_main_runs` (`main([...])` returns 0).

- [ ] Tests fail → implement → pass → full suite green
- [ ] Commit: `feat(graph-eval): leak-free PR replay with baselines and hub variant (#129)`

---

## Phase 5 — Documentation and verification

### Task 16: Docs and housekeeping

**Files:** `README.md`, `CHANGELOG.md`, `docs/ROADMAP.md`, `CLAUDE.md`, `.gitignore`, `carta/install/bootstrap.py` (+ its test if the gitignore list is pinned by one)

- [ ] `README.md`: a new section after *Graph-aware retrieval*, titled **"The graph arm: which docs describe this file?"**. Cover: what an edge is and that no model is involved; `carta graph build`; `carta impact <path>` and `--diff` with one real-looking output block; `--json`; the `graph:` config block; the scan finding; how to run `carta graph replay` and how to read its population line. State plainly what it does not do (no code parsing, no prose paths, identifiers stored but not yet resolved). Add `impact` and `graph` to the "Which command?" guidance if that table lists commands.
- [ ] `CHANGELOG.md`: one **Added** entry under `[Unreleased]`, in the house style (a bold lead sentence, then what and why). Leave the measured numbers as a clearly marked placeholder for Task 17 to fill; do not invent any.
- [ ] `docs/ROADMAP.md`: a short "second arm" note under *How the subsystems relate* (a new `graph` subgraph in the mermaid diagram with nodes for mention edges, co-change and symbol resolution, and an edge from the stale-reference hook's node explaining that a named path is a cheaper signal than a cosine), plus a rationale paragraph under *Design rationale worth keeping*: why a deterministic arm, why it shares no code with the vector stack, why evaluation is PR replay. No per-issue status.
- [ ] `CLAUDE.md`: add `impact` and `graph` rows to the CLI table, `carta_impact` to the MCP list, `graph/` to the module map. Nothing else.
- [ ] `.gitignore`: add `.carta/graph/` under *Carta runtime state*. `bootstrap._update_gitignore`: add the same line to what `carta init` writes (extend its test).
- [ ] Run the repo's own doc checks if any apply (`python -m carta scan` is not required to be clean, but must not crash on the new docs).
- [ ] Commit: `docs(graph): README section, changelog, roadmap note for the graph arm (#129)`

### Task 17: Verification and results

- [ ] Full suite: `python -m pytest carta/ -q -p no:cacheprovider`. Record the exact summary line.
- [ ] Bare-install check, mirroring CI: create a fresh venv with only `pip install -e .` and `pytest`, run `pytest carta/graph -q` in it, on the oldest Python available locally (3.10 if present). The graph tests must pass with no optional extra installed.
- [ ] Import-hygiene test passes (it is in the suite; name it in the report).
- [ ] Self-hosting run on this repository: `carta graph build`, `carta graph status --json`, `carta impact carta/scanner/scanner.py`, `carta impact --diff origin/main...HEAD --json`. Read the output: are the top docs plausible, is every evidence line real?
- [ ] `carta scan` on this repository: read every `missing_path_mention`. Any false positive is a bug in N13 or in the finding's gate; fix it with a new test in the precision table before continuing.
- [ ] Replay on this repository's history: `carta graph replay --last 300 --json results.json` and the text form. Then once more with `--no-cache` to confirm identical output.
- [ ] Append the **Results** section to the spec: the population line and skip reasons, the metric tables with and without hubs, the paired deltas with intervals against both baselines, the alarm rates, and two or three honest sentences on what the numbers do and do not show (population size, whose history, what a "miss" usually was). Fill the changelog placeholder from the same numbers.
- [ ] Commit: `docs(graph): replay results on real history (#129)`

---

## Self-review checklist (run before calling the plan executed)

- Every spec section has a task: store (6), extractor (2–5), build (8), ranking and query (9), CLI (10, 15), MCP (11), scan (12), config (1), eval (13–15), docs (16), results (17).
- The three contracts later work depends on each have a named test: schema accepts `cochange`/`symbol` rows (Task 6), `rank_impact` honours kinds and weights (Task 9), a custom provider can be registered and measured as its own arm (Task 15).
- Purity is tested three ways: extractor with I/O patched to raise (Task 5), package import and build in a clean subprocess (Tasks 0, 8), CLI with sockets blocked (Task 10).
- Leakage is tested two ways: the same-commit trap, and a dirty working tree and index that must not change the report (Task 15).
- Nothing in any fixture, doc or message comes from a private project.

## Out of scope for this plan

Feeding mention edges into `run_search` expansion; co-change and symbol providers; comparing
derived edges with hand-declared frontmatter glob lists; changing the `claude-md-sync` skill; a
stage-1 cache inside the store. Each is recorded under *Deferred* in the spec with its reason.
