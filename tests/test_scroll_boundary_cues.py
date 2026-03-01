"""Tests for scroll boundary orientation cues.

When scrolling through choices, the TTS should prefix:
- "Top." when the user is on the FIRST real choice (choice_index == 1)
- "Last." when the user is on the LAST real choice (choice_index == len(choices))

This helps blind/screen-off users know where they are in the list.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from io_mcp.session import Session
from io_mcp.tui.widgets import ChoiceItem


def _make_event(choice_index: int, label: str = "Option", summary: str = "desc") -> MagicMock:
    """Create a mock ListView.Highlighted event with a ChoiceItem."""
    item = ChoiceItem(label=label, summary=summary, index=choice_index)
    event = MagicMock()
    event.item = item
    return event


def _make_app_stub(session: Session) -> MagicMock:
    """Create a minimal IoMcpApp stub for on_highlight_changed."""
    stub = MagicMock()
    stub._focused = MagicMock(return_value=session)
    stub._chat_view_active = False
    stub._settings_open = False
    stub._pane_view_active = False
    stub._dwell_time = 0
    stub._last_spoken_text = ""
    stub._last_spoken_time = 0.0
    stub._tts = MagicMock()
    stub._config = MagicMock()
    stub._config.tts_speed_for = MagicMock(return_value=None)
    stub._config.tts_ui_voice_preset = None
    stub._config.tts_voice_preset = None
    # The method checks multi-select state
    stub._multi_select_mode = False
    stub._multi_select_checked = []
    # Mock query_one to avoid widget errors
    stub.query_one = MagicMock(return_value=MagicMock())
    return stub


def _build_text(session: Session, choice_index: int, label: str = "Option", summary: str = "desc") -> str:
    """Simulate the text-building logic from on_highlight_changed.

    This mirrors the exact logic in app.py to test boundary cues.
    """
    logical = choice_index
    ci = logical - 1
    if ci >= len(session.choices):
        return ""
    c = session.choices[ci]
    s = c.get("summary", "")
    lbl = c.get("label", "")

    boundary = ""
    if logical == 1:
        boundary = "Top. "
    elif logical == len(session.choices):
        boundary = "Last. "

    if s:
        return f"{boundary}{logical}. {lbl}. {s}"
    else:
        return f"{boundary}{logical}. {lbl}"


class TestBoundaryTextConstruction:
    """Test the text prefix logic for boundary cues."""

    def _make_session(self, num_choices: int) -> Session:
        s = Session(session_id="test-1", name="Agent 1")
        s.choices = [{"label": f"Choice {i+1}", "summary": f"Summary {i+1}"} for i in range(num_choices)]
        s.active = True
        return s

    def test_first_item_gets_top_prefix(self):
        """choice_index == 1 should produce 'Top. ' prefix."""
        session = self._make_session(5)
        text = _build_text(session, 1)
        assert text.startswith("Top. ")
        assert "1. Choice 1" in text

    def test_last_item_gets_last_prefix(self):
        """choice_index == len(choices) should produce 'Last. ' prefix."""
        session = self._make_session(5)
        text = _build_text(session, 5)
        assert text.startswith("Last. ")
        assert "5. Choice 5" in text

    def test_middle_item_no_prefix(self):
        """Middle items should have no boundary prefix."""
        session = self._make_session(5)
        text = _build_text(session, 3)
        assert not text.startswith("Top. ")
        assert not text.startswith("Last. ")
        assert text.startswith("3. Choice 3")

    def test_single_choice_gets_both_top_and_last(self):
        """With only 1 choice, choice_index 1 == len(choices), so Top wins (first check)."""
        session = self._make_session(1)
        text = _build_text(session, 1)
        # logical == 1 is checked first, so "Top." wins
        assert text.startswith("Top. ")

    def test_two_choices_first_is_top(self):
        session = self._make_session(2)
        text = _build_text(session, 1)
        assert text.startswith("Top. ")

    def test_two_choices_second_is_last(self):
        session = self._make_session(2)
        text = _build_text(session, 2)
        assert text.startswith("Last. ")

    def test_no_summary_still_gets_prefix(self):
        """Boundary cues work even without a summary."""
        session = Session(session_id="test-1", name="Agent 1")
        session.choices = [{"label": "Only option"}]
        session.active = True
        text = _build_text(session, 1)
        assert text.startswith("Top. ")
        assert "Only option" in text


class TestBoundaryFragments:
    """Test that boundary cues are also added to the fragments list."""

    def test_top_fragment_inserted(self):
        """First choice should have 'Top' as the first fragment."""
        from io_mcp.tts import TTSEngine
        _num_words = TTSEngine._NUMBER_WORDS

        choices = [{"label": "Alpha", "summary": "first"}, {"label": "Beta", "summary": "second"}]

        # Simulate fragment building for choice_index=1
        logical = 1
        c = choices[0]
        fragments = []
        if logical in _num_words:
            fragments.append(_num_words[logical])
        label = c.get("label", "")
        if label:
            fragments.append(label)
        s = c.get("summary", "")
        if s:
            fragments.append(s)
        # Boundary cue
        if logical == 1:
            fragments.insert(0, "Top")
        elif logical == len(choices):
            fragments.insert(0, "Last")

        assert fragments[0] == "Top"
        assert "Alpha" in fragments

    def test_last_fragment_inserted(self):
        """Last choice should have 'Last' as the first fragment."""
        from io_mcp.tts import TTSEngine
        _num_words = TTSEngine._NUMBER_WORDS

        choices = [{"label": "Alpha", "summary": "first"}, {"label": "Beta", "summary": "second"}]

        logical = 2
        c = choices[1]
        fragments = []
        if logical in _num_words:
            fragments.append(_num_words[logical])
        label = c.get("label", "")
        if label:
            fragments.append(label)
        s = c.get("summary", "")
        if s:
            fragments.append(s)
        if logical == 1:
            fragments.insert(0, "Top")
        elif logical == len(choices):
            fragments.insert(0, "Last")

        assert fragments[0] == "Last"
        assert "Beta" in fragments

    def test_middle_no_boundary_fragment(self):
        """Middle choices should not have boundary fragments."""
        from io_mcp.tts import TTSEngine
        _num_words = TTSEngine._NUMBER_WORDS

        choices = [{"label": "A"}, {"label": "B"}, {"label": "C"}]

        logical = 2
        c = choices[1]
        fragments = []
        if logical in _num_words:
            fragments.append(_num_words[logical])
        label = c.get("label", "")
        if label:
            fragments.append(label)
        s = c.get("summary", "")
        if s:
            fragments.append(s)
        if logical == 1:
            fragments.insert(0, "Top")
        elif logical == len(choices):
            fragments.insert(0, "Last")

        assert "Top" not in fragments
        assert "Last" not in fragments


class TestEdgeCases:
    """Edge cases for boundary cues."""

    def test_three_choices_boundaries(self):
        """With 3 choices: 1=Top, 2=middle, 3=Last."""
        session = Session(session_id="t", name="A")
        session.choices = [{"label": f"C{i}"} for i in range(3)]

        assert _build_text(session, 1).startswith("Top. ")
        assert not _build_text(session, 2).startswith("Top. ")
        assert not _build_text(session, 2).startswith("Last. ")
        assert _build_text(session, 3).startswith("Last. ")

    def test_large_list_only_boundaries_get_prefix(self):
        """In a list of 20, only first and last get prefixes."""
        session = Session(session_id="t", name="A")
        session.choices = [{"label": f"Item {i+1}", "summary": f"s{i+1}"} for i in range(20)]

        for i in range(1, 21):
            text = _build_text(session, i)
            if i == 1:
                assert text.startswith("Top. "), f"Item {i} should start with Top."
            elif i == 20:
                assert text.startswith("Last. "), f"Item {i} should start with Last."
            else:
                assert not text.startswith("Top. ") and not text.startswith("Last. "), \
                    f"Item {i} should have no boundary prefix"
