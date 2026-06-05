"""Unit tests for _colpali_path_in_scope — ColPali directory/glob scoping helper.

Tests that:
- empty scopes list → True (no restriction; current behavior preserved)
- trailing-slash directory prefix entries match files inside that directory
- glob patterns (pathlib PurePath.match semantics) match correctly
- out-of-scope paths return False
"""
import pytest
from carta.embed.pipeline import _colpali_path_in_scope


class TestEmptyScopes:
    def test_empty_scopes_returns_true_for_any_path(self):
        """Empty scopes = no restriction — all PDFs get ColPali (current default behavior)."""
        assert _colpali_path_in_scope("docs/reference/chip.pdf", []) is True

    def test_empty_scopes_returns_true_for_root_pdf(self):
        assert _colpali_path_in_scope("chip.pdf", []) is True

    def test_empty_scopes_returns_true_for_deep_nested_path(self):
        assert _colpali_path_in_scope("a/b/c/d/e.pdf", []) is True


class TestDirectoryPrefixScoping:
    """Trailing-slash entries match by directory prefix."""

    def test_exact_dir_prefix_matches(self):
        """A file directly inside the scoped dir is in scope."""
        assert _colpali_path_in_scope(
            "docs/reference/datasheets/part.pdf",
            ["docs/reference/datasheets/"],
        ) is True

    def test_nested_subdir_matches(self):
        """A file in a subdir of the scoped dir is still in scope."""
        assert _colpali_path_in_scope(
            "docs/reference/datasheets/sub/part.pdf",
            ["docs/reference/datasheets/"],
        ) is True

    def test_sibling_dir_does_not_match(self):
        """A file in a sibling directory is NOT in scope."""
        assert _colpali_path_in_scope(
            "docs/reference/other/part.pdf",
            ["docs/reference/datasheets/"],
        ) is False

    def test_root_level_file_not_matched_by_subdir_scope(self):
        assert _colpali_path_in_scope(
            "part.pdf",
            ["docs/reference/datasheets/"],
        ) is False

    def test_partial_dir_name_not_matched(self):
        """'docs/ref/' should NOT match 'docs/reference/chip.pdf'."""
        assert _colpali_path_in_scope(
            "docs/reference/chip.pdf",
            ["docs/ref/"],
        ) is False

    def test_multiple_scopes_first_matches(self):
        """Multiple scopes — first matching scope returns True immediately."""
        assert _colpali_path_in_scope(
            "docs/diagrams/arch.pdf",
            ["docs/reference/datasheets/", "docs/diagrams/"],
        ) is True

    def test_multiple_scopes_second_matches(self):
        """Multiple scopes — second scope catches a path the first missed."""
        assert _colpali_path_in_scope(
            "docs/diagrams/arch.pdf",
            ["docs/reference/", "docs/diagrams/"],
        ) is True

    def test_multiple_scopes_none_match(self):
        assert _colpali_path_in_scope(
            "firmware/manual.pdf",
            ["docs/reference/", "docs/diagrams/"],
        ) is False


class TestGlobPatternScoping:
    """Non-trailing-slash entries use PurePath.match glob semantics."""

    def test_simple_glob_wildcard_matches(self):
        """*.pdf glob matches a PDF anywhere in the relevant dir."""
        assert _colpali_path_in_scope(
            "docs/diagrams/timing.pdf",
            ["docs/diagrams/*.pdf"],
        ) is True

    def test_double_star_glob_matches_nested(self):
        """docs/diagrams/**/*.pdf matches a deeply-nested PDF."""
        assert _colpali_path_in_scope(
            "docs/diagrams/sub/deep/timing.pdf",
            ["docs/diagrams/**/*.pdf"],
        ) is True

    def test_glob_does_not_match_wrong_extension(self):
        assert _colpali_path_in_scope(
            "docs/diagrams/timing.md",
            ["docs/diagrams/*.pdf"],
        ) is False

    def test_glob_does_not_match_different_dir(self):
        assert _colpali_path_in_scope(
            "firmware/timing.pdf",
            ["docs/diagrams/*.pdf"],
        ) is False

    def test_combined_glob_and_dir_prefix_scopes(self):
        """Mix of glob and dir-prefix scopes — either can match."""
        scopes = ["docs/reference/datasheets/", "docs/diagrams/**/*.pdf"]
        # matches via glob
        assert _colpali_path_in_scope("docs/diagrams/sub/arch.pdf", scopes) is True
        # matches via dir prefix
        assert _colpali_path_in_scope("docs/reference/datasheets/chip.pdf", scopes) is True
        # matches neither
        assert _colpali_path_in_scope("firmware/chip.pdf", scopes) is False


class TestEdgeCases:
    def test_path_equal_to_dir_prefix_bare_without_slash(self):
        """Bare dir name without trailing slash is treated as glob, not prefix."""
        # PurePath("docs/ref/a.pdf").match("docs/ref") → False (no wildcard, no match)
        # This tests that we don't accidentally match partial names
        result = _colpali_path_in_scope("docs/ref/a.pdf", ["docs/ref"])
        # Without trailing slash it's a glob pattern — PurePath.match("docs/ref")
        # matches paths whose last component equals "docs/ref", which a .pdf file won't.
        assert result is False

    def test_single_star_does_not_cross_slash(self):
        assert _colpali_path_in_scope("docs/diagrams/sub/timing.pdf", ["docs/diagrams/*.pdf"]) is False

    def test_double_star_matches_direct_child(self):
        assert _colpali_path_in_scope("docs/diagrams/timing.pdf", ["docs/diagrams/**/*.pdf"]) is True

    def test_single_star_glob_in_scope(self):
        """'**' alone in scopes matches everything (degenerate case)."""
        assert _colpali_path_in_scope("anywhere/deep/file.pdf", ["**"]) is True
