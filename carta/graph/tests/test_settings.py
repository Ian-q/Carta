"""graph_settings: defaults, merging, validation and the extraction fingerprint."""
from __future__ import annotations

import copy

import pytest

from carta.config import DEFAULTS, ConfigError
from carta.graph.settings import DEFAULT_SETTINGS, GraphConfigError, graph_settings


def test_settings_from_empty_cfg_equals_defaults():
    assert graph_settings({}) == DEFAULT_SETTINGS
    assert graph_settings(copy.deepcopy(DEFAULTS)) == graph_settings(
        {"excluded_paths": DEFAULTS["excluded_paths"]})
    s = DEFAULT_SETTINGS
    assert s.enabled is True
    assert s.doc_globs == ("**/*.md",)
    assert s.ident_enabled is True and s.ident_min_length == 3
    assert s.impact_top == 10 and s.impact_kinds == ("mention",)
    assert dict(s.impact_weights) == {"mention": 1.0, "symbol": 0.8, "cochange": 0.5}
    assert s.replay_hub_share == 0.2 and s.replay_min_population == 30


def test_settings_are_hashable_and_immutable():
    hash(DEFAULT_SETTINGS)
    with pytest.raises(Exception):
        DEFAULT_SETTINGS.enabled = False  # type: ignore[misc]


def test_user_values_override_and_merge():
    s = graph_settings({"graph": {"impact": {"top": 3}, "doc_globs": ["docs/**/*.md", "*.rst"]}})
    assert s.impact_top == 3
    assert dict(s.impact_weights)["cochange"] == 0.5
    assert s.doc_globs == ("docs/**/*.md", "*.rst")


def test_excluded_paths_come_from_top_level():
    s = graph_settings({"excluded_paths": ["vendor/", "*.tmp"]})
    assert s.excluded_paths == ("vendor/", "*.tmp")


@pytest.mark.parametrize("patterns, needle", [
    ([{"name": "msg", "regex": "("}], "msg"),
    ([{"name": "a", "regex": "x"}, {"name": "a", "regex": "y"}], "a"),
    ([{"name": "has space", "regex": "x"}], "has space"),
    ([{"name": "msg", "regex": "x", "where": "everywhere"}], "msg"),
    ([{"regex": "x"}], "name"),
    ([{"name": "msg"}], "msg"),
    (["not-a-mapping"], "pattern"),
])
def test_pattern_validation(patterns, needle):
    with pytest.raises(GraphConfigError) as err:
        graph_settings({"graph": {"patterns": patterns}})
    assert needle in str(err.value)
    assert isinstance(err.value, ConfigError)


def test_valid_pattern_is_compiled():
    s = graph_settings({"graph": {"patterns": [{"name": "message", "regex": r"\bMSG_[A-Z]+\b"}]}})
    (spec,) = s.patterns
    assert (spec.name, spec.where) == ("message", "all")
    assert spec.compiled.search("see MSG_START here")


@pytest.mark.parametrize("graph", [
    {"doc_globs": "**/*.md"}, {"doc_globs": []}, {"identifiers": {"min_length": "x"}},
    {"impact": {"weights": {"mention": "heavy"}}}, {"patterns": "x"},
])
def test_malformed_values_raise(graph):
    with pytest.raises(GraphConfigError):
        graph_settings({"graph": graph})


def test_fingerprint_changes_with_extraction_inputs_only():
    base = DEFAULT_SETTINGS.fingerprint()
    changed = [
        {"graph": {"doc_globs": ["docs/**/*.md"]}},
        {"graph": {"patterns": [{"name": "m", "regex": "X"}]}},
        {"excluded_paths": ["vendor/"]},
        {"graph": {"identifiers": {"stoplist": ["parse_frame"]}}},
        {"graph": {"identifiers": {"enabled": False}}},
    ]
    for cfg in changed:
        assert graph_settings(cfg).fingerprint() != base, cfg
    same = [
        {"graph": {"impact": {"top": 3}}},
        {"graph": {"replay": {"hub_share": 0.5}}},
        {"graph": {"scan": {"missing_paths": False}}},
        {"graph": {"enabled": False}},
    ]
    for cfg in same:
        assert graph_settings(cfg).fingerprint() == base, cfg


def test_fingerprint_is_stable_across_calls_and_key_order():
    a = graph_settings({"graph": {"identifiers": {"stoplist": ["b_b", "a_a"]}, "doc_globs": ["*.md"]}})
    b = graph_settings({"graph": {"doc_globs": ["*.md"], "identifiers": {"stoplist": ["a_a", "b_b"]}}})
    assert a.fingerprint() == b.fingerprint() == a.fingerprint()
    assert len(a.fingerprint()) == 64


# -- review round 2 ----------------------------------------------------------------

@pytest.mark.parametrize("section, message", [
    ({"enabled": "no"}, "graph.enabled"),
    ({"enabled": 0}, "graph.enabled"),
    ({"identifiers": {"enabled": "false"}}, "graph.identifiers.enabled"),
    ({"scan": {"missing_paths": "off"}}, "graph.scan.missing_paths"),
    ({"impact": {"top": 0}}, "graph.impact.top"),
    ({"impact": {"top": -3}}, "graph.impact.top"),
    ({"impact": {"min_score": float("nan")}}, "graph.impact.min_score"),
    ({"impact": {"min_score": float("inf")}}, "graph.impact.min_score"),
])
def test_booleans_and_impact_limits_are_type_checked(section, message):
    with pytest.raises(GraphConfigError, match=message):
        graph_settings({"graph": section})
