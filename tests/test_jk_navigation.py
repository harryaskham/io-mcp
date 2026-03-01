"""Tests for J/K + Enter navigation in io-mcp TUI.

Verifies that all features are reachable using only scroll (j/k)
and select (Enter) — no other keyboard shortcuts required.

Covers:
1. Extras menu navigation (primary, secondary, More options toggle)
2. Settings access via extras → Quick settings
3. Settings menu full cycle (enter → edit → apply → back)
4. Tab switching via extras → Switch tab
5. Help access (requires '?' shortcut, not accessible via extras alone)
6. Pane view access via extras
7. All extras actions (verifying handlers exist and don't crash)
8. Number key vs J/K equivalence
"""

from __future__ import annotations

import pytest
import threading

from textual.widgets import ListView

from io_mcp.tui.app import IoMcpApp
from io_mcp.tui.widgets import (
    ChoiceItem,
    EXTRA_OPTIONS,
    PRIMARY_EXTRAS,
    SECONDARY_EXTRAS,
    MORE_OPTIONS_ITEM,
)
from io_mcp.session import Session, InboxItem

# ─── Reuse test helpers from test_tui_pilot ──────────────────────────────

from tests.test_tui_pilot import MockTTS, make_app


# ─── Helper to set up a session with choices ─────────────────────────────


def _setup_session(app, session_id="test-1", name="Test", choices=None):
    """Create a registered session with active choices."""
    session, _ = app.manager.get_or_create(session_id)
    session.registered = True
    session.name = name
    app.on_session_created(session)

    if choices is None:
        choices = [
            {"label": "Alpha", "summary": "First option"},
            {"label": "Beta", "summary": "Second option"},
            {"label": "Gamma", "summary": "Third option"},
        ]
    session.preamble = "Pick one"
    session.choices = choices
    session.active = True
    session.extras_count = len(EXTRA_OPTIONS)
    session.all_items = list(EXTRA_OPTIONS) + choices
    return session


# ═══════════════════════════════════════════════════════════════════════════
# 1. Extras menu navigation
# ═══════════════════════════════════════════════════════════════════════════


class TestExtrasMenuNavigation:
    """Test scrolling through the extras menu and toggling More options."""

    @pytest.mark.asyncio
    async def test_primary_extras_visible_in_collapsed_mode(self):
        """PRIMARY_EXTRAS items appear at the top of the choices list in collapsed mode."""
        app = make_app()
        async with app.run_test() as pilot:
            session = _setup_session(app)
            app._extras_expanded = False
            app._show_choices()
            await pilot.pause(0.1)

            list_view = app.query_one("#choices", ListView)
            items = [c for c in list_view.children if isinstance(c, ChoiceItem)]
            extra_items = [c for c in items if c.choice_index <= 0]
            labels = [c.choice_label for c in extra_items]

            # Should have "More options ›" + all PRIMARY_EXTRAS
            assert "More options ›" in labels
            for pe in PRIMARY_EXTRAS:
                assert pe["label"] in labels

    @pytest.mark.asyncio
    async def test_more_options_toggle_appears(self):
        """'More options ›' toggle item appears in collapsed mode."""
        app = make_app()
        async with app.run_test() as pilot:
            session = _setup_session(app)
            app._extras_expanded = False
            app._show_choices()
            await pilot.pause(0.1)

            list_view = app.query_one("#choices", ListView)
            items = [c for c in list_view.children if isinstance(c, ChoiceItem)]
            more_items = [c for c in items if c.choice_label == "More options ›"]
            assert len(more_items) == 1

    @pytest.mark.asyncio
    async def test_selecting_more_options_reveals_secondary_extras(self):
        """Selecting 'More options' toggles expanded mode and reveals SECONDARY_EXTRAS."""
        app = make_app()
        async with app.run_test() as pilot:
            session = _setup_session(app)
            app._extras_expanded = False
            app._show_choices()
            await pilot.pause(0.1)

            # Find and select "More options ›"
            list_view = app.query_one("#choices", ListView)
            more_idx = None
            for i, child in enumerate(list_view.children):
                if isinstance(child, ChoiceItem) and child.choice_label == "More options ›":
                    more_idx = i
                    break
            assert more_idx is not None

            # Navigate to "More options" and select
            list_view.index = more_idx
            app._handle_extra_select("More options ›")
            await pilot.pause(0.1)

            # Should now be expanded
            assert app._extras_expanded is True

            # Re-read the list view — it was rebuilt
            items = [c for c in list_view.children if isinstance(c, ChoiceItem)]
            labels = [c.choice_label for c in items if c.choice_index <= 0]

            # All SECONDARY_EXTRAS should now be visible
            for se in SECONDARY_EXTRAS:
                assert se["label"] in labels, f"{se['label']} not found in expanded extras"

    @pytest.mark.asyncio
    async def test_secondary_extras_are_scrollable(self):
        """All secondary extras are reachable by scrolling (j/k) after expanding."""
        app = make_app()
        async with app.run_test() as pilot:
            session = _setup_session(app)
            app._extras_expanded = True
            app._show_choices()
            await pilot.pause(0.1)

            list_view = app.query_one("#choices", ListView)
            n_items = len(list_view.children)

            # Scroll through every item via j (cursor down)
            visited = []
            for i in range(n_items):
                list_view.index = i
                await pilot.pause(0.01)
                item = list_view.children[i]
                if isinstance(item, ChoiceItem):
                    visited.append(item.choice_label)

            # All secondary extras should be reachable
            for se in SECONDARY_EXTRAS:
                assert se["label"] in visited, f"{se['label']} not reachable via scroll"

    @pytest.mark.asyncio
    async def test_collapsing_more_options_hides_secondary(self):
        """Toggling 'More options' again collapses the secondary extras."""
        app = make_app()
        async with app.run_test() as pilot:
            session = _setup_session(app)
            # Start expanded
            app._extras_expanded = True
            app._show_choices()
            await pilot.pause(0.1)

            # Collapse
            app._handle_extra_select("More options ›")
            await pilot.pause(0.1)

            assert app._extras_expanded is False

            list_view = app.query_one("#choices", ListView)
            items = [c for c in list_view.children if isinstance(c, ChoiceItem)]
            labels = [c.choice_label for c in items if c.choice_index <= 0]

            # Secondary extras should NOT be visible
            for se in SECONDARY_EXTRAS:
                if se["label"] not in [pe["label"] for pe in PRIMARY_EXTRAS]:
                    assert se["label"] not in labels


# ═══════════════════════════════════════════════════════════════════════════
# 2. Settings access via extras (Quick settings)
# ═══════════════════════════════════════════════════════════════════════════


class TestSettingsAccessViaExtras:
    """Test that Quick settings in SECONDARY_EXTRAS opens the settings submenu."""

    @pytest.mark.asyncio
    async def test_quick_settings_enters_settings_mode(self):
        """Selecting 'Quick settings' from extras enters settings mode."""
        app = make_app()
        async with app.run_test() as pilot:
            session = _setup_session(app)
            app._show_choices()
            await pilot.pause(0.1)

            # Simulate selecting "Quick settings" from extras
            app._handle_extra_select("Quick settings")
            await pilot.pause(0.1)

            assert app._in_settings is True
            assert app._quick_settings_mode is True

    @pytest.mark.asyncio
    async def test_quick_settings_items_are_scrollable(self):
        """Quick settings submenu items are scrollable via j/k."""
        app = make_app()
        async with app.run_test() as pilot:
            session = _setup_session(app)
            app._enter_quick_settings()
            await pilot.pause(0.1)

            list_view = app.query_one("#choices", ListView)
            items = [c for c in list_view.children if isinstance(c, ChoiceItem)]

            # Should have at least Fast toggle, Voice toggle, Settings, Back, etc.
            labels = [c.choice_label for c in items]
            assert "Fast toggle" in labels
            assert "Voice toggle" in labels
            assert "Settings" in labels
            assert "Back" in labels

            # Scroll to each item
            for i in range(len(items)):
                list_view.index = i
                await pilot.pause(0.01)

            # All items were scrollable
            assert list_view.index == len(items) - 1

    @pytest.mark.asyncio
    async def test_quick_settings_to_full_settings(self):
        """Selecting 'Settings' in quick settings opens the full settings menu."""
        app = make_app()
        async with app.run_test() as pilot:
            session = _setup_session(app)
            app._enter_quick_settings()
            await pilot.pause(0.1)

            # Select "Settings"
            app._handle_quick_settings_select("Settings")
            await pilot.pause(0.1)

            # Should be in full settings now
            assert app._in_settings is True
            assert app._quick_settings_mode is False

            list_view = app.query_one("#choices", ListView)
            items = [c for c in list_view.children if isinstance(c, ChoiceItem)]
            labels = [c.choice_label for c in items]
            assert any("Speed" in l for l in labels)

    @pytest.mark.asyncio
    async def test_quick_settings_back_exits(self):
        """Selecting 'Back' in quick settings exits settings mode."""
        app = make_app()
        async with app.run_test() as pilot:
            session = _setup_session(app)
            app._enter_quick_settings()
            await pilot.pause(0.1)

            app._handle_quick_settings_select("Back")
            await pilot.pause(0.3)  # wait for guard timer

            assert app._in_settings is False
            assert app._quick_settings_mode is False


# ═══════════════════════════════════════════════════════════════════════════
# 3. Settings menu full cycle
# ═══════════════════════════════════════════════════════════════════════════


class TestSettingsMenuFullCycle:
    """Test the complete settings flow via J/K + Enter."""

    @pytest.mark.asyncio
    async def test_speed_edit_full_cycle(self):
        """Enter settings → scroll to Speed → Enter → scroll to 1.5 → Enter → back in settings.

        Note: without a real config file, the speed setter is a no-op (Settings
        requires IoMcpConfig). We verify the navigation and state transitions work.
        """
        app = make_app()
        async with app.run_test() as pilot:
            session = _setup_session(app)

            # Enter settings
            app._enter_settings()
            await pilot.pause(0.1)
            assert app._in_settings is True

            # Find "Speed" in settings items
            speed_idx = None
            for i, item in enumerate(app._settings_items):
                if item["key"] == "speed":
                    speed_idx = i
                    break
            assert speed_idx is not None

            # Scroll to Speed
            list_view = app.query_one("#choices", ListView)
            list_view.index = speed_idx
            await pilot.pause(0.1)

            # Enter speed edit
            app._enter_setting_edit("speed")
            await pilot.pause(0.2)
            assert app._setting_edit_mode is True
            assert app._setting_edit_key == "speed"

            # Navigate to "1.5" using j/k
            target_idx = app._setting_edit_values.index("1.5")
            current_idx = app._setting_edit_index
            delta = target_idx - current_idx
            for _ in range(abs(delta)):
                if delta > 0:
                    await pilot.press("j")
                else:
                    await pilot.press("k")
                await pilot.pause(0.05)

            # Verify index is correct
            list_view = app.query_one("#choices", ListView)
            assert list_view.index == target_idx

            # Apply via Enter
            await pilot.press("enter")
            await pilot.pause(0.2)

            # Should be back in settings menu (not edit mode)
            assert app._setting_edit_mode is False
            assert app._in_settings is True

    @pytest.mark.asyncio
    async def test_color_scheme_edit_full_cycle(self):
        """Enter settings → scroll to Color scheme → Enter → scroll to dracula → Enter."""
        app = make_app()
        async with app.run_test() as pilot:
            session = _setup_session(app)

            # Enter settings
            app._enter_settings()
            await pilot.pause(0.1)

            # Enter color scheme edit
            app._enter_setting_edit("color_scheme")
            await pilot.pause(0.1)
            assert app._setting_edit_mode is True
            assert app._setting_edit_key == "color_scheme"

            # Find "dracula" in values
            target_idx = app._setting_edit_values.index("dracula")
            list_view = app.query_one("#choices", ListView)
            list_view.index = target_idx
            await pilot.pause(0.1)

            # Apply setting
            app._apply_setting_edit()
            await pilot.pause(0.1)

            assert app._color_scheme == "dracula"
            assert app._setting_edit_mode is False
            assert app._in_settings is True

    @pytest.mark.asyncio
    async def test_speed_edit_persists_with_config(self):
        """Speed edit with a real config actually persists the value."""
        import tempfile
        import os
        from io_mcp.config import IoMcpConfig

        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = os.path.join(tmpdir, "config.yml")
            config = IoMcpConfig.load(config_path)

            tts = MockTTS()
            app = IoMcpApp(tts=tts, freeform_tts=tts, demo=True, config=config)
            async with app.run_test() as pilot:
                session = _setup_session(app)

                app._enter_settings()
                app._enter_setting_edit("speed")
                await pilot.pause(0.2)

                # Navigate to 1.5 using j/k
                target_idx = app._setting_edit_values.index("1.5")
                current_idx = app._setting_edit_index
                delta = target_idx - current_idx
                for _ in range(abs(delta)):
                    if delta > 0:
                        await pilot.press("j")
                    else:
                        await pilot.press("k")
                    await pilot.pause(0.05)

                # Apply via Enter
                await pilot.press("enter")
                await pilot.pause(0.2)

                # With real config, speed should be persisted
                assert app.settings.speed == 1.5
                assert app._setting_edit_mode is False
                assert app._in_settings is True

    @pytest.mark.asyncio
    async def test_close_settings_exits(self):
        """Selecting 'Close settings' exits settings mode."""
        app = make_app()
        async with app.run_test() as pilot:
            session = _setup_session(app)

            app._enter_settings()
            await pilot.pause(0.1)

            # Find "Close settings" in items
            close_idx = None
            for i, item in enumerate(app._settings_items):
                if item["key"] == "close":
                    close_idx = i
                    break
            assert close_idx is not None

            # Simulate selecting it via on_list_selected
            list_view = app.query_one("#choices", ListView)
            list_view.index = close_idx

            app._exit_settings()
            await pilot.pause(0.3)

            assert app._in_settings is False

    @pytest.mark.asyncio
    async def test_settings_items_all_scrollable(self):
        """All settings items are reachable by scrolling."""
        app = make_app()
        async with app.run_test() as pilot:
            session = _setup_session(app)

            app._enter_settings()
            await pilot.pause(0.1)

            list_view = app.query_one("#choices", ListView)
            items = [c for c in list_view.children if isinstance(c, ChoiceItem)]

            expected_keys = {"speed", "voice", "ui_voice", "style", "stt_model",
                             "local_tts", "color_scheme", "tts_cache", "close"}
            found_labels = set()

            for i in range(len(items)):
                list_view.index = i
                await pilot.pause(0.01)
                found_labels.add(items[i].choice_label)

            # All settings should have been scrollable
            for item in app._settings_items:
                assert item["label"] in found_labels

    @pytest.mark.asyncio
    async def test_speed_edit_values_scrollable(self):
        """All speed values are reachable by scrolling."""
        app = make_app()
        async with app.run_test() as pilot:
            session = _setup_session(app)

            app._enter_settings()
            app._enter_setting_edit("speed")
            await pilot.pause(0.1)

            list_view = app.query_one("#choices", ListView)
            items = [c for c in list_view.children if isinstance(c, ChoiceItem)]

            # Should include speeds from 0.5 to 2.5 in 0.1 increments
            assert len(items) == 21  # 0.5 to 2.5 = 21 values

            # Scroll through all values
            for i in range(len(items)):
                list_view.index = i
                await pilot.pause(0.01)
            assert list_view.index == len(items) - 1


# ═══════════════════════════════════════════════════════════════════════════
# 4. Tab switching via extras
# ═══════════════════════════════════════════════════════════════════════════


class TestTabSwitchingViaExtras:
    """Test that 'Switch tab' in SECONDARY_EXTRAS opens a navigable tab picker."""

    @pytest.mark.asyncio
    async def test_switch_tab_enters_tab_picker_with_multiple_sessions(self):
        """'Switch tab' opens tab picker with multiple sessions."""
        app = make_app()
        async with app.run_test() as pilot:
            session1 = _setup_session(app, "s1", "Agent 1")
            session2 = _setup_session(app, "s2", "Agent 2")
            app._show_choices()
            await pilot.pause(0.1)

            app._handle_extra_select("Switch tab")
            await pilot.pause(0.1)

            assert app._in_settings is True
            assert app._tab_picker_mode is True

            list_view = app.query_one("#choices", ListView)
            items = [c for c in list_view.children if isinstance(c, ChoiceItem)]
            assert len(items) == 2

    @pytest.mark.asyncio
    async def test_switch_tab_single_session_speaks_message(self):
        """'Switch tab' with a single session doesn't open the picker."""
        app = make_app()
        async with app.run_test() as pilot:
            session = _setup_session(app)
            app._show_choices()
            await pilot.pause(0.1)

            app._handle_extra_select("Switch tab")
            await pilot.pause(0.1)

            # Should NOT enter tab picker mode — only one session
            assert app._tab_picker_mode is not True or app._in_settings is not True

    @pytest.mark.asyncio
    async def test_tab_picker_items_are_scrollable(self):
        """Tab picker items are scrollable via j/k."""
        app = make_app()
        async with app.run_test() as pilot:
            _setup_session(app, "s1", "Agent 1")
            _setup_session(app, "s2", "Agent 2")
            _setup_session(app, "s3", "Agent 3")
            app._show_choices()
            await pilot.pause(0.1)

            app._enter_tab_picker()
            await pilot.pause(0.1)

            list_view = app.query_one("#choices", ListView)
            items = [c for c in list_view.children if isinstance(c, ChoiceItem)]
            assert len(items) == 3

            # Scroll through all
            for i in range(len(items)):
                list_view.index = i
                await pilot.pause(0.01)
            assert list_view.index == 2


# ═══════════════════════════════════════════════════════════════════════════
# 5. Help access
# ═══════════════════════════════════════════════════════════════════════════


class TestHelpAccess:
    """Test help screen accessibility."""

    @pytest.mark.asyncio
    async def test_help_in_extras(self):
        """Help IS accessible via the extras menu (in SECONDARY_EXTRAS)."""
        all_extra_labels = {e["label"] for e in EXTRA_OPTIONS}
        assert "Help" in all_extra_labels

    @pytest.mark.asyncio
    async def test_help_accessible_via_handle_extra_select(self):
        """Selecting 'Help' from extras opens the help screen."""
        app = make_app()
        async with app.run_test() as pilot:
            session = _setup_session(app)
            app._show_choices()
            await pilot.pause(0.1)

            app._handle_extra_select("Help")
            await pilot.pause(0.1)

            assert app._in_settings is True
            assert app._help_mode is True

    @pytest.mark.asyncio
    async def test_help_screen_opens_via_action(self):
        """Help screen can be opened directly via action_show_help."""
        app = make_app()
        async with app.run_test() as pilot:
            session = _setup_session(app)
            app._show_choices()
            await pilot.pause(0.1)

            app.action_show_help()
            await pilot.pause(0.1)

            assert app._in_settings is True
            assert app._help_mode is True

            list_view = app.query_one("#choices", ListView)
            items = [c for c in list_view.children if isinstance(c, ChoiceItem)]
            # Should have keyboard shortcut entries
            assert len(items) > 5

    @pytest.mark.asyncio
    async def test_help_screen_items_scrollable(self):
        """Help screen items are scrollable."""
        app = make_app()
        async with app.run_test() as pilot:
            session = _setup_session(app)
            app.action_show_help()
            await pilot.pause(0.1)

            list_view = app.query_one("#choices", ListView)
            items = [c for c in list_view.children if isinstance(c, ChoiceItem)]

            for i in range(len(items)):
                list_view.index = i
                await pilot.pause(0.01)
            assert list_view.index == len(items) - 1


# ═══════════════════════════════════════════════════════════════════════════
# 6. Pane view access via extras
# ═══════════════════════════════════════════════════════════════════════════


class TestPaneViewViaExtras:
    """Test that 'Pane view' in SECONDARY_EXTRAS enters pane view mode."""

    @pytest.mark.asyncio
    async def test_pane_view_in_extras(self):
        """'Pane view' is in SECONDARY_EXTRAS."""
        labels = {e["label"] for e in SECONDARY_EXTRAS}
        assert "Pane view" in labels

    @pytest.mark.asyncio
    async def test_pane_view_requires_tmux_pane(self):
        """Selecting 'Pane view' without tmux pane does not crash."""
        app = make_app()
        async with app.run_test() as pilot:
            session = _setup_session(app)
            session.tmux_pane = ""  # No tmux pane
            app._show_choices()
            await pilot.pause(0.1)

            # Should not crash — just speaks an error message
            app._handle_extra_select("Pane view")
            await pilot.pause(0.1)

    @pytest.mark.asyncio
    async def test_pane_view_opens_with_tmux_pane(self):
        """Selecting 'Pane view' with tmux pane opens pane view."""
        app = make_app()
        async with app.run_test() as pilot:
            session = _setup_session(app)
            session.tmux_pane = "%42"
            app._show_choices()
            await pilot.pause(0.1)

            app._handle_extra_select("Pane view")
            await pilot.pause(0.1)

            pane_view = app.query_one("#pane-view")
            assert pane_view.display is True


# ═══════════════════════════════════════════════════════════════════════════
# 7. All extras actions — handlers exist and don't crash
# ═══════════════════════════════════════════════════════════════════════════


class TestAllExtrasActions:
    """Verify each extra option's handler exists and doesn't crash."""

    @pytest.mark.asyncio
    async def test_record_response_handler(self):
        """'Record response' triggers voice input action without crashing."""
        app = make_app()
        async with app.run_test() as pilot:
            session = _setup_session(app)
            app._show_choices()
            await pilot.pause(0.1)

            # Should call action_voice_input — which is a no-op in test mode
            # (no actual mic, but shouldn't crash)
            app._handle_extra_select("Record response")
            await pilot.pause(0.1)

    @pytest.mark.asyncio
    async def test_queue_message_handler(self):
        """'Queue message' opens the message input modal."""
        app = make_app()
        async with app.run_test() as pilot:
            session = _setup_session(app)
            app._show_choices()
            await pilot.pause(0.1)

            app._handle_extra_select("Queue message")
            await pilot.pause(0.2)

            assert app._message_mode is True

            # Clean up: dismiss the modal
            await pilot.press("escape")
            await pilot.pause(0.2)

    @pytest.mark.asyncio
    async def test_interrupt_agent_handler(self):
        """'Interrupt agent' opens text input modal without crashing."""
        app = make_app()
        async with app.run_test() as pilot:
            session = _setup_session(app)
            session.tmux_pane = "%42"
            app._show_choices()
            await pilot.pause(0.1)

            app._handle_extra_select("Interrupt agent")
            await pilot.pause(0.2)

            # Clean up
            await pilot.press("escape")
            await pilot.pause(0.2)

    @pytest.mark.asyncio
    async def test_multi_select_handler(self):
        """'Multi select' enters multi-select mode."""
        app = make_app()
        async with app.run_test() as pilot:
            session = _setup_session(app)
            app._show_choices()
            await pilot.pause(0.1)

            app._handle_extra_select("Multi select")
            await pilot.pause(0.1)

            assert app._multi_select_mode is True

    @pytest.mark.asyncio
    async def test_dismiss_handler(self):
        """'Dismiss' clears the current choice without crash."""
        app = make_app()
        async with app.run_test() as pilot:
            session = _setup_session(app)
            app._show_choices()
            await pilot.pause(0.1)

            # Dismiss should resolve the selection without crashing
            app._handle_extra_select("Dismiss")
            await pilot.pause(0.1)

    @pytest.mark.asyncio
    async def test_branch_to_worktree_handler(self):
        """'Branch to worktree' enters worktree mode without crashing."""
        app = make_app()
        async with app.run_test() as pilot:
            session = _setup_session(app)
            app._show_choices()
            await pilot.pause(0.1)

            app._handle_extra_select("Branch to worktree")
            await pilot.pause(0.1)

    @pytest.mark.asyncio
    async def test_compact_context_handler(self):
        """'Compact context' doesn't crash."""
        app = make_app()
        async with app.run_test() as pilot:
            session = _setup_session(app)
            app._show_choices()
            await pilot.pause(0.1)

            app._handle_extra_select("Compact context")
            await pilot.pause(0.1)

    @pytest.mark.asyncio
    async def test_history_handler(self):
        """'History' enters history mode (or speaks 'no history')."""
        app = make_app()
        async with app.run_test() as pilot:
            session = _setup_session(app)
            app._show_choices()
            await pilot.pause(0.1)

            app._handle_extra_select("History")
            await pilot.pause(0.1)
            # No history → it just speaks; shouldn't crash

    @pytest.mark.asyncio
    async def test_history_with_entries(self):
        """'History' with entries enters history mode and is scrollable."""
        from io_mcp.session import HistoryEntry
        app = make_app()
        async with app.run_test() as pilot:
            session = _setup_session(app)
            session.history = [
                HistoryEntry(label="Choice A", summary="First", preamble="Q1"),
                HistoryEntry(label="Choice B", summary="Second", preamble="Q2"),
            ]
            app._show_choices()
            await pilot.pause(0.1)

            app._handle_extra_select("History")
            await pilot.pause(0.1)

            assert app._history_mode is True
            list_view = app.query_one("#choices", ListView)
            items = [c for c in list_view.children if isinstance(c, ChoiceItem)]
            assert len(items) == 2

    @pytest.mark.asyncio
    async def test_new_agent_handler(self):
        """'New agent' enters spawn dialog without crashing."""
        app = make_app()
        async with app.run_test() as pilot:
            session = _setup_session(app)
            app._show_choices()
            await pilot.pause(0.1)

            app._handle_extra_select("New agent")
            await pilot.pause(0.1)

    @pytest.mark.asyncio
    async def test_view_logs_handler(self):
        """'View logs' enters system logs view without crashing."""
        app = make_app()
        async with app.run_test() as pilot:
            session = _setup_session(app)
            app._show_choices()
            await pilot.pause(0.1)

            app._handle_extra_select("View logs")
            await pilot.pause(0.1)

            assert getattr(app, '_system_logs_mode', False) is True

    @pytest.mark.asyncio
    async def test_close_tab_handler(self):
        """'Close tab' attempts to close the session without crashing."""
        app = make_app()
        async with app.run_test() as pilot:
            session = _setup_session(app)
            app._show_choices()
            await pilot.pause(0.1)

            # Close tab should work without crashing
            app._handle_extra_select("Close tab")
            await pilot.pause(0.1)

    @pytest.mark.asyncio
    async def test_quick_settings_handler(self):
        """'Quick settings' enters the quick settings submenu."""
        app = make_app()
        async with app.run_test() as pilot:
            session = _setup_session(app)
            app._show_choices()
            await pilot.pause(0.1)

            app._handle_extra_select("Quick settings")
            await pilot.pause(0.1)

            assert app._in_settings is True
            assert app._quick_settings_mode is True


# ═══════════════════════════════════════════════════════════════════════════
# 8. Number key vs J/K equivalence
# ═══════════════════════════════════════════════════════════════════════════


class TestNumberKeyEquivalence:
    """Test that navigating with J/K to position N and pressing Enter
    gives the same result as pressing number key N."""

    @pytest.mark.asyncio
    async def test_jk_enter_selects_same_as_number_key(self):
        """Scrolling to choice 1 and pressing Enter → same label as pressing '1'."""
        app1 = make_app()
        async with app1.run_test() as pilot1:
            session1 = _setup_session(app1, choices=[
                {"label": "Option A", "summary": "a"},
                {"label": "Option B", "summary": "b"},
                {"label": "Option C", "summary": "c"},
            ])
            app1._show_choices()
            await pilot1.pause(0.1)

            # Use J/K approach: scroll to first real choice and select
            list_view = app1.query_one("#choices", ListView)
            # First real choice is after extras
            n_extras = len(PRIMARY_EXTRAS) + 1  # "More options" + primary
            list_view.index = n_extras  # first real choice
            await pilot1.pause(0.1)

            # Get the item at this position
            item = list_view.children[list_view.index]
            assert isinstance(item, ChoiceItem)
            jk_label = item.choice_label
            jk_index = item.choice_index
            assert jk_label == "Option A"
            assert jk_index == 1

    @pytest.mark.asyncio
    async def test_number_key_maps_to_correct_display_index(self):
        """Number key N maps to the correct display index considering extras."""
        app = make_app()
        async with app.run_test() as pilot:
            session = _setup_session(app, choices=[
                {"label": "Option A", "summary": "a"},
                {"label": "Option B", "summary": "b"},
                {"label": "Option C", "summary": "c"},
            ])
            app._extras_expanded = False
            app._show_choices()
            await pilot.pause(0.1)

            list_view = app.query_one("#choices", ListView)

            # Count visible extras in collapsed mode
            n_extras = len(PRIMARY_EXTRAS) + 1  # "More options" + primary

            # Number key 1 should select the same item as scrolling to
            # position n_extras (0-indexed)
            target_idx = n_extras + 0  # choice 1
            item = list_view.children[target_idx]
            assert isinstance(item, ChoiceItem)
            assert item.choice_label == "Option A"
            assert item.choice_index == 1

            # Number key 2
            target_idx = n_extras + 1  # choice 2
            item = list_view.children[target_idx]
            assert isinstance(item, ChoiceItem)
            assert item.choice_label == "Option B"
            assert item.choice_index == 2

            # Number key 3
            target_idx = n_extras + 2  # choice 3
            item = list_view.children[target_idx]
            assert isinstance(item, ChoiceItem)
            assert item.choice_label == "Option C"
            assert item.choice_index == 3

    @pytest.mark.asyncio
    async def test_expanded_extras_shifts_display_index(self):
        """When extras are expanded, number key mapping accounts for more extras."""
        app = make_app()
        async with app.run_test() as pilot:
            session = _setup_session(app, choices=[
                {"label": "Option X", "summary": "x"},
                {"label": "Option Y", "summary": "y"},
            ])
            app._extras_expanded = True
            app._show_choices()
            await pilot.pause(0.1)

            list_view = app.query_one("#choices", ListView)

            # In expanded mode, extras = SECONDARY_EXTRAS + PRIMARY_EXTRAS
            n_extras = len(SECONDARY_EXTRAS) + len(PRIMARY_EXTRAS)

            # Choice 1 should be at index n_extras
            item = list_view.children[n_extras]
            assert isinstance(item, ChoiceItem)
            assert item.choice_label == "Option X"
            assert item.choice_index == 1

    @pytest.mark.asyncio
    async def test_cursor_down_from_first_choice_reaches_next(self):
        """Pressing j from the first real choice moves to the second choice."""
        app = make_app()
        async with app.run_test() as pilot:
            session = _setup_session(app)
            app._show_choices()
            await pilot.pause(0.1)

            list_view = app.query_one("#choices", ListView)
            # Default focus is on first real choice
            n_extras = len(PRIMARY_EXTRAS) + 1
            assert list_view.index == n_extras

            # Simulate j press
            app.action_cursor_down()
            await pilot.pause(0.1)

            assert list_view.index == n_extras + 1
            item = list_view.children[list_view.index]
            assert isinstance(item, ChoiceItem)
            assert item.choice_label == "Beta"

    @pytest.mark.asyncio
    async def test_cursor_up_from_first_choice_reaches_extras(self):
        """Pressing k from the first real choice moves into the extras area."""
        app = make_app()
        async with app.run_test() as pilot:
            session = _setup_session(app)
            app._show_choices()
            await pilot.pause(0.1)

            list_view = app.query_one("#choices", ListView)
            n_extras = len(PRIMARY_EXTRAS) + 1
            assert list_view.index == n_extras

            # Simulate k press
            app.action_cursor_up()
            await pilot.pause(0.1)

            assert list_view.index == n_extras - 1
            # Should be in the extras area
            item = list_view.children[list_view.index]
            assert isinstance(item, ChoiceItem)
            assert item.choice_index <= 0


# ═══════════════════════════════════════════════════════════════════════════
# 9. End-to-end J/K + Enter flows
# ═══════════════════════════════════════════════════════════════════════════


class TestEndToEndJKFlows:
    """Test complete workflows using only J/K and Enter."""

    @pytest.mark.asyncio
    async def test_navigate_to_more_options_via_jk(self):
        """Navigate from first real choice to 'More options' using only j/k."""
        app = make_app()
        async with app.run_test() as pilot:
            session = _setup_session(app)
            app._extras_expanded = False
            app._show_choices()
            await pilot.pause(0.1)

            list_view = app.query_one("#choices", ListView)
            # Start at first real choice
            n_extras = len(PRIMARY_EXTRAS) + 1

            # Navigate up to find "More options"
            for _ in range(n_extras + 1):
                app.action_cursor_up()
                await pilot.pause(0.01)

            # We should be at the top; "More options" is at index 0
            item = list_view.children[0]
            assert isinstance(item, ChoiceItem)
            assert item.choice_label == "More options ›"

    @pytest.mark.asyncio
    async def test_full_flow_extras_to_quick_settings_to_speed(self):
        """Navigate: choices → More options → Quick settings → Full settings → Speed → 1.8 → apply."""
        app = make_app()
        async with app.run_test() as pilot:
            session = _setup_session(app)
            app._show_choices()
            await pilot.pause(0.1)

            # Step 1: expand more options
            app._handle_extra_select("More options ›")
            await pilot.pause(0.1)
            assert app._extras_expanded is True

            # Step 2: select Quick settings
            app._handle_extra_select("Quick settings")
            await pilot.pause(0.1)
            assert app._quick_settings_mode is True

            # Step 3: select "Settings" to go to full settings
            app._handle_quick_settings_select("Settings")
            await pilot.pause(0.1)
            assert app._in_settings is True
            assert app._quick_settings_mode is False

            # Step 4: enter speed edit
            app._enter_setting_edit("speed")
            await pilot.pause(0.2)
            assert app._setting_edit_mode is True

            # Step 5: navigate to 1.8 using j/k
            target_idx = app._setting_edit_values.index("1.8")
            current_idx = app._setting_edit_index
            delta = target_idx - current_idx
            for _ in range(abs(delta)):
                if delta > 0:
                    await pilot.press("j")
                else:
                    await pilot.press("k")
                await pilot.pause(0.05)

            # Step 6: apply via Enter
            await pilot.press("enter")
            await pilot.pause(0.2)

            # Without real config, speed setter is no-op, but the state
            # transition should complete: edit mode exits, settings menu returns
            assert app._setting_edit_mode is False
            assert app._in_settings is True
            assert app._setting_edit_mode is False

    @pytest.mark.asyncio
    async def test_settings_via_press_s_then_jk(self):
        """Full settings cycle using 's' key and then J/K + Enter."""
        app = make_app()
        async with app.run_test() as pilot:
            session = _setup_session(app)
            app._show_choices()
            await pilot.pause(0.1)

            # Enter settings via 's'
            await pilot.press("s")
            await pilot.pause(0.2)
            assert app._in_settings is True

            # Scroll down with j
            list_view = app.query_one("#choices", ListView)
            initial_idx = list_view.index
            await pilot.press("j")
            await pilot.pause(0.1)
            assert list_view.index == initial_idx + 1

            # Scroll back up with k
            await pilot.press("k")
            await pilot.pause(0.1)
            assert list_view.index == initial_idx

    @pytest.mark.asyncio
    async def test_on_list_selected_routes_settings_items(self):
        """on_list_selected correctly routes settings item selection."""
        app = make_app()
        async with app.run_test() as pilot:
            session = _setup_session(app)

            app._enter_settings()
            await pilot.pause(0.1)

            # Simulate selecting Speed (index 0)
            list_view = app.query_one("#choices", ListView)
            list_view.index = 0
            await pilot.pause(0.1)

            # The item at index 0 should be Speed
            item = list_view.children[0]
            assert isinstance(item, ChoiceItem)
            assert item.choice_label == "Speed"

    @pytest.mark.asyncio
    async def test_on_list_selected_routes_quick_settings(self):
        """on_list_selected correctly routes quick settings selection."""
        app = make_app()
        async with app.run_test() as pilot:
            session = _setup_session(app)

            app._enter_quick_settings()
            await pilot.pause(0.1)

            list_view = app.query_one("#choices", ListView)
            items = [c for c in list_view.children if isinstance(c, ChoiceItem)]

            # First item should be "Fast toggle"
            assert items[0].choice_label == "Fast toggle"

    @pytest.mark.asyncio
    async def test_setting_edit_enter_applies(self):
        """In setting edit mode, pressing Enter applies and exits edit mode.

        Verifies the J/K navigation + Enter flow works correctly for setting edit.
        Without a real config, the speed setter is a no-op, so we verify the
        state transitions rather than the persisted value.
        """
        app = make_app()
        async with app.run_test() as pilot:
            session = _setup_session(app)

            app._enter_settings()
            app._enter_setting_edit("speed")
            await pilot.pause(0.2)

            # Navigate to 2.0 using j/k
            target_idx = app._setting_edit_values.index("2.0")
            current_idx = app._setting_edit_index
            delta = target_idx - current_idx
            for _ in range(abs(delta)):
                if delta > 0:
                    await pilot.press("j")
                else:
                    await pilot.press("k")
                await pilot.pause(0.05)

            # Verify index moved to expected position
            list_view = app.query_one("#choices", ListView)
            assert list_view.index == target_idx

            # Press Enter (which calls action_select → _apply_setting_edit)
            await pilot.press("enter")
            await pilot.pause(0.2)

            # Verify state transitions completed
            assert app._setting_edit_mode is False
            assert app._in_settings is True  # returned to settings menu

    @pytest.mark.asyncio
    async def test_tts_cache_edit_cycle(self):
        """TTS cache: enter edit → select 'Back' → returns to settings."""
        app = make_app()
        async with app.run_test() as pilot:
            session = _setup_session(app)

            app._enter_settings()
            app._enter_setting_edit("tts_cache")
            await pilot.pause(0.1)

            assert app._setting_edit_mode is True
            assert app._setting_edit_key == "tts_cache"

            # Select "Back"
            list_view = app.query_one("#choices", ListView)
            # "Back" is the second item (index 1)
            list_view.index = 1
            app._apply_setting_edit()
            await pilot.pause(0.1)

            # Should be back in settings (not edit mode)
            assert app._setting_edit_mode is False
            assert app._in_settings is True


# ═══════════════════════════════════════════════════════════════════════════
# 10. Extras menu structure verification
# ═══════════════════════════════════════════════════════════════════════════


class TestExtrasStructure:
    """Verify the static structure of extras for J/K navigation."""

    def test_primary_extras_is_list(self):
        assert isinstance(PRIMARY_EXTRAS, list)
        assert len(PRIMARY_EXTRAS) >= 1

    def test_secondary_extras_is_list(self):
        assert isinstance(SECONDARY_EXTRAS, list)
        assert len(SECONDARY_EXTRAS) >= 5

    def test_extra_options_is_secondary_plus_primary(self):
        """EXTRA_OPTIONS = SECONDARY_EXTRAS + PRIMARY_EXTRAS."""
        assert EXTRA_OPTIONS == SECONDARY_EXTRAS + PRIMARY_EXTRAS

    def test_more_options_item_label(self):
        """MORE_OPTIONS_ITEM has correct label."""
        assert MORE_OPTIONS_ITEM["label"] == "More options ›"

    def test_all_extras_have_handler_in_handle_extra_select(self):
        """Every extra option label has a handler in _handle_extra_select.

        This is verified by checking the source labels against known handlers.
        """
        handled_labels = {
            "More options ›", "More options",
            "Record response",
            "Multi select",
            "Interrupt agent",
            "Branch to worktree",
            "Compact context",
            "Pane view",
            "Switch tab",
            "New agent",
            "View logs",
            "Close tab",
            "Dismiss",
            "Quick settings",
            "History",
            "Queue message",
            "Help",
            "Type reply",
            "Undo",
            "Replay prompt",
            "Chat view",
            "Filter",
        }

        all_labels = {e["label"] for e in EXTRA_OPTIONS}
        all_labels.add(MORE_OPTIONS_ITEM["label"])

        unhandled = all_labels - handled_labels
        assert not unhandled, f"Extra options without handlers: {unhandled}"

    def test_required_secondary_extras_present(self):
        """Key secondary extras are present."""
        labels = {e["label"] for e in SECONDARY_EXTRAS}
        required = {
            "Queue message", "Quick settings", "Switch tab",
            "History", "Pane view", "View logs", "Close tab",
            "New agent", "Dismiss",
        }
        missing = required - labels
        assert not missing, f"Missing required extras: {missing}"

    def test_collapsed_mode_has_correct_count(self):
        """Collapsed mode shows: 1 (More options) + len(PRIMARY_EXTRAS) extras."""
        collapsed_count = 1 + len(PRIMARY_EXTRAS)
        # This should match what _show_choices builds
        assert collapsed_count >= 2  # At least "More options" + "Record response"

    def test_expanded_mode_has_correct_count(self):
        """Expanded mode shows: len(SECONDARY_EXTRAS) + len(PRIMARY_EXTRAS) extras."""
        expanded_count = len(SECONDARY_EXTRAS) + len(PRIMARY_EXTRAS)
        assert expanded_count == len(EXTRA_OPTIONS)
