"""Tests for progress bar helper in carta/ui/progress.py."""

import pytest

from carta.ui.progress import Progress


def make_progress(total=10):
    """Create a Progress instance in plain mode."""
    p = Progress(total=total)
    p._tty = False
    return p


class TestBar:
    """Tests for the _bar() method."""

    def test_bar_empty_at_zero(self):
        """Bar should be empty when idx=0, total=10."""
        p = make_progress(total=10)
        p._idx = 0
        bar = p._bar()
        assert bar == "[          ]"
        assert bar.count(" ") == 10

    def test_bar_full_at_total(self):
        """Bar should be full when idx=total."""
        p = make_progress(total=10)
        p._idx = 10
        bar = p._bar()
        assert bar == "[==========]"
        assert bar.count("=") == 10

    def test_bar_halfway(self):
        """Bar should be half-full when idx=5, total=10."""
        p = make_progress(total=10)
        p._idx = 5
        bar = p._bar()
        assert bar == "[====>     ]"

    def test_bar_one_of_ten(self):
        """Bar should show one filled segment when idx=1, total=10."""
        p = make_progress(total=10)
        p._idx = 1
        bar = p._bar()
        assert bar == "[=>        ]"

    def test_bar_unknown_total(self):
        """Bar should return [??] when total=0."""
        p = make_progress(total=0)
        p._idx = 0
        bar = p._bar()
        assert bar == "[??]"

    def test_bar_idx_exceeds_total(self):
        """Bar should cap at total even if idx > total."""
        p = make_progress(total=10)
        p._idx = 15
        bar = p._bar()
        assert bar == "[==========]"
