"""The identifier shape filter.

A backticked token is kept as an (unresolved) identifier only when it is a
syntactic identifier *and* carries code shape: an inner underscore, a case
hump, a qualifier, or call parentheses. That drops prose words, shell flags
and commands on shape alone, with no word list to maintain.

Precision of what passes is unmeasured: the case-hump rule also admits
product and third-party names. Nothing ranks these tokens today; they are
stored for later symbol resolution, which must measure them.
"""
from __future__ import annotations

import re
from typing import Optional, Tuple

_SEP = r"(?:\.|::|->|#)"
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:" + _SEP + r"[A-Za-z_][A-Za-z0-9_]*)*$")
_SPLIT_RE = re.compile(_SEP)
_INNER_UNDERSCORE = re.compile(r"[A-Za-z0-9]_+[A-Za-z0-9]")
_CASE_HUMP = re.compile(r"[a-z][A-Z]")
MAX_LENGTH = 80

# Language keywords, literals and Python builtin functions. A token made only
# of these is not a project identifier however it is punctuated (`len()`).
KEYWORD_STOPLIST = frozenset("""
true false null none nil undefined nan self this super cls and or not is in if else elif for while
do end def class return yield import from as with try except finally raise pass break continue
lambda global nonlocal assert del async await new delete void int float double char bool boolean
string str list dict set tuple object type var let const function func fn struct enum union
static public private protected final abstract interface extends implements package namespace
using include define typedef sizeof switch case default goto throw throws catch then echo exit
len open print range repr sorted sum min max abs any all map filter zip iter next id hash input
isinstance issubclass getattr setattr hasattr callable enumerate reversed round format vars dir
bytes bytearray frozenset complex slice property staticmethod classmethod
""".split())

FILE_EXTENSIONS = frozenset("""
md markdown rst txt adoc py pyi pyc ipynb js mjs cjs jsx ts tsx json jsonl yaml yml toml ini cfg
conf env lock xml html htm css scss less svg png jpg jpeg gif webp ico pdf csv tsv xlsx xls docx
pptx c h cc cpp cxx hpp hh hxx rs go java kt kts swift rb php pl pm lua sh bash zsh fish ps1 bat
cmd sql db sqlite sqlite3 proto thrift graphql gql tf hcl cmake mk make gradle sln csproj cs fs vb
dart ex exs erl hs ml scala clj r jl m mm asm s ld dts dtsi overlay hex bin elf so dll dylib exe
a o obj lib jar war whl egg zip tar gz tgz bz2 xz log tmp bak patch diff map min tpl j2 jinja
vue svelte astro tex bib dbc
""".split())

TLDS = frozenset("com org net io dev ai sh co uk edu gov app".split())

# Hardcoded rather than read from ``builtins`` so the filter gives the same
# answer on every Python version.
BUILTIN_EXCEPTIONS = frozenset("""
BaseException Exception ArithmeticError AssertionError AttributeError BlockingIOError
BrokenPipeError BufferError BytesWarning ChildProcessError ConnectionAbortedError ConnectionError
ConnectionRefusedError ConnectionResetError DeprecationWarning EOFError EncodingWarning
EnvironmentError FileExistsError FileNotFoundError FloatingPointError FutureWarning GeneratorExit
IOError ImportError ImportWarning IndentationError IndexError InterruptedError IsADirectoryError
KeyError KeyboardInterrupt LookupError MemoryError ModuleNotFoundError NameError
NotADirectoryError NotImplementedError OSError OverflowError PendingDeprecationWarning
PermissionError ProcessLookupError RecursionError ReferenceError ResourceWarning RuntimeError
RuntimeWarning StopAsyncIteration StopIteration SyntaxError SyntaxWarning SystemError SystemExit
TabError TimeoutError TypeError UnboundLocalError UnicodeDecodeError UnicodeEncodeError
UnicodeError UnicodeTranslateError UnicodeWarning UserWarning ValueError Warning
ZeroDivisionError ExceptionGroup BaseExceptionGroup
""".split())


def _strip_call(text: str) -> Optional[Tuple[str, bool]]:
    """Remove one trailing balanced ``(...)``. ``None`` when parentheses do not balance."""
    if not text.endswith(")"):
        return (text, False) if "(" not in text else None
    depth = 0
    for i in range(len(text) - 1, -1, -1):
        ch = text[i]
        if ch == ")":
            depth += 1
        elif ch == "(":
            depth -= 1
            if depth == 0:
                head = text[:i]
                if "(" in head or ")" in head:
                    return None
                return head, True
    return None


def parse_identifier(span: str, *, min_length: int = 3,
                     stoplist: frozenset = frozenset()) -> Optional[Tuple[str, bool]]:
    """Return ``(token, written_as_call)`` for a code-shaped identifier, else ``None``."""
    text = span.strip()
    if not text or len(text) > 400:
        return None
    if text[0] in "&*@":
        text = text[1:]
    text = text.rstrip(";,")
    stripped = _strip_call(text)
    if stripped is None:
        return None
    token, was_call = stripped
    if not (min_length <= len(token) <= MAX_LENGTH) or not _IDENT_RE.match(token):
        return None
    parts = _SPLIT_RE.split(token)
    qualified = len(parts) > 1
    has_shape = (
        bool(_INNER_UNDERSCORE.search(token))
        or bool(_CASE_HUMP.search(token))
        or (qualified and all(len(p) >= 2 for p in parts))
        or was_call
    )
    if not has_shape:
        return None
    if token in stoplist or token in BUILTIN_EXCEPTIONS:
        return None
    if all(p.lower() in KEYWORD_STOPLIST or p in stoplist for p in parts):
        return None
    if qualified:
        last = parts[-1].lower()
        if last in FILE_EXTENSIONS or last in TLDS:
            return None
        if all(len(p) < 2 for p in parts):
            return None
    return token, was_call


def identifier_token(span: str, *, min_length: int = 3,
                     stoplist: frozenset = frozenset()) -> Optional[str]:
    """Return the normalised identifier for a backticked span, or ``None``."""
    parsed = parse_identifier(span, min_length=min_length, stoplist=stoplist)
    return parsed[0] if parsed else None
