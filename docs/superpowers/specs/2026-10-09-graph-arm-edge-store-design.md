# The Graph Arm: A Deterministic Edge Store and Doc→Source Mention Edges

**Date:** 2026-10-09
**Status:** Approved under delegation (unattended overnight run; the architecture below was settled
before this document was written and is recorded here, not reopened). Review before merging.
**Issues:** #129 (this spec, in full). The store schema also covers what #130 (git co-change
edges) and #131 (symbol edges) will write, and the evidence column is the per-edge reason of #107.
**Plan:** [`../plans/2026-10-09-graph-arm-edge-store.md`](../plans/2026-10-09-graph-arm-edge-store.md)

## The question this answers

Carta's embedding arm answers "which passage is about this?". It cannot answer the reverse
question an agent has while editing code: **"I am changing these files; which docs describe
them?"** Today the only graph Carta holds is the hand-written `related:` frontmatter, and at 0.9.0
that graph (84 edges over 74 docs on the measured corpus) was too sparse to move retrieval.

This adds a second arm beside the embedding arm: a **deterministic graph**. A doc that names
`src/parser/frame.py` in backticks has declared a relation to that file more reliably than any
cosine score can, and extracting it needs no model. Same input, same edges, every time, with
Qdrant and Ollama switched off.

## Scope

In: the edge store; the mention extractor; `carta impact`; `carta graph build|status|replay`; the
`carta_impact` MCP tool; one `carta scan` finding; a config section; the PR-replay eval with three
baselines.

Out (each has a reason in *Decisions*): feeding these edges into `run_search` graph expansion;
co-change edges (#130) and symbol resolution (#131), beyond a schema that already fits them;
parsing source code; any model on the extraction path; wikilinks.

## Hard properties

| Property | How it is held |
|---|---|
| No vector stack | `carta/graph/` imports only the stdlib, PyYAML, `carta.config` and `carta.registry` (for the carta-home helper). It does **not** import `carta.scanner.scanner`: that module imports `carta.embed.induct` at the top and with it the whole vector stack (A1). The exclusion rule is reimplemented in `paths.py` and pinned to the scanner's by a parity test; the scanner importing `carta.graph.scan` lazily is the only allowed direction. A subprocess test imports the package, runs a build and an impact query, and asserts `qdrant_client`, `fastembed`, `torch`, `requests` and `carta.embed.pipeline` are absent from `sys.modules`. |
| Deterministic | Extraction is a pure function. Stored rows hold no timestamps, no absolute paths, no mtimes. Output order is always an explicit sort. `edges_digest()` (below) is equal for a fresh build and for any sequence of incremental builds that ends at the same tree. |
| Rebuildable | The store is a derived cache. Delete it and `carta graph build` reproduces the same digest. A store written by an **older** schema is deleted and rebuilt; there are no migrations. A store written by a **newer** schema is left untouched and opening it raises `StoreVersionError` ("written by a newer Carta"), so two installs of different versions pointed at one repo do not delete each other's store on every call (A17). |
| No new dependencies | stdlib `sqlite3`, `subprocess` (git), `re`, `json`, `hashlib`, `pathlib`, `posixpath`, `urllib.parse`, `random` (seeded) plus PyYAML. Python 3.10 syntax and APIs only; in particular `sqlite3.connect(autocommit=...)` (3.12) is forbidden. |
| Offline tests | No test touches the network, Qdrant, Ollama or a model. Tests that need git build a throwaway repository in `tmp_path` with a fixed identity and fixed dates. |

Note that `carta.embed.lifecycle.compute_file_hash` cannot be reused: that module imports
`qdrant_client` at the top. The graph arm carries its own three-line hash with the same
normalisation, and a test (which is allowed to import the embed package) pins the two to agree.

## Package layout

```
carta/graph/
  __init__.py     docstring only
  model.py        Edge, Candidate, constants (kinds, dst types, MENTION_EXTRACTOR_VERSION, SCHEMA_VERSION)
  settings.py     GraphSettings (frozen), graph_settings(cfg), fingerprint
  paths.py        glob_to_regex, expand_braces, PathIndex, is_doc_path, is_excluded_rel
  markdown.py     scan_text(text, patterns) -> list[Candidate]          (stage 1, text only)
  idents.py       identifier_token(span) -> str | None                  (the shape filter)
  mentions.py     resolve_candidates(...), extract_mentions(...)         (stage 2 + composition)
  store.py        EdgeStore
  gitio.py        git plumbing (ls-files, ls-tree, cat-file --batch, rev-list, diff-tree, diff)
  build.py        list_repo_paths, build_graph
  query.py        ImpactIndex, rank_impact, run_impact, format_impact
  scan.py         missing_path_findings
  eval/
    __init__.py
    metrics.py    score_ranking, aggregate, paired_bootstrap
    baselines.py  popularity_ranking, basename_ranking, basename_tokens
    replay.py     run_replay, format_replay, main
  tests/          one test module per source module, plus conftest.py (git fixture)
```

## Edge store

One SQLite file at `<repo>/.carta/graph/edges.db`. The directory is created with its own
`.gitignore` containing `*`, so the store never shows as untracked in an install whose root
`.gitignore` predates this feature. Journal mode stays at the default (no `-wal`/`-shm` files);
`busy_timeout` is 5000 ms; each build is one `BEGIN IMMEDIATE` transaction. The connection is
opened with `isolation_level=None` and `BEGIN IMMEDIATE` / `COMMIT` / `ROLLBACK` are issued
explicitly, which behaves the same on Python 3.10, 3.11 and 3.12 (A17).

```sql
PRAGMA user_version = 1;                 -- SCHEMA_VERSION

CREATE TABLE meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
) WITHOUT ROWID;

CREATE TABLE sources (
  extractor         TEXT    NOT NULL,    -- 'mention' today; 'symbol' for a symbol-index file later
  path              TEXT    NOT NULL,    -- repo-relative POSIX
  sha256            TEXT    NOT NULL,    -- LF-normalised content hash
  extractor_version INTEGER NOT NULL,
  meta              TEXT    NOT NULL DEFAULT '{}',     -- canonical JSON; per-node state (A3)
  PRIMARY KEY (extractor, path)
) WITHOUT ROWID;

CREATE TABLE edges (
  id                INTEGER PRIMARY KEY,
  kind              TEXT    NOT NULL,    -- mention | ident | pattern | cochange | symbol | related
  src               TEXT    NOT NULL,    -- repo-relative POSIX path
  src_type          TEXT    NOT NULL DEFAULT 'doc',    -- doc | file
  dst               TEXT    NOT NULL,
  dst_type          TEXT    NOT NULL,    -- file | doc | dir | glob | missing | token | pattern
  line              INTEGER NOT NULL DEFAULT 0,        -- 1-based; 0 = not line-anchored
  key               TEXT    NOT NULL DEFAULT '',       -- discriminator: the identifier for symbol edges (A2)
  evidence          TEXT    NOT NULL DEFAULT '',
  weight            REAL    NOT NULL DEFAULT 1.0,
  meta              TEXT    NOT NULL DEFAULT '{}',     -- canonical JSON object
  extractor         TEXT    NOT NULL,
  extractor_version INTEGER NOT NULL,
  UNIQUE (kind, src, dst, line, key)
);
CREATE INDEX edges_dst ON edges (dst, kind);
CREATE INDEX edges_src ON edges (src, kind);
CREATE INDEX edges_kind_type ON edges (kind, dst_type);
```

`meta` keys written by this work: `mention.settings_sha` (fingerprint of the settings that affect
extraction), `mention.paths_sha` (sha256 over the sorted path set), `mention.extractor_version`.

**Why this already fits the later kinds, with no migration:**

| Later writer | Rows |
|---|---|
| #130 co-change | `kind='cochange'`, `src_type='file'`, `dst_type='file'` (or `doc`), `line=0`, `weight` = the normalised score, `meta` = `{"support": n, "first": sha, "last": sha}`, `evidence` = a generated phrase such as "changed together in 9 commits". Its incremental cursor is a `meta` key (`cochange.last_commit`), since its input is history, not a hashed file. One row per ordered pair, so both directions are indexed by `edges_dst`. **Per-file state** (each file's own commit count, which confidence and lift need and which must be updated per commit from the cursor) lives in `sources` rows under `extractor='cochange'`: `path` = the file, `sha256` = `''`, `meta` = `{"commits": n}`; the total commit count is a `meta` key (A3). |
| #131 symbol | `kind='symbol'`, `src_type='doc'`, `dst` = defining file, `dst_type='file'`, `line` = the doc line, `key` = the identifier (so two identifiers on one doc line that are defined in one file are two rows, A2), `evidence` = the doc line, `meta` = `{"name": "...", "symbol_kind": "function", "def_line": 42}`. It reads its input from the `kind='ident'` rows this work stores. The symbol-index file gets a `sources` row under `extractor='symbol'`. |
| #107 annotated `related:` | `kind='related'`, `dst_type='doc'`, `evidence` = the author's reason. |

**`evidence` is one slot with one meaning:** the human-readable reason this edge exists. For a
mention it is the doc line that made it (whitespace collapsed, at most 240 characters, ending `…`
when cut). For a `related:` edge it will be the reason the author wrote. That is the schema #107
asks to share.

### Store API (`carta/graph/store.py`)

```python
class EdgeStore:
    def __init__(self, db_path: Path) -> None            # creates dir, .gitignore and schema; rebuilds an older or corrupt file; raises StoreVersionError on a newer one
    def close(self) -> None
    def __enter__(self) -> "EdgeStore"; def __exit__(self, *exc) -> None
    def transaction(self)                                 # context manager, BEGIN IMMEDIATE .. COMMIT/ROLLBACK
    def get_meta(self, key: str) -> Optional[str]
    def set_meta(self, key: str, value: str) -> None
    def source_hashes(self, extractor: str) -> dict[str, str]             # path -> sha256
    def replace_source(self, extractor: str, path: str, sha256: str,
                       extractor_version: int, edges: list[Edge], meta: str = "{}") -> None  # delete that source's rows, insert, upsert sources
    def source_meta(self, extractor: str) -> dict[str, str]                # path -> canonical JSON
    def set_source_meta(self, extractor: str, path: str, meta: str) -> None
    def source_edges(self, extractor: str, path: str) -> list[Edge]
    def remove_source(self, extractor: str, path: str) -> None
    def clear_extractor(self, extractor: str) -> None
    def iter_edges(self, kinds: Optional[Sequence[str]] = None,
                   dst_types: Optional[Sequence[str]] = None,
                   extractors: Optional[Sequence[str]] = None) -> Iterator[Edge]   # ORDER BY kind, src, dst, line, key
    def counts(self) -> dict                              # {"sources": n, "edges": n, "by_kind": {...}, "by_dst_type": {...}}
    def edges_digest(self, extractors: Optional[Sequence[str]] = None) -> str   # sha256 over the canonical dump
```

`edges_digest()` hashes one line per edge, in `iter_edges()` order, each line the JSON array
`[kind, src, src_type, dst, dst_type, line, key, evidence, weight, meta, extractor, extractor_version]`
with `ensure_ascii=False` and compact separators, followed by one line per `sources` row. The row
`id` is excluded. With `extractors` given, only edges and sources written by those extractors are
hashed: once co-change rows exist the all-edges digest moves with every commit, so each arm's
determinism is asserted on its own digest (the mention tests use `edges_digest(("mention",))`)
(A3). This is the equality the determinism tests assert; the file's bytes are not
compared, because SQLite page layout depends on write order.

### The edge record (`carta/graph/model.py`)

```python
@dataclass(frozen=True, order=True)
class Edge:
    kind: str
    src: str
    dst: str
    dst_type: str
    line: int = 0
    key: str = ""                    # discriminator inside (kind, src, dst, line); '' for mentions
    evidence: str = ""
    weight: float = 1.0
    meta: str = "{}"                 # canonical JSON: sort_keys=True, separators=(",", ":")
    src_type: str = "doc"
    extractor: str = "mention"
    extractor_version: int = MENTION_EXTRACTOR_VERSION

@dataclass(frozen=True)
class Candidate:
    raw: str                         # the text as written
    ctx: str                         # link | code | fence | frontmatter | pattern
    line: int                        # 1-based
    evidence: str                    # already collapsed and truncated
    single: bool = True              # False when it is one token out of a multi-token code span
    pattern: str = ""                # pattern name when ctx == 'pattern'
```

`MENTION_EXTRACTOR_VERSION = 1`. Versions are **per extractor** (A3): #130 and #131 carry their
own constants and stamp their own rows, so bumping one arm never invalidates another. Any change
to what the mention extractor emits for an unchanged input bumps its version, which invalidates
every stored mention source and every replay cache entry.

## Mention extraction

Two stages, so that the expensive part can be cached by content alone:

```python
# stage 1, markdown.py: depends on the text and the configured patterns, nothing else
def scan_text(text: str, patterns: Sequence[PatternSpec] = ()) -> list[Candidate]

# stage 2, mentions.py: depends on where the doc sits and on which paths exist
def resolve_candidates(doc_path: str, candidates: Sequence[Candidate], index: PathIndex,
                       settings: GraphSettings) -> list[Edge]

# the pure function of the architecture: (doc path, doc text, set of repo paths) -> edges
def extract_mentions(doc_path: str, text: str, repo_paths, settings: GraphSettings = DEFAULT_SETTINGS) -> list[Edge]
```

`repo_paths` is either an iterable of repo-relative POSIX paths or an already built `PathIndex`.
None of the three functions reads the filesystem, the clock, the environment or git. The returned
list is sorted (`Edge` ordering) and de-duplicated on `(kind, src, dst, line, key)`, keeping the
highest `weight` (ties: the smallest edge in `Edge` ordering).

### Stage 1: where candidates come from

A line-based state machine over the text (BOM stripped, CRLF read as LF, line numbers 1-based
over the original lines):

1. **Frontmatter**: a leading `---` line through the next `---` line. Every whitespace-separated
   token, with YAML punctuation (`-`, `:`, quotes, brackets, commas) stripped from its ends, is a
   candidate with `ctx='frontmatter'`. This is what picks up `related:` and any hand-declared
   source-glob list a project keeps in frontmatter.
2. **Fenced blocks**: opened by up to three spaces then three or more backticks or tildes, closed
   by a run of the same character at least as long. Inside, each line is split on whitespace and
   on `` "'`()<>,;=[]{}| ``; a token is a candidate (`ctx='fence'`) only when it contains `/`.
   Bare filenames and identifiers inside fences are ignored: shell transcripts and code samples
   are too noisy.
3. **Markdown links and images** outside fences: inline `[text](target "title")`, `![alt](target)`
   and reference definitions `[label]: target`. The target is a candidate with `ctx='link'`.
   Angle-bracket autolinks and wikilinks are not read.
4. **Inline code spans** outside fences: text between matching backtick runs on one line. A span
   with no whitespace is one candidate, `ctx='code'`, `single=True`. A span that as a whole is an
   identifier followed by one balanced parenthesised argument list (`parse_frame(buf, n)`) is
   also one `single=True` candidate, decided **before** the whitespace test, so a call written
   with its arguments reaches the identifier filter (A12). Any other span with whitespace (a
   command line) is split on whitespace and each token containing `/` becomes a candidate with
   `single=False`. A link target inside a code span is read as code only.
5. **Project patterns**: every configured regex is run over each line in its `where` scope
   (`all`, `prose` = outside fences and frontmatter, or `code` = inside fences and code spans).
   Each match is a candidate with `ctx='pattern'`, `raw` = group 1 if the pattern has a group,
   otherwise the whole match.

Plain prose is never read for paths. A path written without backticks or a link is not extracted,
by design: precision first.

### Stage 2: path normalisation rules

Applied in order to each candidate with `ctx` in `link`, `code`, `fence`, `frontmatter`. The first
rule that rejects ends the candidate (a single-token code span then falls through to the
identifier filter).

| # | Rule |
|---|---|
| N1 | Strip surrounding whitespace, matching quotes and `<>`. Strip trailing `.,;:!` and an unbalanced trailing `)`. Keep a trailing `/`. A `ctx='link'` target is percent-decoded (`My%20Notes.md` is `My Notes.md`); a link target is therefore the one context in which a path may contain a space (A11). |
| N2 | Cut a suffix: `#fragment` (unless the text with the `#` kept names an existing file or directory, as in `src/C#/x.cs`), `?query`, `:LINE`, `:LINE:COL`, `:LINE-LINE`, `:L10-L20`, and, after a filename that has an extension, `::anything` or `:identifier` (a pytest node id `tests/test_x.py::test_y`, or `file.py:func`) (A12). A candidate that is only a fragment (`#section`) is rejected. |
| N3 | Reject if empty, longer than 300 characters, or containing whitespace, a backslash, `$`, `<`, `>`, `\|`, `"`, `'`, `…` or `...`. |
| N4 | Reject URLs and non-paths: a scheme (`^[A-Za-z][A-Za-z0-9+.-]*:`), a leading `//`, `www.`, `~`, `-` or `@`, or an e-mail shape. |
| N5 | Leading `/`: in `ctx='link'` it means the repo root (the hosting convention) and is stripped; in every other context it is an absolute filesystem path and is rejected. |
| N6 | Braces: expand one level of `{a,b,c}` into separate candidates (at most 16); any other `{` or `}` rejects (template placeholder). |
| N7 | If it now contains `*`, `?` or `[`, it is first tried as a literal name through N8 to N10 (a route directory such as `src/app/[id]/page.tsx` is a real file); only when no such file or directory exists is it a **glob**: see below. |
| N8 | Resolve to a repo-relative path with `posixpath.normpath`. `ctx='link'`: relative to the doc's directory first, then from the repo root. Other contexts: from the repo root first, then the doc's directory, then each ancestor of the doc's directory, nearest first (`meta.via='relative'` when a non-root base resolved it), so a README inside a subproject can name `src/main.c` relative to itself (A11b); a candidate starting `../` resolves from the doc's directory only; one starting `./` resolves from the doc's directory and then from the repo root (commands in docs are commonly written from the root). N9 and N10 are tried at each base in turn before the next base. A result that begins with `..` is outside the repo and is rejected. |
| N9 | Exact file in the index: an edge with `dst_type='doc'` if the target is itself a doc (matches `doc_globs`), otherwise `'file'`. |
| N10 | Directory: the candidate ends with `/`, or has two or more segments, and names a directory in the index (any directory that contains at least one path). Edge with `dst` in canonical form (trailing `/`), `dst_type='dir'`. A single bare word without a trailing slash is never a directory mention (`tests`, `docs` and `build` are words). |
| N11 | Suffix match, for a candidate with two or more segments that did not resolve: if exactly one indexed file (or directory) ends with `/` + candidate on a segment boundary, resolve to it, with `meta.via='suffix'`. Two or more matches: no edge. |
| N12 | Bare filename (no `/`), only for `ctx` `code` (single-token), `link` or `frontmatter`, and only when it has an extension matching `\.[A-Za-z0-9]{1,8}$` with a non-numeric extension: resolve to the repo-root file, else the sibling in the doc's directory, else the one file in the repo with that basename (`meta.via='basename'`). Two or more matches: no edge. |
| N13 | **Missing**: nothing resolved, `ctx` is not `fence`, the candidate has a `/`, and it has one of two shapes: (a) two or more segments and a last segment with a non-numeric extension, whatever its first segment is (so a mention of anything under a top-level directory that was since removed is still recorded); or (b) it ends with `/` and its first segment is a top-level directory in the index. For `ctx='link'` a bare filename with an extension also qualifies (a broken sibling link). It is **not** a placeholder: no adjacent segment pair `path/to`, and no segment that, compared whole and case-insensitively (the last segment by its stem), is one of `foo`, `bar`, `baz`, `qux`, `example`, `placeholder`, `xxx`, `your`, or begins `your-`, `your_`, `my-`, `my_`; a segment that names an existing directory at that position is never a placeholder (so `examples/`, `bazel/`, and a real `foo/` are ordinary directories). Its first segment is not `.git` or `.carta`, which are outside the path set by design. Edge with `dst` = the normalised path, `dst_type='missing'`. A link that is neither rooted nor explicit has two readings, beside the doc and from the repo root: the first is the edge above and the second is recorded as well with `meta.alt = true`, so that history and an impact query can find the doc under either. `./x` in inline code from a doc in a subdirectory has two readings too and is recorded under neither. Precision for the *scan finding* comes from the history gate, not from this rule (A10). |
| N14 | Drop a self-mention (`dst == doc_path`). |

Matching is case-sensitive throughout. A target is never dropped for being under
`excluded_paths`: exclusion decides which docs are *sources*, not which paths may be named.

**Globs (N7).** Supported syntax: `**` (any depth), `*` (within a segment), `?`, and character
classes (`fsm.[ch]`). `glob_to_regex` translates `**/` to `(?:.*/)?`, `**` to `.*`, `*` to
`[^/]*`, `?` to `[^/]`, and passes a `[...]` class through. A glob must contain a `/` or begin
`*.`; it is anchored at the repo root (or the doc's directory when it starts `./` or `../`); it
must match at least one indexed path, otherwise no edge (never a `missing` edge). Stored as
`dst` = the glob, `dst_type='glob'`, `meta.matches` = the match count.

**Edge fields for a path mention:** `kind='mention'`; `weight` is 1.0 for `link`, `code` and
`frontmatter` and 0.5 for `fence`; `meta` holds `ctx`, `raw` (when it differs from `dst`) and
`via` (when N11/N12 resolved it). A doc-to-doc mention is exactly a `mention` edge whose
`dst_type` is `'doc'`; nothing else is needed to tell it from doc-to-source.

### The identifier shape filter (`idents.py`)

```python
def identifier_token(span: str, *, min_length: int = 3, stoplist: frozenset[str] = frozenset()) -> Optional[str]
```

Applied only to a **single-token inline code span that did not become a path edge**. It returns
the normalised token, or `None`. The stored edge is `kind='ident'`, `dst_type='token'`,
`dst` = the token, unresolved, for #131 to resolve.

Normalise: strip one leading `&`, `*` or `@`; strip a trailing call suffix `(...)` (remember that
it was there); strip a trailing `;` or `,`.

Accept only if **all** hold:

1. Length between `min_length` and 80.
2. It matches `^[A-Za-z_][A-Za-z0-9_]*(?:(?:\.|::|->|#)[A-Za-z_][A-Za-z0-9_]*)*$`. This alone
   drops flags (`--force`), assignments (`KEY=value`), paths, numbers, versions (`v1.2.3`),
   shell variables (`$HOME`), anything with spaces (`git commit -m`), and punctuation.
3. It has **code shape**, at least one of:
   - contains `_` between alphanumerics (`parse_frame`, `MAX_RETRIES`);
   - has a lower-to-upper transition (`parseFrame`, `EdgeStore`);
   - is qualified by `.`, `::`, `->` or `#` with every part at least two characters
     (`store.get_meta`, `Parser::next`);
   - was written as a call (`main()`, `init()`).
4. It is not rejected by any of:
   - every part is in the lowercase prose/keyword stoplist built into the module (language
     keywords and literals such as `true`, `false`, `null`, `none`, `self`, `this`, and so on)
     or in the configured `stoplist`;
   - it is the name of a Python builtin exception or warning (`RuntimeError`, `KeyError`,
     `FileNotFoundError`, …), which passes the case-hump rule but is never defined in a
     repository (A18);
   - a qualified token whose last part is a known file extension (`config.yaml`: that is a
     filename, handled by N12, and an unresolved one is not an identifier) or a top-level domain
     (`com`, `org`, `net`, `io`, `dev`, `ai`, `sh`);
   - an abbreviation shape (`e.g`, `i.e`: parts shorter than two characters).

So a plain lowercase word in backticks (`` `git` ``, `` `pip` ``, `` `main` ``, `` `string` ``)
is never an identifier, an all-caps acronym without an underscore (`` `HTTP` ``, `` `TODO` ``) is
never an identifier, and a multi-word command never reaches the filter.

**Identifier precision is unmeasured** (A18). The case-hump rule also accepts backticked product
and third-party names (`GitHub`, `PyTorch`, `QdrantClient`). They are not ranked by `carta impact`
and so cost nothing today, but #131 must not inherit this filter as settled: `carta graph status`
reports the `ident` count so the size of that input is visible, and its precision is to be
measured when #131 resolves it.

### Project patterns

```yaml
graph:
  patterns:
    - name: message              # required, [a-z0-9_-]+, unique
      regex: '\bMSG_[A-Z0-9_]+\b'   # required, Python re syntax
      where: all                 # all | prose | code   (default all)
```

A match becomes `kind='pattern'`, `dst_type='pattern'`, `dst` = `<name>:<captured text>`. An
invalid regex, a duplicate or malformed name, or an unknown `where` raises `GraphConfigError`
(a `carta.config.ConfigError` subclass) naming the pattern, at settings-load time, before any
file is read. Patterns run with the stdlib `re`; a pathological regex is the project's own risk
and is documented as such.

## Build

```python
# build.py
def list_repo_paths(repo_root: Path) -> list[str]
def build_graph(repo_root: Path, cfg: dict, *, force: bool = False) -> dict
```

`list_repo_paths`: `git ls-files -z --cached --others --exclude-standard` run with
`cwd=repo_root`, so the list is relative to the Carta root and limited to it even when that root
is a subdirectory of a larger checkout. Entries that are deleted in the working tree but still in
the index are dropped by an existence check. An entry ending in `/` is a nested repository or
worktree, which git reports as one opaque entry, and is dropped. When the directory is not a git
work tree, or git is absent, it falls back to `os.walk` skipping `.git` and the always-excluded
directories. In **both** modes everything under `.carta/` is left out of the path set: it holds
the store itself and Carta's runtime files, and letting them in would make every build change the
path set and so force the next one to be a full pass (A15). The result is sorted.

`build_graph`:

1. Load settings; compute `settings_sha`. List paths; compute `paths_sha`.
2. Docs are the paths that match `graph.doc_globs` and are not excluded (`excluded_paths` plus the
   always-excluded directories, through `carta.graph.paths.is_excluded_rel`, which has the same
   semantics as `carta.scanner.scanner.is_excluded` and a parity test to keep it so).
3. A **full pass** is required when `force` is set, or the stored `mention.settings_sha`,
   `mention.paths_sha` or `mention.extractor_version` differs. Otherwise the pass is
   **incremental**: only docs whose content hash differs from `sources.sha256`.
4. Every doc is read and hashed on every build (bytes, CRLF folded to LF, sha256). There is no
   mtime shortcut. In a full pass every doc is re-extracted; in an incremental pass only the
   changed ones. Either way a doc's rows are rewritten only when its hash or its edge list
   actually differs.
5. Sources whose doc no longer exists (or is now excluded) are removed with their edges.
6. Meta keys are updated. One transaction for the whole build.

Returns `{"docs": n, "extracted": n, "unchanged": n, "removed": n, "edges": n, "full": bool,
"digest": "<sha256>", "by_kind": {...}}`.

The honest cost statement: an incremental build still reads every doc, and adding or deleting any
file triggers re-extraction of all docs, because whether a candidate resolves depends on the path
set. For a corpus of hundreds of markdown files that is well under a second of regex work. The
plan carries a budget test (500 synthetic docs of about 16 kB with about 110 code spans each,
the measured shape of a real doc, full build under 10 s on CI). If a corpus ever
outgrows that, the two-stage split means a stage-1 cache keyed by content hash drops in without
touching the extractor; it is not built now.

## Query: `carta impact`

### Ranking

The ranking core is a pure function over an in-memory index, and it is the *same* function the
replay eval scores, so the eval measures what ships.

```python
# query.py
class ImpactIndex:
    def __init__(self, edges: Iterable[Edge], n_docs: int) -> None
    def df(self, kind: str, dst: str) -> int               # distinct source docs with that kind of edge to dst

def rank_impact(index: ImpactIndex, paths: Sequence[str], *,
                kinds: Sequence[str] = ("mention",),
                weights: Mapping[str, float] = DEFAULT_KIND_WEIGHTS,
                top: int = 10, min_score: float = 0.0,
                exclude_docs: Collection[str] = ()) -> list[dict]
```

For a queried path `q` and a doc `d`, let `T(d, q)` be the targets `t` that `d` has an edge to
and that **match** `q`:

| Match | Condition | `spec(t, q)` |
|---|---|---|
| exact | `t == q` (types `file`, `doc`, `missing`) | 1.0 |
| dir | `t` is a `dir` target and `q` lies under it, `depth` segments below (a direct child has depth 1) | `1 / (1 + depth)` |
| glob | `t` is a `glob` target whose regex matches `q` | 0.5 |
| under | `q` ends with `/` (the caller asked about a directory) and `t` lies under it | 1.0 |

Then:

```
idf(t)        = ln(1 + N / df(t))                    N = docs in the corpus, df(t) = docs mentioning t
tf(d, t)      = 1 + ln(n)                            n = distinct lines of d that mention t
contribution(d, q) = max over t in T(d, q) of
                     w_kind * spec(t, q) * idf(t) * tf(d, t) * w_edge(d, t)
score(d)      = sum over q of contribution(d, q)
```

`w_edge(d, t)` is the largest stored `weight` among that doc's edges to `t` (1.0, or 0.5 when the
only mentions are inside fenced blocks). `w_kind` comes from `graph.impact.weights`.

What the formula encodes, on the 74-doc scale of the measured corpus: a file named by 2 docs has
`idf = ln(1 + 37) = 3.64`; a file named by 60 docs has `idf = ln(1 + 1.23) = 0.80`. A doc that
names the exact file outranks one that names its parent directory by 2x and its grandparent by 3x
before `idf` is applied, and directory targets are usually common, so their `idf` is low as well.
Taking the maximum per queried path means a doc is not paid twice for naming both a file and its
directory; summing across queried paths means a doc that covers more of a change ranks higher.
Repeating a mention helps only logarithmically.

**Amended aggregation (A7).** Summing a directory match once per queried path let a doc that
names a common directory outrank a doc that names one changed file exactly on any multi-file
diff. So `dir` and `glob` matches are aggregated **per target, not per queried path**:

```
value(d, t, q) = w_kind * spec(t, q) * idf(t) * tf(d, t) * w_edge(d, t)

exact/under:  each queried path q that d matches exactly contributes
              max over its exact targets of value(d, t, q), as above.
dir/glob:     each queried path q that d does NOT match exactly is assigned to the one
              dir/glob target of d with the largest value(d, t, q) (ties: smaller target string).
              A target t with assigned set A_t then contributes ONCE:
                  max over q in A_t of value(d, t, q)  *  (1 + ln |A_t|)
score(d)    = sum of exact contributions + sum of per-target contributions
```

A path is reported in `unmatched_paths` when no doc has an exact, glob, or **depth-1** directory
match for it; a doc naming a top-level package directory does not make every file beneath it
"matched". The ranking tests include "an exact mention of one file beats a directory mention
that covers ten".

**Direction of file-sourced edges (A9).** `rank_impact` returns docs. For a row with
`src_type='doc'` the returned doc is `src` and the queried path is matched against `dst`. For a
row with `src_type='file'` (co-change) the returned doc is `dst`, read only when
`dst_type='doc'` and `src` is a queried path; a co-change writer stores both ordered directions,
so no reverse lookup is needed. The ranking tests carry `src_type='file'` rows in both
directions.

Results are sorted by `score` descending, then doc path ascending. Scores are rounded to 6 places
at output, never before sorting.

Later kinds use the same function: a `cochange` edge from a queried file to a doc contributes
`w_cochange * weight` (its stored score, no `idf`, since the score is already normalised), and a
`symbol` edge is a `mention`-shaped edge with its own `w_kind`. `kinds` selects which are read.

### CLI

```
carta impact [PATH ...] [--diff [RANGE]] [--top N] [--kinds K[,K...]] [--include-docs]
             [--min-score X] [--no-build] [--json]
```

`PATH` and `--diff` are **mutually exclusive** (A16): argparse would otherwise read
`carta impact --diff src/a.py` as a range named `src/a.py`. Giving both exits 1; and when the
`--diff` value is not a revision range but does name an existing path, the error says so
("`src/a.py` is a path, not a range; pass it without --diff").

- `PATH ...`: repo-relative paths (or paths relative to the current directory; both are
  normalised to repo-relative). A trailing `/` asks about everything under a directory.
- `--diff [RANGE]`: take the paths from `git diff --name-only --no-renames --relative RANGE`.
  Bare `--diff` means `<default-branch>...HEAD`, the same default as `carta hook check --diff`.
  Docs changed by the diff are left out of the *query* unless `--include-docs`, and any result
  doc that the diff already edits is marked `in_diff`.
- `--top N`: default `graph.impact.top` (10). `--kinds`: default `graph.impact.kinds`
  (`mention`). `--min-score`: default `graph.impact.min_score` (0).
- The store is brought up to date (incremental build) before the query unless `--no-build`.
- Exactly one of `PATH ...` or `--diff` is required; otherwise exit 1 with usage. Exit 0 for any
  successful query, including one with no results.

Human output:

```
3 docs mention 2 changed paths (74 docs, 1,912 edges)

 1. docs/design/parser.md                                   score 5.83
      src/parser/frame.py       exact, named by 2 docs
        L41: The framing rules live in `src/parser/frame.py`.
        L88: See [frame.py](../../src/parser/frame.py) for the state table.
      src/parser/checksum.py    via src/parser/ (dir), named by 5 docs
        L12: Everything under `src/parser/` is covered by this document.
 2. README.md  [already in this diff]                        score 0.80
      ...

No doc mentions: tools/gen_tables.py
```

At most 3 evidence lines are printed per target (all of them are in the JSON).

`--json` prints exactly this object and nothing else:

```json
{
  "schema": 1,
  "query": {"paths": ["src/parser/frame.py", "tools/gen_tables.py"],
            "diff": "main...HEAD", "kinds": ["mention"], "top": 10, "min_score": 0.0},
  "store": {"docs": 74, "edges": 1912, "digest": "9f2c...", "built": true},
  "matched_docs": 1,
  "results": [
    {"rank": 1, "doc": "docs/design/parser.md", "score": 5.83, "in_diff": false,
     "matches": [
       {"path": "src/parser/frame.py", "target": "src/parser/frame.py", "target_type": "file",
        "kind": "mention", "match": "exact", "df": 2, "contribution": 5.1,
        "evidence": [{"line": 41, "text": "The framing rules live in `src/parser/frame.py`."}]}
     ]}
  ],
  "unmatched_paths": ["tools/gen_tables.py"],
  "skipped_paths": [{"path": "docs/notes.md", "reason": "doc"}]
}
```

`diff` is `null` for an explicit path query. Keys are always present; lists are empty rather than
absent. `matched_docs` is how many docs matched before `top` and `min_score` were applied, so an
empty or cut result can say so. `top` below 1 and a non-finite `min_score` are errors, on the
command line and in the config.

```
carta graph build [--force] [--json]
carta graph status [--json]
carta graph replay ...            (the eval; see below)
```

`graph build` prints one line (`Graph: 74 docs, 1,912 edges (3 re-extracted, 71 unchanged)`) or
the `build_graph` dict. `graph status` never builds: it prints whether the store exists, its
schema and extractor versions, doc and edge counts by kind and by target type, the digest, and
whether it is **stale** (settings, path set or any doc hash differs from what is stored), as
`{"exists", "schema_version", "extractor_version", "docs", "edges", "by_kind", "by_dst_type",
"digest", "stale", "stale_reasons"}`.

`impact`, `graph build` and `graph status` need a Carta project (`.carta/config.yaml`), like
every other command. They load the config but never contact Qdrant or Ollama.

### MCP

```python
@mcp_server.tool()
def carta_impact(paths: list[str] | None = None, diff: str | None = None,
                 top_k: int = 10, kinds: list[str] | None = None,
                 include_docs: bool = False) -> dict
```

Returns the same object as `carta impact --json`, or `{"error": "invalid_request" |
"service_unavailable", "detail": "..."}` in the server's existing style. `diff=""` means the
default range. `carta.graph` is imported inside the handler, so the server's import cost is
unchanged, and the handler never prints.

## Scan finding: `missing_path_mention`

`carta scan` gains one finding type, built from the `dst_type='missing'` edges.

```python
# scan.py
def missing_path_findings(repo_root: Path, cfg: dict) -> list[dict]
```

A `missing` edge already passed N13 (looks like a repo path, not a URL, not a placeholder, not
outside the repo, not in a fenced block). It becomes
a finding only if it also passes all of:

1. The path does not exist on disk either (covers ignored build outputs that are present).
2. The doc is not matched by `graph.scan.ignore_docs` (default: changelogs).
3. **There is positive evidence the path used to be real or is a broken link.** One of:
   - git history deleted or renamed that exact path, found with a single
     `git log --no-merges -M --diff-filter=DR --name-status --format= -z --relative -n <history_limit>`
     run with `cwd` at the Carta root, so the paths are root-relative like the edges even when
     the root is a subdirectory of the checkout (A15). A `missing` **directory** counts as
     evidenced when any deleted path lies under it (git lists files only) (A10);
   - the mention is a markdown link target (`meta.ctx == 'link'`) **and** the target's extension
     matches a doc glob or its parent directory exists in the index, **and**
     `git check-ignore` does not match it, **and** the edge is not an `alt` reading. This is
     the only history-free reason, and the two
     conditions keep it off site-root links (`/guide/setup/`), rendered names (`setup.html` for
     `setup.md`) and links into generated output (`htmlcov/index.html`) (A11). Those cases, and a
     percent-encoded link to a file with a space in its name, are rows in the precision table.

A history hit is dropped as well when `git check-ignore` matches the path: a build output or a
local config that was committed once, then untracked and ignored, is not "deleted from the
repository", it is simply not generated on this checkout. When history confirms one reading of
a link, the other reading of the same link is not also reported as broken.

A path that never existed and was written in backticks (a planned file, an output name, an
illustrative path) produces no finding. That is the precision choice: the finding fires when Carta
can show the path was real.

Shape, following the scanner's conventions (`type`, `severity`, `doc`, `detail`, plus extras):

```json
{"type": "missing_path_mention", "severity": "warning", "doc": "docs/design/parser.md",
 "detail": "mentions `src/parser/old_frame.py` (line 41), which no longer exists; renamed to src/parser/frame.py",
 "path": "src/parser/old_frame.py", "line": 41, "reason": "renamed", "renamed_to": "src/parser/frame.py"}
```

`reason` is `renamed`, `deleted` or `broken_link`; `renamed_to` is present only for `renamed` and
only when the new path still exists. One finding per (doc, path), on the first line that mentions
it. Sorted by doc, then line.

`run_scan` calls it after the structural checks, inside a `try`: any failure (no git, a locked
store, a bad pattern) yields no findings and never fails the scan. It is skipped when
`graph.enabled` or `graph.scan.missing_paths` is false. `stats` gains `missing_path_mentions`.

## Configuration

A new top-level `graph` section in `carta/config.py` `DEFAULTS`. It is unrelated to
`search.graph`, which keeps controlling `related:` expansion in retrieval.

```yaml
graph:
  enabled: true
  doc_globs: ["**/*.md"]          # which files are docs (sources of edges, and the eval's gold)
  identifiers:
    enabled: true
    min_length: 3
    stoplist: []                  # extra tokens never stored as identifiers
  patterns: []                    # [{name, regex, where}]
  impact:
    top: 10
    min_score: 0.0
    kinds: ["mention"]
    weights: {mention: 1.0, symbol: 0.8, cochange: 0.5}
  scan:
    missing_paths: true
    ignore_docs: ["CHANGELOG*", "**/CHANGELOG*"]
    history_limit: 5000           # commits of history searched for deletions and renames
  replay:
    hub_share: 0.2
    max_files: 100
    popularity_window: 200
    min_population: 30            # below this the report says "inconclusive"
```

`excluded_paths` (top level, existing) is honoured for doc discovery. `load_config` adds `graph`
to the keys that must be mappings. `graph_settings(cfg)` deep-merges `cfg.get("graph")` over the
defaults itself, so a minimal test config with no `graph` key works. The settings fingerprint
covers `doc_globs`, `identifiers`, `patterns`, `excluded_paths` and `MENTION_EXTRACTOR_VERSION`: exactly
the inputs that change what is extracted.

## The eval: PR replay

`carta/graph/eval/replay.py`, reachable as `carta graph replay` and
`python -m carta.graph.eval.replay`.

**The idea.** Every mainline commit that touched both code and docs is a free labelled example:
the code it touched is the query, the docs it edited are the answer.

```
carta graph replay [--repo PATH] [--range A..B | --last N] [--arm NAME=KIND[+KIND...]]...
                   [--k 1,3,5,10] [--hub-share F] [--max-files N] [--popularity-window N]
                   [--doc-glob GLOB]... [--per-commit] [--no-cache] [--cache-dir DIR] [--json [PATH]]
```

`--repo` defaults to the current git work tree and needs **no** Carta config (defaults apply),
so the eval can be pointed at any repository. `--last N` takes the most recent N mainline
commits; the default is `--last 0`, the **whole** first-parent history, because eligible commits
are a small share of scanned ones (A5); `--range` takes an explicit range. Default arm: `mention=mention`.

### Algorithm

1. **Commits.** One process for the whole range (A8):
   `git log --first-parent --diff-merges=first-parent --no-renames --name-status -z --reverse
   --format=<sha and parents> <range>`. Each commit is compared with its **first parent**, which
   is one PR's whole change in a squash-merge repository and also in a merge-commit repository.
   A rename is a delete plus an add, so nothing depends on similarity heuristics.
2. **Split.** Exclusion applies to the **doc side only** (A14): a path that matches `doc_globs`
   but is excluded is neither a source nor gold, and is counted (`excluded_doc_paths`). Every
   other changed path stays in the query, whatever directory it is in: the default exclusions
   match `build/`, `dist/`, `temp/` and `tmp/` anywhere in a path, and dropping
   `tools/build/gen.py` from the query would contradict the rule that exclusion decides which
   docs are sources, not which paths may be named. Doc paths go to one side, everything else to
   the query.
   - **gold** = docs the commit modified or deleted. A doc the commit *creates* is not gold: it
     did not exist at the parent, so no method could have ranked it. Such docs are counted.
   - **query** = every non-doc path the commit added, modified or deleted.
3. **Eligibility.** A commit is scored iff query and gold are both non-empty. Otherwise it is
   skipped under exactly one reason, checked in this order: `root_commit` (no parent), `empty`
   (no changes against the first parent), `bulk` (more than `max_files` changed paths),
   `docs_only` (no non-doc path), `no_docs_edited` (no doc path at all: a source-only commit),
   `only_new_docs` (docs were touched but all were created).
4. **No leakage.** Everything a method sees for commit `c` comes from the tree of `c`'s parent,
   read through git objects only: `git ls-tree -r -z <parent>` for the path set and blob ids, one
   long-lived `git cat-file --batch` for contents. The working tree and the index are never read,
   and the store at `.carta/graph/edges.db` is never opened. Counts used by the popularity
   baseline cover only commits strictly before `c`.
5. **Edges at the parent.** For each doc in the parent tree, stage 1 (`scan_text`) is cached by
   `(blob id, MENTION_EXTRACTOR_VERSION, settings fingerprint)`; stage 2 (`resolve_candidates`)
   is run against the parent's `PathIndex`. **Cost is bounded as follows (A8).** One mutable
   `PathIndex` is kept across the walk and each commit's adds and deletes are applied to it
   (`PathIndex.add` / `PathIndex.remove`; suffixes, basenames and directories are dict-indexed,
   never scanned). When a commit changes the path set, only these docs are re-resolved: a doc
   whose blob changed; a doc holding a glob candidate; and a doc that has a candidate whose last
   segment, or any directory segment, appears among the segments of the added or deleted paths.
   Every other doc keeps its edges. A test asserts this gives the same edges as extracting from
   scratch at every step, and a budget test (200 synthetic commits over 100 docs) bounds the
   run time.
6. **Rank.** Each arm builds an `ImpactIndex` from the edges of its kinds and calls `rank_impact`
   with the query paths and `top = max(k)`. Baselines produce a ranked list from the same inputs.
7. **Score** against gold, aggregate, bootstrap, report.

**Caching.** Memory always; on disk too unless `--no-cache`, in one SQLite file at
`<carta home>/cache/graph-replay/<sha256 of the repository's absolute path, first 16 hex>.db` (or
under `--cache-dir`), with tables `scan(blob, fingerprint, payload)` and
`tokens(blob, version, payload)`. The carta home comes from `carta.registry.carta_home()`, the
project's single definition of machine-level state, which honours `CARTA_HOME`; the test suite
points that at a temp directory, and a test asserts a replay creates nothing under
`Path.home()` (A13). A blob id is a content address, so an entry is stale only when the code
that read it changes: the fingerprint column covers the extractor and settings, and the
`version` column (`TOKENIZER_VERSION`) covers `basename_tokens`. Replaying 300 commits reads each distinct doc blob once, and a
second run reads none.

**Arms and later edge kinds.** An arm is a named set of edge kinds. Each kind has a provider:

```python
class EdgeProvider(Protocol):
    kind: str
    def edges_at(self, snapshot: "Snapshot") -> list[Edge]: ...
    def observe(self, commit: str, changes: Sequence[tuple[str, str]]) -> None: ...

PROVIDERS: dict[str, EdgeProvider]      # 'mention' registered here; #130 and #131 register theirs
```

Providers are **stateful and driven in commit order** (A9). The loop calls `edges_at(snapshot)`
to score a commit and only afterwards `observe(commit, changes)` with that commit's name-status
list, so a history-mined provider (co-change) updates its counts once per commit instead of
re-walking history at every step, and can never see the commit it is being scored on. A provider
instance belongs to one run. `Snapshot` exposes the parent commit id, the path index, the doc
list, a blob reader, and `prior_changes` (the `(sha, name-status list)` of every mainline commit
strictly before it, warm-up included) for a provider that prefers to be stateless. Asking for a kind with no provider is an error that names the kind. So
`--arm mention=mention --arm both=mention+cochange --arm cochange=cochange` will give #130 its
three numbers (each alone, and the union) on one population in one run.

### Baselines, scored on the identical population

- **`popularity`**: docs ranked by how many of the previous `popularity_window` mainline commits
  edited them, counting only commits before the one being scored (history before the eval window
  is read for warm-up). Ties by path. Only docs present at the parent. This is "always suggest
  the docs people edit most", and it is strong wherever a changelog exists.
- **`basename`**: docs whose text contains the basename (with extension) of a changed file as a
  whole token, ranked by the number of distinct changed basenames found, then total occurrences,
  then path. It is what `grep -wF "$(basename f)"` over the docs gives, with no path
  understanding. Doc text is tokenised once per blob (split on anything outside
  `[A-Za-z0-9_.+-]`, trailing dots stripped) and cached.
- **`basename_idf`** (A6): the same basename matches, scored with the **same** `idf` and `tf` as
  `rank_impact` (`idf` over docs containing the basename, `tf = 1 + ln(occurrences)`, summed
  over changed basenames). Plain `basename` has no rarity weighting and so favours the longest
  docs; a win over it could be credited to `idf` rather than to edges. `graph[mention]` against
  `basename_idf` is the honest ablation: same weighting, token match against path understanding.

### Metrics

Per eligible commit, with ranked list `R` and gold set `G`:

- `recall@k = |R[:k] ∩ G| / |G|`
- `precision@k = |R[:k] ∩ G| / k` (divided by `k`, not by how many were returned)
- `any_hit@k = 1 if R[:k] ∩ G else 0`
- `rr = 1 / rank` of the first gold doc anywhere in the ranking, 0 when there is none; **MRR**
  is its mean. It is deliberately not truncated at `max(k)`: a truncated value, and with it the
  paired interval and the verdict, would move with the `--k` list

for `k` in 1, 3, 5, 10, macro-averaged over eligible commits, plus the mean number of docs
returned. Each method gets a 95% bootstrap interval on recall@5, any_hit@5 and MRR, and each
arm gets a **paired** bootstrap against each baseline (delta, interval, and the counts of
commits better / worse / tied), 2,000 resamples from `random.Random(0)`, so the numbers are
reproducible.

**Hub variant.** A hub is a doc that is **in gold for more than `hub_share` of the eligible
commits** (a changelog, a status page). The denominator is the eligible population, not the
scanned one (A4): most scanned commits are ineligible, so a changelog that is gold in more than
half of the scored commits can still be under 20% of the scanned ones, and the variant would be
the same table. Hubs inflate every method: predicting the changelog is always right. The report
lists each hub with its share and repeats every table with hubs removed from gold and from every
ranking. They are removed **before** truncation to top-k, by passing them as `exclude_docs` to
`rank_impact` and to every baseline; removing them afterwards would shorten the popularity list,
which ranks hubs first by construction, and bias the comparison. Commits whose gold becomes
empty leave that variant's population, and its size is stated. Hubs are found over
the whole window, which uses later commits, but it is a filter on the population applied
identically to every method, not an input to any of them.

**Alarm rate.** Source-only commits (`no_docs_edited`) have no labels, so they are not scored,
but for each method the report gives the share of them for which it returned anything and the
mean number returned at 5. A method that always returns ten docs looks the same on both
populations; a selective one does not. This is reported as an unlabelled proxy, not as a
false-positive rate: some of those commits should have edited a doc.

### Report

The population is always printed first, in both formats:

```
Replay  repo=<head sha>  range=<A>..<B>
Population: 300 commits scanned, 96 eligible (32.0%)
  skipped: no_docs_edited 141, docs_only 48, only_new_docs 9, bulk 4, empty 1, root_commit 1
  gold docs per eligible commit: mean 1.8, median 1
  docs in corpus at range end: 74     created-in-commit docs not counted as gold: 31

                     R@1    R@3    R@5    R@10   P@5    any@5  MRR    returned
graph[mention]       ...
popularity           ...
basename             ...

graph[mention] vs popularity   dMRR +0.xx [lo, hi]   better n / worse n / tied n
graph[mention] vs basename     dMRR +0.xx [lo, hi]   ...

Without hub docs (>20% of commits: CHANGELOG.md, ...): 71 eligible
  ...same tables...

Source-only commits (141, unlabelled): share with any prediction / mean returned at 5
  ...
```

`--json` carries the same content as
`{"schema", "repo": {"head", "range"}, "settings", "population": {"commits_scanned", "eligible",
"skipped": {...}, "gold_docs_mean", "gold_docs_median", "docs_at_end", "new_docs_not_gold"},
"methods": {name: {"n", "recall_at": {"1": ..}, "precision_at": {..}, "any_hit_at": {..}, "mrr",
"mean_returned", "ci95": {...}}}, "paired": {...}, "no_hub": {"hub_docs": [...], "population":
{...}, "methods": {...}, "paired": {...}}, "alarm": {...}, "per_commit": [...]}`.
`per_commit` (commit id, counts, gold, first-gold rank per method) is included only with
`--per-commit`. No timestamps and no absolute paths appear in the output; floats are rounded to
6 places.

### How results will be judged

The feature is judged on impact queries, not on retrieval metrics. It is worth shipping if, on a
real repository's history, `graph[mention]` beats all three baselines on MRR with a paired
interval that excludes zero **in the no-hub variant**. Beating popularity only with hubs included
proves nothing.

**Power (A5).** This repository alone cannot decide that: about 59 of its 405 first-parent
commits are eligible, and far fewer once the real hubs are removed. So a variant whose population
is below `graph.replay.min_population` (30) prints a warning and is labelled **inconclusive**
rather than pass or fail; the self-evaluation runs over the full history, not the last 300
commits; and the headline numbers are to come from a second, larger public repository with a
long squash-merge history, run with the same command (`--repo`). The numbers, with their populations, go into a *Results* section appended to this spec
once the implementation runs.

## Decisions on the issue's "To explore" list

| Question | Decision | Reason |
|---|---|---|
| Precision of backticked identifiers | A shape filter, not a word list: a token must be a syntactic identifier **and** carry code shape (underscore, case hump, qualifier, or call parentheses). Extracted from single-token inline spans only, never from fenced blocks. Stored unresolved as `kind='ident'`; they do not take part in `carta impact` ranking. | Prose words, flags and commands fail on shape without a stoplist to maintain, and unresolved tokens cannot mislead a query they are not part of. Their precision gets measured when #131 resolves them. |
| Renames and moves | Edges do **not** follow `git mv`. The old path becomes a `missing_path_mention` finding, and the finding names the new path when git recorded the rename. | The doc's text is now wrong on the page. Following the rename silently would hide the one thing the reader has to fix. |
| Globs and directory mentions | Both are stored as written (`dst_type` `dir` / `glob`) and matched against the queried path at query time, discounted by distance (`1/(1+depth)` for a directory, 0.5 for a glob). One level of `{a,b}` is expanded; `**`, `*`, `?` and `[...]` are supported. | Storing the mention rather than its expansion keeps the edge count small and keeps the edge valid for files created under the directory later. The discount keeps "names the folder" below "names the file". |
| Worktrees and nested checkouts | The store belongs to the working tree that holds `.carta/`: each worktree builds its own, and nothing is kept in the shared git directory. Paths come from `git ls-files` run at the Carta root, so nested repositories and submodules are opaque, and worktree directories stay excluded as they already are. | A worktree is on a different branch with different docs; a shared store would be wrong for all but one of them. Rebuilding is cheap. |
| Feed graph expansion in `run_search`? | Not in this work. Doc-to-doc mention edges are stored and distinguishable (`dst_type='doc'`), so a later change can add them to `build_related_graph`; nothing in `run_search` changes now. | Expansion was measured neutral at 0.9.0 because a listwise reranker rescored the pool regardless of order, and #107 concluded the lever is evidence, not order. Wiring it changes retrieval, which must be gated by the retrieval eval; that needs the vector stack this arm is defined not to need. The issue itself calls it secondary. |

Further decisions made while refining:

| Topic | Decision | Reason |
|---|---|---|
| What is a doc | Any path matching `graph.doc_globs` (default every `*.md`, repo-wide, so the README and agent-instruction files count), minus `excluded_paths`. Not limited to `docs_root`. | The reverse lookup is most useful for exactly the top-level docs that sit outside `docs/`. |
| mtime fast path | None: every doc is hashed on every build. | mtimes differ across clones and worktrees and would make the store unreproducible; hashing a few hundred small files costs milliseconds. |
| Schema changes | Bump `SCHEMA_VERSION`, delete, rebuild. No migrations ever. | The store is a cache of the repository. |
| Plain-prose paths | Not extracted. | A path outside backticks or a link is indistinguishable from prose with a slash in it ("and/or", "read/write"). |
| Wikilinks | Not read. | They resolve by title, not path, which is the `related:` resolver's job; out of scope here. |
| Never-existed paths | Stored as `missing` edges (they match an exact query, so a doc written before the code is found once the file lands) but never reported by `carta scan`. | Planned files and output names are not errors. |

## Review amendments

An adversarial review of this spec raised 18 points before any code was written. All were
accepted; each is folded into the text above and tagged where it lands. None was rejected. Two
were accepted with a narrower change than suggested, noted in the table.

| # | Sev. | Point | Disposition |
|---|---|---|---|
| A1 | blocker | `carta.scanner.scanner` loads the whole embed package, so the import-hygiene test could never pass. | Accepted. Removed from the allowed imports; exclusion reimplemented in `paths.py` with a parity test. |
| A2 | major | `UNIQUE (kind, src, dst, line)` drops the second of two identifiers on one doc line defined in one file. | Accepted. New `key` column in the unique constraint; the contract test inserts two such rows. |
| A3 | major | No per-node state for an incremental co-change miner; one global extractor version; one digest over all kinds. | Accepted. `sources.meta`; per-extractor versions; `edges_digest(extractors)`. |
| A4 | major | Hub share measured against scanned commits makes the no-hub variant a copy. | Accepted. Hubs defined over the eligible population, listed with shares, removed before truncation. |
| A5 | major | The acceptance test is underpowered on this repository. | Accepted. Minimum population and an "inconclusive" label; full-history default; a second repository for the headline. |
| A6 | major | The basename baseline lacks rarity weighting, so a win cannot be credited to edges. | Accepted. Third baseline `basename_idf`. |
| A7 | major | Per-path summing lets a directory mention outrank an exact one on multi-file diffs. | Accepted. Per-target aggregation with `1 + ln(covered)`; stricter `unmatched_paths`. |
| A8 | major | Replay re-resolves every doc on every path-set change, with no bound. | Accepted. Mutable dict-indexed `PathIndex`, selective re-resolution, one `git log` process, a replay budget test. |
| A9 | major | Stateless providers cannot serve co-change; ranking direction for file-sourced rows unstated. | Accepted. `observe()` after scoring, `prior_changes` on `Snapshot`, direction stated. |
| A10 | major | N13 misses removed top-level directories, treats `examples/` and `bazel/` as placeholders, and never evidences a removed directory. | Accepted. Top-level requirement dropped for extension-bearing paths; whole-segment placeholders that never shadow a real directory; directory evidence from deleted paths beneath it. Narrowed: hyphen- or underscore-delimited `your-` / `my-` prefixes are kept, since the delimiter removes the real-word collision. |
| A11 | major | `broken_link` fires with no history on site links, rendered names and generated output; links are not percent-decoded. | Accepted, as written. |
| A11b | minor | A nested README naming a path relative to itself gets no edge. | Accepted. Extra resolution bases after the repo root. Root stays first: it is the dominant convention, and nearest-first would re-point existing unambiguous mentions. |
| A12 | minor | Pytest node ids and calls written with arguments are dropped. | Accepted, both. |
| A13 | minor | Replay cache bypasses `CARTA_HOME`; token cache has no version. | Accepted. |
| A14 | minor | Replay drops excluded non-doc paths from the query. | Accepted. Exclusion is doc-side only. |
| A15 | minor | Nested repos enter the path set as files; `.carta/` enters it in the fallback; history query not `--relative`. | Accepted, all three. |
| A16 | minor | `--diff [RANGE]` beside positional paths is ambiguous. | Accepted. Mutually exclusive, with a message that names the ambiguity. |
| A17 | minor | `BEGIN IMMEDIATE` needs explicit transaction control on 3.10; version mismatch deletes in both directions. | Accepted, both. |
| A18 | minor | The case-hump rule admits product and exception names as identifiers. | Accepted. Builtin exception names stoplisted; `graph status` reports the count; precision recorded as unmeasured. |

## Implementation notes: store, extractor, build

Details settled while writing the first third of the work. Each is pinned by a test; none
changes the architecture.

**Stage 1**
- A fence may be indented by any amount (fences inside list items are common), not only up to
  three spaces.
- A frontmatter token is a candidate only when it contains `/` or `.`; a bare word cannot be a
  path and is never an identifier.
- Any `](target)` is read as a link, which also catches an image nested in a link's text.
  Footnote definitions (`[^1]: ...`) are not reference definitions.

**Stage 2**
- N1 also strips a matching pair of parentheses or backticks around the candidate, and bold
  markers that ended up inside a span (`**src/a.py**`); a real glob keeps its stars because they
  sit next to a slash.
- N2 cuts `?query` in links only. Elsewhere `?` is a glob character.
- N4 rejects anything that still contains `:` after N2 (an scp-style remote, a `module:attr`
  reference).
- N7: after normalising, a glob must still contain a `/` or begin `*.`, and must have at least
  one literal letter or digit outside character classes. `*/`, `**/` and a pasted regex such as
  `[^/]*` are not globs.
- A bare name (no `/`) is handled by N12 alone, so `` `Makefile` `` and `` `LICENSE` `` in
  backticks stay words. A link is explicit, so a bare link target resolves to a sibling or a root
  file whatever its name, and never by basename search: a link whose target is elsewhere is
  broken, and becomes a `missing` edge at the sibling path.
- A candidate written as a directory (`x.py/`) that names a file gives no edge.
- N11: two or more suffix matches give no edge at all, not a `missing` one. The path exists; it
  is the mention that is ambiguous.
- `dst_type='doc'` means the target is a **source** doc: it matches `doc_globs` and is not
  excluded. An excluded markdown file is a `file` target.
- An `ident` edge has `meta` `{"call": true}` when it was written with call parentheses, as a
  hint for #131. Python builtin function names (`len()`, `open()`) are in the keyword stoplist.

**Store**
- `replace_source` refuses an edge whose `src` or `extractor` is not the source being written
  (`ValueError`), so one extractor cannot write into another's rows. Two edges with one identity
  in a single write: the last wins.
- `EdgeStore.open_existing(path)` returns `None` when the file is absent and creates nothing,
  but like any normal open it rebuilds a damaged file. `EdgeStore.open_readonly(path)` never
  writes; `graph_status` and `impact --no-build` use it.
- Opening creates `.carta/graph/.gitignore` (`*`) when it is missing.

**Build**
- The report also carries `enabled`, `written` (sources whose stored rows actually changed, which
  is 1 when adding a file resolves one doc's `missing` edge even though all docs were
  re-extracted) and `skipped` (docs that could not be read).
- A doc containing a NUL byte is not text: it is kept as a source with no edges. Other bytes are
  decoded as UTF-8 with replacement, so a doc with a stray invalid byte still yields its edges.
- `graph_status` reasons: `no_store`, `newer_schema`, `unreadable`, `extractor_changed`,
  `settings_changed`, `paths_changed`, `docs_changed`, in that order. `unreadable` is a file that
  is damaged or is not a store; status opens it read-only (`EdgeStore.open_readonly`) and leaves
  it exactly as found.
- `gitio.py` holds only `run_git`, `is_work_tree` and `ls_files` so far; the history readers the
  eval needs are added with it. git is always run with `GIT_DIR`, `GIT_WORK_TREE` and
  `GIT_INDEX_FILE` removed from the environment, so a build started from inside a git hook reads
  the repository it was pointed at.
- Measured on this repository (324 paths, 113 docs): 7.3k edges extracted in 0.3 s. The budget
  test (500 docs of 16 kB, 55,000 mentions) runs in under 4 s locally against a 10 s limit.
  The limit is asserted only with `CARTA_TIMING_TESTS=1`; the default run builds a fifth of that
  corpus and asserts the work done, because a wall-clock limit fails on a loaded machine.

## Implementation notes: query, CLI, MCP, scan finding

Details settled while writing the second third of the work. Each is pinned by a test.

**Ranking (`query.py`)**
- `rank_impact` returns unrounded scores (`run_impact` rounds to 6 places for output) and accepts
  `top=None` for "everything", which the replay needs.
- `idf` uses `N = max(n_docs, docs seen in the edges, df)`, so a caller passing a stale or zero
  `n_docs` can never produce a negative or undefined weight.
- When several kinds are read, a doc's value for one queried path is the **maximum** over kinds,
  the literal reading of the formula. Whether independent kinds should instead add is a question
  for #130 to settle with the replay; the per-kind arms make it measurable.
- A file-sourced row is worth `w_kind * weight` and is reported as match `cochange` with
  `target` = the queried path. It counts as a strong match, alongside `exact` and `under`.
- A queried directory (`src/parser/`) matches a `dir` target of the same name as `exact`, targets
  beneath it as `under`, and a `dir` target above it as `dir`. For `under`, a doc contributes its
  single best target beneath the directory; naming five files under it is not worth more than
  naming one. A queried path never matches a glob when it is itself a directory.
- **Reported contributions.** A `dir` or `glob` target contributes once (A7); in the output that
  amount is split evenly over the paths assigned to it, so a result's match contributions always
  sum to its score.
- **What A7 does and does not guarantee.** With per-target aggregation a directory mention
  covering `n` changed files is worth `0.5 * idf * (1 + ln n)`. At equal rarity that passes an
  exact mention of one file once `n >= 3`. The "exact beats a directory that covers ten" test
  holds because the directory is named by more docs (5 of 20 against 1 of 20), which is the usual
  case but not a law. Before A7 the same directory mention was worth `0.5 * idf * n`.
- `ImpactIndex.is_matched(path, kinds)` is the `unmatched_paths` rule: exact, `under`, glob, a
  file-sourced row, or a directory target exactly one level up.

**Query inputs**
- `normalise_query_paths(repo_root, cwd, raw) -> (paths, skipped)`. `./x` and `../x` are read from
  the current directory; otherwise the reading that exists on disk wins (current directory
  first) and a path that exists under neither is taken as repo-relative, which is what asking
  about a deleted file needs. Skip reasons: `outside_repo`, `repo_root`, and `doc` (diff mode).
- An explicit path query keeps docs in the query (doc-to-doc mentions answer "who links to this
  page"); only `--diff` drops changed docs, unless `--include-docs`.
- `git diff` is run as `git diff --name-only --no-renames --relative -z <range> --`, so a value
  that is not a revision can never be read as a pathspec, and a value starting with `-` is refused.
- `run_impact(..., cwd=None)`: the CLI passes the process directory, the MCP tool the repo root.
- `ImpactError` is a `ValueError`: the CLI prints it and exits 1; the MCP tool maps it, and a
  `GraphConfigError`, to `invalid_request`.
- `store.docs` is the number of mention sources; `store.edges` counts every edge in the store,
  identifiers included, so it equals what `carta graph status` prints.

**CLI**
- `carta graph` with no subcommand prints its usage to stderr and exits 1. `--json` on `build`
  and `status` prints the report dict with sorted keys. A disabled graph arm is exit 0 with a
  one-line notice.
- None of the three commands calls the update notifier, and a subprocess test asserts that
  running them loads no module of the vector stack.

**Scan finding**
- The history search is bounded by commits **walked**: `git rev-list --no-merges -n <limit> HEAD`
  piped into `git log --no-walk=unsorted --stdin -M --diff-filter=DR --name-status --format= -z
  --relative`. A single `git log -n` counts the commits it *prints*, so with a diff filter it
  would walk all of history looking for `limit` commits that deleted something.
- A path has one history record, its newest. Rename chains are followed (at most 32 hops) to the
  surviving name; when the chain ends at a path that is gone too, the finding keeps
  `reason='renamed'`, carries no `renamed_to`, and says "renamed, then removed".
- A directory that is still on disk but holds no file is treated as removed: git does not track
  directories, and an empty one is commonly left behind.
- `broken_link` is limited to file-form link targets (a link to a missing *directory* is only
  reported with history), requires the doc-glob extension or an indexed parent directory (the
  repo root counts), is dropped when a doc with the same stem sits beside it (`setup.html` next
  to `setup.md` is a rendered name), and is dropped when `git check-ignore` matches.
- `run_scan` shows a "checking mentioned paths" step and calls the finding through the module
  (`carta.graph.scan.missing_path_findings`), so it can be patched in tests.
- Measured on this repository's history at the time of writing: 6 findings, all true (a renamed
  `conftest.py`, a deleted workflow file named by three docs, a deleted planning file), in 0.5 s.

## Implementation notes: the replay eval

What was settled while building `carta/graph/eval/`, where it refines or departs from the text
above. Where the two disagree, this section describes the code.

**Modules.** `metrics.py` (`score_ranking`, `aggregate`, `bootstrap_ci`, `paired_bootstrap`),
`baselines.py` (`popularity_ranking`, `PopularityWindow`, `basename_tokens`, `basename_ranking`,
`basename_idf_ranking`, `BasenameIndex`), `incremental.py` (`MentionState`), `cache.py`
(`ReplayCache`, `default_cache_path`), `replay.py` (`run_replay`, `classify_commit`,
`format_replay`, `main`, `Snapshot`, `EdgeProvider`, `PROVIDERS`, `register_provider`). The git
readers the walk needs are in `gitio.py`: `first_parent_log`, `ls_tree`, `BlobReader`,
`rev_list_first_parent`, `rev_parse`, `toplevel`.

**One git process for the walk, with blob ids.** The log is read with `--raw --no-abbrev`
instead of `--name-status`, so every added or modified path arrives with its new blob id and no
`ls-tree` is needed per commit. `ls-tree` runs once, at the parent of the first scanned commit,
to seed the tree; from there the path set, the blob map and the doc set are advanced by each
commit's changes. Submodule entries are dropped from both. The **whole** first-parent history up
to the range end is always walked, whatever the window: commits before the window warm up the
popularity counts and are handed to every provider's `observe`.

**Selective re-resolution is by recorded lookups, not by path segments** (a stronger form of
A8). The segment rule in the Algorithm section is correct but weak in practice: every doc that
names anything under `src/` shares the segment `src` with every file added under it, so most docs
were re-resolved on most commits. `MentionState` instead resolves each doc through a recorder
that notes every question asked of the path index (`has_file`, `has_dir`, `by_basename`,
`suffix_matches`, a top-level directory test, `glob_matches`), and re-resolves a doc only when an
added or deleted path could change one of those answers: the same file path, the same basename, a
matching suffix, a directory that came into or went out of existence, or a path matching one of
its globs. The recorder offers only those lookups, so a new lookup in the resolver fails loudly
instead of becoming an untracked dependency. Checked against from-scratch resolution on random
histories (`test_replay_state.py`), on a 200-commit synthetic history, and by `incremental=False`
on real histories (identical reports). On a repository with several hundred docs this took the
walk from about 14,000 doc resolutions to about 3,000.

**Ranking reads a reduced edge set.** To rank one commit the mention provider hands
`rank_impact` only the edges whose target can match the query (the queried paths, the directories
above them, and every glob). Rarity is counted per target and every edge of a returned target is
included, so the ranking is identical to ranking over all edges; a test asserts it. A provider
opts in by defining `relevant_edges(snapshot, paths)`; without it `edges_at(snapshot)` is used.

**Providers are factories.** `PROVIDERS` maps a kind to `factory(ReplayContext) -> EdgeProvider`,
because a provider is stateful and belongs to one run. `register_provider(kind, factory)` adds
one. A test registers a toy co-change provider and scores `mention`, `cochange` and their union
in one run, and asserts the order of calls: `observe` once per mainline commit, in order, and
`edges_at` never before the commits preceding it were observed. Arms are paired with every
baseline **and with each other**, which is the comparison #130 needs.

**Hubs without a second pass.** Every method's full ranking is kept for each eligible commit, and
the no-hub variant filters hubs out of it before truncating. That equals passing them as
`exclude_docs` (a test pins the equivalence) and needs no second walk.

**What counts.** `new_docs_not_gold` and `excluded_doc_paths` leave out the root commit, which
creates everything. Paths under `.git/` and `.carta/` are not part of the path set and are
dropped from a commit's changes before it is classified. A type change (`T`) is a modification.
A doc rename is a delete plus an add, so the old path is gold and the new one is a created doc.

**Judgement.** Each arm gets `pass` (every paired MRR interval against a baseline lies above
zero), `fail`, or `inconclusive` (fewer than `graph.replay.min_population` eligible commits), in
both variants; the no-hub one is the one that counts.

**Report additions.** `judgement`, `population.conclusive`, `population.excluded_doc_paths`,
`no_hub.population.dropped`, and a paired `recall_at_5` beside the paired `mrr`. Run counters and
timings (`stats`) are deliberately outside the report, which stays a function of the history and
the settings; the command prints one timing line on stderr.

**Command line.** `carta graph replay` forwards its arguments to `replay.main`, which is also
`python -m carta.graph.eval.replay`. Added options: `--min-population`, `--resamples`,
`--exclude PATTERN` (an extra `excluded_paths` entry). When `<repo>/.carta/config.yaml` exists
its `graph:` and `excluded_paths` are honoured; nothing else in it is read and no other key is
required. That file is read from the **work tree**, tracked or not, so it is an input the
history does not fix: the report records `settings.config` (`defaults`, `.carta/config.yaml`,
with ` + command line` when `--doc-glob` or `--exclude` was given) and `settings.excluded_paths`,
prints both, and `--no-config` ignores the file. `--json` alone prints JSON; `--json PATH`
writes it and prints the text.

**Cache.** Payloads are zlib-compressed JSON. The in-memory layer is bounded (512 entries per
table), since a long history has far more doc blobs than a walk needs at once. An unreadable or
unwritable cache file degrades to memory without an error.

## Deferred

- Comparing derived edges with hand-declared source-glob lists in frontmatter (the issue's third
  test). The frontmatter tokens are already extracted as edges, so the comparison is a small
  report over the store; it needs a corpus that has such lists.
- Re-running the retrieval eval with doc-to-doc mention edges in graph expansion (see above).
- Pointing the `claude-md-sync` skill at `carta impact --diff`. The command is the input the
  issue asks for; changing the skill's procedure is its own reviewed change.
- A stage-1 cache inside the store, if a corpus outgrows the build budget.

## Results

Replay of this repository's own mainline
(`carta graph replay --range origin/main --no-cache --no-config`), at `792b6690`, extractor
version 2. `--no-config` means the built-in defaults: `**/*.md` are docs, and the default
`excluded_paths` apply (`node_modules/`, `.venv/`, `*.tmp`, `.planning/`, `.worktrees/`,
`.claude/worktrees/`, `.carta/`, `.pio/`, `build/`, `temp/`). Without `--no-config` the command
picks up this repository's own `.carta/config.yaml`, whose shorter exclusion list gives the same
54 commits and the same numbers. `--no-cache`, a cold cache and a warm cache give byte-identical
JSON. MRR reads the whole ranking (see *Amendments from the second review*).

```
Population: 403 commits scanned, 54 eligible (13.4%)
  skipped: no_docs_edited 203, docs_only 128, only_new_docs 14, empty 2, bulk 1, root_commit 1
  gold docs per eligible commit: mean 2.0, median 2
  docs in corpus at range end: 111     created-in-commit docs not counted as gold: 106

                R@1    R@3    R@5    R@10   P@5    any@5  MRR    returned
graph[mention]  0.035  0.182  0.210  0.262  0.063  0.278  0.191  7.7
popularity      0.302  0.791  0.885  0.944  0.319  0.981  0.731  9.6
basename        0.073  0.201  0.259  0.340  0.093  0.370  0.226  7.9
basename_idf    0.077  0.194  0.228  0.290  0.078  0.296  0.215  7.9

graph[mention] vs popularity    dMRR -0.540 [-0.654, -0.423]   better 4 / worse 43 / tied 7
graph[mention] vs basename      dMRR -0.036 [-0.103, +0.023]   better 25 / worse 17 / tied 12
graph[mention] vs basename_idf  dMRR -0.024 [-0.095, +0.039]   better 30 / worse 8 / tied 16

Without hub docs (>20% of eligible commits: CHANGELOG.md 50%, README.md 50%, CLAUDE.md 24%): 21 eligible
graph[mention]  0.111  0.270  0.270  0.270  0.086  0.333  0.276  5.6
popularity      0.270  0.529  0.781  0.810  0.248  0.905  0.564  9.2
basename        0.111  0.254  0.317  0.413  0.114  0.381  0.287  6.2
basename_idf    0.111  0.254  0.270  0.317  0.105  0.333  0.273  6.2

graph[mention] vs popularity    dMRR -0.288 [-0.456, -0.113]   better 2 / worse 15 / tied 4
graph[mention] vs basename      dMRR -0.011 [-0.080, +0.049]   better 4 / worse 4 / tied 13
graph[mention] vs basename_idf  dMRR +0.003 [-0.068, +0.060]   better 5 / worse 2 / tied 14
graph[mention]: INCONCLUSIVE (21 eligible commits, fewer than the 30 needed to call it either way)

Source-only commits (203, unlabelled): share with any prediction / mean returned at 5
graph[mention] 0.872 / 3.49    popularity 1.000 / 5.00    basename 0.887 / 3.30
```

**How to read the two tables.** The first one is not a fair fight and is not the verdict. Three
docs are gold in a quarter to a half of the eligible commits, so a method that always answers
"the changelog and the README" scores an MRR of 0.73 without reading anything. That table mostly
measures who predicts the hubs. The table without hubs is the one to judge by, and here it has
21 commits, below the 30 needed to call it. So on this repository's public history there is no
conclusive hub-free comparison yet, for this arm or for any later one: #130 must be judged on the
no-hub table of a larger history, never on the first table. Also, P@5 divides by 5, so it is not
comparable between a method that returns one doc and one that always returns ten.

**What this shows.** The acceptance test set out under *How results will be judged* is **not
met** on this repository. Formally the no-hub variant is inconclusive, as predicted (A5): 21
commits. But the direction is not in doubt for popularity, which beats the mention arm in both
variants with intervals well clear of zero, and against the two basename baselines the mention
arm is indistinguishable (every paired interval includes zero). Nothing here supports a claim
that mention edges rank better than `grep` for a basename.

**Why, from reading the commits.** Three rows were re-derived independently (each doc read with
`git show <parent>:<doc>`, edges extracted from scratch, popularity recounted from `git log`, the
basename match redone with a plain regex) and agreed with the replay on query, gold and all
three ranks: `2b3d0f92` (graph 1, popularity 5, basename 1), `21242250` (graph none, popularity
1, basename none) and `854290c6` (graph 5, popularity 3, basename 1). They show the two causes:

1. *The docs this project edits with its code do not name files.* In 33 of the 54 eligible
   commits the only gold docs are `README.md`, `CHANGELOG.md` and `CLAUDE.md`, which describe
   behaviour and commands. In `21242250` the README gained a section on export and import and
   names none of `carta/cli.py`, `carta/share.py`, `carta/tests/test_share.py`. No reading of
   doc text can find that link; it is what co-change edges (#130) are for.
2. *The docs that do name files are mostly frozen.* The plans and specs under
   `docs/superpowers/` name nearly every source file, many times, and are never edited again.
   They fill the top of the mention ranking: in `854290c6` four dated plans outrank `CLAUDE.md`,
   the doc that was edited, although it names `carta/cli.py` and the `carta/` tree.

**A check on cause 2.** Treating the planning archive as not-docs
(`--no-config --exclude docs/superpowers/`) leaves 53 eligible commits and changes the picture:

```
                R@1    R@3    R@5    R@10   P@5    any@5  MRR    returned
graph[mention]  0.175  0.369  0.599  0.621  0.189  0.792  0.453  4.9
popularity      0.308  0.816  0.933  0.962  0.336  1.000  0.742  8.5
basename        0.159  0.328  0.394  0.419  0.147  0.547  0.387  5.6
basename_idf    0.160  0.328  0.384  0.419  0.140  0.547  0.362  5.6

graph[mention] vs popularity    dMRR -0.290 [-0.428, -0.150]   better 10 / worse 33 / tied 10
graph[mention] vs basename      dMRR +0.066 [-0.008, +0.135]   better 25 / worse 5 / tied 23
graph[mention] vs basename_idf  dMRR +0.091 [+0.003, +0.166]   better 29 / worse 3 / tied 21
```

MRR goes from 0.191 to 0.453, the commits with no gold doc in the top ten from 35 to 10, and the
mention arm now beats `basename_idf` with an interval that excludes zero (with all docs it did
not). It still loses to popularity, and the no-hub variant (20 commits; mention 0.375, basename
0.365, popularity 0.669) is still inconclusive. This run was chosen after seeing the first one,
so it is a diagnosis and not a result: it says frozen docs are a large part of the gap, not that
the arm passes. Its report is told apart from the headline run by `settings.config`
(`defaults + command line`) and the extra entry in `settings.excluded_paths`.

**What follows.** (a) `carta impact` needs a way to keep frozen docs out of its ranking, or
below living ones: an `ignore_docs` list as the scan finding already has, or a recency prior.
(b) Popularity is a strong prior on any repository with a changelog and should be treated as a
feature to combine with, not only as a baseline. (c) The headline number has to come from a
larger history with more non-hub commits; this one cannot decide it. (d) #130 is the arm that
addresses cause 1, and the replay already scores `mention`, `cochange` and their union in one run.

**Speed.** 403 commits (257 of them ranked, 111 docs at the end) replay in 1.3 s with no cache,
about 310 commits per second, on an Apple-silicon laptop. The cost is roughly linear in doc
resolutions, not in commits times docs.

## Amendments from the second review

Three reviewers ran the implementation against throwaway repositories and this one's history.
What changed, and what was left:

| # | Finding | Change |
|---|---|---|
| B1 | `carta impact --diff` lost most of its recall on a change that **deletes** the paths a doc names: the store is built from the working tree, where a suffix, a bare filename, a glob and a root-relative link no longer resolve, and a removed directory is stored as `missing`. | For a diff that deletes paths, and for a queried path that is not on disk, the docs are read once more in memory against the path set with the deleted paths put back (and the added ones taken out), and those edges are ranked with the stored ones. Nothing is written. A `missing` target ending in `/` covers the files that were under it. A test compares impact before the deletion with `--diff` after it. |
| B2 | The scan finding reported a once-tracked path that git now ignores (a build output, a local config) as "deleted from the repository". | `git check-ignore` is applied to history hits as well as to broken links. |
| B3 | A link that is not found was recorded beside the doc only, so a root-relative link from a doc in a subdirectory was never confirmed by history. | Both readings are recorded; the second carries `meta.alt` and is never a `broken_link` by itself. |
| B4 | `--top 0`, a negative `top`, `--min-score nan` hid every result and the output said no doc mentions the paths. | Refused in the query and in the config. `matched_docs` in the JSON; the text says when a filter hid the matches or `top` cut them. |
| B5 | `./x` in inline code resolved beside the doc only. | The repo root is tried second. With two readings and neither found, no `missing` edge. |
| B6 | A name containing `[`, `?` or `*` could never be mentioned; `#` always cut; `:L10-L20` and `--flag=path` were not read. | Literal lookup before the glob branch; `#` kept when the full text names a real path; `:L` line suffixes; code tokens holding a `/` are split on `=` (never a URL). |
| B7 | `graph.enabled: "no"` was truthy. | Boolean keys must be booleans. |
| B8 | `--resamples 0` crashed, `--last -5` meant "all", an unwritable `--json` path raised. | Usage errors. |
| B9 | `carta graph status` replaced a damaged store with an empty one. | Read-only open; `unreadable` is reported and the file is left as found. |
| B10 | The replay CLI read the work-tree config and the report did not say so; `excluded_paths` sets the population. | `settings.config`, `settings.excluded_paths`, a `Settings:` line, `--no-config`. The published run uses `--no-config`. |
| B11 | The column labelled MRR was truncated at `max(--k)`, so it moved with `--k`. | The reciprocal rank reads the whole ranking. All published MRR values were regenerated; they rise slightly for the methods with deep first hits and the verdicts do not change. |
| B12 | A range not on one first-parent line (`main...feat`) was silently truncated. | Refused with a message. |
| B13 | The main table is gameable by predicting hubs. | The text gives the verdict on the no-hub table only when hubs exist, and says why; `verdict_table` in the JSON; P@k is footnoted. |
| B14 | Wall-clock asserts in two tests failed on a loaded machine. | Opt-in through `CARTA_TIMING_TESTS=1`. |
| B15 | README and changelog said an exact mention always outranks a directory mention. | Reworded to what holds: per changed path it does (half weight per level), but a directory mention grows as `1 + ln(n)` with the changed files it covers and passes an exact mention of one file from three covered files up, at equal rarity. |

**Extractor version 2.** B3, B5 and B6 change what the extractor emits, so
`MENTION_EXTRACTOR_VERSION` is 2: stores are re-extracted on the next build and replay caches
keyed by the old fingerprint are not read.

**Left open.**

- *Combining edge kinds.* A reviewer registered a toy co-change provider (edge weight = raw
  co-edit count) and scored `mention`, `cochange` and their union on the same 54 commits: MRR
  0.166 / 0.683 / 0.296 under the truncated metric. The union scored far below the better kind.
  The provider was a toy, so the absolute numbers mean little, but the combination rule (best
  value per queried path across kinds, with per-kind weights and unnormalised co-change
  weights) is not settled. #130 must add a replay test with two providers of known quality and
  choose between summing across kinds and normalising scores per kind before its `both` arm is
  read.
- *Renames in an impact query.* The path set "before" a diff is rebuilt from the working tree,
  not read from the merge base, and doc text is the working tree's. A doc that was itself edited
  in the same change to stop naming the path is not found; that is the intended reading (it was
  updated).
- *A queried path that never existed* triggers the same in-memory pass as a deleted one. It costs
  one extraction over the docs and cannot produce a wrong match, only an unneeded pass.

## Security hardening

A commit-time security review and a follow-up audit ran the implementation against hostile
repositories. Everything a repository supplies is untrusted input: doc text, file names (any byte
but NUL and `/`), the files and links under `.carta/`, and config values. The fixes are
structural, one mechanism per class, and every one has a regression test that fails on the code
before it (`test_hardening_extract.py`, `test_hardening_fs.py`, `test_hardening_git.py`).

| # | Finding | Change |
|---|---|---|
| S1 | A glob compiled to a backtracking regex (`**/` to `(?:.*/)?`, `*` to `[^/]*`), so match time was polynomial in path length with the wildcard count as the degree: nine `**/` against one 81-character path ran for more than 40 s. Reachable from any doc, from stored glob edges at query time, from the replay and from config. | `paths.GlobMatcher`: the glob is run as a state machine, all states advanced one character at a time. A match costs at most `len(glob) * len(path)` steps; nothing backtracks. `glob_to_regex` is kept as an alias (callers only used `.match`). A fuzz test holds the matcher to the old translation on every input the old one accepted. |
| S2 | A reversed class range (`[z-a]`) raised `re.error` out of the build, the impact query and the MCP tool, and silently switched the scan finding off. | No regex is built, so nothing can raise. A glob with a reversed range, or longer than `MAX_GLOB_LENGTH`, matches nothing; in config it is a `GraphConfigError`. `carta scan` records `stats.missing_path_check: "failed"` when the graph check raises, so a check that did not run no longer reads as a clean repository. |
| S3 | Stage 1 was quadratic in line length (the link pattern re-scanned the rest of the line from every `](`; code-span membership was a linear scan per match). | The link target and title are length-bounded and the target is atomic (lookahead plus backreference, which also works on Python 3.10); membership is a bisect. |
| S4 | Bounds were checked after the work: the candidate length after wrapper stripping (quadratic on `((((...))))`, and the strip then let a 400 KB candidate through as a valid edge), and there was no bound on doc size, line length, candidates, distinct globs per doc, or path depth in the suffix index (cubic). | `carta/graph/limits.py`, checked at entry: `MAX_DOC_BYTES` 2 MiB (stat before read; blob size from the `cat-file` header), `MAX_LINE_LENGTH` 8192, `MAX_CANDIDATES_PER_DOC` 50,000, `MAX_RAW_CANDIDATE_LENGTH` 1024 (first statement of `_normalise`), `MAX_WRAPPER_LAYERS` 8, `MAX_GLOBS_PER_DOC` 256, `MAX_SUFFIX_DEPTH` 32. Not configurable: a limit the repository can raise is not a limit. |
| S5 | A doc that is a symbolic link was read through it: a link to a file outside the repository put that file's lines into `edges.evidence` and into `carta impact` output. A FIFO named like a doc blocked the build forever. | `safeio.read_regular_file`, the one way doc content (and the replay's config) is read: no symbolic link in any component below the root, regular files only, opened non-blocking, size checked before reading. A symlinked doc stays in the path set as a name and is never a source; it is counted in `skipped`. |
| S6 | `EdgeStore` created directories, wrote `.gitignore` and opened SQLite through symbolic links a repository can ship: any of `.carta`, `.carta/graph`, `edges.db` or its journal files could redirect a write. | The store refuses to open (`StoreUnsafeError`) when the database, a file SQLite keeps beside it, or either of its own two directories is a link; `.gitignore` is created with `O_EXCL | O_NOFOLLOW`. `graph status` reports `unsafe_store`. |
| S7 | `carta graph replay --json PATH` wrote through an existing link; `--cache-dir` opened SQLite through one; the repository's `config.yaml` was read through one and a YAML error echoed the offending line. | The report is written to a new file and renamed into place (a link is replaced, its target untouched). A linked cache file means no disk cache. The config is read with `read_regular_file`; errors name the file and the line number, never its content. Cache entries are inflated with a size cap and validated field by field; a bad one is a miss. |
| S8 | `git log` output was framed with `\x01`/`\x02`, which a file name may contain: one name forged a whole commit record and hid the next real change. Tokens were stripped and filtered after the NUL split, and readers skipped what they could not parse, so a file named `\n` shifted every later record. The object-name check used `match` with `$` (a trailing newline passed, putting `cat-file --batch` one reply out of step) and never checked the object type. `check-ignore` returned "nothing ignored" when one path made git exit 128. | `gitio.py` rewritten around four rules: output is split on NUL and never post-processed; each stream is a fixed grammar (arity by status, object names by full match, commits in the order asked) and anything else raises `GitError`; commit boundaries come from `rev-list`, separately from paths (`diff-tree --stdin` with each commit's first parent, so the change stream holds no free text); revisions are refused when empty, starting with `-` or holding a control character, and passed after `--end-of-options`. `BlobReader` checks the reply header in full and closes itself on any reply it did not expect. `ignored_paths` asks with `-v -n` so every path is accounted for, sets aside paths behind a link, and isolates a path git refuses instead of dropping the batch. More redirecting `GIT_*` variables are scrubbed. `diff_name_status` requires a range. |
| S9 | A doc that failed to read (a permission or I/O error) was treated as gone: its edges were deleted and the store reported fresh. | Its rows are kept, the build reports it under `unreadable`, and `graph status` reports `docs_unreadable` until a build reads every doc. |
| S10 | Config numbers `inf`/`nan` raised a bare `OverflowError`; `hub_share: .nan` was accepted; list sizes and pattern lengths were unbounded; evidence was printed with terminal escape sequences intact; a `--diff` error told the caller whether an arbitrary path exists. | Finite numbers only; `MAX_CONFIG_GLOBS`, `MAX_PATTERNS`, `MAX_PATTERN_REGEX_LENGTH`; control and bidirectional-override characters are stripped from evidence; the "is a path, not a range" hint is given for names inside the repository only. |
| S11 | Rows read back from `edges.db` were trusted, and the file sits in the repository's own `.carta/`, so a repository can ship one. SQLite types are per value: a planted row could carry an infinite or textual weight into the ranking, an escape sequence in `evidence` to the terminal (stripping happened at extraction only), a source of `../../x`, a meta that is not an object (a crash in every reader that parses it); a planted schema could carry a trigger that runs on the next build's first write. | The schema must equal the one `_SCHEMA` creates, row for row of `sqlite_master`. Every row is checked where it is read (`store._row_to_edge`, `_source_row`, `_meta_row`): known kinds and types, repo-relative paths with no empty, `.` or `..` part, text that is text and within `MAX_STORED_TEXT_LENGTH`, a finite non-negative float weight, evidence with no control character, meta that parses to an object. A writable open reads every row first and replaces a file that fails; a read-only open raises `StoreUnreadableError` (`graph status`: `unreadable`). A store over `MAX_STORE_BYTES` (1 GiB, with its journal) is not opened and not replaced (`unsafe_store`). Tests: `test_hardening_store.py`. |
| S12 | `carta impact` asked the file system through symbolic links the repository ships: `--diff link/x` answered "is a path, not a range" only when `x` existed beyond the link, and a typed path gained a trailing `/` when a link pointed at a directory, so the output said whether a file outside the repository exists and what it is. A bad range carried git's stderr (a usage dump, absolute paths), and store errors named the store by absolute path to the MCP client. | `safeio.entry_kind`: what a path is in the repository's own tree, by `lstat` per component, with nothing reported for anything reached through a link. The hint, the reading of typed paths and the "deleted" test all use it. A range git refuses gets one fixed sentence; store errors name `.carta/graph/edges.db`. |
| S13 | File names reached the terminal raw: a doc or target named with an escape sequence, or a bidirectional override, drove the terminal from `carta impact` and from the replay's text report (hub docs, and the globs of the replayed repository's config). | `model.printable` writes each control character out as its escape; every repository-supplied name in `format_impact` and `format_replay` goes through it. JSON output is unchanged (it was always escaped). |
| S14 | Two of the replay's bounds could be defeated. `graph.replay.max_files`, the bulk rule, was read from the replayed repository's config with no ceiling (and `0` or a negative value silently emptied the population). `MAX_DOC_BYTES` bounded one doc, but the per-commit text cache held every doc of the tree at once on the first sync: a few thousand compressible 2 MiB docs are gigabytes. | `graph.replay.max_files` must be within 1..`MAX_REPLAY_COMMIT_FILES` (5,000); `--max-files` on the command line is the caller's own and is not capped. The text cache is dropped whenever it would pass `MAX_REPLAY_TEXT_BYTES` (64 MiB of text); it is only ever a saving, and a test holds the report identical with the budget at 700 characters. Tests: `test_hardening_replay.py`. |
| S15 | The ceiling added in S14 was on one setting; its siblings read from the same config had a finite check and nothing else. `graph.scan.history_limit` is the number of commits git is asked for (a negative value means all of them), `graph.replay.popularity_window` and `min_population` are counts, `hub_share` is a share, `impact.top`, `identifiers.min_length` and the kind weights were unbounded above (a negative weight silently removed a kind). | One gate, `settings._within`, on every number a config sets: `impact.top` 1..1,000, `impact.weights.*` 0..1,000, `identifiers.min_length` 1..64, `scan.history_limit` 0..100,000, `replay.hub_share` 0..1, `replay.popularity_window` and `replay.min_population` 0..1,000,000, `replay.max_files` 1..5,000. Outside is a `GraphConfigError`. |

**Extractor version 3.** The bounds and the stripped control characters change what the extractor
emits for pathological input, so stores re-extract on the next build and old replay caches are
not read. The replay on this repository's history was re-run before and after
(`--no-cache`, with and without `--no-config`): every number in the report is identical; only
`settings.fingerprint` differs, as it must.

**Behaviour that changed on purpose.**

- A symlinked doc is no longer an edge source, even when its target is inside the repository
  (the target file itself still is). The replay reads git objects, where a link is a small blob
  holding its target path; it is unchanged.
- `[!-x]` is now "not `-`, not `x`". The old translation read it as the range `/` to `x`.
- `diff_name_status("HEAD")` raises. It used to compare with the working tree, against its own
  docstring.
- `graph.patterns` from a replayed repository's config are ignored unless `--trust-config`;
  `settings.config` in the report says so. A regular expression can be written to take
  exponential time and the stdlib cannot bound one, so patterns are for trusted config only:
  `carta scan`, `carta graph build` and the MCP tool read the project's own config, as every
  other Carta command does.
- git 2.30 or newer is needed (`rev-parse --end-of-options`).

**Left open.**

- An adversarial 2 MiB doc built from `](<` costs seconds, not minutes: stage 1 is linear with a
  constant of `MAX_LINK_TARGET_LENGTH` per `](`.
- The checks are `lstat` then open. A process that swaps a file for a link between the two is
  outside the model (the adversary is repository content, not a concurrent local user); reads
  additionally open with `O_NOFOLLOW` component by component where the platform allows.
- fnmatch (`excluded_paths`) is linear on the supported Pythons and was not replaced.
- A shipped store that is well formed is believed. The checks in S11 make planted rows harmless
  (they cannot crash, hang, leave the repository or drive the terminal), not correct: a store
  whose source hashes match the working tree and whose rows are valid but invented ranks what it
  was told to. `carta graph build --force` re-extracts everything. Refusing a store that git
  tracks would close the clone case and is not done yet.
- The replay still holds the whole first-parent change log in memory (see the co-change notes
  for the cap added with the streaming reader, C6), and every eligible commit keeps each method's
  full ranking. Both grow with the history, not with one hostile object.
- The MCP tool's catch-all returns `str(exception)` for an error nobody anticipated, as the
  other Carta tools do; such a message can name an absolute path.

## Co-change edges (#130)

The second writer into the edge store. It mines the first-parent git history for files that
change in the same commits and stores each such pair as two `cochange` rows, one per direction,
with `src_type='file'`. It needs no parsing and no model, and it covers what the mention arm
cannot: a doc that describes behaviour without naming the file, a generated file and its source,
two files in different languages that share no symbol. An edge says "historically changed
together" and nothing more; every surface that shows one says so.

Where this section and the earlier text disagree (the schema table's "Later writer" row, the
defaults under *Configuration*, the open question under *Left open*), this section describes the
code.

**Modules.** `carta/graph/cochange.py` (`PairTable`, `build_cochange`, `cochange_status`,
`countable_path`), `carta/graph/eval/cochange_arm.py` (the replay provider),
`carta/graph/eval/holdout.py` (the temporal holdout). `gitio.py` gained `first_parent_rows`,
`iter_log_entries` / `log_entries` (the two halves of `first_parent_log`; the first streams and
takes a per-commit limit), `iter_added_deleted`, `work_tree_root`, `is_unborn`, `is_shallow`, and
`LogEntry.removed` (the blob a deleted path had), `.n_changes` and `.truncated`.

### The miner

`PairTable` is pure: no I/O, no clock. It is fed mainline commits oldest first and holds, for
each file, how many counted commits touched it, and for each pair, how many touched both. The
build persists one and the replay carries one forward, so there is one set of rules:

| Rule | Why |
|---|---|
| One mainline commit is one transaction. Every commit is compared with its **first parent**. | On a squash-merge repository that is one pull request. On a merge-commit repository the merge carries the whole branch, so the unit is still the pull request and nothing has to be grouped afterwards. |
| A commit with no parent is never counted. | The root creates everything. So does the boundary commit of a shallow clone, which looks like a root. |
| A commit that changes more than `max_files` paths (30) adds nothing: no pair, and no commit to any file's own count. The test is made before any pair is formed. | A formatting sweep, a vendored drop or a mass move would couple everything to everything. Pairs are quadratic in the files of a commit, so this is also the cost bound; the config value is capped at 500. |
| A deleted path loses its counts and its pairs. If it returns it starts from nothing. | Edges must point at files that exist. It also keeps the table to live files. |
| A **pure move** keeps its history: a commit that deletes one path and adds another with the same blob, matched one to one (the empty blob excepted), moves the counts. Anything else is a delete and an add. | See *Renames* below. |
| `score = confidence`: the share of the source file's commits that also changed the target. | See *Confidence or lift* below. |
| A co-change counts half after `half_life` commits (100). Positions on the mainline, not dates. | Dates in history are not trustworthy and positions are reproducible. With confidence the decay is local: `conf(a→b)` only moves when `a` is touched, so an incremental build rewrites only the files in the new commits. |
| An edge needs `support >= min_support` (1) and `score >= min_score` (0.1). | See *The floor* below. |
| A pair still below `min_support` is forgotten once `pending_window` (1,000) commits pass without it recurring. | Bounds the pair state when the floor is above 1. Applied lazily, at the next co-change, so it is a function of the history and an incremental build cannot diverge from a full one. |
| A path is counted only if it is in the path set, at most 1,024 characters, valid UTF-8 and free of control characters. | File names are untrusted; these are stored and printed. |

Per-direction rows carry `weight` = the score rounded to 6 places, `evidence` = `changed together
in 9 of 14 commits` (no file name in it), and `meta` = `{"support", "commits", "first", "last"}`.
`dst_type` is `doc` when the target is a source doc, else `file`.

### Storage and the incremental build

No schema change. State lives where A3 put it: one `sources` row per file under
`extractor='cochange'`, `sha256=''`, `meta` = `{"commits", "w", "seq", "pairs"}`. A pair's state
(`[support, decayed weight, first, last, position]`) is written once, in the row of the smaller
of its two paths, so pairs below the floor survive between builds without a second table. Meta
keys: `cochange.last_commit` (the cursor), `cochange.seq` (mainline commits seen),
`cochange.settings_sha`, `cochange.extractor_version`, `cochange.totals`, and
`cochange.summary` (what the last build reported: counts, edge count, digest).

`build_cochange` lists the first-parent line of `HEAD` (object names only), and continues from
the cursor **only if** the stored settings and version match and the cursor is still at the same
position on that line. A rebase, a branch switch, an amended commit or a deepened shallow clone
fails that test and the history is mined again from the start. When the cursor is `HEAD` itself
the build returns what `cochange.summary` recorded and **does not read the pair state at all**.
Otherwise only the commits after the cursor are read (`diff-tree --stdin`, asked in batches of
2,000 and read as a stream, one commit in memory at a time) and only the files they touch are
rewritten.

The bulk limit is applied **while git's answer is read**. A commit over `max_files` is counted
record by record and none of its paths is kept, so a sweep over 20,000 files costs no more
memory than a one-line fix. The two things a bulk commit still does (it drops the files it
deletes and carries a pure move) are then read from git again for that one commit, in passes
that keep only the deleted paths the table has counts for and, per blob of those, a count of the
paths deleted and added with it. Memory is bounded by the table, not by the commit. The tests
assert the table is identical to one fed every change of every commit, on random histories with
sweeps, mass moves, deletions and a one-to-two copy. `edges_digest(("cochange",))` covers the edges and the state rows, and the tests
assert it is equal for an incremental build and a forced full one after every step of a history
with deletions, a move, a sweep and a file that returns; and, on random histories, that stopping
and resuming at any commit gives the same table.

The whole pass is one transaction. A git failure leaves the previous rows and cursor,
`carta graph build` reports `cochange.status: "failed"` and carries on with the mention edges,
and `graph status` goes on saying the store is stale: `cochange_behind` when a commit could not
be read, `cochange_git_failed` when the history could not even be listed. **A history git cannot
read is never taken for an empty one.** Only two things are an empty history: a directory with
no repository at or above it (git finds none *and* there is no `.git` entry on the way up), and
a branch with no commit yet (`HEAD` names a ref that does not exist). A damaged ref, a missing
object, a repository git refuses, or git itself failing all raise, and nothing is cleared. Stored state is validated field by field
when it is loaded; anything that is not exactly the written shape is discarded and the history
mined again.

`carta graph build` runs the pass by default (`graph.cochange.enabled`, and `--no-cochange` to
skip it for one run without touching what is stored). `carta scan` skips it. A repository with
no commit and a directory that is not a repository give **no edges and no error**. A shallow
clone is mined as far as its history goes and the report says `shallow: true`: its boundary
commit looks like a root and is not counted, so depth 1 and 2 give no edges and a deeper clone
gives edges from the commits it has (the counts are then lower than the full history's). When the Carta root is a subdirectory of the
git root, paths outside it are dropped before anything is counted. Measured here: 438 mainline
commits mined from nothing in 0.86 s including the mention pass (5,419 edges over 333 files),
0.30 s when there is nothing new. On a synthetic store of 124,750 pairs a build with nothing
new took 2.5 s before the summary row existed and takes 0.04 s with it.

`graph status` gains `cochange` (`enabled`, `commits`, `head`, `stale`, `reason`) and the stale
reasons `cochange_behind`, `cochange_settings_changed`, `cochange_disabled`,
`cochange_git_failed`.

### Decisions on the issue's "To explore" list

Every number below is MRR on the temporal holdout (next section), cut at 80% and at 50% of the
mainline, on two histories: this repository (403 mainline commits; 63 and 122 scored commits,
397 and 556 seed queries) and a second, private one about twice as long with about ten times the
files (883 mainline commits; 98 and 114 scored commits, 649 and 507 queries). Intervals are
paired 95% bootstraps over commits. Only aggregate numbers of the second history are given.

**These are in-sample numbers.** Every default in this table, and the fusion mode and weight in
the next section, was chosen by looking at these same four holdout populations and the replay
tables that the results are then quoted from. No repository and no period of history was held
back. The measurements are accurate; as estimates of what the chosen defaults will do elsewhere
they are optimistic by an unknown amount. The deltas in this table were measured before the
holdout stopped scoring deleted paths (see *The temporal holdout*); that rule changes no query
on this repository and 2 of 649 and 22 of 507 on the second history, and the headline rows
below are re-measured with it.

| Question | Decision | Evidence |
|---|---|---|
| Confidence or lift | **Confidence.** | Confidence beats lift on all four populations: +0.523 [+0.456, +0.592] and +0.372 [+0.307, +0.438] here, +0.125 [+0.057, +0.188] and +0.111 [+0.070, +0.158] on the second history. Lift divides by how often the target changes, which puts rare coincidences first; the files that really do move with a change are often the busy ones. Confidence is also the right direction for the question asked ("I changed this; what moves with it?") and it is local, which is what makes the build incremental. Lift stays available (`measure: lift`); every row is then rewritten on each build. |
| Chronic co-travellers (changelog, lockfile) | No ignore list. | Confidence is asymmetric, which is the point: `conf(a → CHANGELOG)` is high and true, `conf(CHANGELOG → a)` is low. Whether a changelog is a *useful* answer is the hub question the replay already separates. |
| Bulk commits | Skipped above `max_files`, default **30**. | 15 is worse (−0.065 [−0.093, −0.042] on the second history at 80%). 60 is better on the second history (+0.062 [+0.040, +0.085], +0.042 [+0.028, +0.057]) and worse or equal here (+0.001; −0.044 [−0.057, −0.030]). No consistent winner, so the cheaper value stays; a repository with large pull requests should raise it. |
| Support floor | `min_support` **1**, with `min_score` 0.1 doing the pruning. | A floor of 2 loses on all four: −0.060 [−0.082, −0.041] and −0.159 here, −0.109 [−0.141, −0.080] and −0.066 on the second history. A single co-change is weak evidence but it is often all a rarely changed file has. The confidence floor is the adaptive version of the same idea: for a file with 11 or more commits a single co-change falls below 0.1 by itself. It also bounds the edge table: a file's confidences sum to at most `max_files − 1`, so no file has more than `(max_files − 1) / min_score` = 290 edges. `min_score` 0 instead of 0.1 moves MRR by at most 0.004 and adds 10 to 30% edges. |
| Time decay | `half_life` **100** commits. | Helps here (+0.013 [+0.002, +0.025] and +0.093 [+0.074, +0.113]) and makes no detectable difference on the second history (+0.007 [−0.004, +0.016], −0.007 [−0.018, +0.006]). 50 and 200 were tried and neither is consistently better than 100. Kept on because it never measurably hurt; it is not established as a general gain. |
| Renames | Follow a **pure move** only (same blob, one to one). | Git's similarity detection (`-M`) is a heuristic whose answer depends on thresholds; blob identity is exact and needs nothing but what the log already prints. In this repository's history it covers 17 of the 24 renames `-M` reports. A move that also edits the file loses its history. **Measured effect: none** (0.000 on three populations, −0.003 on one), so this is kept for the case a holdout barely samples, a query about a file just after it moved, and `follow_renames: false` turns it off. |
| Shallow clones, little history | No error; mined as far as the history goes, flagged `shallow: true`. | Depth 1 and 2 give no edges (the boundary commit is not counted); depth 4 of the test history gives its edges; after `fetch --unshallow` the history is mined again from the start. All tested. |
| Cost, pruning | One `rev-list` per build, a streamed `diff-tree` for new commits only, per-file rewrites. Nothing is loaded when there is no new commit; the pair state is loaded whole when there is one. | See *Left open* for the bound that is not there. |
| Merge-commit repositories | Nothing to do: first-parent comparison makes the merge the transaction. | Tested. |

Tried and not adopted: shrinking confidence toward zero for files with few commits
(`support / (commits + k)`). It cannot change a single-seed ranking, and on the replay it moved
MRR by 0.01 to 0.04 on 54 commits, inside the noise.

### `carta impact`

`graph.impact.kinds` now defaults to `["mention", "cochange"]`. A history row is read from a
changed file to a doc, exactly as A9 said. Output labels the two apart whenever a result holds a
history match:

```
$ carta impact carta/cli.py --top 2
Top 2 of 49 docs that are tied to 1 path (113 docs, 12,991 edges)
  named: the doc names the path.   history: they changed in the same commits; that is correlation, not a claim that the doc describes it.

 1. CLAUDE.md  score 1.412548
      carta/cli.py  [history] changed together in 8 of 49 commits (git history; the doc need not name it)
      carta/cli.py  [named] exact, named by 42 docs
        L27: - argparse (stdlib) - CLI argument parsing in `carta/cli.py`
        ...
 2. CHANGELOG.md  score 1.243643
      carta/cli.py  [history] changed together in 11 of 49 commits (git history; the doc need not name it)
      carta/cli.py  [named] via carta/ (dir), named by 17 docs
        L379: - Reorganized test suite (moved `conftest.py` out of `carta/` to repo root).
```

In JSON (schema unchanged) a history match has `kind: "cochange"`, `match: "cochange"`,
`target_type: "file"` and one evidence entry with `line: 0` and the count as its text. With no
history match in the results the wording is what it was (`N docs mention ...`, no labels).

**A diff is not evidence about itself.** The store is mined up to `HEAD`, which for
`carta impact --diff RANGE` already holds the commits of the range. A pair whose *first* shared
commit lies in the range has no evidence older than the question, so its row is left out of
that query (a pair with older evidence stays, its count including the range). Without this the
docs a change already edits would rank themselves. A path query has no range and reads the store
as it is. History is mined before a query only when a history kind is asked for.

### How kinds combine (settles the open question)

The earlier rule, the best value per queried path across kinds, compares numbers in different
units: a mention score is `idf * tf` and reaches 5 or more, a confidence is at most 1. Four rules
were implemented (`graph.impact.combine`, and `--arm NAME=KIND+KIND@MODE` in the replay) and
scored on the same commits:

- `max`: the earlier rule. `sum`: add the kinds' scores.
- `norm`: rank each kind alone, divide each score by its kind's best, weight, add.
- `rrf`: rank each kind alone and add `(60 + 1) / (60 + rank)` per kind (reciprocal rank fusion,
  scaled so that first place in one kind is worth that kind's weight).

A kind that returns nothing takes no part, so fusing with an empty ranking is the identity.

MRR, with the union's paired difference against each alternative:

| | this repository, all (54) | this repository, no hubs (21) | second history, all (306) | second history, no hubs (284) |
|---|---|---|---|---|
| `mention` alone | 0.191 | 0.276 | 0.293 | 0.289 |
| `cochange` alone | 0.615 | 0.404 | 0.504 | 0.499 |
| union, `max` | 0.242 | 0.324 | 0.300 | 0.297 |
| union, `sum` | 0.250 | 0.324 | 0.314 | 0.311 |
| union, `rrf` | 0.401 | 0.409 | 0.506 | 0.502 |
| union, `norm` | **0.489** | 0.381 | **0.528** | **0.526** |
| `norm` − `sum` | +0.239 [+0.146, +0.336] | +0.057 [+0.001, +0.151] | +0.214 | +0.215 |
| `norm` − `rrf` | +0.088 [+0.020, +0.157] | −0.028 [−0.081, +0.003] | +0.023 [−0.003, +0.048] | +0.024 [−0.001, +0.050] |

**Decision: `norm`, with equal weights** (`graph.impact.weights.cochange` goes from 0.5 to 1.0).
Adding raw scores, or taking the larger, is wrong: it loses to either fusion on every table and
is barely better than the mention arm alone, which is what the earlier reviewer's toy provider
showed. Between the two that work, `norm` is ahead on three tables of four (one interval excludes
zero) and behind on the 21-commit one; `rrf` is kept as an option because it does not depend on
score scale at all. A weight of 0.5 for co-change was worse than 1.0 for both fusions on every
table of this repository's history (for `norm`: 0.361 against 0.489, and 0.341 against 0.381).
`test_a_good_provider_and_a_loud_bad_one` is the replay test the open question asked for: a
provider that is right with small scores and one that is wrong with large ones; `sum` and `max`
follow the loud one, `norm` and `rrf` do not.

### The replay arm, and leakage

`--arm cochange=cochange` and `--arm both=mention+cochange` work out of the box. The provider
holds one `PairTable` and nothing else. The loop ranks a commit from the state at its parent and
only then hands the commit to `observe_entry` (the whole `LogEntry`, so pure moves are followed
as the build follows them), so the edges for a commit come from commits strictly before it and
the running table costs one update per commit. Tests: a commit whose only evidence is itself and
the commits after it gets no ranking; a spy asserts the table has seen exactly the commits
before the parent at every ranking; two histories that agree up to a commit give it the same
row whatever follows; and the table at the end of a replay equals the edges a build stores.

### The temporal holdout (`carta graph holdout`)

```
carta graph holdout [--repo PATH] [--rev REV] [--cut REV | --cut-share F]
                    [--cochange NAME[=KEY:VALUE,...]]... [--graph NAME=PATH]... [--directed]
                    [--k 1,3,5,10] [--max-files N] [--popularity-window N]
                    [--min-population N] [--no-config] [--resamples N] [--per-commit] [--json [PATH]]
```

Edges are built from the mainline up to a cut (default: the first 80% of commits) and frozen.
For every later commit with at least two files that existed at the cut, each such file takes a
turn as the **seed**; a method is given the seed alone and ranks the commit's other files. A
file born after the cut is neither a seed nor an answer, and neither is a path the commit
**deletes**: the miner counts no co-change for a deletion, so the eval does not ask for one. The
unit is the commit (a commit's row
is the mean over its seeds, and intervals are bootstraps over commits), so one large commit does
not outvote many small ones. Skips are counted by reason: `single_file`, `bulk`, `unknown_files`.

Methods on the identical queries: each `--cochange` variant; `popularity` (the files changed most
often before the cut, the same list for every seed); `directory` (the other files of the seed's
directory, most changed first); and each `--graph NAME=PATH`, an external file-to-file graph
scored as `graph:NAME`. The file is JSON (`{"edges": [[src, dst, weight?], ...]}`, a bare list,
`{"src": ["dst"]}` or `{"src": {"dst": weight}}`) or text (`src<TAB>dst[<TAB>weight]`). An edge
counts both ways unless `--directed`; its neighbours are ranked by weight, limited to files that
existed at the cut. Its size (64 MiB) and edge count (2,000,000) are checked before it is used,
rows that are not a valid edge are counted as rejected, and the report says in so many words
that Carta cannot check when the graph was built: it must come from the tree at the cut.

On this repository (`--rev origin/main --no-config`, cut at 80%):

```
Population: 403 mainline commits; 322 before the cut (edges are built from these only), 81 after
  scored: 63 commits, 397 seed queries (each file of a commit takes a turn as the seed)
  skipped: single_file 13, unknown_files 3, bulk 2
  files at the cut: 300     other files to find per query: mean 5.3, median 4
  cochange: 316 commits counted, 1 bulk skipped, 1,451 pairs, 2,215 edges

            R@1    R@3    R@5    R@10   P@5    any@5  MRR    answered  returned
cochange    0.248  0.404  0.465  0.509  0.300  0.848  0.724  0.998     7.6
popularity  0.072  0.135  0.293  0.439  0.174  0.586  0.401  1.000     10.0
directory   0.106  0.131  0.136  0.153  0.099  0.428  0.365  0.973     6.1

cochange vs popularity    dMRR +0.323 [+0.260, +0.381]   better 58 / worse 5 / tied 0
cochange vs directory     dMRR +0.359 [+0.298, +0.420]   better 56 / worse 4 / tied 3
```

Cut at 50% (201 commits before, 122 scored, 556 queries): co-change 0.689, popularity 0.184,
directory 0.314; +0.505 [+0.446, +0.563] and +0.374 [+0.320, +0.431].

On the second history, cut at 80% (706 commits before, 98 scored, 647 queries, 4,147 files at
the cut): co-change 0.445, popularity 0.218, directory 0.346; +0.227 [+0.160, +0.295] and
+0.099 [+0.033, +0.161]. Cut at 50% (441 before, 109 scored, 485 queries): co-change 0.248,
popularity 0.076, **directory 0.357**; +0.172 [+0.122, +0.222] against popularity and
**−0.109 [−0.162, −0.060] against directory**. (Before deleted paths were left out these read
98 and 649, 0.446 / 0.218 / 0.350, and 114 and 507, 0.242 / 0.075 / 0.367; this repository's
rows did not move.) With 441 commits spread over about 3,000 files
the table answers only 75% of the queries, and "the other files in this directory" is the better
guess. Co-change needs history in proportion to the size of the tree.

### Results on the replay

`carta graph replay --range origin/main --no-cache --no-config --arm mention=mention
--arm cochange=cochange --arm both=mention+cochange`, at `792b6690`. The mention rows are
identical to the *Results* section above.

```
Population: 403 commits scanned, 54 eligible (13.4%)

                 R@1    R@3    R@5    R@10   P@5    any@5  MRR    returned
graph[mention]   0.035  0.182  0.210  0.262  0.063  0.278  0.191  7.7
graph[cochange]  0.278  0.602  0.637  0.725  0.233  0.778  0.615  6.9
graph[both]      0.154  0.470  0.602  0.799  0.215  0.796  0.489  8.9
popularity       0.302  0.791  0.885  0.944  0.319  0.981  0.731  9.6
basename         0.073  0.201  0.259  0.340  0.093  0.370  0.226  7.9
basename_idf     0.077  0.194  0.228  0.290  0.078  0.296  0.215  7.9

graph[cochange] vs popularity      dMRR -0.116 [-0.212, -0.021]   better 6 / worse 21 / tied 27
graph[cochange] vs basename_idf    dMRR +0.401 [+0.252, +0.540]   better 35 / worse 9 / tied 10
graph[both] vs graph[mention]      dMRR +0.298 [+0.202, +0.397]   better 36 / worse 9 / tied 9
graph[both] vs graph[cochange]     dMRR -0.127 [-0.216, -0.038]   better 10 / worse 23 / tied 21

Without hub docs (>20% of eligible commits: CHANGELOG.md 50%, README.md 50%, CLAUDE.md 24%): 21 eligible
graph[mention]   0.111  0.270  0.270  0.270  0.086  0.333  0.276  5.6
graph[cochange]  0.213  0.372  0.406  0.573  0.152  0.476  0.404  5.9
graph[both]      0.159  0.389  0.423  0.549  0.152  0.524  0.381  7.9
popularity       0.270  0.529  0.781  0.810  0.248  0.905  0.564  9.2
basename         0.111  0.254  0.317  0.413  0.114  0.381  0.287  6.2
basename_idf     0.111  0.254  0.270  0.317  0.105  0.333  0.273  6.2

graph[both] vs graph[mention]      dMRR +0.105 [+0.025, +0.212]   better 8 / worse 2 / tied 11
graph[both] vs graph[cochange]     dMRR -0.023 [-0.179, +0.144]   better 5 / worse 6 / tied 10
every arm: INCONCLUSIVE (21 eligible commits, fewer than the 30 needed)

Source-only commits (203, unlabelled): share with any prediction / mean returned at 5
graph[mention] 0.872 / 3.49    graph[cochange] 0.586 / 2.05    graph[both] 0.897 / 4.09
```

**Reading it.** On this repository the issue's test, "the union has to beat both", is **not
met**. Co-change alone is the best graph arm on the table with hubs (it predicts the changelog
and the README, which is what that table rewards) and it is still behind popularity there. The
union beats the mention arm in both tables with intervals clear of zero, and is behind co-change
alone with hubs and level with it without. The table without hubs is the one to judge by and it
has 21 commits. The cause is the one diagnosed above: the mention ranking is full of dated plans
nobody edits. With that archive excluded (`--exclude docs/superpowers/`, a diagnosis, not a
result: 53 and 20 commits) the union is ahead of both: 0.672 against 0.647 and 0.453 with hubs,
0.463 against 0.446 and 0.375 without.

The second history is large enough to call (883 commits scanned, 306 eligible, one hub doc at
22%, 284 eligible without it; 483 docs at the end):

| | MRR, all | MRR, no hub | R@5, no hub | R@10, no hub |
|---|---|---|---|---|
| `graph[mention]` | 0.293 | 0.289 | 0.310 | 0.407 |
| `graph[cochange]` | 0.504 | 0.499 | 0.453 | 0.507 |
| `graph[both]` | 0.528 | 0.526 | 0.492 | 0.567 |
| popularity | 0.348 | 0.255 | 0.193 | 0.289 |
| basename | 0.293 | 0.289 | 0.322 | 0.399 |
| basename_idf | 0.281 | 0.274 | 0.307 | 0.392 |

There `graph[cochange]` and `graph[both]` each **pass**: every paired MRR interval against a
baseline lies above zero in both variants (co-change against popularity +0.244
[+0.183, +0.303] without the hub, against `basename_idf` +0.225 [+0.166, +0.284]). It is the
first conclusive pass for the graph arm. `graph[mention]` fails as before. The union against
co-change alone: MRR +0.027 [−0.007, +0.064] (not separated), recall@5 **+0.039
[+0.009, +0.073]**; against mention alone +0.24 on MRR and +0.183 [+0.140, +0.227] on recall@5.
That recall@5 result is **post-selection**: it is the best of four fusion modes and two weights
tried on this same population, its lower bound is +0.009, and the reciprocal-rank fusion that
was not chosen sits at +0.004 [−0.038, +0.046] on MRR against co-change alone. Read it as "not
worse", not as an established gain.
So on the one population big enough to decide, the union beats both kinds on recall and beats
mention and ties co-change on MRR. That, and the mention arm losing to the union on every table
of both histories, is the case for reading both kinds by default. It is a case made in-sample:
before the default kinds are treated as settled it should be confirmed on a later slice of
history or on a third repository, neither of which has been done. The price is the alarm rate:
the union returns something for 90% of source-only commits, co-change alone for 53 to 59%.

### Security notes

Written to the standard of *Security hardening*: no regular expression is built; the bulk test
is made while git's answer is read, before a path of an over-limit commit is kept, and the
external-graph bounds come before the work they protect; the store is only reached
through `EdgeStore`; reports are written by `write_text_replacing`; git is read through the
existing NUL-delimited grammar, now one token at a time (`iter_log_entries` refuses a row that
is not full object names, a path listed twice in one commit and any token out of place; `--rev`
and a cut revision go through `rev_parse`, which refuses a leading `-`, and a revision that
names no commit is an error, not an empty report); a file name is counted, stored and printed
only if every character of it is printable (`str.isprintable`, the plain space included), which
leaves out C0 and C1 controls, line and paragraph separators, bidirectional overrides and other
format characters, no-break spaces, unassigned code points and bytes that were not UTF-8; a
failure never advances the cursor, and a history that cannot be listed is a failure, not an
empty history;
stored state is validated before it is trusted; and a git error message is made printable and
cut to 300 characters before it reaches a report. The external graph file is named by the
caller on the command line and is read where it points, link or not; it is refused if it is not
a regular file, so a FIFO cannot block the run.

**Final sweep (after the first review).** Each of these had a bound in the wrong place or none,
and each has a test in `test_hardening_cochange.py` that fails on the code before it.

| # | Finding | Change |
|---|---|---|
| C1 | The streamed reader split `tail + chunk` on NUL with no limit on the tail. Git puts no limit on the length of a name in a tree and a name of one repeated byte packs about 1000 to 1, so a tree object of 64 KiB held a 64 MiB path in memory, copied again for every chunk read (48 s, 4 GiB measured); `MAX_COCHANGE_PATH_LENGTH` was only applied afterwards. | `MAX_GIT_TOKEN_BYTES` (64 KiB), checked in `_stream_tokens` as each chunk arrives and before the chunk's first part is joined to what was carried over, so what is held is one record within the limit plus the 64 KiB just read; a longer record is a `GitError`, so the pass fails, the cursor does not move and the store stays stale. |
| C2 | Nothing bounded the pair table across commits: every commit may pair up to `max_files` new files (124,750 pairs at the ceiling of 500, which the repository's own config can set), and an expired pending pair is reset in place, never removed. The table is held whole and written to the store. | `MAX_COCHANGE_PAIRS` (2,000,000), counted over pairs that are held: a deleted file's pairs leave the count with it, and when the table is full the pairs the pending window has already expired are removed before anything is refused (they are below the floor, so no edge, and would start over at their next co-change; a test holds every result equal with and without the sweep). Only a table still full does not start a new pair (the test is made before the pair exists). That is never silent: `capped` is in the build report, in `graph status` (`cochange.capped`) and in a line both commands print. A stored table over the cap is not loaded. Past the cap the result is bounded but depends on what was held, so an incremental and a full build may differ there. With `min_support` 1 (the default) no pair is ever pending, so nothing can be swept and the cap is the only bound. |
| C3 | Stored pair state could raise what the "mine again instead of trusting" path does not catch: `OverflowError` (a weight that is an integer of hundreds of digits), `RecursionError` (state nested past the parser), `ValueError` from `int()` on a count `str.isdigit` accepts (a superscript digit, or thousands of digits). Each was a traceback out of `carta graph build`, `graph status` and `carta impact`. The stored cursor was reported as it was found. | `_float` and `_obj` turn the first two into the `ValueError` the caller already handles; the count is read by `_seq_number` (ASCII digits, at most 18); the cursor is reported only when it is a commit id. |
| C4 | `_history_edges` used a pair's stored `first` commit as a set member: a list there was a `TypeError`, deep nesting a `RecursionError`. | A `first` that is not a string is not a commit of the range; the row is kept. |
| C5 | The holdout's `--graph` loader stripped every leading `./` before it checked a path's length (quadratic: one 512 KB line took 3.5 s), let `float()` raise `OverflowError` on a long integer weight, split the whole text into a list of lines, and counted only accepted edges against `MAX_EXTERNAL_GRAPH_EDGES`, so a file of rejected rows was unbounded work. | Length first, one `./` stripped; `OverflowError` is a rejected row; lines are read one at a time; every row, accepted or not, counts against the limit. |
| C6 | The replay holds the whole first-parent change log, and a history can change far more paths than it takes bytes to store (a renamed directory is one new tree and every path beneath it). | `log_entries(max_total=)`: the list is given up, with a `ReplayError`, once it holds more than `MAX_REPLAY_HISTORY_CHANGES` (5,000,000) path changes. The same number is the per-commit limit of the streamed reader, so one commit over it is cut off while it is read and never collected. The replay is still not a streaming walk. |

### Left open

- **Pair state is bounded only by `MAX_COCHANGE_PAIRS`** (C2 above), whatever `min_support` is.
  The edge table is bounded (290 edges per file),
  but every pair ever seen between two live files keeps a state entry, at most 435 new ones per
  commit. Here that is 3,668 pairs; on a very long history with wide commits it reaches the cap
  and is loaded whole on each build that has new commits (a build with none reads
  nothing). Measured on a synthetic store of 124,750 pairs: 2.6 s for a build with one new
  one-file commit. The pending window does not bound it: an expired pair is reset, not removed. A
  windowed history or a partial load would do it without costing recall. A partial load needs
  each pair to be reachable from both of its files (it is stored once, under the smaller path),
  which is a change of the stored format. Not built.
- **Every default was selected in-sample**, on the four holdout populations and two replay
  histories reported here; see the note above the decisions table. No held-out repository or
  period exists yet.
- **`str.isprintable` follows the Unicode tables of the running Python.** A code point assigned
  in a newer Unicode version is countable on a newer interpreter and not on an older one, so
  two interpreters can disagree about a file whose name uses one.
- **The replay still holds its whole range in memory** (`first_parent_log`), bulk commits
  included, up to `MAX_REPLAY_HISTORY_CHANGES` path changes (C6); only the build and the holdout
  stream. It is an eval over a range the caller names.
- **A JSON `--graph` file is parsed whole** before any row is counted; its only bound is the
  64 MiB on the file, and `json.loads` holds many times that. The file is named on the command
  line, so this is the caller's own input.
- **Decay and the cap were tuned on two repositories**, one of them this one, and scored on the
  same ones. `half_life` is a
  gain here and a wash on the other; `max_files` 30 against 60 points opposite ways.
- **The evidence text shows raw counts while the weight is decayed**, so "13 of 49 commits" can
  rank below "8 of 49" when the 8 are recent.
- **A pure move counts as a commit of the moved file**, which lowers its confidences slightly.
- **A diff's own commits still raise the count of a pair that had older evidence.** Only a pair
  with no older evidence is dropped.
- **`min_score` under fusion.** With one kind answering, scores are that kind's own; with two,
  they are fused values (at most the sum of the weights under `norm`). `--min-score` applies to
  whichever it is.
- **Frozen docs still fill the mention ranking**, which is what holds the union below co-change
  alone on this repository. Follow-up (a) of *What follows* is still the fix.
- **Similarity-detected renames** are not followed; 7 of 24 in this history are lost.

## Symbol edges (#131)

Docs name functions, types and constants far more often than they name files. The mention
extractor already stores those backticked identifiers unresolved (`kind='ident'`). This section
resolves them to the files that define them, **from a symbol index another tool wrote**. Carta
reads the index. It does not parse code and it owns no call graph.

### Shape

```
carta/graph/
  symbols.py        SymbolRow, parse_symbols, SymbolTable, resolve_token, resolve_doc,
                    the two providers, build_symbol_edges, symbol_status
  symbol_scan.py    dangling_symbol and ambiguous_symbol for carta scan
  eval/symbol_replay.py   SymbolSource and the replay's symbol provider
```

**Provider interface.** A provider yields rows `(name, kind, file, line[, scope])`. `name` is the
unqualified name (a row written as `ns::fn` is split into scope `ns`, name `fn`), `file` a
repo-relative POSIX path, `line` 1-based or 0. `symbols.symbols_input(repo, settings, index)`
returns `(input_sha, load)`: a cheap hash of what the index would be made from, and a callable
that makes it. Two providers:

| Provider | Input | Notes |
|---|---|---|
| `file` | `graph.symbols.file`, a path inside the repository | JSON lines (`name`, `kind`, `file`, `line`, optional `scope`), TSV (`name kind file line [scope]`; `#` comments and a header row are skipped), or saved `ctags --output-format=json` (its `path` is the file, its `_type: ptag` lines are skipped). Told apart by the first non-blank line. This is the generic route: export any code-graph tool's index to one of the first two. |
| `ctags` | universal-ctags, if installed | Found as `$CARTA_CTAGS`, else `ctags`, `universal-ctags`, `uctags`, `ctags-universal` on `PATH`. A candidate counts only when `--list-features` exits 0 and lists `json`; the BSD `ctags` macOS ships fails that probe. Run as `--options=NONE --links=no --sort=no --output-format=json --fields=+n -f - -L <list>` from the repository root. |

**Off by default** (`graph.symbols.provider: none`). An `auto` value (use ctags when it happens to
be installed) was considered and rejected: the edges of a tree would then depend on what is on
the machine, every test that builds a store would run a third-party binary when one is present,
and running a C parser over every source file is something a project should ask for.

**States.** `off` (no provider), `unavailable` (a provider is configured and has nothing to read:
no binary, no `file` named, the file absent; symbol edges are cleared and the status line says
why; not stale, not an error), `failed` (an index was expected and could not be loaded: the file
is a symbolic link, too large or unreadable, there are more rows than the bound, ctags exited
non-zero, timed out or printed too much; **the stored edges are kept** and `graph status`
reports `symbols_failed` until a load succeeds), `ok`.

**Edges.** As reserved above: `kind='symbol'`, `src` the doc, `dst` the defining file,
`dst_type='file'`, `line` the doc line, `key` the identifier as written, `evidence` the doc
line, `weight` `1 / files` when a name resolves to more than one file, `meta`
`{"name", "symbol_kind", "def_line"[, "candidates", "via"]}`. They are written under
`extractor='symbol'` with `SYMBOL_EXTRACTOR_VERSION` (1), one `sources` row per doc whose `meta`
holds that doc's counts and its ambiguous identifiers. (The spec above put a `sources` row on
the index file; `replace_source` requires `edge.src == path`, so the row is per doc and the
index is recorded in `meta` keys `symbol.*` instead.) `GraphSettings.symbol_fingerprint()` is
separate from `fingerprint()`: turning symbols on re-extracts no mention, and the mention digest
is byte-identical with and without them (checked on this repository).

**Build.** `build_graph` runs the symbol pass after the mention pass, in its own transaction;
the index is loaded outside any transaction because ctags can take seconds. The pass is skipped
when the input hash, the settings, the extractor version and a hash of the identifiers are all
unchanged. For the `file` provider the input hash is the file's bytes. For `ctags` it is the
content of every code file plus the ctags version: there is no mtime shortcut here either, at
the cost of reading the code on every build (0.07 s for the 240 files of this repository; ctags
itself takes 0.13 s). A full `carta graph build` here takes 0.9 s with symbols on.

**Which files can be a definition site.** A file in the path set that is not a doc and is not
under `excluded_paths`. That answers "generated and vendored code": what git ignores is not in
the path set, and `node_modules/` and the like are excluded by default. ctags parses Markdown
headings as symbols; docs are never handed to it, and a row naming a doc is dropped.

**Which kinds.** Everything except `local`, `parameter`, `label`, `unknown`, `import`, `alias`,
`namespace`, `using`, `externvar`, `prototype` and `anon`. ctags reports a Python
`import x as y` as a `namespace`; a C++ namespace is lost with it, which costs little (it is
defined in many files and would be ambiguous anyway).

### Ambiguity policy

`resolve_token(table, token, mentioned, max_definitions)`:

1. The last part of the token is the name (`Store.put` looks up `put`).
2. One defining file: resolved.
3. Several: narrowed by the **qualifier** (a definition whose scope, file name or a directory is
   the part before the name: `FrameParser.parse`, `table.init`, `wire::init`), then by **the
   paths the same doc mentions** (a definition in a file the doc names, or under a directory it
   names). If that leaves at most `max_definitions` files the name resolves, each edge weighing
   `1 / files`; otherwise it is `ambiguous` and makes no edge.
4. A qualified token whose qualifier matches nothing local resolves on its name alone only when
   the name is distinctive (inner underscore or case hump) and has one defining file. So
   `store.replace_source` resolves and `subprocess.run` does not land on a local `run`.
5. A member that is not in the index falls back to its owner (`Parser.gone` to where `Parser`
   is defined, `via: owner`).

`max_definitions` defaults to **1**. The numbers, all on this repository with universal-ctags
6.2.1 (240 code files, 4,353 definitions kept of 4,495 rows, 3,825 names, 113 docs):

| Defining files | 1 | 2 | 3 | 4 or more |
|---|---|---|---|---|
| Names | 3,661 (95.7%) | 127 | 20 | 17 |

| `max_definitions` | (doc, identifier) pairs resolved | ambiguous | not in the index | edges | replay MRR, mention+symbol |
|---|---|---|---|---|---|
| 1 | 1,349 | 38 | 1,031 | 2,514 | 0.201 |
| 2 | 1,371 | 16 | 1,031 | 2,560 | 0.200 |
| 3 | 1,377 | 10 | 1,031 | 2,593 | 0.200 |
| 5 | 1,383 | 4 | 1,031 | 2,631 | 0.200 |

Population: 2,418 (doc, identifier) pairs from 1,347 distinct identifiers (4,089 occurrences).
A limit of 5 recovers 34 pairs (1.4%) as split-weight edges and does not move the replay, so
the default is the rule that never guesses. Of the 2,514 edges at the default, 2,444
resolve directly, 35 through a path the doc mentions, 13 through a qualifier, 16 on a
distinctive name behind a foreign qualifier and 6 through the owner. Down-weighting instead of
dropping is what `1 / files` does for a project that raises the limit.

42.6% of pairs are not in the index at all. That is the precision of the identifier shape
filter (A18), measured at last: those are library names, product names, config keys and
identifiers from other repositories. They make no edge, so they cost nothing.

### `carta impact`

`rank_impact` already scores any path-typed edge kind. With a provider configured `run_impact`
adds `symbol` to the default kinds (naming a provider is the opt-in; `--kinds` still wins), with
the existing weight 0.8. A match's `kind` is `symbol` in the JSON, and the text form says
"symbol: the doc names an identifier defined here". Not done: a deleted file's symbols. The
index no longer has them, so `carta impact --diff` on a deletion finds the docs that named the
file, not the docs that named only its functions (see `dangling_symbol`).

### Scan findings

Both are **off by default** (`graph.scan.dangling_symbols`, `graph.scan.ambiguous_symbols`).

**`dangling_symbol`: telling "external" from "gone".** An identifier the index does not know is
usually not this repository's. A finding therefore needs positive evidence, as
`missing_path_mention` does:

1. the name occurs as a whole word in **no code file today** (one pass over the code with a
   fixed word pattern; a name that is still imported, called or configured is not gone), and
2. **git shows the code had it** at a commit that changed this mention in the doc. The commits
   come from a pickaxe limited to the one doc (`git log -S<identifier> -- <doc>`, literal
   pathspec); the newest and the oldest are checked with `git grep -l -z -w -F` on that
   commit's tree, counting only files that are code by the rule above.

An external name fails test 2. A doc that says "we removed `foo()`" in the commit that removed
it fails it too, as does a mention written after the fact. Without git there is no finding. The
check needs no symbol index: "is it still a word in the code" is a text question. An identifier
that resolved only through its owner (`Parser.gone`) is still a candidate. Lookups are bounded
(`MAX_SYMBOL_HISTORY_CHECKS`, 1,000 per scan; the word scan by `MAX_CODE_SCAN_BYTES`); running
out, an unreadable code file or a git error sets `stats.symbol_check: "incomplete"`, and an
exception sets `"failed"`, so a check that could not finish never reads as a clean result.

Hand-check on this repository (at the commit this work started from; changelogs ignored):

| | Count |
|---|---|
| (doc, identifier) pairs whose name is in no code file today | 103 |
| of those, with history evidence (findings) | 10 |
| findings that are a symbol defined here and later removed | 8 |
| findings that are a name the code once used and never defined | 2 |
| true findings in dated plans, specs or trackers (the rest are in a live guide) | 6 of 8 |

93 of 103 candidates are silenced as never having been in the code. 8 of 10 is a good rate on
a sample that is far too small, and most of the true ones are in documents nobody will fix. It
ships off. The 2 misses are honest about what the test is: "the code no longer contains this
word", not "a definition was deleted". The scan took 5 s here.

**`ambiguous_symbol`** (severity `info`): an identifier the index defines in more files than the
limit, with nothing to narrow it. 33 findings here, among them `main` and `__init__`. The doc
is not wrong, so this is for a project that wants its docs to qualify names.

Not done: the issue's "this moved" finding (an identifier that used to resolve to one file and
now resolves to another). It needs the previous index, and the store is a derived cache that
does not keep one.

### Replay

A replay may only use what existed at a commit's parent, and a symbol index made today is not
that. Two answers were open: a flag that acknowledges the leak, or an index rebuilt per commit
if that could be made cheap. Both exist, and the per-commit one is the route to use:

| `--symbols` | Index used for a commit | Leak |
|---|---|---|
| `ctags` | universal-ctags run on the parent's code, read from git objects into a scratch directory outside the repository under generated names; results kept per `(blob, basename)`, so after the first commit only changed files are indexed | none |
| `tracked:PATH` | the repository's own tracked symbols file, as of the parent | none |
| `file:PATH` | one file as it is today | **yes**; refused without `--accept-symbol-leak`; the report's `symbols.leak` is true and the text prints a warning. Definitions in files the parent did not have are dropped, which removes part of the leak. |

An arm that reads `symbol` with no `--symbols` is an error that lists the three. The provider
runs its own mention state to get each doc's identifiers at the parent, so `--arm s=symbol`
works without a mention arm. `--max-definitions N` overrides the limit for a run.

Results, `carta graph replay --range origin/main --no-cache --no-config --arm mention=mention
--arm symbol=symbol --arm both=mention+symbol --symbols ctags` at `792b6690`, 403 commits
scanned, 54 eligible, 8.4 s:

| | R@1 | R@3 | R@5 | R@10 | any@5 | MRR |
|---|---|---|---|---|---|---|
| graph[mention] | 0.035 | 0.182 | 0.210 | 0.262 | 0.278 | 0.191 |
| graph[symbol] | 0.035 | 0.069 | 0.113 | 0.249 | 0.185 | 0.151 |
| graph[both] | 0.035 | 0.201 | 0.210 | 0.315 | 0.278 | 0.201 |
| popularity | 0.302 | 0.791 | 0.885 | 0.944 | 0.981 | 0.731 |
| basename | 0.073 | 0.201 | 0.259 | 0.340 | 0.370 | 0.226 |
| basename_idf | 0.077 | 0.194 | 0.228 | 0.290 | 0.296 | 0.215 |

- **both against mention:** MRR +0.010, 95% interval -0.000 to +0.021; 25 commits better, 5
  worse, 24 tied. Recall@10 0.262 to 0.315, recall@5 unchanged.
- **Without the three hub docs** (21 commits, labelled inconclusive): mention 0.276, symbol
  0.211, both 0.273; paired -0.003 (-0.021 to +0.010). Recall@10 0.270 to 0.365.
- **Source-only commits** (203, unlabelled): a prediction for 87.7% against 87.2%, 3.74 docs
  returned at 5 against 3.49. Slightly more to read, as expected of more edges.
- **What the edges add structurally** (today's tree): 538 doc-to-code-file pairs from symbols
  against 688 from mentions; 193 of the 538 are not already a mention pair; 4 code files and 4
  docs are reachable only through a symbol.
- **The leak, measured.** The same run with today's index (`--symbols file:` an export of
  today's ctags, `--accept-symbol-leak`): symbol alone 0.167 instead of 0.151, both 0.204, and
  the paired difference against mention becomes +0.013 with an interval of +0.001 to +0.024.
  The leak is what would have turned "not shown" into "significant".

Reading: symbol edges are a small, mostly harmless addition on this history. They lift recall
deeper in the ranking and leave the top of it alone, and they do not change the verdict of the
Results section: the docs that get edited with code here do not name code. The comparison that
would decide whether they are worth more (the no-hub table) has 21 commits and is inconclusive.

### Security

The symbol arm is written to the standard of the hardening section, and adds these surfaces:

| Surface | Rule |
|---|---|
| The symbols file | Read with `safeio.read_regular_file`: a path inside the repository from config (never absolute, never upward), no symbolic link in any component, size checked before reading (`MAX_SYMBOLS_FILE_BYTES`, 64 MiB). |
| Index rows | A line over `MAX_SYMBOL_LINE_BYTES` is skipped before it is parsed; JSON that is not an object, or nests too deeply, is a skipped line. A name must be an identifier, a file a clean relative path that is in the path set, a kind a short lowercase word, a line a small positive integer. More than `MAX_SYMBOL_ROWS` rows is a failed load, not a truncated index. At most 64 definitions are kept per name. No regular expression is ever built from index or doc text. |
| Running ctags | `--options=NONE` (the repository's `.ctags.d` is not read), `--links=no`, an explicit file list in a private temporary directory, each name prefixed `./`; names with control characters, links, non-regular files and files over 2 MiB are not listed. A binary inside the repository being read is never run, whatever `PATH` says. Output is read line by line with a byte ceiling and a wall-clock timeout, and a row about a file that was not listed is dropped. |
| The replay's scratch tree | A new directory outside the repository; blob content is written under a number and, only when the basename is plain, that basename. |
| Git lookups | `commits_changing_text` passes the identifier as one `-S` argument and the doc as a literal pathspec; `files_with_word` takes only a full object name and an identifier, reads names with `-z`, and raises when a name is not under the tree it asked about. |
| Failure | A failed load keeps the edges and is reported stale; an incomplete or failed scan check is recorded as such. |

### Left open

- The symbol pass re-reads every code file on each build to decide whether ctags must run. It
  is cheap here; a very large tree may want the `file` provider and an index made in CI.
- `graph status` with the `ctags` provider runs the capability probe and hashes the code. It
  still writes nothing.
- ctags decides languages by file name, and the per-commit replay keeps a file's basename only
  when it is made of plain characters; an oddly named file may be indexed as another language
  there than in a live build.
- A row's `kind` is whatever the tool calls it. The ignored-kind list is tuned to ctags names.
- JSON keys are symbols to ctags (233 definitions here), so a backticked config key can
  resolve to a JSON file. That was left in: it is a true reference.
- The word scan treats every non-doc text file as code, so a name that survives only in a
  comment or a data file is "still there". That errs towards silence.
- Tested against universal-ctags 6.2.1 on macOS only.
