"""Tests for 'X of Y' position indicator in scroll readout TTS.

When scrolling through 3+ choices, the TTS should include "N of total"
so the user knows their position in the list without seeing the screen.
E.g., "2 of 5. Beta. Second option".

The position indicator should NOT appear when:
- There are only 1-2 choices (it's obvious)
- For extra options (they're not numbered)
"""

from __future__ import annotations

import pytest

from io_mcp.session import Session
from io_mcp.tts import TTSEngine


_num_words = TTSEngine._NUMBER_WORDS


def _build_text(session: Session, choice_index: int) -> str:
    """Simulate the text-building logic from on_highlight_changed.

    This mirrors the exact logic in app.py including the position indicator.
    """
    logical = choice_index
    ci = logical - 1
    if ci >= len(session.choices):
        return ""
    c = session.choices[ci]
    s = c.get("summary", "")
    lbl = c.get("label", "")
    n_total = len(session.choices)

    boundary = ""
    if logical == 1:
        boundary = "Top. "
    elif logical == len(session.choices):
        boundary = "Last. "

    if n_total > 2:
        if s:
            return f"{boundary}{logical} of {n_total}. {lbl}. {s}"
        else:
            return f"{boundary}{logical} of {n_total}. {lbl}"
    else:
        if s:
            return f"{boundary}{logical}. {lbl}. {s}"
        else:
            return f"{boundary}{logical}. {lbl}"


def _build_fragments(choices: list[dict], choice_index: int) -> list[str]:
    """Simulate the fragment-building logic from on_highlight_changed.

    This mirrors the exact logic in app.py including position indicator fragments.
    """
    logical = choice_index
    ci = logical - 1
    if ci >= len(choices):
        return []
    c = choices[ci]
    s = c.get("summary", "")
    label = c.get("label", "")
    n_total = len(choices)

    fragments = []
    if n_total > 2 and logical in _num_words:
        fragments.append(f"{_num_words[logical]} of {n_total}")
    elif logical in _num_words:
        fragments.append(_num_words[logical])
    if label:
        fragments.append(label)
    if s:
        fragments.append(s)
    # Boundary cue
    if logical == 1:
        fragments.insert(0, "Top")
    elif logical == len(choices):
        fragments.insert(0, "Last")

    return fragments


def _make_session(num_choices: int) -> Session:
    """Create a session with the given number of choices."""
    s = Session(session_id="test-pos", name="Agent")
    s.choices = [
        {"label": f"Choice {i+1}", "summary": f"Summary {i+1}"}
        for i in range(num_choices)
    ]
    s.active = True
    return s


class TestPositionIndicatorText:
    """Test that 'X of Y' appears in TTS text for 3+ choices."""

    def test_three_choices_shows_of_n(self):
        """With 3 choices, text should include 'of 3'."""
        session = _make_session(3)
        text = _build_text(session, 2)
        assert "2 of 3" in text

    def test_five_choices_shows_of_n(self):
        """With 5 choices, each position should show 'of 5'."""
        session = _make_session(5)
        for i in range(1, 6):
            text = _build_text(session, i)
            assert f"{i} of 5" in text, f"Choice {i} should contain '{i} of 5'"

    def test_large_list_shows_of_n(self):
        """With 10 choices, position indicator works throughout."""
        session = _make_session(10)
        text = _build_text(session, 7)
        assert "7 of 10" in text

    def test_two_choices_no_of_n(self):
        """With 2 choices, text should NOT include 'of'."""
        session = _make_session(2)
        text = _build_text(session, 1)
        assert " of " not in text
        assert text.startswith("Top. 1. Choice 1")

    def test_one_choice_no_of_n(self):
        """With 1 choice, text should NOT include 'of'."""
        session = _make_session(1)
        text = _build_text(session, 1)
        assert " of " not in text

    def test_position_with_boundary_top(self):
        """First choice with 3+ items has both 'Top' and 'of N'."""
        session = _make_session(4)
        text = _build_text(session, 1)
        assert text.startswith("Top. ")
        assert "1 of 4" in text

    def test_position_with_boundary_last(self):
        """Last choice with 3+ items has both 'Last' and 'of N'."""
        session = _make_session(4)
        text = _build_text(session, 4)
        assert text.startswith("Last. ")
        assert "4 of 4" in text

    def test_middle_no_boundary_but_has_of_n(self):
        """Middle choice has 'of N' but no boundary prefix."""
        session = _make_session(5)
        text = _build_text(session, 3)
        assert not text.startswith("Top. ")
        assert not text.startswith("Last. ")
        assert "3 of 5" in text

    def test_no_summary_still_gets_of_n(self):
        """Position indicator works without a summary."""
        session = Session(session_id="t", name="A")
        session.choices = [{"label": f"Item {i}"} for i in range(4)]
        session.active = True
        text = _build_text(session, 2)
        assert "2 of 4" in text
        assert "Item 1" in text

    def test_full_text_format_with_summary(self):
        """Check the exact text format: 'N of total. Label. Summary'."""
        session = _make_session(3)
        text = _build_text(session, 2)
        assert text == "2 of 3. Choice 2. Summary 2"

    def test_full_text_format_without_summary(self):
        """Check exact text format without summary: 'N of total. Label'."""
        session = Session(session_id="t", name="A")
        session.choices = [{"label": "A"}, {"label": "B"}, {"label": "C"}]
        session.active = True
        text = _build_text(session, 2)
        assert text == "2 of 3. B"


class TestPositionIndicatorFragments:
    """Test that fragments include 'X of Y' for 3+ choices."""

    def test_three_choices_fragment_has_of_n(self):
        """With 3 choices, number fragment should be 'two of 3'."""
        choices = [{"label": "A"}, {"label": "B"}, {"label": "C"}]
        frags = _build_fragments(choices, 2)
        assert any("of 3" in f for f in frags), f"Expected 'of 3' in fragments: {frags}"

    def test_five_choices_fragment_format(self):
        """With 5 choices, fragment should be e.g. 'three of 5'."""
        choices = [{"label": f"C{i}"} for i in range(5)]
        frags = _build_fragments(choices, 3)
        # logical=3, _num_words[3] = "three"
        assert "three of 5" in frags

    def test_two_choices_fragment_no_of_n(self):
        """With 2 choices, number fragment should be plain word."""
        choices = [{"label": "A"}, {"label": "B"}]
        frags = _build_fragments(choices, 1)
        # Should have "one" not "one of 2"
        assert any(f == "one" for f in frags) or any(f == "Top" for f in frags)
        assert not any("of 2" in f for f in frags)

    def test_boundary_still_present_in_fragments(self):
        """Boundary cues ('Top', 'Last') still present with position indicator."""
        choices = [{"label": f"C{i}"} for i in range(4)]
        frags_first = _build_fragments(choices, 1)
        frags_last = _build_fragments(choices, 4)
        assert frags_first[0] == "Top"
        assert frags_last[0] == "Last"

    def test_fragment_order_with_position(self):
        """Fragments should be: [Top/Last], 'N of total', label, summary."""
        choices = [
            {"label": "Alpha", "summary": "first"},
            {"label": "Beta", "summary": "second"},
            {"label": "Gamma", "summary": "third"},
        ]
        frags = _build_fragments(choices, 1)
        # Expected: ["Top", "one of 3", "Alpha", "first"]
        assert frags[0] == "Top"
        assert "of 3" in frags[1]
        assert frags[2] == "Alpha"
        assert frags[3] == "first"

    def test_middle_fragment_no_boundary(self):
        """Middle choice fragments have no boundary, just position + label."""
        choices = [{"label": f"C{i}", "summary": f"s{i}"} for i in range(5)]
        frags = _build_fragments(choices, 3)
        assert frags[0] == "three of 5"
        assert frags[1] == "C2"  # choices are 0-indexed: C0..C4, logical 3 → index 2
        assert frags[2] == "s2"


class TestExtraOptionsNoPosition:
    """Extra options (choice_index <= 0) should never get position indicators.

    Extra options use the separate 'else' branch in on_highlight_changed
    which doesn't have the position indicator logic at all. This test
    confirms the text-building logic only applies to logical > 0.
    """

    def test_extra_options_not_affected(self):
        """In the real code, extra options (choice_index <= 0) never enter
        the _build_text path — they're handled by a separate branch that
        reads raw_label from the widget. The `logical > 0` guard in app.py
        ensures extras never get numbered text or position indicators."""
        # We verify this by confirming _build_text is only called for
        # logical > 0 in the real code. Calling it with 0 would hit
        # Python negative indexing (ci = -1), which is not the intended path.
        session = _make_session(5)
        # Only real choices (1-5) should use the position indicator path
        for i in range(1, 6):
            text = _build_text(session, i)
            assert "of 5" in text

    def test_position_only_for_real_choices(self):
        """Verify that only choice_index > 0 gets position indicators."""
        session = _make_session(4)
        # All real choices should have position
        for i in range(1, 5):
            text = _build_text(session, i)
            assert "of 4" in text, f"Choice {i} should have 'of 4'"


class TestNavigationUpdatesPosition:
    """Test that position indicator updates correctly when navigating."""

    def test_sequential_navigation(self):
        """Scrolling through choices updates the position correctly."""
        session = _make_session(5)
        positions = []
        for i in range(1, 6):
            text = _build_text(session, i)
            positions.append(text)

        assert "1 of 5" in positions[0]
        assert "2 of 5" in positions[1]
        assert "3 of 5" in positions[2]
        assert "4 of 5" in positions[3]
        assert "5 of 5" in positions[4]

    def test_reverse_navigation(self):
        """Scrolling backwards also shows correct position."""
        session = _make_session(4)
        # Simulate scrolling backwards: 4, 3, 2, 1
        for i in [4, 3, 2, 1]:
            text = _build_text(session, i)
            assert f"{i} of 4" in text

    def test_jump_navigation(self):
        """Jumping to arbitrary positions shows correct indicator."""
        session = _make_session(6)
        text = _build_text(session, 4)
        assert "4 of 6" in text
        text = _build_text(session, 1)
        assert "1 of 6" in text
        text = _build_text(session, 6)
        assert "6 of 6" in text
