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
import threading
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

    @mock.patch("io_mcp.notifications.urllib.request.urlopen")
    def test_send_urllib_error_does_not_raise(self, mock_urlopen):
        """urllib.error.URLError is caught, not raised."""
        import urllib.error
        mock_urlopen.side_effect = urllib.error.URLError("Name or service not known")

        d, ch = self._make_dispatcher("ntfy")
        event = self._make_event()
        # Should not raise
        d._send(ch, event)

    @mock.patch("io_mcp.notifications.urllib.request.urlopen")
    def test_send_http_error_does_not_raise(self, mock_urlopen):
        """urllib.error.HTTPError (e.g. 500) is caught, not raised."""
        import urllib.error
        mock_urlopen.side_effect = urllib.error.HTTPError(
            url="http://example.com", code=500, msg="Internal Server Error",
            hdrs=None, fp=None,
        )

        d, ch = self._make_dispatcher("slack")
        event = self._make_event()
        # Should not raise
        d._send(ch, event)

    @mock.patch("io_mcp.notifications.urllib.request.urlopen")
    def test_send_timeout_error_does_not_raise(self, mock_urlopen):
        """Timeout errors are caught, not raised."""
        import socket
        mock_urlopen.side_effect = socket.timeout("timed out")

        d, ch = self._make_dispatcher("discord")
        event = self._make_event()
        # Should not raise
        d._send(ch, event)

    @mock.patch("io_mcp.notifications.urllib.request.urlopen")
    def test_send_unknown_channel_type_logs_warning(self, mock_urlopen):
        """Unknown channel type should not crash, just log a warning."""
        ch = NotificationChannel(
            name="test",
            channel_type="telegram",  # unknown type
            url="http://example.com",
            events=["all"],
        )
        d = NotificationDispatcher(
            channels=[ch],
            cooldown_secs=0,
            enabled=True,
        )
        # Should not raise
        d._send(ch, self._make_event())
        # Should not have called urlopen since type is unknown
        mock_urlopen.assert_not_called()

    @mock.patch("io_mcp.notifications.urllib.request.urlopen")
    def test_send_ntfy_without_tags(self, mock_urlopen):
        """ntfy request should omit Tags header when event has no tags."""
        mock_resp = mock.MagicMock()
        mock_resp.status = 200
        mock_resp.__enter__ = mock.MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = mock.MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        d, ch = self._make_dispatcher("ntfy")
        event = NotificationEvent(
            event_type="agent_connected",
            title="Connected",
            message="New agent",
            tags=[],  # empty tags
        )
        d._send(ch, event)

        req = mock_urlopen.call_args[0][0]
        # Tags header should not be set when tags list is empty
        assert req.get_header("Tags") is None

    @mock.patch("io_mcp.notifications.urllib.request.urlopen")
    def test_send_ntfy_uses_event_priority_over_channel(self, mock_urlopen):
        """ntfy should use the event priority when set, not the channel default."""
        mock_resp = mock.MagicMock()
        mock_resp.status = 200
        mock_resp.__enter__ = mock.MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = mock.MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        ch = NotificationChannel(
            name="test", channel_type="ntfy",
            url="http://ntfy.sh/topic", priority=2,
        )
        d = NotificationDispatcher(channels=[ch], cooldown_secs=0, enabled=True)

        event = NotificationEvent(
            event_type="error", title="Error", message="Boom", priority=5,
        )
        d._send(ch, event)

        req = mock_urlopen.call_args[0][0]
        assert req.get_header("Priority") == "5"

    @mock.patch("io_mcp.notifications.urllib.request.urlopen")
    def test_send_slack_without_session_name(self, mock_urlopen):
        """Slack payload should not include context block when no session_name."""
        mock_resp = mock.MagicMock()
        mock_resp.status = 200
        mock_resp.__enter__ = mock.MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = mock.MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        d, ch = self._make_dispatcher("slack")
        event = NotificationEvent(
            event_type="error", title="Error", message="Something failed",
            session_name="",  # no session
        )
        d._send(ch, event)

        req = mock_urlopen.call_args[0][0]
        payload = json.loads(req.data.decode("utf-8"))
        # Should only have header + section blocks, no context block
        block_types = [b["type"] for b in payload["blocks"]]
        assert "context" not in block_types

    @mock.patch("io_mcp.notifications.urllib.request.urlopen")
    def test_send_discord_without_session_name(self, mock_urlopen):
        """Discord embed should not include footer when no session_name."""
        mock_resp = mock.MagicMock()
        mock_resp.status = 200
        mock_resp.__enter__ = mock.MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = mock.MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        d, ch = self._make_dispatcher("discord")
        event = NotificationEvent(
            event_type="error", title="Error", message="Boom",
            session_name="",  # no session
        )
        d._send(ch, event)

        req = mock_urlopen.call_args[0][0]
        payload = json.loads(req.data.decode("utf-8"))
        embed = payload["embeds"][0]
        assert "footer" not in embed

    @mock.patch("io_mcp.notifications.urllib.request.urlopen")
    def test_send_discord_has_correct_color_for_event(self, mock_urlopen):
        """Discord embed colors should map correctly to event types."""
        mock_resp = mock.MagicMock()
        mock_resp.status = 200
        mock_resp.__enter__ = mock.MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = mock.MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        d, ch = self._make_dispatcher("discord")

        # Test error → red
        d._send(ch, NotificationEvent(
            event_type="error", title="E", message="M",
        ))
        req = mock_urlopen.call_args[0][0]
        payload = json.loads(req.data.decode("utf-8"))
        assert payload["embeds"][0]["color"] == 0xBF616A

        # Test agent_connected → green
        d._send(ch, NotificationEvent(
            event_type="agent_connected", title="C", message="M",
        ))
        req = mock_urlopen.call_args[0][0]
        payload = json.loads(req.data.decode("utf-8"))
        assert payload["embeds"][0]["color"] == 0xA3BE8C

        # Test unknown event → default blue
        d._send(ch, NotificationEvent(
            event_type="custom_event", title="X", message="M",
        ))
        req = mock_urlopen.call_args[0][0]
        payload = json.loads(req.data.decode("utf-8"))
        assert payload["embeds"][0]["color"] == 0x88C0D0

    @mock.patch("io_mcp.notifications.urllib.request.urlopen")
    def test_send_discord_has_timestamp(self, mock_urlopen):
        """Discord embed should include an ISO8601 timestamp."""
        mock_resp = mock.MagicMock()
        mock_resp.status = 200
        mock_resp.__enter__ = mock.MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = mock.MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        d, ch = self._make_dispatcher("discord")
        event = self._make_event()
        d._send(ch, event)

        req = mock_urlopen.call_args[0][0]
        payload = json.loads(req.data.decode("utf-8"))
        embed = payload["embeds"][0]
        assert "timestamp" in embed
        # ISO8601 format check
        assert embed["timestamp"].endswith("Z")
        assert "T" in embed["timestamp"]

    @mock.patch("io_mcp.notifications.urllib.request.urlopen")
    def test_send_webhook_uses_custom_method(self, mock_urlopen):
        """Generic webhook should use the configured HTTP method."""
        mock_resp = mock.MagicMock()
        mock_resp.status = 200
        mock_resp.__enter__ = mock.MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = mock.MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        ch = NotificationChannel(
            name="test", channel_type="webhook",
            url="http://example.com/hook", method="PUT",
        )
        d = NotificationDispatcher(channels=[ch], cooldown_secs=0, enabled=True)
        d._send(ch, self._make_event())

        req = mock_urlopen.call_args[0][0]
        assert req.get_method() == "PUT"

    @mock.patch("io_mcp.notifications.urllib.request.urlopen")
    def test_send_webhook_includes_all_event_fields(self, mock_urlopen):
        """Generic webhook JSON payload should contain all NotificationEvent fields."""
        mock_resp = mock.MagicMock()
        mock_resp.status = 200
        mock_resp.__enter__ = mock.MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = mock.MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        d, ch = self._make_dispatcher("webhook")
        event = NotificationEvent(
            event_type="error", title="Title", message="Msg",
            session_name="S", session_id="sid-1",
            priority=5, tags=["a", "b"],
            extra={"key": "value"},
        )
        d._send(ch, event)

        req = mock_urlopen.call_args[0][0]
        payload = json.loads(req.data.decode("utf-8"))
        assert payload["event_type"] == "error"
        assert payload["title"] == "Title"
        assert payload["message"] == "Msg"
        assert payload["session_name"] == "S"
        assert payload["session_id"] == "sid-1"
        assert payload["priority"] == 5
        assert payload["tags"] == ["a", "b"]
        assert payload["extra"] == {"key": "value"}
        assert "timestamp" in payload
        assert isinstance(payload["timestamp"], float)

    @mock.patch("io_mcp.notifications.urllib.request.urlopen")
    def test_send_empty_message(self, mock_urlopen):
        """Sending an event with an empty message should not crash."""
        mock_resp = mock.MagicMock()
        mock_resp.status = 200
        mock_resp.__enter__ = mock.MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = mock.MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        for ch_type in ("ntfy", "slack", "discord", "webhook"):
            mock_urlopen.reset_mock()
            d, ch = self._make_dispatcher(ch_type)
            event = NotificationEvent(
                event_type="error", title="T", message="",
            )
            d._send(ch, event)
            mock_urlopen.assert_called_once()

    @mock.patch("io_mcp.notifications.urllib.request.urlopen")
    def test_send_very_long_message(self, mock_urlopen):
        """Sending an event with a very long message should not crash."""
        mock_resp = mock.MagicMock()
        mock_resp.status = 200
        mock_resp.__enter__ = mock.MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = mock.MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        long_msg = "x" * 100_000

        for ch_type in ("ntfy", "slack", "discord", "webhook"):
            mock_urlopen.reset_mock()
            d, ch = self._make_dispatcher(ch_type)
            event = NotificationEvent(
                event_type="error", title="T", message=long_msg,
            )
            d._send(ch, event)
            mock_urlopen.assert_called_once()

    @mock.patch("io_mcp.notifications.urllib.request.urlopen")
    def test_send_unicode_message(self, mock_urlopen):
        """Messages with unicode/emoji should be properly encoded."""
        mock_resp = mock.MagicMock()
        mock_resp.status = 200
        mock_resp.__enter__ = mock.MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = mock.MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        d, ch = self._make_dispatcher("ntfy")
        event = NotificationEvent(
            event_type="error", title="⚠️ Alert", message="日本語テスト 🎉",
        )
        d._send(ch, event)

        req = mock_urlopen.call_args[0][0]
        assert req.data == "日本語テスト 🎉".encode("utf-8")


class TestCooldownMechanism:
    """Thorough tests for the cooldown mechanism."""

    def _make_channel(self, name="test", events=None):
        return NotificationChannel(
            name=name, channel_type="webhook",
            url="http://example.com", events=events or ["all"],
        )

    def test_cooldown_is_per_channel_and_event_type(self):
        """Cooldown key is (channel_name, event_type) — same event to different channels should both fire."""
        ch1 = self._make_channel(name="channel-a")
        ch2 = self._make_channel(name="channel-b")
        d = NotificationDispatcher(
            channels=[ch1, ch2], cooldown_secs=60.0, enabled=True,
        )

        with mock.patch.object(d, '_send') as mock_send:
            event = NotificationEvent(
                event_type="error", title="E", message="M",
            )
            d.notify(event)
            time.sleep(0.2)

            # Both channels should get the notification
            assert mock_send.call_count == 2
            called_channels = {call[0][0].name for call in mock_send.call_args_list}
            assert called_channels == {"channel-a", "channel-b"}

    def test_cooldown_expires_after_timeout(self):
        """After cooldown_secs elapses, the same event type should be sent again."""
        ch = self._make_channel()
        d = NotificationDispatcher(
            channels=[ch], cooldown_secs=0.3, enabled=True,
        )

        with mock.patch.object(d, '_send') as mock_send:
            event = NotificationEvent(
                event_type="error", title="E", message="M",
            )
            d.notify(event)
            time.sleep(0.15)
            assert mock_send.call_count == 1

            # Still within cooldown
            d.notify(event)
            time.sleep(0.15)
            assert mock_send.call_count == 1

            # Wait for cooldown to expire
            time.sleep(0.3)
            d.notify(event)
            time.sleep(0.15)
            assert mock_send.call_count == 2

    def test_cooldown_zero_allows_all(self):
        """With cooldown_secs=0, every notification should go through."""
        ch = self._make_channel()
        d = NotificationDispatcher(
            channels=[ch], cooldown_secs=0, enabled=True,
        )

        with mock.patch.object(d, '_send') as mock_send:
            for i in range(5):
                d.notify(NotificationEvent(
                    event_type="error", title="E", message=f"M{i}",
                ))
            time.sleep(0.3)

            assert mock_send.call_count == 5

    def test_cooldown_independent_per_event_type(self):
        """Cooldown on one event type should not affect another."""
        ch = self._make_channel()
        d = NotificationDispatcher(
            channels=[ch], cooldown_secs=60.0, enabled=True,
        )

        with mock.patch.object(d, '_send') as mock_send:
            # Send each event type once — all should go through
            for etype in ALL_EVENT_TYPES:
                d.notify(NotificationEvent(
                    event_type=etype, title="T", message="M",
                ))
            time.sleep(0.3)

            assert mock_send.call_count == len(ALL_EVENT_TYPES)

    def test_clear_cooldowns_resets_all(self):
        """clear_cooldowns() should reset tracking for all event types and channels."""
        ch1 = self._make_channel(name="a")
        ch2 = self._make_channel(name="b")
        d = NotificationDispatcher(
            channels=[ch1, ch2], cooldown_secs=60.0, enabled=True,
        )

        with mock.patch.object(d, '_send') as mock_send:
            # Send events to both channels
            d.notify(NotificationEvent(event_type="error", title="T", message="M"))
            d.notify(NotificationEvent(event_type="health_warning", title="T", message="M"))
            time.sleep(0.2)

            initial_count = mock_send.call_count
            assert initial_count == 4  # 2 events × 2 channels

            # All should be cooled down
            d.notify(NotificationEvent(event_type="error", title="T", message="M"))
            d.notify(NotificationEvent(event_type="health_warning", title="T", message="M"))
            time.sleep(0.2)
            assert mock_send.call_count == initial_count  # no new sends

            # Clear and resend
            d.clear_cooldowns()
            d.notify(NotificationEvent(event_type="error", title="T", message="M"))
            d.notify(NotificationEvent(event_type="health_warning", title="T", message="M"))
            time.sleep(0.2)
            assert mock_send.call_count == initial_count + 4


class TestEventFiltering:
    """Thorough tests for channel event filtering."""

    def test_channel_with_single_event(self):
        """Channel subscribed to one event only receives that event."""
        ch = NotificationChannel(
            name="t", channel_type="webhook", url="http://x",
            events=["error"],
        )
        assert ch.accepts_event("error") is True
        for etype in ALL_EVENT_TYPES - {"error"}:
            assert ch.accepts_event(etype) is False

    def test_channel_with_subset_of_events(self):
        """Channel subscribed to multiple events receives only those."""
        ch = NotificationChannel(
            name="t", channel_type="webhook", url="http://x",
            events=["error", "health_warning", "pulse_down"],
        )
        assert ch.accepts_event("error") is True
        assert ch.accepts_event("health_warning") is True
        assert ch.accepts_event("pulse_down") is True
        assert ch.accepts_event("agent_connected") is False
        assert ch.accepts_event("health_unresponsive") is False
        assert ch.accepts_event("pulse_recovered") is False

    def test_all_subscription_accepts_unknown_events(self):
        """'all' subscription should even accept event types not in ALL_EVENT_TYPES."""
        ch = NotificationChannel(
            name="t", channel_type="webhook", url="http://x",
            events=["all"],
        )
        assert ch.accepts_event("totally_made_up_event") is True
        assert ch.accepts_event("") is True

    def test_all_mixed_with_specific_accepts_all(self):
        """If 'all' is in the events list with other events, it should accept everything."""
        ch = NotificationChannel(
            name="t", channel_type="webhook", url="http://x",
            events=["all", "error"],
        )
        assert ch.accepts_event("health_warning") is True
        assert ch.accepts_event("custom") is True

    def test_dispatcher_respects_per_channel_filters(self):
        """With multiple channels, each receives only its subscribed events."""
        ch_errors = NotificationChannel(
            name="errors", channel_type="webhook", url="http://a",
            events=["error"],
        )
        ch_health = NotificationChannel(
            name="health", channel_type="webhook", url="http://b",
            events=["health_warning", "health_unresponsive"],
        )
        ch_all = NotificationChannel(
            name="all", channel_type="webhook", url="http://c",
            events=["all"],
        )
        d = NotificationDispatcher(
            channels=[ch_errors, ch_health, ch_all],
            cooldown_secs=0, enabled=True,
        )

        with mock.patch.object(d, '_send') as mock_send:
            # error → ch_errors + ch_all
            d.notify(NotificationEvent(event_type="error", title="T", message="M"))
            time.sleep(0.15)
            assert mock_send.call_count == 2
            sent_to = {call[0][0].name for call in mock_send.call_args_list}
            assert sent_to == {"errors", "all"}

            mock_send.reset_mock()

            # health_warning → ch_health + ch_all
            d.notify(NotificationEvent(event_type="health_warning", title="T", message="M"))
            time.sleep(0.15)
            assert mock_send.call_count == 2
            sent_to = {call[0][0].name for call in mock_send.call_args_list}
            assert sent_to == {"health", "all"}

            mock_send.reset_mock()

            # agent_connected → ch_all only
            d.notify(NotificationEvent(event_type="agent_connected", title="T", message="M"))
            time.sleep(0.15)
            assert mock_send.call_count == 1
            assert mock_send.call_args[0][0].name == "all"


class TestChannelsFromConfigExtended:
    """Extended tests for channels_from_config parsing."""

    def test_discord_channel(self):
        """Discord channel type should be parsed correctly."""
        channels = channels_from_config([
            {
                "name": "disc",
                "type": "discord",
                "url": "https://discord.com/api/webhooks/123/abc",
                "events": ["error", "health_unresponsive"],
            }
        ])
        assert len(channels) == 1
        ch = channels[0]
        assert ch.channel_type == "discord"
        assert ch.url == "https://discord.com/api/webhooks/123/abc"
        assert ch.accepts_event("error") is True
        assert ch.accepts_event("agent_connected") is False

    def test_unknown_extra_fields_are_ignored(self):
        """Extra keys in the config dict should be silently ignored."""
        channels = channels_from_config([
            {
                "name": "test",
                "type": "ntfy",
                "url": "http://ntfy.sh/topic",
                "unknown_field": "some value",
                "another_extra": 42,
            }
        ])
        assert len(channels) == 1
        assert channels[0].name == "test"

    def test_empty_url_string_is_skipped(self):
        """Channel with url='' (empty string) should be skipped."""
        channels = channels_from_config([
            {"name": "bad", "type": "ntfy", "url": ""},
        ])
        assert len(channels) == 0

    def test_no_name_uses_default(self):
        """Channel without a name key should default to 'unnamed'."""
        channels = channels_from_config([
            {"type": "webhook", "url": "http://example.com"},
        ])
        assert len(channels) == 1
        assert channels[0].name == "unnamed"

    def test_preserves_channel_order(self):
        """channels_from_config should maintain the order of input."""
        channels = channels_from_config([
            {"name": "first", "type": "ntfy", "url": "http://a"},
            {"name": "second", "type": "slack", "url": "http://b"},
            {"name": "third", "type": "discord", "url": "http://c"},
            {"name": "fourth", "type": "webhook", "url": "http://d"},
        ])
        assert [ch.name for ch in channels] == ["first", "second", "third", "fourth"]
        assert [ch.channel_type for ch in channels] == ["ntfy", "slack", "discord", "webhook"]

    def test_skips_invalid_entries_but_keeps_valid(self):
        """Invalid entries should be skipped while valid ones are kept."""
        channels = channels_from_config([
            {"name": "good", "type": "ntfy", "url": "http://good"},
            {"name": "bad"},  # no URL
            {"name": "also-good", "type": "slack", "url": "http://also-good"},
        ])
        assert len(channels) == 2
        assert channels[0].name == "good"
        assert channels[1].name == "also-good"

    def test_custom_method_and_headers(self):
        """Custom HTTP method and headers should be preserved."""
        channels = channels_from_config([
            {
                "name": "custom",
                "type": "webhook",
                "url": "http://example.com",
                "method": "PATCH",
                "headers": {"Authorization": "Bearer token123", "X-App": "io-mcp"},
            }
        ])
        assert len(channels) == 1
        ch = channels[0]
        assert ch.method == "PATCH"
        assert ch.headers == {"Authorization": "Bearer token123", "X-App": "io-mcp"}

    def test_priority_values(self):
        """Priority values should be preserved from config."""
        channels = channels_from_config([
            {"name": "low", "type": "ntfy", "url": "http://a", "priority": 1},
            {"name": "high", "type": "ntfy", "url": "http://b", "priority": 5},
        ])
        assert channels[0].priority == 1
        assert channels[1].priority == 5


class TestCreateDispatcherExtended:
    """Extended tests for create_dispatcher factory."""

    def test_config_with_notifications_disabled(self):
        """Config with enabled=False → disabled dispatcher."""
        config = mock.MagicMock()
        config.expanded = {
            "config": {
                "notifications": {
                    "enabled": False,
                    "channels": [
                        {"name": "t", "type": "ntfy", "url": "http://x", "events": ["all"]},
                    ],
                }
            }
        }
        d = create_dispatcher(config)
        assert d.enabled is False

    def test_config_with_custom_cooldown(self):
        """Cooldown from config should be passed to dispatcher."""
        config = mock.MagicMock()
        config.expanded = {
            "config": {
                "notifications": {
                    "enabled": True,
                    "cooldownSecs": 120,
                    "channels": [
                        {"name": "t", "type": "ntfy", "url": "http://x"},
                    ],
                }
            }
        }
        d = create_dispatcher(config)
        assert d.enabled is True
        assert d._cooldown_secs == 120.0

    def test_config_with_no_config_key(self):
        """Config.expanded with no 'config' key → disabled dispatcher."""
        config = mock.MagicMock()
        config.expanded = {}
        d = create_dispatcher(config)
        assert d.enabled is False

    def test_config_default_cooldown(self):
        """When cooldownSecs is not specified, default of 60 should be used."""
        config = mock.MagicMock()
        config.expanded = {
            "config": {
                "notifications": {
                    "enabled": True,
                    "channels": [
                        {"name": "t", "type": "ntfy", "url": "http://x"},
                    ],
                }
            }
        }
        d = create_dispatcher(config)
        assert d._cooldown_secs == 60.0

    def test_config_channels_without_urls_yields_disabled(self):
        """If all channels lack URLs, dispatcher should be disabled."""
        config = mock.MagicMock()
        config.expanded = {
            "config": {
                "notifications": {
                    "enabled": True,
                    "channels": [
                        {"name": "bad1", "type": "ntfy"},
                        {"name": "bad2", "type": "slack"},
                    ],
                }
            }
        }
        d = create_dispatcher(config)
        assert d.enabled is False
        assert d.channel_count == 0

    def test_config_multiple_channels_parsed(self):
        """Multiple valid channels should all be present in the dispatcher."""
        config = mock.MagicMock()
        config.expanded = {
            "config": {
                "notifications": {
                    "enabled": True,
                    "channels": [
                        {"name": "a", "type": "ntfy", "url": "http://a"},
                        {"name": "b", "type": "slack", "url": "http://b"},
                        {"name": "c", "type": "discord", "url": "http://c"},
                        {"name": "d", "type": "webhook", "url": "http://d"},
                    ],
                }
            }
        }
        d = create_dispatcher(config)
        assert d.enabled is True
        assert d.channel_count == 4

    def test_config_without_expanded_attr(self):
        """Config object without expanded attribute should not crash."""
        config = mock.MagicMock(spec=[])  # no attributes
        d = create_dispatcher(config)
        assert d.enabled is False


class TestEventEmojiExtended:
    """Extended tests for _event_emoji helper."""

    def test_all_known_event_types_have_emoji(self):
        """Every event in ALL_EVENT_TYPES should have a specific (non-default) emoji."""
        default_emoji = "📋"
        for etype in ALL_EVENT_TYPES:
            emoji = _event_emoji(etype)
            assert emoji != default_emoji, f"Event '{etype}' has no specific emoji"

    def test_pulse_events(self):
        """pulse_down and pulse_recovered should have specific emojis."""
        assert _event_emoji("pulse_down") == "🔇"
        assert _event_emoji("pulse_recovered") == "🔊"

    def test_empty_string_event(self):
        """Empty string event type should return default emoji."""
        assert _event_emoji("") == "📋"


class TestThreadSafety:
    """Tests for thread safety of the dispatcher."""

    def test_concurrent_notifications_no_crash(self):
        """Multiple threads sending notifications concurrently should not crash."""
        ch = NotificationChannel(
            name="test", channel_type="webhook",
            url="http://example.com", events=["all"],
        )
        d = NotificationDispatcher(
            channels=[ch], cooldown_secs=0, enabled=True,
        )

        errors = []

        def send_many(thread_id):
            try:
                for i in range(20):
                    d.notify(NotificationEvent(
                        event_type=f"event_{thread_id}_{i}",
                        title=f"T{thread_id}",
                        message=f"M{i}",
                    ))
            except Exception as e:
                errors.append(e)

        with mock.patch.object(d, '_send'):
            threads = [
                threading.Thread(target=send_many, args=(tid,))
                for tid in range(5)
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=5)

        assert errors == [], f"Thread errors: {errors}"

    def test_concurrent_notify_and_clear_cooldowns(self):
        """Calling clear_cooldowns() while notify() is running should not crash."""
        ch = NotificationChannel(
            name="test", channel_type="webhook",
            url="http://example.com", events=["all"],
        )
        d = NotificationDispatcher(
            channels=[ch], cooldown_secs=0.1, enabled=True,
        )

        errors = []

        def notify_loop():
            try:
                for i in range(50):
                    d.notify(NotificationEvent(
                        event_type="error", title="T", message=f"M{i}",
                    ))
            except Exception as e:
                errors.append(e)

        def clear_loop():
            try:
                for _ in range(50):
                    d.clear_cooldowns()
            except Exception as e:
                errors.append(e)

        with mock.patch.object(d, '_send'):
            t1 = threading.Thread(target=notify_loop)
            t2 = threading.Thread(target=clear_loop)
            t1.start()
            t2.start()
            t1.join(timeout=5)
            t2.join(timeout=5)

        assert errors == [], f"Thread errors: {errors}"


class TestDispatcherEdgeCases:
    """Edge case tests for the dispatcher."""

    def test_empty_channels_list(self):
        """Dispatcher with empty channels list should silently do nothing."""
        d = NotificationDispatcher(channels=[], cooldown_secs=0, enabled=True)
        assert d.channel_count == 0
        # Should not raise
        d.notify(NotificationEvent(event_type="error", title="T", message="M"))

    def test_none_channels_defaults_to_empty(self):
        """Passing channels=None should default to empty list."""
        d = NotificationDispatcher(channels=None, cooldown_secs=0, enabled=True)
        assert d.channel_count == 0

    def test_notify_when_disabled_is_noop(self):
        """Disabled dispatcher with channels should still do nothing."""
        ch = NotificationChannel(
            name="test", channel_type="webhook",
            url="http://example.com", events=["all"],
        )
        d = NotificationDispatcher(
            channels=[ch], cooldown_secs=0, enabled=False,
        )
        with mock.patch.object(d, '_send') as mock_send:
            d.notify(NotificationEvent(event_type="error", title="T", message="M"))
            time.sleep(0.1)
            mock_send.assert_not_called()

    def test_channel_count_reflects_channels(self):
        """channel_count property should reflect the number of channels."""
        d0 = NotificationDispatcher(channels=[], enabled=True)
        assert d0.channel_count == 0

        channels = [
            NotificationChannel(name=f"ch{i}", channel_type="webhook", url=f"http://{i}")
            for i in range(5)
        ]
        d5 = NotificationDispatcher(channels=channels, enabled=True)
        assert d5.channel_count == 5

    @mock.patch("io_mcp.notifications.urllib.request.urlopen")
    def test_notify_end_to_end_with_real_send(self, mock_urlopen):
        """End-to-end test: notify() → background thread → _send() → HTTP call."""
        mock_resp = mock.MagicMock()
        mock_resp.status = 200
        mock_resp.__enter__ = mock.MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = mock.MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        ch = NotificationChannel(
            name="e2e", channel_type="webhook",
            url="http://example.com/hook", events=["all"],
        )
        d = NotificationDispatcher(
            channels=[ch], cooldown_secs=0, enabled=True,
        )

        d.notify(NotificationEvent(
            event_type="agent_connected", title="Connected",
            message="Agent joined", session_name="Test Agent",
        ))
        # Wait for background thread
        time.sleep(0.3)

        mock_urlopen.assert_called_once()
        req = mock_urlopen.call_args[0][0]
        payload = json.loads(req.data.decode("utf-8"))
        assert payload["event_type"] == "agent_connected"
        assert payload["session_name"] == "Test Agent"

    @mock.patch("io_mcp.notifications.urllib.request.urlopen")
    def test_notify_multiple_channel_types_end_to_end(self, mock_urlopen):
        """End-to-end: one event dispatched to ntfy, slack, discord, webhook channels."""
        mock_resp = mock.MagicMock()
        mock_resp.status = 200
        mock_resp.__enter__ = mock.MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = mock.MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        channels = [
            NotificationChannel(name="n", channel_type="ntfy", url="http://ntfy", events=["all"]),
            NotificationChannel(name="s", channel_type="slack", url="http://slack", events=["all"]),
            NotificationChannel(name="d", channel_type="discord", url="http://discord", events=["all"]),
            NotificationChannel(name="w", channel_type="webhook", url="http://webhook", events=["all"]),
        ]
        d = NotificationDispatcher(
            channels=channels, cooldown_secs=0, enabled=True,
        )

        d.notify(NotificationEvent(
            event_type="error", title="Test", message="Msg",
            session_name="Agent", session_id="id-1",
        ))
        time.sleep(0.5)

        # All 4 channels should have been called
        assert mock_urlopen.call_count == 4

        # Verify each used the correct URL
        called_urls = {call[0][0].full_url for call in mock_urlopen.call_args_list}
        assert called_urls == {"http://ntfy", "http://slack", "http://discord", "http://webhook"}
