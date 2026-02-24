"""Tests for the io-mcp notification webhook system.

Tests cover:
- NotificationEvent creation and defaults
- NotificationChannel event filtering
- NotificationDispatcher cooldown logic
- channels_from_config parsing
- create_dispatcher factory
- Notification sending (with mocked HTTP)
"""

from __future__ import annotations

import json
import time
import unittest.mock as mock

import pytest

from io_mcp.notifications import (
    ALL_EVENT_TYPES,
    NotificationChannel,
    NotificationDispatcher,
    NotificationEvent,
    channels_from_config,
    create_dispatcher,
    _event_emoji,
)


class TestNotificationEvent:
    """Tests for NotificationEvent dataclass."""

    def test_default_values(self):
        e = NotificationEvent(
            event_type="health_warning",
            title="Test",
            message="Agent stuck",
        )
        assert e.event_type == "health_warning"
        assert e.title == "Test"
        assert e.message == "Agent stuck"
        assert e.session_name == ""
        assert e.session_id == ""
        assert e.priority == 3
        assert e.tags == []
        assert e.extra == {}
        assert e.timestamp > 0

    def test_custom_values(self):
        e = NotificationEvent(
            event_type="error",
            title="Error",
            message="Something broke",
            session_name="Agent 1",
            session_id="abc-123",
            priority=5,
            tags=["urgent", "error"],
            extra={"detail": "traceback here"},
        )
        assert e.session_name == "Agent 1"
        assert e.session_id == "abc-123"
        assert e.priority == 5
        assert len(e.tags) == 2
        assert e.extra["detail"] == "traceback here"


class TestNotificationChannel:
    """Tests for NotificationChannel event filtering."""

    def test_accepts_all_events(self):
        ch = NotificationChannel(
            name="test",
            channel_type="webhook",
            url="http://example.com",
            events=["all"],
        )
        assert ch.accepts_event("health_warning") is True
        assert ch.accepts_event("error") is True
        assert ch.accepts_event("anything") is True

    def test_accepts_specific_events(self):
        ch = NotificationChannel(
            name="test",
            channel_type="ntfy",
            url="http://example.com",
            events=["health_warning", "error"],
        )
        assert ch.accepts_event("health_warning") is True
        assert ch.accepts_event("error") is True
        assert ch.accepts_event("agent_connected") is False
        assert ch.accepts_event("health_unresponsive") is False

    def test_default_events_is_all(self):
        ch = NotificationChannel(
            name="test",
            channel_type="webhook",
            url="http://example.com",
        )
        assert ch.accepts_event("anything") is True

    def test_empty_events_rejects_all(self):
        ch = NotificationChannel(
            name="test",
            channel_type="webhook",
            url="http://example.com",
            events=[],
        )
        assert ch.accepts_event("health_warning") is False


class TestNotificationDispatcher:
    """Tests for NotificationDispatcher."""

    def test_disabled_dispatcher_does_nothing(self):
        d = NotificationDispatcher(enabled=False)
        assert d.enabled is False
        # Should not raise
        d.notify(NotificationEvent(
            event_type="health_warning",
            title="Test",
            message="Msg",
        ))

    def test_no_channels_does_nothing(self):
        d = NotificationDispatcher(channels=[], enabled=True)
        assert d.channel_count == 0
        d.notify(NotificationEvent(
            event_type="health_warning",
            title="Test",
            message="Msg",
        ))

    def test_cooldown_prevents_duplicate(self):
        ch = NotificationChannel(
            name="test",
            channel_type="webhook",
            url="http://example.com",
            events=["all"],
        )
        d = NotificationDispatcher(
            channels=[ch],
            cooldown_secs=60.0,
            enabled=True,
        )

        with mock.patch.object(d, '_send') as mock_send:
            event1 = NotificationEvent(
                event_type="health_warning",
                title="Test",
                message="Msg1",
            )
            event2 = NotificationEvent(
                event_type="health_warning",
                title="Test",
                message="Msg2",
            )

            # First call should go through
            d.notify(event1)
            import time as _t
            _t.sleep(0.1)  # let thread start

            # Second identical event type should be cooled down
            d.notify(event2)
            _t.sleep(0.1)

            # Only one send should have happened
            assert mock_send.call_count == 1

    def test_different_event_types_not_cooled(self):
        ch = NotificationChannel(
            name="test",
            channel_type="webhook",
            url="http://example.com",
            events=["all"],
        )
        d = NotificationDispatcher(
            channels=[ch],
            cooldown_secs=60.0,
            enabled=True,
        )

        with mock.patch.object(d, '_send') as mock_send:
            event1 = NotificationEvent(
                event_type="health_warning",
                title="Warning",
                message="Msg1",
            )
            event2 = NotificationEvent(
                event_type="error",
                title="Error",
                message="Msg2",
            )

            d.notify(event1)
            d.notify(event2)
            import time as _t
            _t.sleep(0.2)

            # Both should have been sent (different event types)
            assert mock_send.call_count == 2

    def test_channel_event_filter(self):
        ch = NotificationChannel(
            name="test",
            channel_type="webhook",
            url="http://example.com",
            events=["error"],
        )
        d = NotificationDispatcher(
            channels=[ch],
            cooldown_secs=0,
            enabled=True,
        )

        with mock.patch.object(d, '_send') as mock_send:
            # This should NOT be sent (channel only accepts "error")
            d.notify(NotificationEvent(
                event_type="health_warning",
                title="Warning",
                message="Msg",
            ))
            import time as _t
            _t.sleep(0.1)

            assert mock_send.call_count == 0

            # This SHOULD be sent
            d.notify(NotificationEvent(
                event_type="error",
                title="Error",
                message="Msg",
            ))
            _t.sleep(0.1)

            assert mock_send.call_count == 1

    def test_clear_cooldowns(self):
        ch = NotificationChannel(
            name="test",
            channel_type="webhook",
            url="http://example.com",
            events=["all"],
        )
        d = NotificationDispatcher(
            channels=[ch],
            cooldown_secs=60.0,
            enabled=True,
        )

        with mock.patch.object(d, '_send') as mock_send:
            event = NotificationEvent(
                event_type="health_warning",
                title="Test",
                message="Msg",
            )

            d.notify(event)
            import time as _t
            _t.sleep(0.1)
            assert mock_send.call_count == 1

            # Clear cooldowns, should allow re-send
            d.clear_cooldowns()

            d.notify(event)
            _t.sleep(0.1)
            assert mock_send.call_count == 2

    def test_multiple_channels(self):
        ch1 = NotificationChannel(
            name="ntfy",
            channel_type="ntfy",
            url="http://ntfy.sh/test",
            events=["health_warning"],
        )
        ch2 = NotificationChannel(
            name="slack",
            channel_type="slack",
            url="http://hooks.slack.com/xxx",
            events=["all"],
        )
        d = NotificationDispatcher(
            channels=[ch1, ch2],
            cooldown_secs=0,
            enabled=True,
        )

        with mock.patch.object(d, '_send') as mock_send:
            d.notify(NotificationEvent(
                event_type="health_warning",
                title="Warning",
                message="Msg",
            ))
            import time as _t
            _t.sleep(0.2)

            # Both channels should receive the event
            assert mock_send.call_count == 2


class TestChannelsFromConfig:
    """Tests for channels_from_config parser."""

    def test_empty_list(self):
        assert channels_from_config([]) == []

    def test_valid_ntfy_channel(self):
        channels = channels_from_config([
            {
                "name": "phone",
                "type": "ntfy",
                "url": "https://ntfy.sh/my-topic",
                "priority": 4,
                "events": ["health_warning", "error"],
            }
        ])
        assert len(channels) == 1
        ch = channels[0]
        assert ch.name == "phone"
        assert ch.channel_type == "ntfy"
        assert ch.url == "https://ntfy.sh/my-topic"
        assert ch.priority == 4
        assert ch.accepts_event("health_warning")
        assert ch.accepts_event("error")
        assert not ch.accepts_event("agent_connected")

    def test_valid_slack_channel(self):
        channels = channels_from_config([
            {
                "name": "team",
                "type": "slack",
                "url": "https://hooks.slack.com/services/XXX",
                "events": ["all"],
            }
        ])
        assert len(channels) == 1
        assert channels[0].channel_type == "slack"
        assert channels[0].accepts_event("anything")

    def test_valid_webhook_with_headers(self):
        channels = channels_from_config([
            {
                "name": "custom",
                "type": "webhook",
                "url": "https://example.com/hook",
                "method": "PUT",
                "headers": {"Authorization": "Bearer abc"},
                "events": ["error"],
            }
        ])
        assert len(channels) == 1
        ch = channels[0]
        assert ch.method == "PUT"
        assert ch.headers["Authorization"] == "Bearer abc"

    def test_skips_channel_without_url(self):
        channels = channels_from_config([
            {"name": "bad", "type": "ntfy"},
        ])
        assert len(channels) == 0

    def test_multiple_channels(self):
        channels = channels_from_config([
            {"name": "a", "type": "ntfy", "url": "http://a"},
            {"name": "b", "type": "slack", "url": "http://b"},
            {"name": "c", "type": "discord", "url": "http://c"},
        ])
        assert len(channels) == 3

    def test_defaults_for_missing_fields(self):
        channels = channels_from_config([
            {"name": "minimal", "url": "http://example.com"},
        ])
        assert len(channels) == 1
        ch = channels[0]
        assert ch.channel_type == "webhook"  # default
        assert ch.method == "POST"           # default
        assert ch.priority == 3              # default
        assert ch.events == ["all"]          # default
        assert ch.headers == {}              # default


class TestCreateDispatcher:
    """Tests for create_dispatcher factory."""

    def test_none_config_returns_disabled(self):
        d = create_dispatcher(None)
        assert d.enabled is False
        assert d.channel_count == 0

    def test_config_without_notifications(self):
        """Config with no notifications section → disabled dispatcher."""
        config = mock.MagicMock()
        config.expanded = {"config": {}}
        d = create_dispatcher(config)
        assert d.enabled is False

    def test_config_with_notifications_enabled(self):
        """Config with valid notifications → enabled dispatcher."""
        config = mock.MagicMock()
        config.expanded = {
            "config": {
                "notifications": {
                    "enabled": True,
                    "cooldownSecs": 30,
                    "channels": [
                        {
                            "name": "test",
                            "type": "ntfy",
                            "url": "http://ntfy.sh/test",
                            "events": ["all"],
                        }
                    ],
                }
            }
        }
        d = create_dispatcher(config)
        assert d.enabled is True
        assert d.channel_count == 1

    def test_config_enabled_no_channels(self):
        """Config with enabled=True but no channels → disabled."""
        config = mock.MagicMock()
        config.expanded = {
            "config": {
                "notifications": {
                    "enabled": True,
                    "channels": [],
                }
            }
        }
        d = create_dispatcher(config)
        assert d.enabled is False


class TestEventEmoji:
    """Tests for _event_emoji helper."""

    def test_known_events(self):
        assert _event_emoji("health_warning") == "⚠️"
        assert _event_emoji("health_unresponsive") == "🔴"
        assert _event_emoji("error") == "❌"
        assert _event_emoji("agent_connected") == "🟢"
        assert _event_emoji("agent_disconnected") == "⚪"
        assert _event_emoji("choices_timeout") == "⏰"

    def test_unknown_event(self):
        assert _event_emoji("something_unknown") == "📋"


class TestAllEventTypes:
    """Verify the ALL_EVENT_TYPES constant."""

    def test_contains_expected_types(self):
        expected = {
            "health_warning",
            "health_unresponsive",
            "choices_timeout",
            "agent_connected",
            "agent_disconnected",
            "error",
            "pulse_down",
            "pulse_recovered",
        }
        assert ALL_EVENT_TYPES == expected

    def test_is_frozenset(self):
        assert isinstance(ALL_EVENT_TYPES, frozenset)


class TestNotificationSending:
    """Tests for actual notification sending methods (with mocked HTTP)."""

    def _make_dispatcher(self, channel_type, events=None):
        ch = NotificationChannel(
            name="test",
            channel_type=channel_type,
            url="http://example.com/hook",
            events=events or ["all"],
            priority=3,
            headers={"X-Custom": "header"},
        )
        return NotificationDispatcher(
            channels=[ch],
            cooldown_secs=0,
            enabled=True,
        ), ch

    def _make_event(self):
        return NotificationEvent(
            event_type="health_warning",
            title="Agent Warning",
            message="Agent may be stuck",
            session_name="Agent 1",
            session_id="test-123",
            priority=4,
            tags=["warning"],
        )

    @mock.patch("io_mcp.notifications.urllib.request.urlopen")
    def test_send_ntfy(self, mock_urlopen):
        mock_resp = mock.MagicMock()
        mock_resp.status = 200
        mock_resp.__enter__ = mock.MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = mock.MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        d, ch = self._make_dispatcher("ntfy")
        event = self._make_event()
        d._send(ch, event)

        mock_urlopen.assert_called_once()
        req = mock_urlopen.call_args[0][0]
        assert req.get_method() == "POST"
        assert req.get_header("Title") == "Agent Warning"
        assert req.get_header("Priority") == "4"
        assert req.get_header("Tags") == "warning"
        assert req.get_header("X-custom") == "header"
        assert req.data == b"Agent may be stuck"

    @mock.patch("io_mcp.notifications.urllib.request.urlopen")
    def test_send_slack(self, mock_urlopen):
        mock_resp = mock.MagicMock()
        mock_resp.status = 200
        mock_resp.__enter__ = mock.MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = mock.MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        d, ch = self._make_dispatcher("slack")
        event = self._make_event()
        d._send(ch, event)

        mock_urlopen.assert_called_once()
        req = mock_urlopen.call_args[0][0]
        payload = json.loads(req.data.decode("utf-8"))
        assert "blocks" in payload
        assert "Agent Warning" in payload["text"]
        assert payload["blocks"][0]["type"] == "header"

    @mock.patch("io_mcp.notifications.urllib.request.urlopen")
    def test_send_discord(self, mock_urlopen):
        mock_resp = mock.MagicMock()
        mock_resp.status = 200
        mock_resp.__enter__ = mock.MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = mock.MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        d, ch = self._make_dispatcher("discord")
        event = self._make_event()
        d._send(ch, event)

        mock_urlopen.assert_called_once()
        req = mock_urlopen.call_args[0][0]
        payload = json.loads(req.data.decode("utf-8"))
        assert "embeds" in payload
        embed = payload["embeds"][0]
        assert "Agent Warning" in embed["title"]
        assert embed["footer"]["text"] == "Agent: Agent 1"

    @mock.patch("io_mcp.notifications.urllib.request.urlopen")
    def test_send_generic_webhook(self, mock_urlopen):
        mock_resp = mock.MagicMock()
        mock_resp.status = 200
        mock_resp.__enter__ = mock.MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = mock.MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        d, ch = self._make_dispatcher("webhook")
        event = self._make_event()
        d._send(ch, event)

        mock_urlopen.assert_called_once()
        req = mock_urlopen.call_args[0][0]
        payload = json.loads(req.data.decode("utf-8"))
        assert payload["event_type"] == "health_warning"
        assert payload["title"] == "Agent Warning"
        assert payload["message"] == "Agent may be stuck"
        assert payload["session_name"] == "Agent 1"
        assert payload["session_id"] == "test-123"
        assert payload["priority"] == 4

    @mock.patch("io_mcp.notifications.urllib.request.urlopen")
    def test_send_failure_does_not_raise(self, mock_urlopen):
        """HTTP errors are caught and logged, not raised."""
        mock_urlopen.side_effect = Exception("Connection refused")

        d, ch = self._make_dispatcher("webhook")
        event = self._make_event()
        # Should not raise
        d._send(ch, event)
