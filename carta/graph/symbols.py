"""Symbol edges: resolve the identifiers docs name to the files that define them.

Carta does not parse code. It reads a symbol index that another tool wrote,
as rows of ``(name, kind, file, line)``, and joins it to the backticked
identifiers the mention extractor already stores (``kind='ident'``). Two
providers produce rows:

* ``file``: a symbols file in the repository. JSON lines
  (``{"name", "kind", "file", "line"[, "scope"]}``), tab-separated values
  (``name<TAB>kind<TAB>file<TAB>line[<TAB>scope]``), or the output of
  ``ctags --output-format=json`` saved to a file. Any code-graph tool can be
  exported to one of the first two.
* ``ctags``: universal-ctags, run over the repository's code files, if such
  a binary is installed. It is recognised by what it can do
  (``--list-features`` lists ``json``), never by its name: the ``ctags`` that
  macOS ships is a different program.

No provider, no binary, or no file means no symbol edges and a status that
says why. It is never an error.

Everything read here is untrusted: the index (its names, kinds and paths),
the names of the files handed to ctags, and what ctags prints about them.
Sizes and counts are bounded before the work, the symbols file is never read
through a symbolic link, and a row that is not exactly a row is skipped.
"""
from __future__ import annotations

import hashlib
import json
import os
import posixpath
import re
import shutil
import signal
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Iterable, List, NamedTuple, Optional, Sequence, Tuple

from carta.graph import limits
from carta.graph.limits import (
    MAX_DEFINITIONS_PER_NAME, MAX_SYMBOL_LINE_BYTES, MAX_SYMBOL_NAME_LENGTH,
    MAX_SYMBOLS_FILE_BYTES,
)
from carta.graph.model import (
    MENTION_EXTRACTOR, SYMBOL_EXTRACTOR, SYMBOL_EXTRACTOR_VERSION, Edge, canonical_meta,
)
from carta.graph.paths import PathIndex, is_doc_path, is_excluded_rel
from carta.graph.safeio import SkippedFile, read_regular_file
from carta.graph.settings import GraphSettings
from carta.graph.store import EdgeStore, store_path

# Read through the module so a test can lower them.
MAX_SYMBOL_ROWS = limits.MAX_SYMBOL_ROWS
MAX_CTAGS_FILES = limits.MAX_CTAGS_FILES
MAX_CTAGS_OUTPUT_BYTES = limits.MAX_CTAGS_OUTPUT_BYTES
CTAGS_TIMEOUT_SECONDS = limits.CTAGS_TIMEOUT_SECONDS

META_STATE = "symbol.state"                 # ok | off | unavailable | failed
META_DETAIL = "symbol.detail"
META_PROVIDER = "symbol.provider"
META_INPUT = "symbol.input_sha"             # what the index was made from
META_INDEX = "symbol.index_sha"             # the rows themselves
META_ROWS = "symbol.rows"
META_SETTINGS = "symbol.settings_sha"
META_VERSION = "symbol.extractor_version"
META_IDENTS = "symbol.idents_sha"           # the identifiers that were resolved

CTAGS_NAMES = ("ctags", "universal-ctags", "uctags", "ctags-universal")
CTAGS_ENV = "CARTA_CTAGS"

# Kinds that are not a definition a doc would name: a function's own locals
# and parameters, a jump label, and the names an import brings in (ctags
# calls a Python ``import x as y`` a namespace).
IGNORED_KINDS = frozenset(("local", "parameter", "label", "unknown", "import", "alias",
                           "namespace", "using", "externvar", "prototype", "anon"))

# Languages that hold data or prose, not code. A key in a JSON report or a
# heading is "defined in one file" only in the sense that it is written there;
# ctags reports them (kinds string, object, array, ...) and an edge to them is
# noise. Compared in lower case against the ``language`` field of a row, which
# universal-ctags writes with ``--fields=+l`` and a symbols file may carry.
DATA_LANGUAGES = frozenset((
    "json", "yaml", "toml", "iniconf", "javaproperties", "xml", "plistxml", "svg", "openapi",
    "ansibleplaybook", "diff", "passwd", "markdown", "rmarkdown", "quarto", "asciidoc",
    "restructuredtext", "org", "txt2tags", "pod", "rdoc", "man", "scdoc", "frontmatter",
    "tex", "bibtex", "biblatex"))

# Kinds that own members: a member that is not in the index falls back to one
# of these whatever its name looks like.
OWNER_KINDS = frozenset(("class", "struct", "interface", "enum", "trait", "union", "typedef",
                         "type", "protocol", "record", "module"))

# The public top-level modules of the Python standard library, pinned here so
# that the result does not depend on the interpreter Carta runs under. A
# qualifier from this list that matches nothing in the repository is somebody
# else's: ``subprocess.check_output`` is not the local ``check_output``.
FOREIGN_QUALIFIERS = frozenset("""
    abc aifc argparse array ast asynchat asyncio asyncore atexit audioop base64 bdb
    binascii binhex bisect builtins bz2 cProfile calendar cgi cgitb chunk cmath cmd code codecs
    codeop collections colorsys compileall concurrent configparser contextlib contextvars copy
    copyreg crypt csv ctypes curses dataclasses datetime dbm decimal difflib dis distutils
    doctest email encodings ensurepip enum errno faulthandler fcntl filecmp fileinput fnmatch
    fractions ftplib functools gc genericpath getopt getpass gettext glob graphlib grp gzip
    hashlib heapq hmac html http idlelib imaplib imghdr imp importlib inspect io ipaddress
    itertools json keyword lib2to3 linecache locale logging lzma mailbox mailcap marshal math
    mimetypes mmap modulefinder msilib msvcrt multiprocessing netrc nis nntplib nt ntpath
    nturl2path numbers opcode operator optparse os ossaudiodev pathlib pdb pickle pickletools
    pipes pkgutil platform plistlib poplib posix posixpath pprint profile pstats pty pwd
    py_compile pyclbr pydoc pydoc_data pyexpat queue quopri random re readline reprlib resource
    rlcompleter runpy sched secrets select selectors shelve shlex shutil signal site smtpd
    smtplib sndhdr socket socketserver spwd sqlite3 sre_compile sre_constants sre_parse ssl stat
    statistics string stringprep struct subprocess sunau symtable sys sysconfig syslog tabnanny
    tarfile telnetlib tempfile termios textwrap threading time timeit tkinter token
    tokenize tomllib trace traceback tracemalloc tty turtle turtledemo types typing unicodedata
    unittest urllib uu uuid venv warnings wave weakref webbrowser winreg winsound wsgiref xdrlib
    xml xmlrpc zipapp zipfile zipimport zlib zoneinfo
""".split())

_BOM = b"\xef\xbb\xbf"
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_KIND = re.compile(r"[a-z0-9_ -]{0,40}")
_SEP = re.compile(r"\.|::|->|#")
_INNER_UNDERSCORE = re.compile(r"[A-Za-z0-9]_+[A-Za-z0-9]")
_CASE_HUMP = re.compile(r"[a-z][A-Z]")
_TSV_HEADER = ("name", "kind", "file", "line")


class SymbolsUnavailable(Exception):
    """There is no index to read (no binary, no file). Not a failure: no symbol edges."""


class SymbolsFailed(Exception):
    """An index was expected and could not be loaded. What the store holds is kept."""


class SymbolRow(NamedTuple):
    """One definition, as an index reports it. ``name`` is the unqualified name."""

    name: str
    kind: str
    file: str               # repo-relative POSIX path
    line: int               # 1-based; 0 when the index does not say
    scope: str = ""         # the enclosing class, namespace or struct, when known


@dataclass(frozen=True)
class ParsedSymbols:
    rows: List[SymbolRow]
    skipped: int            # lines that were not a row
    format: str             # jsonl | tsv | ''


# -- reading rows ----------------------------------------------------------------

def _clean(text: str) -> bool:
    return not any(ord(ch) < 0x20 or ord(ch) == 0x7f for ch in text)


def _file_field(value: object) -> Optional[str]:
    if not isinstance(value, str):
        return None
    text = value
    while text.startswith("./"):
        text = text[2:]
    if (not text or len(text) > 1024 or text.startswith("/") or not _clean(text)
            or any(part in ("", ".", "..") for part in text.split("/"))):
        return None
    return text


def make_row(name: object, kind: object, file: object, line: object,
             scope: object = "") -> Optional[SymbolRow]:
    """One validated row, or ``None`` when any field is not what a row holds."""
    if not isinstance(name, str) or not 0 < len(name) <= MAX_SYMBOL_NAME_LENGTH:
        return None
    parts = _SEP.split(name)
    leaf = parts[-1]
    if _NAME.fullmatch(leaf) is None:
        return None
    path = _file_field(file)
    if path is None:
        return None
    if not isinstance(scope, str) or len(scope) > MAX_SYMBOL_NAME_LENGTH or not _clean(scope):
        scope = ""
    if not scope and len(parts) > 1:
        scope = ".".join(parts[:-1])
    kind_text = kind.strip().lower() if isinstance(kind, str) else ""
    if _KIND.fullmatch(kind_text) is None:
        kind_text = ""
    number = line if isinstance(line, int) and not isinstance(line, bool) else 0
    if not 0 < number < 2 ** 31:
        number = 0
    return SymbolRow(leaf, kind_text, path, number, scope)


def _row_from_json(text: str) -> Tuple[Optional[SymbolRow], bool]:
    """``(row, counted)``: ``counted`` is False for a line that is rightly not a row."""
    try:
        obj = json.loads(text)
    except (ValueError, RecursionError):
        return None, True
    if not isinstance(obj, dict):
        return None, True
    if "_type" in obj and obj["_type"] != "tag":
        return None, False                              # a ctags pseudo-tag
    language = obj.get("language")
    if isinstance(language, str) and language.strip().lower() in DATA_LANGUAGES:
        return None, False                              # a key in a data file, a heading
    # ctags writes the file as "path" (its "file" is a flag); a plain symbols
    # file writes it as "file".
    file = obj.get("path") if isinstance(obj.get("path"), str) else obj.get("file")
    row = make_row(obj.get("name"), obj.get("kind", ""), file, obj.get("line", 0),
                   obj.get("scope", ""))
    return row, True


def _row_from_tsv(text: str) -> Tuple[Optional[SymbolRow], bool]:
    fields = text.split("\t")
    if tuple(f.strip().lower() for f in fields[:4]) == _TSV_HEADER:
        return None, False
    if not 4 <= len(fields) <= 5:
        return None, True
    digits = fields[3].strip()
    line = int(digits) if digits.isascii() and digits.isdigit() and len(digits) <= 10 else 0
    return make_row(fields[0], fields[1], fields[2], line,
                    fields[4] if len(fields) == 5 else ""), True


class _RowReader:
    """Turns index lines into rows, one at a time, with every bound checked first."""

    def __init__(self, fmt: str = "") -> None:
        self.format = fmt
        self.rows: List[SymbolRow] = []
        self.skipped = 0

    def feed(self, raw: bytes) -> None:
        if len(raw) > MAX_SYMBOL_LINE_BYTES:
            self.skipped += 1
            return
        if b"\0" in raw:
            self.skipped += 1
            return
        text = raw.decode("utf-8", "replace").strip("\r\n")
        if not text.strip() or text.startswith("#"):
            return                                      # blank, or a comment in either format
        if not self.format:
            self.format = "jsonl" if text.lstrip().startswith(("{", "[")) else "tsv"
        row, counted = (_row_from_json(text) if self.format == "jsonl" else _row_from_tsv(text))
        if row is None:
            self.skipped += 1 if counted else 0
            return
        if len(self.rows) >= MAX_SYMBOL_ROWS:
            raise SymbolsFailed(f"the symbol index has more than {MAX_SYMBOL_ROWS:,} rows")
        self.rows.append(row)

    def result(self) -> ParsedSymbols:
        return ParsedSymbols(self.rows, self.skipped, self.format)


def parse_symbols(data: bytes, fmt: str = "") -> ParsedSymbols:
    """Rows from the bytes of a symbols file.

    The format is told by the first line that is neither blank nor a ``#``
    comment, after a UTF-8 byte-order mark if there is one. A line that is
    not a row is counted in ``skipped`` and otherwise ignored.
    Raises ``SymbolsFailed`` only when there are more rows than
    ``MAX_SYMBOL_ROWS``: half an index would report real symbols as unknown.
    """
    reader = _RowReader(fmt)
    start = len(_BOM) if data.startswith(_BOM) else 0
    size = len(data)
    while start < size:
        end = data.find(b"\n", start)
        if end < 0:
            end = size
        if end - start > MAX_SYMBOL_LINE_BYTES:
            reader.skipped += 1
        elif end > start:
            reader.feed(data[start:end])
        start = end + 1
    return reader.result()


# -- the table -------------------------------------------------------------------

class SymbolTable:
    """Definitions by name. Immutable once built; ``sha`` identifies its content."""

    def __init__(self, rows: Iterable[SymbolRow] = (),
                 accept: Optional[Callable[[str], bool]] = None) -> None:
        kept = set()
        self.dropped = 0
        verdicts: Dict[str, bool] = {}
        for row in rows:
            if row.kind in IGNORED_KINDS:
                self.dropped += 1
                continue
            if accept is not None:
                ok = verdicts.get(row.file)
                if ok is None:
                    ok = verdicts[row.file] = bool(accept(row.file))
                if not ok:
                    self.dropped += 1
                    continue
            kept.add(row)
        self._rows = sorted(kept, key=lambda r: (r.name, r.file, r.line, r.kind, r.scope))
        self._by_name: Dict[str, List[SymbolRow]] = {}
        self._crowded = set()
        for row in self._rows:
            bucket = self._by_name.setdefault(row.name, [])
            if len(bucket) < MAX_DEFINITIONS_PER_NAME:
                bucket.append(row)
            else:
                self._crowded.add(row.name)
        digest = hashlib.sha256()
        for row in self._rows:
            digest.update(json.dumps(list(row), ensure_ascii=False,
                                     separators=(",", ":")).encode("utf-8", "replace"))
            digest.update(b"\n")
        self.sha = digest.hexdigest()

    def __len__(self) -> int:
        return len(self._rows)

    def rows(self) -> List[SymbolRow]:
        return list(self._rows)

    def names(self) -> int:
        return len(self._by_name)

    def lookup(self, name: str) -> Sequence[SymbolRow]:
        return self._by_name.get(name, ())

    def is_crowded(self, name: str) -> bool:
        """True when the name has more definitions than are kept: ambiguous whatever else."""
        return name in self._crowded

    def files_per_name(self) -> Dict[int, int]:
        """``distinct defining files -> how many names have that many``."""
        out: Dict[int, int] = {}
        for name, rows in self._by_name.items():
            n = len({r.file for r in rows}) + (1 if name in self._crowded else 0)
            out[n] = out.get(n, 0) + 1
        return dict(sorted(out.items()))


# -- resolving -------------------------------------------------------------------

class Mentioned:
    """The paths a doc names itself: what "near this doc" means for a symbol."""

    __slots__ = ("files", "dirs")

    def __init__(self, files: Iterable[str] = (), dirs: Iterable[str] = ()) -> None:
        self.files = frozenset(files)
        self.dirs = frozenset(dirs)         # each ends with "/"

    def covers(self, path: str) -> bool:
        """True when the doc names ``path`` or a directory above it. Costs the path's depth."""
        if path in self.files:
            return True
        if self.dirs:
            cut = path.find("/")
            while cut >= 0:
                if path[:cut + 1] in self.dirs:
                    return True
                cut = path.find("/", cut + 1)
        return False


@dataclass(frozen=True)
class Resolution:
    status: str                         # resolved | ambiguous | unknown
    rows: Tuple[SymbolRow, ...] = ()    # one per defining file, the first definition in it
    candidates: int = 0                 # defining files before any narrowing
    via: str = ""                       # '' | qualifier | mentioned | leaf | owner


_UNKNOWN = Resolution("unknown")


def _has_shape(name: str) -> bool:
    return bool(_INNER_UNDERSCORE.search(name) or _CASE_HUMP.search(name))


def _qualifier_matches(row: SymbolRow, qualifier: str) -> bool:
    if row.scope and _SEP.split(row.scope)[-1] == qualifier:
        return True
    parts = row.file.split("/")
    return posixpath.splitext(parts[-1])[0] == qualifier or qualifier in parts[:-1]


def _one_per_file(rows: Sequence[SymbolRow]) -> Tuple[SymbolRow, ...]:
    first: Dict[str, SymbolRow] = {}
    for row in rows:                    # rows arrive sorted by file, then line
        first.setdefault(row.file, row)
    return tuple(first[f] for f in sorted(first))


def resolve_token(table: SymbolTable, token: str, mentioned: Mentioned, max_definitions: int,
                  alive: Optional[Callable[[str], bool]] = None) -> Resolution:
    """Where the identifier ``token`` is defined, by the ambiguity policy.

    ``token`` is as the mention extractor stores it, so it may be qualified
    (``Store.put``, ``ns::fn``); the last part is the name looked up.

    * One defining file: resolved.
    * Several: narrowed first by the qualifier (a definition whose scope, file
      name or directory is the part before the name), then by the paths the
      same doc mentions. What is left resolves when it is at most
      ``max_definitions`` files (each edge then weighs ``1 / files``), and is
      ``ambiguous`` otherwise.
    * A qualified token whose qualifier matches nothing here is taken on its
      name alone only if the name is distinctive (an inner underscore or a
      case hump), has one defining file, and no part of the qualifier is a
      standard-library module (``FOREIGN_QUALIFIERS``): ``store.replace_source``
      yes, ``other.run`` no, ``subprocess.check_output`` no. This tests the
      shape of the name and one pinned list, nothing more: a third-party
      ``lib.some_name`` whose leaf the repository also defines once still
      resolves here. Such edges carry ``via: leaf``.
    * A member that is not in the index falls back to its owner when the
      owner is distinctive in the same sense (``FrameParser.gone``), or is a
      type (``OWNER_KINDS``) with exactly one defining file (``Parser.gone``
      to where ``class Parser`` is defined). A plain word that is not a type
      (``os.gone``) vouches for nothing.

    ``alive`` leaves out definitions in files it rejects (a tree that lacks them).
    """
    parts = _SEP.split(token)
    leaf, quals = parts[-1], parts[:-1]

    def defs(name: str) -> List[SymbolRow]:
        rows = table.lookup(name)
        return [r for r in rows if alive(r.file)] if alive is not None else list(rows)

    rows = defs(leaf)
    via = ""
    if not rows:
        # The member is gone or was never indexed; its owner may still be here.
        for i in range(len(quals) - 1, -1, -1):
            found = defs(quals[i])
            if not found:
                continue
            if not _has_shape(quals[i]):
                types = [r for r in found if r.kind in OWNER_KINDS]
                if (len(types) != len(found) or len({r.file for r in found}) != 1
                        or table.is_crowded(quals[i])):
                    continue
            rows, leaf, quals, via = found, quals[i], quals[:i], "owner"
            break
        if not rows:
            return _UNKNOWN
    candidates = len({r.file for r in rows})
    crowded = table.is_crowded(leaf)
    if quals:
        matching = [r for r in rows if _qualifier_matches(r, quals[-1])]
        if matching:
            if len({r.file for r in matching}) < candidates or crowded:
                via = via or "qualifier"
            rows, crowded = matching, False
        elif via != "owner":
            if (candidates != 1 or crowded or not _has_shape(leaf)
                    or any(q in FOREIGN_QUALIFIERS for q in quals)):
                return _UNKNOWN
            via = "leaf"
    if len({r.file for r in rows}) > 1:
        near = [r for r in rows if mentioned.covers(r.file)]
        if near and len({r.file for r in near}) < len({r.file for r in rows}):
            rows, crowded, via = near, False, via or "mentioned"
    kept = _one_per_file(rows)
    if crowded or len(kept) > max_definitions:
        return Resolution("ambiguous", (), max(candidates, len(kept)), via)
    return Resolution("resolved", kept, candidates, via)


def mentioned_paths(edges: Iterable[Edge]) -> Mentioned:
    """The files and directories among one doc's mention edges."""
    files, dirs = set(), set()
    for edge in edges:
        if edge.kind != "mention":
            continue
        if edge.dst_type in ("file", "doc"):
            files.add(edge.dst)
        elif edge.dst_type == "dir":
            dirs.add(edge.dst)
    return Mentioned(files, dirs)


@dataclass
class DocSymbols:
    """What one doc's identifiers resolved to."""

    edges: List[Edge]
    resolved: int = 0
    ambiguous: Optional[Dict[str, List[int]]] = None     # token -> [first line, defining files]
    unknown: int = 0


def resolve_doc(doc: str, doc_edges: Sequence[Edge], table: SymbolTable, max_definitions: int,
                alive: Optional[Callable[[str], bool]] = None) -> DocSymbols:
    """Symbol edges for one doc, from its ``ident`` and ``mention`` edges.

    One edge per (identifier, line, defining file): ``dst`` is the file,
    ``key`` the identifier as written, ``evidence`` the doc line.
    """
    mentioned = mentioned_paths(doc_edges)
    out: Dict[tuple, Edge] = {}
    verdicts: Dict[str, Resolution] = {}
    ambiguous: Dict[str, List[int]] = {}
    for edge in doc_edges:
        if edge.kind != "ident" or edge.dst_type != "token":
            continue
        token = edge.dst
        got = verdicts.get(token)
        if got is None:
            got = verdicts[token] = resolve_token(table, token, mentioned, max_definitions, alive)
        if got.status == "ambiguous":
            seen = ambiguous.get(token)
            if seen is None or edge.line < seen[0]:
                ambiguous[token] = [edge.line, got.candidates]
            continue
        if got.status != "resolved":
            continue
        weight = round(1.0 / len(got.rows), 6)
        for row in got.rows:
            meta = {"name": row.name, "symbol_kind": row.kind, "def_line": row.line}
            if got.candidates > 1:
                meta["candidates"] = got.candidates
            if got.via:
                meta["via"] = got.via
            made = Edge(kind="symbol", src=doc, dst=row.file, dst_type="file", line=edge.line,
                        key=token, evidence=edge.evidence, weight=weight,
                        meta=canonical_meta(meta), extractor=SYMBOL_EXTRACTOR,
                        extractor_version=SYMBOL_EXTRACTOR_VERSION)
            out[made.identity] = made
    statuses = [v.status for v in verdicts.values()]
    return DocSymbols(sorted(out.values()), statuses.count("resolved"), ambiguous,
                      statuses.count("unknown"))


# -- providers -------------------------------------------------------------------

def definition_filter(index: PathIndex, settings: GraphSettings) -> Callable[[str], bool]:
    """Which files may be a definition site: in the path set, not a doc, not excluded."""
    def accept(path: str) -> bool:
        return (index.has_file(path) and not is_doc_path(path, settings)
                and not is_excluded_rel(path, settings.excluded_paths))
    return accept


@dataclass(frozen=True)
class Loaded:
    table: SymbolTable
    input_sha: str          # identifies what the index was made from
    detail: str             # one line for a person
    skipped: int = 0        # index lines that were not rows


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_symbols_file(repo_root: Path, rel: str) -> bytes:
    """The bytes of a symbols file inside the repository; never through a link."""
    try:
        return read_regular_file(repo_root, rel, MAX_SYMBOLS_FILE_BYTES)
    except SkippedFile as skipped:
        if skipped.reason == "missing":
            raise SymbolsUnavailable(f"the symbols file {rel} is not there") from None
        why = {"symlink": "is (or is behind) a symbolic link",
               "too_large": f"is larger than {MAX_SYMBOLS_FILE_BYTES:,} bytes",
               "unsafe_path": "is not a path inside the repository"}.get(
                   skipped.reason, "is not a regular file")
        raise SymbolsFailed(f"the symbols file {rel} {why}; it was not read") from None
    except OSError as exc:
        raise SymbolsFailed(
            f"the symbols file {rel} could not be read: {exc.strerror or 'I/O error'}") from None


def _file_input(repo_root: Path, settings: GraphSettings, index: PathIndex
                ) -> Tuple[bytes, str]:
    """The symbols file and the sha of what its edges are made from.

    That is the file *and* the path set: a row counts only when its file is
    in the tree, so a code file that is added or deleted changes the edges
    without a byte of the symbols file changing.
    """
    rel = settings.symbols_file
    if not rel:
        raise SymbolsUnavailable("graph.symbols.provider is `file` but graph.symbols.file "
                                 "names none")
    data = read_symbols_file(repo_root, rel)
    return data, _sha(b"file\0" + index.fingerprint().encode("ascii") + b"\0" + data)


def require_rows(parsed: ParsedSymbols, what: str) -> None:
    """Raise ``SymbolsFailed`` for an index that has lines and not one row.

    That is a file in another encoding or another format, not an empty index,
    and reading it as "no definitions" would call the store fresh.
    """
    if not parsed.rows and parsed.skipped:
        raise SymbolsFailed(
            f"{what} has no row that could be read ({parsed.skipped:,} "
            f"line{'' if parsed.skipped == 1 else 's'} skipped); it has to be UTF-8, as JSON "
            f"lines or tab-separated values")


@dataclass(frozen=True)
class Ctags:
    path: str
    version: str


def _probe(binary: str, *args: str) -> Optional[str]:
    try:
        proc = subprocess.run(
            [binary, "--options=NONE", *args], stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=limits.CTAGS_PROBE_TIMEOUT_SECONDS, check=False)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0 or len(proc.stdout) > 1024 * 1024:
        return None
    return proc.stdout.decode("utf-8", "replace")


def _inside(path: str, root: Path) -> bool:
    try:
        real, base = os.path.realpath(path), os.path.realpath(str(root))
    except OSError:
        return True
    return real == base or real.startswith(base + os.sep)


def find_universal_ctags(repo_root: Path) -> Optional[Ctags]:
    """A universal-ctags binary, or ``None``.

    ``$CARTA_CTAGS`` when set, else the usual names on ``PATH``. A candidate
    counts only if ``--list-features`` succeeds and lists ``json``; a binary
    that lives inside the repository being read is never run.
    """
    override = os.environ.get(CTAGS_ENV, "").strip()
    found = [override] if override else [shutil.which(name) for name in CTAGS_NAMES]
    seen = set()
    for candidate in found:
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        candidate = os.path.abspath(candidate)
        if _inside(candidate, repo_root):
            continue
        features = _probe(candidate, "--list-features")
        if features is None or not any(line.split()[:1] == ["json"]
                                       for line in features.splitlines()):
            continue
        version = (_probe(candidate, "--version") or "").splitlines()[:1]
        text = "".join(ch for ch in (version[0] if version else "") if 0x20 <= ord(ch) < 0x7f)
        return Ctags(candidate, text.split(",")[0].strip()[:80] or "universal-ctags")
    return None


def _ctags_files(repo_root: Path, index: PathIndex, settings: GraphSettings
                 ) -> Tuple[List[str], str]:
    """The code files ctags is given, and a hash of their content.

    Regular files only (never a link), under the size limit, with a name
    ctags' line-per-file list can carry.
    """
    accept = definition_filter(index, settings)
    files: List[str] = []
    digest = hashlib.sha256()
    for rel in index.paths():
        if not accept(rel) or not _clean(rel):
            continue
        try:
            data = read_regular_file(repo_root, rel, limits.MAX_CTAGS_FILE_BYTES)
        except SkippedFile:
            continue
        except OSError:
            raise SymbolsFailed(f"a code file could not be read: {rel[:200]}") from None
        if len(files) >= MAX_CTAGS_FILES:
            raise SymbolsFailed(f"more than {MAX_CTAGS_FILES:,} code files; ctags was not run")
        files.append(rel)
        digest.update(rel.encode("utf-8", "surrogateescape") + b"\0"
                      + hashlib.sha256(data).digest())
    return files, digest.hexdigest()


def ctags_command(binary: str, listing: str) -> List[str]:
    """The one way ctags is run: no option files, no links, JSON with line and language."""
    return [binary, "--options=NONE", "--links=no", "--sort=no", "--output-format=json",
            "--fields=+nl", "-f", "-", "-L", listing]


def _kill_tree(proc: "subprocess.Popen") -> None:
    """Kill ``proc`` and, where it leads its own process group, everything in it.

    A configured binary may be a wrapper; a child of it that inherited the
    pipe would otherwise keep the reader waiting after the wrapper is dead.
    """
    if os.name == "posix":
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (OSError, AttributeError):
            pass
    try:
        proc.kill()
    except OSError:
        pass


def run_ctags(repo_root: Path, ctags: Ctags, files: Sequence[str]) -> ParsedSymbols:
    """Run universal-ctags over ``files`` and read the rows it prints.

    The repository's own ctags option files are not read (``--options=NONE``),
    links are not followed, and the run is bounded in time and in output. The
    time limit covers what the binary starts as well: it runs in its own
    process group and the whole group is killed.
    """
    reader = _RowReader("jsonl")
    if not files:
        return reader.result()
    wanted = set(files)
    with tempfile.TemporaryDirectory(prefix="carta-ctags-") as scratch:
        listing = os.path.join(scratch, "files")
        with open(listing, "w", encoding="utf-8", errors="surrogateescape", newline="\n") as fh:
            fh.write("".join(f"./{rel}\n" for rel in files))
        command = ctags_command(ctags.path, listing)
        try:
            # Its own session, so that the time limit can stop whatever it starts.
            proc = subprocess.Popen(command, cwd=str(repo_root), stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                    start_new_session=os.name == "posix")
        except (OSError, ValueError) as exc:
            raise SymbolsFailed(f"could not run ctags: {exc}") from None
        timed_out = threading.Event()

        def stop() -> None:
            timed_out.set()
            _kill_tree(proc)

        timer = threading.Timer(CTAGS_TIMEOUT_SECONDS, stop)
        timer.daemon = True
        timer.start()
        total = 0
        failure = ""
        try:
            assert proc.stdout is not None
            oversized = False
            while True:
                chunk = proc.stdout.readline(MAX_SYMBOL_LINE_BYTES + 1)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_CTAGS_OUTPUT_BYTES:
                    failure = "ctags printed more than the output limit"
                    break
                complete = chunk.endswith(b"\n")
                if oversized or not complete and len(chunk) > MAX_SYMBOL_LINE_BYTES:
                    reader.skipped += 0 if oversized else 1
                    oversized = not complete
                    continue
                reader.feed(chunk)
        except SymbolsFailed as exc:
            failure = str(exc)
        finally:
            timer.cancel()
            if failure:
                _kill_tree(proc)
            if proc.stdout is not None:
                proc.stdout.close()
            code = proc.wait()
    if timed_out.is_set():
        raise SymbolsFailed(f"ctags did not finish in {CTAGS_TIMEOUT_SECONDS} s")
    if failure:
        raise SymbolsFailed(failure)
    if code != 0:
        raise SymbolsFailed(f"ctags exited with status {code}")
    # ctags is told which files to read; a row about any other is not believed.
    kept = [row for row in reader.rows if row.file in wanted]
    return ParsedSymbols(kept, reader.skipped + len(reader.rows) - len(kept), "jsonl")


def symbols_input(repo_root: Path, settings: GraphSettings, index: PathIndex
                  ) -> Tuple[str, Callable[[], Loaded]]:
    """``(input sha, load)`` for the configured provider.

    The sha says what the index would be made from, and is cheap next to
    making it; ``load()`` makes it. Raises ``SymbolsUnavailable`` or
    ``SymbolsFailed`` from either step.
    """
    accept = definition_filter(index, settings)
    if settings.symbols_provider == "file":
        data, input_sha = _file_input(repo_root, settings, index)

        def load_file() -> Loaded:
            parsed = parse_symbols(data)
            require_rows(parsed, f"the symbols file {settings.symbols_file}")
            return Loaded(SymbolTable(parsed.rows, accept), input_sha,
                          f"{settings.symbols_file} ({parsed.format or 'empty'})", parsed.skipped)
        return input_sha, load_file
    if settings.symbols_provider == "ctags":
        ctags = find_universal_ctags(repo_root)
        if ctags is None:
            raise SymbolsUnavailable(
                "no universal-ctags binary was found (the ctags on PATH, if any, cannot "
                "write JSON); install universal-ctags, or point graph.symbols at a file")
        files, content = _ctags_files(repo_root, index, settings)
        input_sha = _sha(f"ctags\0{ctags.version}\0{content}".encode("utf-8"))

        def load_ctags() -> Loaded:
            parsed = run_ctags(repo_root, ctags, files)
            return Loaded(SymbolTable(parsed.rows, accept), input_sha,
                          f"{ctags.version}, {len(files):,} files", parsed.skipped)
        return input_sha, load_ctags
    raise SymbolsUnavailable("graph.symbols.provider is `none`")


# -- the build pass --------------------------------------------------------------

def _blank_report(settings: GraphSettings) -> dict:
    return {"provider": settings.symbols_provider, "state": "off", "detail": "", "rows": 0,
            "rows_dropped": 0, "names": 0, "docs": 0, "edges": 0, "resolved": 0, "ambiguous": 0,
            "unknown": 0, "written": 0, "unchanged": False, "digest": ""}


def _idents_sha(by_doc: Dict[str, List[Edge]]) -> str:
    digest = hashlib.sha256()
    for doc in sorted(by_doc):
        for e in by_doc[doc]:
            if e.kind == "ident" or (e.kind == "mention" and e.dst_type in ("file", "doc", "dir")):
                digest.update(json.dumps([e.kind, e.src, e.dst, e.dst_type, e.line, e.evidence],
                                         ensure_ascii=False,
                                         separators=(",", ":")).encode("utf-8", "replace"))
                digest.update(b"\n")
    return digest.hexdigest()


def _doc_edges(store: EdgeStore) -> Dict[str, List[Edge]]:
    by_doc: Dict[str, List[Edge]] = {doc: [] for doc in store.source_hashes(MENTION_EXTRACTOR)}
    for edge in store.iter_edges(kinds=("ident", "mention"), extractors=(MENTION_EXTRACTOR,)):
        by_doc.setdefault(edge.src, []).append(edge)
    return by_doc


def _stored(store: EdgeStore, report: dict) -> dict:
    """Fill the totals of a report from what the store holds now."""
    metas = store.source_meta(SYMBOL_EXTRACTOR)
    report["docs"] = len(metas)
    report["edges"] = sum(1 for _ in store.iter_edges(extractors=(SYMBOL_EXTRACTOR,)))
    for raw in metas.values():
        try:
            meta = json.loads(raw)
        except ValueError:
            continue
        report["resolved"] += int(meta.get("resolved", 0))
        report["unknown"] += int(meta.get("unknown", 0))
        report["ambiguous"] += len(meta.get("ambiguous", {}))
    report["rows"] = int(store.get_meta(META_ROWS) or 0)
    report["digest"] = store.edges_digest((SYMBOL_EXTRACTOR,)) if report["docs"] else ""
    return report


def _clear(store: EdgeStore, state: str, detail: str) -> None:
    with store.transaction():
        if store.source_meta(SYMBOL_EXTRACTOR):
            store.clear_extractor(SYMBOL_EXTRACTOR)
        for key, value in ((META_STATE, state), (META_DETAIL, detail), (META_INPUT, ""),
                           (META_INDEX, ""), (META_ROWS, "0"), (META_IDENTS, "")):
            if store.get_meta(key) != value:
                store.set_meta(key, value)


def build_symbol_edges(repo_root: Path, settings: GraphSettings, index: PathIndex, *,
                       force: bool = False) -> dict:
    """Bring the store's symbol edges up to date. Run after the mention pass.

    Returns the ``symbols`` part of the build report. ``state`` is ``off`` (no
    provider configured), ``unavailable`` (a provider is configured and there
    is nothing for it to read: no symbol edges), ``failed`` (the index could
    not be loaded: the edges already stored are kept and ``graph_status``
    reports the store stale) or ``ok``.

    The index is loaded outside any transaction: running ctags can take
    seconds, and nothing else should wait on the store for that.
    """
    report = _blank_report(settings)
    with EdgeStore(store_path(repo_root)) as store:
        if settings.symbols_provider == "none":
            if (store.get_meta(META_STATE) not in (None, "off")
                    or store.source_meta(SYMBOL_EXTRACTOR)):
                _clear(store, "off", "")
            return report
        settings_sha = settings.symbol_fingerprint()
        version = str(SYMBOL_EXTRACTOR_VERSION)
        try:
            input_sha, load = symbols_input(repo_root, settings, index)
            by_doc = _doc_edges(store)
            idents_sha = _idents_sha(by_doc)
            if (not force and store.get_meta(META_STATE) == "ok"
                    and store.get_meta(META_INPUT) == input_sha
                    and store.get_meta(META_SETTINGS) == settings_sha
                    and store.get_meta(META_VERSION) == version
                    and store.get_meta(META_IDENTS) == idents_sha):
                report.update(state="ok", unchanged=True,
                              detail=store.get_meta(META_DETAIL) or "")
                return _stored(store, report)
            loaded = load()
        except SymbolsUnavailable as exc:
            _clear(store, "unavailable", str(exc))
            report.update(state="unavailable", detail=str(exc))
            return report
        except SymbolsFailed as exc:
            # Nothing is deleted: a load that failed says nothing about the code.
            with store.transaction():
                store.set_meta(META_STATE, "failed")
                store.set_meta(META_DETAIL, str(exc))
            report.update(state="failed", detail=str(exc))
            return _stored(store, report)

        table = loaded.table
        alive = index.has_file
        written = 0
        with store.transaction():
            hashes = store.source_hashes(MENTION_EXTRACTOR)
            known = store.source_meta(SYMBOL_EXTRACTOR)
            for doc in sorted(by_doc):
                got = resolve_doc(doc, by_doc[doc], table, settings.symbols_max_definitions,
                                  alive)
                meta = canonical_meta({"resolved": got.resolved, "unknown": got.unknown,
                                       "ambiguous": got.ambiguous or {}})
                if (not force and known.get(doc) == meta
                        and store.source_edges(SYMBOL_EXTRACTOR, doc) == got.edges):
                    continue
                store.replace_source(SYMBOL_EXTRACTOR, doc, hashes.get(doc, ""),
                                     SYMBOL_EXTRACTOR_VERSION, got.edges, meta)
                written += 1
            for doc in sorted(set(known) - set(by_doc)):
                store.remove_source(SYMBOL_EXTRACTOR, doc)
                written += 1
            for key, value in ((META_STATE, "ok"), (META_DETAIL, loaded.detail),
                               (META_PROVIDER, settings.symbols_provider),
                               (META_INPUT, input_sha), (META_INDEX, table.sha),
                               (META_ROWS, str(len(table))), (META_SETTINGS, settings_sha),
                               (META_VERSION, version), (META_IDENTS, idents_sha)):
                store.set_meta(key, value)
        report.update(state="ok", detail=loaded.detail, written=written, names=table.names(),
                      rows_dropped=table.dropped + loaded.skipped)
        return _stored(store, report)


def symbol_status(repo_root: Path, settings: GraphSettings, index: PathIndex,
                  store: EdgeStore) -> Tuple[dict, List[str]]:
    """The ``symbols`` part of ``graph_status`` and its stale reasons. Writes nothing.

    Reasons: ``symbols_changed`` (the index, the settings or the identifiers
    are not the ones the stored edges were made from) and ``symbols_failed``
    (the last load failed, or this one would).
    """
    report = _blank_report(settings)
    stored_state = store.get_meta(META_STATE) or "off"
    reasons: List[str] = []
    if settings.symbols_provider == "none":
        if store.source_meta(SYMBOL_EXTRACTOR):
            reasons.append("symbols_changed")
        return _stored(store, report), reasons
    report["detail"] = store.get_meta(META_DETAIL) or ""
    try:
        input_sha, _load = symbols_input(repo_root, settings, index)
    except SymbolsUnavailable as exc:
        report.update(state="unavailable", detail=str(exc))
        if stored_state != "unavailable":
            reasons.append("symbols_changed")
        return _stored(store, report), reasons
    except SymbolsFailed as exc:
        report.update(state="failed", detail=str(exc))
        return _stored(store, report), ["symbols_failed"]
    report["state"] = stored_state
    if stored_state == "failed":
        reasons.append("symbols_failed")
    elif (stored_state != "ok" or store.get_meta(META_INPUT) != input_sha
          or store.get_meta(META_SETTINGS) != settings.symbol_fingerprint()
          or store.get_meta(META_VERSION) != str(SYMBOL_EXTRACTOR_VERSION)
          or store.get_meta(META_IDENTS) != _idents_sha(_doc_edges(store))):
        reasons.append("symbols_changed")
    return _stored(store, report), reasons
