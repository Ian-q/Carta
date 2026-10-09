"""The identifier shape filter: code-shaped tokens in, prose, flags and commands out."""
from __future__ import annotations

import pytest

from carta.graph.idents import (
    BUILTIN_EXCEPTIONS, FILE_EXTENSIONS, KEYWORD_STOPLIST, TLDS, identifier_token, parse_identifier,
)


@pytest.mark.parametrize("span, expected", [
    ("parse_frame", "parse_frame"),
    ("MAX_RETRIES", "MAX_RETRIES"),
    ("parseFrame", "parseFrame"),
    ("EdgeStore", "EdgeStore"),
    ("store.get_meta", "store.get_meta"),
    ("Parser::next", "Parser::next"),
    ("node->next", "node->next"),
    ("Widget#render", "Widget#render"),
    ("main()", "main"),
    ("init(self, x)", "init"),
    ("run(a, (b + c));", "run"),
    ("&ctx_ptr", "ctx_ptr"),
    ("*buf_len", "buf_len"),
    ("@cached_property", "cached_property"),
    ("CARTA_OLLAMA_KEEP_ALIVE", "CARTA_OLLAMA_KEEP_ALIVE"),
    ("search.graph.enabled", "search.graph.enabled"),
    ("  parse_frame  ", "parse_frame"),
    ("_private_name", "_private_name"),
])
def test_accepts(span, expected):
    assert identifier_token(span) == expected


@pytest.mark.parametrize("span", [
    "git", "pip", "main", "string", "true", "None", "HTTP", "TODO", "--force", "-v",
    "KEY=value", "$HOME", "git commit -m", "v1.2.3", "3.10", "e.g", "i.e", "config.yaml",
    "README.md", "example.com", "ab", "x", "a" * 81, "foo/bar", "<name>", "{placeholder}",
    "self.this", "", "   ", "a.b", "true()", "RuntimeError", "FileNotFoundError", "KeyError",
    "DeprecationWarning", "len()", "open()", "print(x)", "_", "__", "snake_", "9lives_x", "a..b_c", "x_y.", "f(", "f)(",
])
def test_rejects(span):
    assert identifier_token(span) is None


def test_min_length_and_stoplist_are_honoured():
    assert identifier_token("a_b") == "a_b"
    assert identifier_token("a_b", min_length=4) is None
    assert identifier_token("parse_frame", stoplist=frozenset({"parse_frame"})) is None
    assert identifier_token("mod.parse_frame", stoplist=frozenset({"mod", "parse_frame"})) is None
    assert identifier_token("mod.parse_frame", stoplist=frozenset({"mod"})) == "mod.parse_frame"


def test_parse_identifier_reports_call_form():
    assert parse_identifier("main()") == ("main", True)
    assert parse_identifier("parse_frame") == ("parse_frame", False)
    assert parse_identifier("git") is None


@pytest.mark.parametrize("span", ["日本語_name", "(((", "f(a))", "))", "`", "\x00\x01", "a(b)(c)",
                                  "ünïcode_näme", "💥", "(", "()", "x" * 5000])
def test_returns_none_never_raises_on_odd_input(span):
    assert identifier_token(span) is None


def test_constant_tables_are_lowercase_or_exact():
    assert all(w == w.lower() for w in KEYWORD_STOPLIST)
    assert {"yaml", "md", "py"} <= FILE_EXTENSIONS and {"com", "io"} <= TLDS
    assert "ValueError" in BUILTIN_EXCEPTIONS
