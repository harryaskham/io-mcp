"""Tests for the chat view voice button (🎤) TTS feedback and Enter handling.

Verifies that:
- VoiceButton is focusable (can_focus = True)
- VoiceButton posts a Pressed message on Enter key
- The app speaks "Voice input" when the voice button receives focus
- Pressing Enter on the voice button triggers voice recording
- The voice button widget is rendered in the chat input bar
"""

import pytest

from textual.events import DescendantFocus
from textual.widgets import ListView

from io_mcp.tui.app import IoMcpApp
from io_mcp.tui.widgets import ChoiceItem, VoiceButton, EXTRA_OPTIONS, PRIMARY_EXTRAS
from io_mcp.session import Session

from tests.test_tui_pilot import MockTTS, make_app
from tests.test_chat_view import _setup_session_with_choices


# ─── Test: VoiceButton widget properties ──────────────────────────────


def test_voice_button_is_focusable():
    """VoiceButton has can_focus = True so it can receive keyboard focus."""
    assert VoiceButton.can_focus is True


def test_voice_button_pressed_message():
    """VoiceButton.Pressed message stores a reference to the button."""
    btn = VoiceButton("🎤", id="test-btn")
    msg = VoiceButton.Pressed(btn)
    assert msg.voice_button is btn
    assert msg.control is btn


# ─── Test: Voice button renders in chat input bar ─────────────────────


@pytest.mark.asyncio
async def test_voice_button_exists_in_app():
    """The app contains a VoiceButton with id='chat-voice-btn'."""
    app = make_app()
    async with app.run_test() as pilot:
        btn = app.query_one("#chat-voice-btn", VoiceButton)
        assert btn is not None
        assert isinstance(btn, VoiceButton)


@pytest.mark.asyncio
async def test_voice_button_is_in_chat_input_bar():
    """The voice button is inside the #chat-input-bar container."""
    app = make_app()
    async with app.run_test() as pilot:
        btn = app.query_one("#chat-voice-btn", VoiceButton)
        parent = btn.parent
        assert parent is not None
        assert parent.id == "chat-input-bar"


# ─── Test: TTS feedback on focus ──────────────────────────────────────


@pytest.mark.asyncio
async def test_voice_button_focus_speaks_voice_input():
    """Focusing the voice button triggers TTS saying 'Voice input'."""
    app = make_app()
    async with app.run_test() as pilot:
        _setup_session_with_choices(app)

        # Track _speak_ui calls
        spoken = []
        original_speak_ui = app._speak_ui

        def mock_speak_ui(text, **kwargs):
            spoken.append(text)

        app._speak_ui = mock_speak_ui

        # Focus the voice button
        btn = app.query_one("#chat-voice-btn", VoiceButton)
        btn.focus()
        await pilot.pause(0.1)

        # on_descendant_focus should have fired and spoken "Voice input"
        assert "Voice input" in spoken


# ─── Test: Enter triggers voice recording ─────────────────────────────


@pytest.mark.asyncio
async def test_voice_button_enter_triggers_voice_input():
    """Pressing Enter on the voice button triggers action_voice_input."""
    app = make_app()
    async with app.run_test() as pilot:
        _setup_session_with_choices(app)

        # Track action_voice_input calls
        voice_calls = []
        original_action = app.action_voice_input

        def mock_voice_input():
            voice_calls.append(True)

        app.action_voice_input = mock_voice_input

        # Focus the voice button and press Enter
        btn = app.query_one("#chat-voice-btn", VoiceButton)
        btn.focus()
        await pilot.pause(0.1)
        await pilot.press("enter")
        await pilot.pause(0.1)

        # action_voice_input should have been called
        assert len(voice_calls) >= 1


@pytest.mark.asyncio
async def test_voice_button_pressed_message_fires():
    """VoiceButton posts a Pressed message which triggers action_voice_input."""
    app = make_app()
    async with app.run_test() as pilot:
        _setup_session_with_choices(app)

        # Track action_voice_input calls (the handler calls this)
        voice_calls = []

        def mock_voice():
            voice_calls.append(True)

        app.action_voice_input = mock_voice

        btn = app.query_one("#chat-voice-btn", VoiceButton)
        # Post the Pressed message directly to verify the handler routes it
        btn.post_message(VoiceButton.Pressed(btn))
        await pilot.pause(0.2)

        assert len(voice_calls) >= 1


# ─── Test: Voice button click still works ─────────────────────────────


@pytest.mark.asyncio
async def test_voice_button_click_still_triggers_voice_input():
    """Clicking the voice button still triggers voice recording (backward compat)."""
    app = make_app()
    async with app.run_test() as pilot:
        _setup_session_with_choices(app)

        # Enable chat view so click handler is active
        app._chat_view_active = True

        voice_calls = []

        def mock_voice_input():
            voice_calls.append(True)

        app.action_voice_input = mock_voice_input

        # Simulate click by calling on_click with a mock event
        class MockEvent:
            pass

        event = MockEvent()
        btn = app.query_one("#chat-voice-btn", VoiceButton)
        event.widget = btn
        app.on_click(event)

        assert len(voice_calls) == 1


# ─── Test: _on_voice_button_focus method ──────────────────────────────


@pytest.mark.asyncio
async def test_on_voice_button_focus_method():
    """_on_voice_button_focus speaks 'Voice input' via _speak_ui."""
    app = make_app()
    async with app.run_test() as pilot:
        _setup_session_with_choices(app)

        spoken = []

        def mock_speak_ui(text, **kwargs):
            spoken.append(text)

        app._speak_ui = mock_speak_ui

        # Call the method directly
        app._on_voice_button_focus()

        assert "Voice input" in spoken


# ─── Test: on_descendant_focus routing ────────────────────────────────


@pytest.mark.asyncio
async def test_on_descendant_focus_routes_voice_button():
    """on_descendant_focus calls _on_voice_button_focus for the voice button."""
    app = make_app()
    async with app.run_test() as pilot:
        _setup_session_with_choices(app)

        focus_calls = []
        original_focus = app._on_voice_button_focus

        def mock_focus():
            focus_calls.append(True)

        app._on_voice_button_focus = mock_focus

        # Simulate the event
        btn = app.query_one("#chat-voice-btn", VoiceButton)
        event = DescendantFocus(btn)
        app.on_descendant_focus(event)

        assert len(focus_calls) == 1


@pytest.mark.asyncio
async def test_on_descendant_focus_ignores_other_widgets():
    """on_descendant_focus does NOT call _on_voice_button_focus for non-voice widgets."""
    app = make_app()
    async with app.run_test() as pilot:
        _setup_session_with_choices(app)

        focus_calls = []

        def mock_focus():
            focus_calls.append(True)

        app._on_voice_button_focus = mock_focus

        # Simulate focus on a different widget (e.g., chat-input)
        from io_mcp.tui.widgets import SubmitTextArea
        other = app.query_one("#chat-input", SubmitTextArea)
        event = DescendantFocus(other)
        app.on_descendant_focus(event)

        assert len(focus_calls) == 0
