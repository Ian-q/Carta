"""Validated, immutable settings for the graph arm.

``graph_settings(cfg)`` merges the user's ``graph:`` section over the defaults
itself, so a config dict with no ``graph`` key (a minimal test config, or a
repository with no Carta config at all) works.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from typing import Any, Mapping

from carta.config import DEFAULTS, ConfigError
from carta.graph.limits import (
    MAX_COCHANGE_COMMIT_FILES, MAX_CONFIG_GLOBS, MAX_GLOB_LENGTH, MAX_PATTERN_REGEX_LENGTH,
    MAX_PATTERNS, MAX_SYMBOL_DEFINITIONS_SETTING,
)
from carta.graph.model import MENTION_EXTRACTOR_VERSION, SYMBOL_EXTRACTOR_VERSION
from carta.graph.paths import glob_problem
from carta.graph.limits import (  # noqa: I001
    MAX_IDENT_MIN_LENGTH, MAX_IMPACT_TOP, MAX_IMPACT_WEIGHT, MAX_REPLAY_COMMIT_FILES,
    MAX_REPLAY_WINDOW, MAX_SCAN_HISTORY_LIMIT,
)

_NAME_RE = re.compile(r"^[a-z0-9_-]+$")
_WHERE = ("all", "prose", "code")
SYMBOL_PROVIDERS = ("none", "ctags", "file")


class GraphConfigError(ConfigError):
    """The ``graph:`` section of the config is malformed."""


@dataclass(frozen=True)
class PatternSpec:
    """One project-defined regex pattern."""

    name: str
    regex: str
    where: str
    compiled: "re.Pattern[str]"


COCHANGE_MEASURES = ("confidence", "lift")


@dataclass(frozen=True)
class CochangeSettings:
    """``graph.cochange``: how git history is mined for files that change together."""

    enabled: bool = True
    max_files: int = 30
    min_support: int = 1
    min_score: float = 0.1
    measure: str = "confidence"
    half_life: int = 100
    pending_window: int = 1000
    follow_renames: bool = True

    def fingerprint(self, graph: "GraphSettings", version: int) -> str:
        """sha256 over everything that changes which co-change edges are stored."""
        payload = {
            "cochange": [self.max_files, self.min_support, self.min_score, self.measure,
                         self.half_life, self.pending_window, self.follow_renames],
            "doc_globs": list(graph.doc_globs),
            "excluded_paths": list(graph.excluded_paths),
            "extractor_version": version,
        }
        text = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class GraphSettings:
    enabled: bool
    doc_globs: tuple
    excluded_paths: tuple
    ident_enabled: bool
    ident_min_length: int
    ident_stoplist: frozenset
    patterns: tuple
    impact_top: int
    impact_min_score: float
    impact_kinds: tuple
    impact_weights: tuple            # sorted (kind, weight) pairs; dict() it to look up
    scan_missing_paths: bool
    scan_ignore_docs: tuple
    scan_history_limit: int
    replay_hub_share: float
    replay_max_files: int
    replay_popularity_window: int
    replay_min_population: int
    cochange: CochangeSettings = CochangeSettings()
    impact_combine: str = "norm"
    # The symbol arm (#131). Defaulted so the mention arm's callers need not name them.
    symbols_provider: str = "none"
    symbols_file: str = ""
    symbols_max_definitions: int = 1
    scan_dangling_symbols: bool = False
    scan_ambiguous_symbols: bool = False

    def symbol_fingerprint(self) -> str:
        """sha256 over the inputs that change what the symbol arm emits.

        Kept apart from ``fingerprint``: turning symbols on or off must not
        re-extract a single mention.
        """
        payload = {
            "provider": self.symbols_provider,
            "file": self.symbols_file,
            "max_definitions": self.symbols_max_definitions,
            "excluded_paths": list(self.excluded_paths),
            "doc_globs": list(self.doc_globs),
            "extractor_version": SYMBOL_EXTRACTOR_VERSION,
        }
        text = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def fingerprint(self) -> str:
        """sha256 over exactly the inputs that change what the extractor emits."""
        payload = {
            "doc_globs": list(self.doc_globs),
            "excluded_paths": list(self.excluded_paths),
            "ident": [self.ident_enabled, self.ident_min_length, sorted(self.ident_stoplist)],
            "patterns": [[p.name, p.regex, p.where] for p in self.patterns],
            "extractor_version": MENTION_EXTRACTOR_VERSION,
        }
        text = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _merge(base: Mapping[str, Any], override: Mapping[str, Any]) -> dict:
    out = dict(base)
    for key, value in override.items():
        if isinstance(out.get(key), dict) and isinstance(value, dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def _mapping(value: Any, where: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise GraphConfigError(f"{where} must be a mapping, got {type(value).__name__}")
    return value


def _str_list(value: Any, where: str, *, non_empty: bool = False) -> tuple:
    if not isinstance(value, (list, tuple)) or not all(isinstance(v, str) for v in value):
        raise GraphConfigError(f"{where} must be a list of strings")
    if non_empty and not value:
        raise GraphConfigError(f"{where} must not be empty")
    return tuple(value)


def _glob_list(value: Any, where: str, *, non_empty: bool = False,
               path_globs: bool = True) -> tuple:
    """A bounded list of globs. A glob that can match nothing is a config mistake."""
    globs = _str_list(value, where, non_empty=non_empty)
    if len(globs) > MAX_CONFIG_GLOBS:
        raise GraphConfigError(f"{where} has {len(globs)} entries; at most "
                               f"{MAX_CONFIG_GLOBS} are allowed")
    for glob in globs:
        problem = glob_problem(glob) if path_globs else (
            f"is longer than {MAX_GLOB_LENGTH} characters"
            if len(glob) > MAX_GLOB_LENGTH else None)
        if problem:
            raise GraphConfigError(f"{where}: {glob[:60]!r} {problem}")
    return globs


def _number(value: Any, where: str, kind: type) -> Any:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise GraphConfigError(f"{where} must be a number, got {value!r}")
    if not math.isfinite(value):
        raise GraphConfigError(f"{where} must be a finite number, got {value!r}")
    if kind is int and int(value) != value:
        raise GraphConfigError(f"{where} must be a whole number, got {value!r}")
    return kind(value)


def _within(value: Any, where: str, kind: type, low: Any, high: Any) -> Any:
    """A number in ``low..high``. Every number a config sets is a count, a size or a
    weight somewhere downstream, and a repository's config is not the caller's."""
    number = _number(value, where, kind)
    if not low <= number <= high:
        raise GraphConfigError(f"{where} must be between {low} and {high}, got {number}")
    return number


def _flag(value: Any, where: str) -> bool:
    """A real boolean. ``"no"`` and ``"false"`` are truthy strings, so they are refused."""
    if not isinstance(value, bool):
        raise GraphConfigError(f"{where} must be true or false, got {value!r}")
    return value


def _patterns(value: Any) -> tuple:
    if not isinstance(value, (list, tuple)):
        raise GraphConfigError("graph.patterns must be a list of pattern mappings")
    if len(value) > MAX_PATTERNS:
        raise GraphConfigError(f"graph.patterns has {len(value)} entries; at most "
                               f"{MAX_PATTERNS} are allowed")
    out = []
    seen = set()
    for i, item in enumerate(value):
        if not isinstance(item, dict):
            raise GraphConfigError(f"graph.patterns[{i}]: each pattern must be a mapping")
        name = item.get("name")
        if not isinstance(name, str) or not name:
            raise GraphConfigError(f"graph.patterns[{i}]: a pattern needs a name")
        if not _NAME_RE.match(name):
            raise GraphConfigError(
                f"graph.patterns: pattern name {name!r} must match [a-z0-9_-]+")
        if name in seen:
            raise GraphConfigError(f"graph.patterns: duplicate pattern name {name!r}")
        seen.add(name)
        regex = item.get("regex")
        if not isinstance(regex, str) or not regex:
            raise GraphConfigError(f"graph.patterns: pattern {name!r} needs a regex")
        if len(regex) > MAX_PATTERN_REGEX_LENGTH:
            raise GraphConfigError(
                f"graph.patterns: pattern {name!r} has a regex of {len(regex)} characters; "
                f"at most {MAX_PATTERN_REGEX_LENGTH} are allowed")
        where = item.get("where", "all")
        if where not in _WHERE:
            raise GraphConfigError(
                f"graph.patterns: pattern {name!r} has unknown where {where!r} "
                f"(expected one of {', '.join(_WHERE)})")
        try:
            compiled = re.compile(regex)
        except re.error as exc:
            raise GraphConfigError(
                f"graph.patterns: pattern {name!r} has an invalid regex: {exc}") from exc
        out.append(PatternSpec(name, regex, where, compiled))
    return tuple(out)


def _bounded_int(value: Any, where: str, low: int, high: int) -> int:
    number = _number(value, where, int)
    if not low <= number <= high:
        raise GraphConfigError(f"{where} must be between {low} and {high}, got {number}")
    return number


IMPACT_COMBINE = ("max", "sum", "norm", "rrf")


def _combine(value: Any) -> str:
    if value not in IMPACT_COMBINE:
        raise GraphConfigError(
            f"graph.impact.combine must be one of {', '.join(IMPACT_COMBINE)}, got {value!r}")
    return value


def _cochange(value: Any) -> CochangeSettings:
    c = _mapping(value, "graph.cochange")
    measure = c["measure"]
    if measure not in COCHANGE_MEASURES:
        raise GraphConfigError(
            f"graph.cochange.measure must be one of {', '.join(COCHANGE_MEASURES)}, "
            f"got {measure!r}")
    min_score = _number(c["min_score"], "graph.cochange.min_score", float)
    if min_score < 0.0:
        raise GraphConfigError(f"graph.cochange.min_score must not be negative, got {min_score}")
    return CochangeSettings(
        enabled=_flag(c["enabled"], "graph.cochange.enabled"),
        # Pairs are quadratic in the files of one commit, so the cap is a hard bound.
        max_files=_bounded_int(c["max_files"], "graph.cochange.max_files", 2,
                               MAX_COCHANGE_COMMIT_FILES),
        min_support=_bounded_int(c["min_support"], "graph.cochange.min_support", 1, 1_000_000),
        min_score=min_score,
        measure=measure,
        half_life=_bounded_int(c["half_life"], "graph.cochange.half_life", 0, 10_000_000),
        pending_window=_bounded_int(c["pending_window"], "graph.cochange.pending_window",
                                    0, 10_000_000),
        follow_renames=_flag(c["follow_renames"], "graph.cochange.follow_renames"),
    )


def _symbols_file(value: Any) -> str:
    """A repo-relative path to a symbols file, or ``''``. Never absolute, never upward."""
    if not isinstance(value, str):
        raise GraphConfigError(f"graph.symbols.file must be a string, got {value!r}")
    if len(value) > 4096:               # before any work on it: the strip below re-slices
        raise GraphConfigError(
            "graph.symbols.file must be a path inside the repository, relative to its root")
    text = value.strip().replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    if not text:
        return ""
    if (len(text) > 1024 or text.startswith("/") or (len(text) > 1 and text[1] == ":")
            or any(part in ("", ".", "..") for part in text.split("/"))
            or any(ord(ch) < 0x20 or ord(ch) == 0x7f for ch in text)):
        raise GraphConfigError(
            "graph.symbols.file must be a path inside the repository, relative to its root")
    return text


def graph_settings(cfg: Mapping[str, Any]) -> GraphSettings:
    """Build ``GraphSettings`` from a Carta config dict (any dict; keys are optional)."""
    user = cfg.get("graph") or {}
    g = _merge(DEFAULTS["graph"], _mapping(user, "graph"))
    ident = _mapping(g["identifiers"], "graph.identifiers")
    impact = _mapping(g["impact"], "graph.impact")
    scan = _mapping(g["scan"], "graph.scan")
    replay = _mapping(g["replay"], "graph.replay")
    weights = _mapping(impact["weights"], "graph.impact.weights")
    symbols = _mapping(g["symbols"], "graph.symbols")
    provider = symbols["provider"]
    if provider not in SYMBOL_PROVIDERS:
        raise GraphConfigError(f"graph.symbols.provider must be one of "
                               f"{', '.join(SYMBOL_PROVIDERS)}, got {provider!r}")
    max_defs = _number(symbols["max_definitions"], "graph.symbols.max_definitions", int)
    if not 1 <= max_defs <= MAX_SYMBOL_DEFINITIONS_SETTING:
        raise GraphConfigError(f"graph.symbols.max_definitions must be between 1 and "
                               f"{MAX_SYMBOL_DEFINITIONS_SETTING}, got {max_defs}")
    excluded = cfg.get("excluded_paths", DEFAULTS["excluded_paths"])
    top = _within(impact["top"], "graph.impact.top", int, 1, MAX_IMPACT_TOP)
    # The bulk rule is what keeps one huge commit from being ranked; a value
    # the repository's own config could raise without limit would not be a rule.
    max_files = _within(replay["max_files"], "graph.replay.max_files", int,
                        1, MAX_REPLAY_COMMIT_FILES)
    min_score = _number(impact["min_score"], "graph.impact.min_score", float)
    if not math.isfinite(min_score):
        raise GraphConfigError(f"graph.impact.min_score must be a finite number, got {min_score}")
    return GraphSettings(
        enabled=_flag(g["enabled"], "graph.enabled"),
        doc_globs=_glob_list(g["doc_globs"], "graph.doc_globs", non_empty=True),
        excluded_paths=_glob_list(excluded or [], "excluded_paths", path_globs=False),
        ident_enabled=_flag(ident["enabled"], "graph.identifiers.enabled"),
        ident_min_length=_within(ident["min_length"], "graph.identifiers.min_length", int,
                                 1, MAX_IDENT_MIN_LENGTH),
        ident_stoplist=frozenset(_str_list(ident["stoplist"], "graph.identifiers.stoplist")),
        patterns=_patterns(g["patterns"]),
        impact_top=top,
        impact_min_score=min_score,
        impact_kinds=_str_list(impact["kinds"], "graph.impact.kinds"),
        impact_weights=tuple(sorted(
            (str(k), _within(v, f"graph.impact.weights.{k}", float, 0.0, MAX_IMPACT_WEIGHT))
            for k, v in weights.items())),
        scan_missing_paths=_flag(scan["missing_paths"], "graph.scan.missing_paths"),
        scan_ignore_docs=_glob_list(scan["ignore_docs"], "graph.scan.ignore_docs"),
        scan_history_limit=_within(scan["history_limit"], "graph.scan.history_limit", int,
                                   0, MAX_SCAN_HISTORY_LIMIT),
        replay_hub_share=_within(replay["hub_share"], "graph.replay.hub_share", float, 0.0, 1.0),
        replay_max_files=max_files,
        replay_popularity_window=_within(
            replay["popularity_window"], "graph.replay.popularity_window", int,
            0, MAX_REPLAY_WINDOW),
        replay_min_population=_within(
            replay["min_population"], "graph.replay.min_population", int,
            0, MAX_REPLAY_WINDOW),
        cochange=_cochange(g["cochange"]),
        impact_combine=_combine(impact["combine"]),
        symbols_provider=provider,
        symbols_file=_symbols_file(symbols["file"]),
        symbols_max_definitions=max_defs,
        scan_dangling_symbols=_flag(scan["dangling_symbols"], "graph.scan.dangling_symbols"),
        scan_ambiguous_symbols=_flag(scan["ambiguous_symbols"], "graph.scan.ambiguous_symbols"),
    )


DEFAULT_SETTINGS = graph_settings({})
