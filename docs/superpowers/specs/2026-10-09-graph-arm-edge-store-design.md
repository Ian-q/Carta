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
| N2 | Cut a suffix: `#fragment`, `?query`, `:LINE`, `:LINE:COL`, `:LINE-LINE`, and, after a filename that has an extension, `::anything` or `:identifier` (a pytest node id `tests/test_x.py::test_y`, or `file.py:func`) (A12). A candidate that is only a fragment (`#section`) is rejected. |
| N3 | Reject if empty, longer than 300 characters, or containing whitespace, a backslash, `$`, `<`, `>`, `\|`, `"`, `'`, `…` or `...`. |
| N4 | Reject URLs and non-paths: a scheme (`^[A-Za-z][A-Za-z0-9+.-]*:`), a leading `//`, `www.`, `~`, `-` or `@`, or an e-mail shape. |
| N5 | Leading `/`: in `ctx='link'` it means the repo root (the hosting convention) and is stripped; in every other context it is an absolute filesystem path and is rejected. |
| N6 | Braces: expand one level of `{a,b,c}` into separate candidates (at most 16); any other `{` or `}` rejects (template placeholder). |
| N7 | If it now contains `*`, `?` or `[`, it is a **glob**: see below. |
| N8 | Resolve to a repo-relative path with `posixpath.normpath`. `ctx='link'`: relative to the doc's directory first, then from the repo root. Other contexts: from the repo root first, then the doc's directory, then each ancestor of the doc's directory, nearest first (`meta.via='relative'` when a non-root base resolved it), so a README inside a subproject can name `src/main.c` relative to itself (A11b); a candidate starting `./` or `../` resolves from the doc's directory only. N9 and N10 are tried at each base in turn before the next base. A result that begins with `..` is outside the repo and is rejected. |
| N9 | Exact file in the index: an edge with `dst_type='doc'` if the target is itself a doc (matches `doc_globs`), otherwise `'file'`. |
| N10 | Directory: the candidate ends with `/`, or has two or more segments, and names a directory in the index (any directory that contains at least one path). Edge with `dst` in canonical form (trailing `/`), `dst_type='dir'`. A single bare word without a trailing slash is never a directory mention (`tests`, `docs` and `build` are words). |
| N11 | Suffix match, for a candidate with two or more segments that did not resolve: if exactly one indexed file (or directory) ends with `/` + candidate on a segment boundary, resolve to it, with `meta.via='suffix'`. Two or more matches: no edge. |
| N12 | Bare filename (no `/`), only for `ctx` `code` (single-token), `link` or `frontmatter`, and only when it has an extension matching `\.[A-Za-z0-9]{1,8}$` with a non-numeric extension: resolve to the repo-root file, else the sibling in the doc's directory, else the one file in the repo with that basename (`meta.via='basename'`). Two or more matches: no edge. |
| N13 | **Missing**: nothing resolved, `ctx` is not `fence`, the candidate has a `/`, and it has one of two shapes: (a) two or more segments and a last segment with a non-numeric extension, whatever its first segment is (so a mention of anything under a top-level directory that was since removed is still recorded); or (b) it ends with `/` and its first segment is a top-level directory in the index. For `ctx='link'` a bare filename with an extension also qualifies (a broken sibling link). It is **not** a placeholder: no adjacent segment pair `path/to`, and no segment that, compared whole and case-insensitively (the last segment by its stem), is one of `foo`, `bar`, `baz`, `qux`, `example`, `placeholder`, `xxx`, `your`, or begins `your-`, `your_`, `my-`, `my_`; a segment that names an existing directory at that position is never a placeholder (so `examples/`, `bazel/`, and a real `foo/` are ordinary directories). Its first segment is not `.git` or `.carta`, which are outside the path set by design. Edge with `dst` = the normalised path, `dst_type='missing'`. Precision for the *scan finding* comes from the history gate, not from this rule (A10). |
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
absent.

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
     `git check-ignore` does not match it. This is the only history-free reason, and the two
     conditions keep it off site-root links (`/guide/setup/`), rendered names (`setup.html` for
     `setup.md`) and links into generated output (`htmlcov/index.html`) (A11). Those cases, and a
     percent-encoded link to a file with a space in its name, are rows in the precision table.

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
- `rr = 1 / rank` of the first gold doc within the top `max(k)`, else 0; **MRR** is its mean

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
- `EdgeStore.open_existing(path)` returns `None` when the file is absent and creates nothing;
  `graph_status` uses it.
- Opening creates `.carta/graph/.gitignore` (`*`) when it is missing.

**Build**
- The report also carries `enabled`, `written` (sources whose stored rows actually changed, which
  is 1 when adding a file resolves one doc's `missing` edge even though all docs were
  re-extracted) and `skipped` (docs that could not be read).
- A doc containing a NUL byte is not text: it is kept as a source with no edges. Other bytes are
  decoded as UTF-8 with replacement, so a doc with a stray invalid byte still yields its edges.
- `graph_status` reasons: `no_store`, `newer_schema`, `extractor_changed`, `settings_changed`,
  `paths_changed`, `docs_changed`, in that order.
- `gitio.py` holds only `run_git`, `is_work_tree` and `ls_files` so far; the history readers the
  eval needs are added with it. git is always run with `GIT_DIR`, `GIT_WORK_TREE` and
  `GIT_INDEX_FILE` removed from the environment, so a build started from inside a git hook reads
  the repository it was pointed at.
- Measured on this repository (324 paths, 113 docs): 7.3k edges extracted in 0.3 s. The budget
  test (500 docs of 16 kB, 55,000 mentions) runs in under 4 s locally against a 10 s limit.

## Deferred

- Comparing derived edges with hand-declared source-glob lists in frontmatter (the issue's third
  test). The frontmatter tokens are already extracted as edges, so the comparison is a small
  report over the store; it needs a corpus that has such lists.
- Re-running the retrieval eval with doc-to-doc mention edges in graph expansion (see above).
- Pointing the `claude-md-sync` skill at `carta impact --diff`. The command is the input the
  issue asks for; changing the skill's procedure is its own reviewed change.
- A stage-1 cache inside the store, if a corpus outgrows the build budget.

## Results

To be appended after implementation: the replay report on real history (population first), and
the comparison with the three baselines, with and without hub docs.
