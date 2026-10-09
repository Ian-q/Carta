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
from carta.graph.model import MENTION_EXTRACTOR_VERSION

_NAME_RE = re.compile(r"^[a-z0-9_-]+$")
_WHERE = ("all", "prose", "code")


class GraphConfigError(ConfigError):
    """The ``graph:`` section of the config is malformed."""


@dataclass(frozen=True)
class PatternSpec:
    """One project-defined regex pattern."""

    name: str
    regex: str
    where: str
    compiled: "re.Pattern[str]"


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


def _number(value: Any, where: str, kind: type) -> Any:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise GraphConfigError(f"{where} must be a number, got {value!r}")
    if kind is int and int(value) != value:
        raise GraphConfigError(f"{where} must be a whole number, got {value!r}")
    return kind(value)


def _flag(value: Any, where: str) -> bool:
    """A real boolean. ``"no"`` and ``"false"`` are truthy strings, so they are refused."""
    if not isinstance(value, bool):
        raise GraphConfigError(f"{where} must be true or false, got {value!r}")
    return value


def _patterns(value: Any) -> tuple:
    if not isinstance(value, (list, tuple)):
        raise GraphConfigError("graph.patterns must be a list of pattern mappings")
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


def graph_settings(cfg: Mapping[str, Any]) -> GraphSettings:
    """Build ``GraphSettings`` from a Carta config dict (any dict; keys are optional)."""
    user = cfg.get("graph") or {}
    g = _merge(DEFAULTS["graph"], _mapping(user, "graph"))
    ident = _mapping(g["identifiers"], "graph.identifiers")
    impact = _mapping(g["impact"], "graph.impact")
    scan = _mapping(g["scan"], "graph.scan")
    replay = _mapping(g["replay"], "graph.replay")
    weights = _mapping(impact["weights"], "graph.impact.weights")
    excluded = cfg.get("excluded_paths", DEFAULTS["excluded_paths"])
    top = _number(impact["top"], "graph.impact.top", int)
    if top < 1:
        raise GraphConfigError(f"graph.impact.top must be at least 1, got {top}")
    min_score = _number(impact["min_score"], "graph.impact.min_score", float)
    if not math.isfinite(min_score):
        raise GraphConfigError(f"graph.impact.min_score must be a finite number, got {min_score}")
    return GraphSettings(
        enabled=_flag(g["enabled"], "graph.enabled"),
        doc_globs=_str_list(g["doc_globs"], "graph.doc_globs", non_empty=True),
        excluded_paths=_str_list(excluded or [], "excluded_paths"),
        ident_enabled=_flag(ident["enabled"], "graph.identifiers.enabled"),
        ident_min_length=_number(ident["min_length"], "graph.identifiers.min_length", int),
        ident_stoplist=frozenset(_str_list(ident["stoplist"], "graph.identifiers.stoplist")),
        patterns=_patterns(g["patterns"]),
        impact_top=top,
        impact_min_score=min_score,
        impact_kinds=_str_list(impact["kinds"], "graph.impact.kinds"),
        impact_weights=tuple(sorted(
            (str(k), _number(v, f"graph.impact.weights.{k}", float)) for k, v in weights.items())),
        scan_missing_paths=_flag(scan["missing_paths"], "graph.scan.missing_paths"),
        scan_ignore_docs=_str_list(scan["ignore_docs"], "graph.scan.ignore_docs"),
        scan_history_limit=_number(scan["history_limit"], "graph.scan.history_limit", int),
        replay_hub_share=_number(replay["hub_share"], "graph.replay.hub_share", float),
        replay_max_files=_number(replay["max_files"], "graph.replay.max_files", int),
        replay_popularity_window=_number(
            replay["popularity_window"], "graph.replay.popularity_window", int),
        replay_min_population=_number(
            replay["min_population"], "graph.replay.min_population", int),
    )


DEFAULT_SETTINGS = graph_settings({})
