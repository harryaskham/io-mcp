"""Tests for selection and undo audio feedback.

Verifies that:
- _do_select calls play_chime("select") after stop()
- action_undo_selection calls play_chime("undo") after stop()
- "select" and "undo" chimes are registered and produce tones
"""

from __future__ import annotations

import time
import unittest.mock as mock

import pytest

from io_mcp.session import Session
from io_mcp.tts import TTSEngine


# ─── Helpers ─────────────────────────────────────────────────────────


class FakeConfig:
    """Minimal config for TTSEngine."""

    def __init__(self, chimes_enabled: bool = True):
        self.tts_model_name = "gpt-4o-mini-tts"
        self.tts_voice = "sage"
        self.tts_voice_preset = "sage"
        self.tts_speed = 1.3
        self.tts_emotion = "friendly"
        self.tts_local_backend = "none"
        self.tts_ui_voice = ""
        self.tts_ui_voice_preset = ""
        self.chimes_enabled = chimes_enabled
        self.tts_style_degree = None

    def tts_speed_for(self, context: str) -> float:
        return self.tts_speed

    @property
    def tts_base_url(self) -> str:
        return ""

    @property
    def tts_api_key(self) -> str:
        return ""

    @property
    def tts_provider(self) -> str:
        return "openai"


def _make_engine(config=None) -> TTSEngine:
    """Create a TTSEngine with mocked binary paths."""
    cfg = config or FakeConfig()
    engine = TTSEngine(config=cfg)
    engine._paplay = "/usr/bin/paplay"
    return engine


# ─── Select chime tests ─────────────────────────────────────────────


class TestSelectChime:
    """Test that the 'select' chime produces a high-pitched ping."""

    def test_select_chime_calls_play_tone(self):
        """play_chime('select') should call play_tone at least once."""
        engine = _make_engine()

        with mock.patch.object(engine, "play_tone") as mock_tone:
            engine.play_chime("select")
            time.sleep(0.3)
            assert mock_tone.call_count >= 1, "select chime did not call play_tone"

    def test_select_chime_uses_high_frequency(self):
        """The select chime should use a high-pitched tone (~1200Hz)."""
        engine = _make_engine()

        with mock.patch.object(engine, "play_tone") as mock_tone:
            engine.play_chime("select")
            time.sleep(0.3)
            assert mock_tone.call_count == 1
            freq = mock_tone.call_args[0][0]  # first positional arg = frequency
            assert freq >= 1000, f"Select chime frequency {freq}Hz is too low; expected >= 1000Hz"

    def test_select_chime_is_short(self):
        """The select chime should be brief (<=80ms duration)."""
        engine = _make_engine()

        with mock.patch.object(engine, "play_tone") as mock_tone:
            engine.play_chime("select")
            time.sleep(0.3)
            assert mock_tone.call_count == 1
            duration = mock_tone.call_args[0][1]  # second positional arg = duration_ms
            assert duration <= 80, f"Select chime duration {duration}ms is too long; expected <= 80ms"


# ─── Undo chime tests ───────────────────────────────────────────────


class TestUndoChime:
    """Test that the 'undo' chime produces a descending two-tone."""

    def test_undo_chime_calls_play_tone(self):
        """play_chime('undo') should call play_tone."""
        engine = _make_engine()

        with mock.patch.object(engine, "play_tone") as mock_tone:
            engine.play_chime("undo")
            time.sleep(0.3)
            assert mock_tone.call_count >= 1, "undo chime did not call play_tone"

    def test_undo_chime_plays_two_tones(self):
        """The undo chime should be a two-tone sequence."""
        engine = _make_engine()

        with mock.patch.object(engine, "play_tone") as mock_tone:
            engine.play_chime("undo")
            time.sleep(0.3)
            assert mock_tone.call_count == 2, f"undo chime played {mock_tone.call_count} tones, expected 2"

    def test_undo_chime_descends(self):
        """The undo chime should descend: first tone higher than second."""
        engine = _make_engine()

        with mock.patch.object(engine, "play_tone") as mock_tone:
            engine.play_chime("undo")
            time.sleep(0.3)
            assert mock_tone.call_count == 2
            first_freq = mock_tone.call_args_list[0][0][0]
            second_freq = mock_tone.call_args_list[1][0][0]
            assert first_freq > second_freq, (
                f"Undo chime should descend: first={first_freq}Hz, second={second_freq}Hz"
            )


# ─── Chime registration tests ───────────────────────────────────────


class TestChimeRegistration:
    """Test that 'select' and 'undo' are valid chime names."""

    REQUIRED_CHIMES = ["select", "undo"]

    def test_required_chimes_produce_tones(self):
        """Each required chime name should call play_tone (not be a no-op)."""
        for style in self.REQUIRED_CHIMES:
            engine = _make_engine()

            with mock.patch.object(engine, "play_tone") as mock_tone:
                engine.play_chime(style)
                time.sleep(0.3)
                assert mock_tone.call_count >= 1, (
                    f"Chime '{style}' is not registered — play_tone was not called"
                )

    def test_unknown_chime_is_noop(self):
        """An unregistered chime name should not call play_tone."""
        engine = _make_engine()

        with mock.patch.object(engine, "play_tone") as mock_tone:
            engine.play_chime("totally_bogus_chime_name")
            time.sleep(0.2)
            mock_tone.assert_not_called()


# ─── Integration: _do_select calls play_chime ────────────────────────


class TestDoSelectCallsChime:
    """Test that _do_select invokes play_chime('select')."""

    def test_do_select_plays_select_chime(self):
        """_do_select should call self._tts.play_chime('select')."""
        from io_mcp.tui.app import IoMcpApp
        from io_mcp.tui.widgets import ChoiceItem
        from io_mcp.session import Session, HistoryEntry

        app = mock.MagicMock(spec=IoMcpApp)
        session = Session(session_id="test-sel", name="Test")
        session.choices = [{"label": "Option A", "summary": "desc"}]
        session.active = True

        app._focused = mock.MagicMock(return_value=session)
        app._dwell_task = None
        app._cancel_dwell = mock.MagicMock()
        app._chat_view_active = False

        # Mock the ListView query
        mock_list = mock.MagicMock()
        mock_list.index = 0
        app.query_one = mock.MagicMock(return_value=mock_list)

        # Mock _get_item_at_display_index to return a ChoiceItem
        item = ChoiceItem(label="Option A", summary="desc", index=1)
        app._get_item_at_display_index = mock.MagicMock(return_value=item)

        app._tts = mock.MagicMock()
        app._config = mock.MagicMock()
        app._config.tts_speed_for = mock.MagicMock(return_value=None)
        app._vibrate = mock.MagicMock()
        app._resolve_selection = mock.MagicMock()
        app._show_waiting = mock.MagicMock()
        app._auto_advance_to_next_choices = mock.MagicMock()
        app._settings_just_closed = False

        # Call _do_select on the real method with our mock
        IoMcpApp._do_select(app)

        # Verify play_chime("select") was called
        app._tts.play_chime.assert_called_with("select")

    def test_do_select_stops_before_chime(self):
        """_do_select should call stop() before play_chime('select')."""
        from io_mcp.tui.app import IoMcpApp
        from io_mcp.tui.widgets import ChoiceItem
        from io_mcp.session import Session

        app = mock.MagicMock(spec=IoMcpApp)
        session = Session(session_id="test-ord", name="Test")
        session.choices = [{"label": "Option B", "summary": "desc"}]
        session.active = True

        app._focused = mock.MagicMock(return_value=session)
        app._dwell_task = None
        app._cancel_dwell = mock.MagicMock()
        app._chat_view_active = False

        mock_list = mock.MagicMock()
        mock_list.index = 0
        app.query_one = mock.MagicMock(return_value=mock_list)

        item = ChoiceItem(label="Option B", summary="desc", index=1)
        app._get_item_at_display_index = mock.MagicMock(return_value=item)

        app._tts = mock.MagicMock()
        app._config = mock.MagicMock()
        app._config.tts_speed_for = mock.MagicMock(return_value=None)
        app._vibrate = mock.MagicMock()
        app._resolve_selection = mock.MagicMock()
        app._show_waiting = mock.MagicMock()
        app._auto_advance_to_next_choices = mock.MagicMock()
        app._settings_just_closed = False

        # Track call order
        call_order = []
        app._tts.stop.side_effect = lambda: call_order.append("stop")
        app._tts.play_chime.side_effect = lambda name: call_order.append(f"chime:{name}")

        IoMcpApp._do_select(app)

        assert "stop" in call_order, "stop() was not called"
        assert "chime:select" in call_order, "play_chime('select') was not called"
        stop_idx = call_order.index("stop")
        chime_idx = call_order.index("chime:select")
        assert stop_idx < chime_idx, (
            f"stop() should be called before play_chime('select'), "
            f"but order was: {call_order}"
        )


# ─── Integration: action_undo_selection calls play_chime ─────────────


class TestUndoCallsChime:
    """Test that action_undo_selection invokes play_chime('undo')."""

    def test_undo_plays_undo_chime(self):
        """action_undo_selection should call play_chime('undo')."""
        from io_mcp.tui.app import IoMcpApp
        from io_mcp.session import Session

        app = mock.MagicMock(spec=IoMcpApp)
        session = Session(session_id="test-undo", name="Test")
        session.choices = []
        session.active = False
        # Set up last_choices so undo has something to revert
        session.last_choices = [{"label": "Prev", "summary": "old"}]
        session.last_preamble = "Pick one"

        app._focused = mock.MagicMock(return_value=session)
        app._in_settings = False
        app._chat_view_active = False
        app._tts = mock.MagicMock()
        app._vibrate = mock.MagicMock()
        app._resolve_selection = mock.MagicMock()
        app._speak_ui = mock.MagicMock()

        IoMcpApp.action_undo_selection(app)

        # Verify play_chime("undo") was called
        app._tts.play_chime.assert_called_with("undo")

    def test_undo_stops_before_chime(self):
        """action_undo_selection should call stop() before play_chime('undo')."""
        from io_mcp.tui.app import IoMcpApp
        from io_mcp.session import Session

        app = mock.MagicMock(spec=IoMcpApp)
        session = Session(session_id="test-undo-ord", name="Test")
        session.choices = []
        session.active = False
        session.last_choices = [{"label": "Prev", "summary": "old"}]
        session.last_preamble = "Pick one"

        app._focused = mock.MagicMock(return_value=session)
        app._in_settings = False
        app._chat_view_active = False
        app._tts = mock.MagicMock()
        app._vibrate = mock.MagicMock()
        app._resolve_selection = mock.MagicMock()
        app._speak_ui = mock.MagicMock()

        call_order = []
        app._tts.stop.side_effect = lambda: call_order.append("stop")
        app._tts.play_chime.side_effect = lambda name: call_order.append(f"chime:{name}")

        IoMcpApp.action_undo_selection(app)

        assert "stop" in call_order, "stop() was not called"
        assert "chime:undo" in call_order, "play_chime('undo') was not called"
        stop_idx = call_order.index("stop")
        chime_idx = call_order.index("chime:undo")
        assert stop_idx < chime_idx, (
            f"stop() should be called before play_chime('undo'), "
            f"but order was: {call_order}"
        )
