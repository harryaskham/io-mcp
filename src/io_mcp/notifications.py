"""Notification webhooks for io-mcp.

Sends notifications to external services (ntfy, Slack, Discord, generic
webhooks) when notable events occur — agents needing attention, health
alerts, task completions, errors, etc.

Supports multiple notification channels configured in config.yml:

```yaml
config:
  notifications:
    enabled: true
    cooldownSecs: 60          # min gap between identical notifications
    channels:
      - name: phone
        type: ntfy            # ntfy, slack, discord, or webhook
        url: https://ntfy.sh/my-io-mcp
        priority: 3           # ntfy priority (1-5)
        events: [health_warning, health_unresponsive, choices_timeout]
      - name: team-slack
        type: slack
        url: https://hooks.slack.com/services/XXX
        events: [health_unresponsive, error]
      - name: custom
        type: webhook
        url: https://example.com/hook
        method: POST          # default POST
        headers:
          Authorization: "Bearer ${WEBHOOK_TOKEN}"
        events: [all]         # receive everything
```

Event types:
    health_warning       Agent hasn't made a tool call in a while
    health_unresponsive  Agent appears crashed or stuck
    choices_timeout      Agent has been waiting for user selection too long
    agent_connected      New agent session registered
    agent_disconnected   Agent session cleaned up
    error                An error occurred in the TUI or MCP server
    all                  Catch-all: receive every event type
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Optional

log = logging.getLogger("io-mcp.notifications")


# ─── Event types ────────────────────────────────────────────────────────

ALL_EVENT_TYPES = frozenset({
    "health_warning",
    "health_unresponsive",
    "choices_timeout",
    "agent_connected",
    "agent_disconnected",
    "error",
})


@dataclass
class NotificationEvent:
    """A notification event to dispatch to configured channels."""

    event_type: str
    title: str
    message: str
    session_name: str = ""
    session_id: str = ""
    priority: int = 3           # 1 (min) to 5 (max), used by ntfy
    tags: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)


@dataclass
class NotificationChannel:
    """A configured notification channel."""

    name: str
    channel_type: str           # "ntfy", "slack", "discord", "webhook"
    url: str
    events: list[str] = field(default_factory=lambda: ["all"])
    priority: int = 3           # default ntfy priority
    method: str = "POST"        # HTTP method for generic webhooks
    headers: dict[str, str] = field(default_factory=dict)

    def accepts_event(self, event_type: str) -> bool:
        """Check if this channel should receive the given event type."""
        return "all" in self.events or event_type in self.events


class NotificationDispatcher:
    """Dispatches notification events to configured channels.

    Thread-safe. Sends notifications in background threads to avoid
    blocking the TUI. Implements per-event-type cooldown to prevent
    notification spam.
    """

    def __init__(
        self,
        channels: list[NotificationChannel] | None = None,
        cooldown_secs: float = 60.0,
        enabled: bool = True,
    ) -> None:
        self._channels = channels or []
        self._cooldown_secs = cooldown_secs
        self._enabled = enabled
        self._lock = threading.Lock()
        # Track last notification time per (channel_name, event_type) for cooldown
        self._last_sent: dict[tuple[str, str], float] = {}

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def channel_count(self) -> int:
        return len(self._channels)

    def notify(self, event: NotificationEvent) -> None:
        """Dispatch a notification event to all matching channels.

        Non-blocking: fires off background threads for each HTTP request.
        Respects cooldown to avoid spamming the same notification.
        """
        if not self._enabled or not self._channels:
            return

        now = time.time()

        for channel in self._channels:
            if not channel.accepts_event(event.event_type):
                continue

            # Check cooldown
            key = (channel.name, event.event_type)
            with self._lock:
                last = self._last_sent.get(key, 0.0)
                if now - last < self._cooldown_secs:
                    log.debug(
                        "Notification cooldown: %s/%s (%.0fs remaining)",
                        channel.name,
                        event.event_type,
                        self._cooldown_secs - (now - last),
                    )
                    continue
                self._last_sent[key] = now

            # Send in background
            threading.Thread(
                target=self._send,
                args=(channel, event),
                daemon=True,
            ).start()

    def _send(self, channel: NotificationChannel, event: NotificationEvent) -> None:
        """Send a notification to a specific channel. Runs in background thread."""
        try:
            if channel.channel_type == "ntfy":
                self._send_ntfy(channel, event)
            elif channel.channel_type == "slack":
                self._send_slack(channel, event)
            elif channel.channel_type == "discord":
                self._send_discord(channel, event)
            elif channel.channel_type == "webhook":
                self._send_webhook(channel, event)
            else:
                log.warning("Unknown channel type: %s", channel.channel_type)
        except Exception as exc:
            log.error(
                "Notification send failed for %s/%s: %s",
                channel.name,
                channel.channel_type,
                exc,
            )

    def _send_ntfy(self, channel: NotificationChannel, event: NotificationEvent) -> None:
        """Send notification via ntfy.sh (or self-hosted ntfy)."""
        priority = event.priority if event.priority else channel.priority

        headers = {
            "Title": event.title,
            "Priority": str(priority),
        }
        if event.tags:
            headers["Tags"] = ",".join(event.tags)
        # Merge channel-level headers
        headers.update(channel.headers)

        body = event.message.encode("utf-8")
        req = urllib.request.Request(
            channel.url,
            data=body,
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            log.debug("ntfy response: %s", resp.status)

    def _send_slack(self, channel: NotificationChannel, event: NotificationEvent) -> None:
        """Send notification via Slack incoming webhook."""
        # Build a rich Slack message with context blocks
        emoji = _event_emoji(event.event_type)
        blocks = [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": f"{emoji} {event.title}"},
            },
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": event.message},
            },
        ]

        if event.session_name:
            blocks.append({
                "type": "context",
                "elements": [
                    {"type": "mrkdwn", "text": f"*Agent:* {event.session_name}"},
                ],
            })

        payload = json.dumps({
            "text": f"{emoji} {event.title}: {event.message}",
            "blocks": blocks,
        }).encode("utf-8")

        headers = {"Content-Type": "application/json"}
        headers.update(channel.headers)

        req = urllib.request.Request(
            channel.url,
            data=payload,
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            log.debug("Slack response: %s", resp.status)

    def _send_discord(self, channel: NotificationChannel, event: NotificationEvent) -> None:
        """Send notification via Discord webhook."""
        emoji = _event_emoji(event.event_type)

        # Discord color codes (decimal)
        color_map = {
            "health_warning": 0xEBCB8B,      # yellow
            "health_unresponsive": 0xBF616A,  # red
            "error": 0xBF616A,                # red
            "choices_timeout": 0xD08770,      # orange
            "agent_connected": 0xA3BE8C,      # green
            "agent_disconnected": 0x4C566A,   # gray
        }

        embed = {
            "title": f"{emoji} {event.title}",
            "description": event.message,
            "color": color_map.get(event.event_type, 0x88C0D0),
            "timestamp": time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime(event.timestamp)
            ),
        }

        if event.session_name:
            embed["footer"] = {"text": f"Agent: {event.session_name}"}

        payload = json.dumps({
            "embeds": [embed],
        }).encode("utf-8")

        headers = {"Content-Type": "application/json"}
        headers.update(channel.headers)

        req = urllib.request.Request(
            channel.url,
            data=payload,
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            log.debug("Discord response: %s", resp.status)

    def _send_webhook(self, channel: NotificationChannel, event: NotificationEvent) -> None:
        """Send notification via generic webhook (JSON POST)."""
        payload = json.dumps({
            "event_type": event.event_type,
            "title": event.title,
            "message": event.message,
            "session_name": event.session_name,
            "session_id": event.session_id,
            "priority": event.priority,
            "tags": event.tags,
            "extra": event.extra,
            "timestamp": event.timestamp,
        }).encode("utf-8")

        headers = {"Content-Type": "application/json"}
        headers.update(channel.headers)

        req = urllib.request.Request(
            channel.url,
            data=payload,
            headers=headers,
            method=channel.method,
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            log.debug("Webhook response: %s", resp.status)

    def clear_cooldowns(self) -> None:
        """Clear all cooldown tracking (e.g. after config reload)."""
        with self._lock:
            self._last_sent.clear()


def _event_emoji(event_type: str) -> str:
    """Map event types to emoji for rich notifications."""
    return {
        "health_warning": "⚠️",
        "health_unresponsive": "🔴",
        "choices_timeout": "⏰",
        "agent_connected": "🟢",
        "agent_disconnected": "⚪",
        "error": "❌",
    }.get(event_type, "📋")


def channels_from_config(config_channels: list[dict]) -> list[NotificationChannel]:
    """Build NotificationChannel instances from raw config dicts.

    Args:
        config_channels: List of channel dicts from config.yml.

    Returns:
        List of NotificationChannel instances, skipping any with invalid config.
    """
    channels = []
    for ch in config_channels:
        try:
            name = ch.get("name", "unnamed")
            channel_type = ch.get("type", "webhook")
            url = ch.get("url", "")
            if not url:
                log.warning("Notification channel '%s' has no URL, skipping", name)
                continue

            channels.append(NotificationChannel(
                name=name,
                channel_type=channel_type,
                url=url,
                events=ch.get("events", ["all"]),
                priority=ch.get("priority", 3),
                method=ch.get("method", "POST"),
                headers=ch.get("headers", {}),
            ))
        except Exception as exc:
            log.warning("Failed to parse notification channel: %s", exc)

    return channels


def create_dispatcher(config: Any) -> NotificationDispatcher:
    """Create a NotificationDispatcher from an IoMcpConfig instance.

    Reads config.notifications.{enabled, cooldownSecs, channels} and
    builds the dispatcher. Returns a disabled dispatcher if notifications
    are not configured.

    Args:
        config: An IoMcpConfig instance (or None).

    Returns:
        A NotificationDispatcher (possibly disabled if no config).
    """
    if config is None:
        return NotificationDispatcher(enabled=False)

    notif_config = getattr(config, 'expanded', {}).get("config", {}).get("notifications", {})

    enabled = bool(notif_config.get("enabled", False))
    cooldown = float(notif_config.get("cooldownSecs", 60))
    raw_channels = notif_config.get("channels", [])

    channels = channels_from_config(raw_channels) if raw_channels else []

    if enabled and not channels:
        log.info("Notifications enabled but no channels configured")
        enabled = False

    return NotificationDispatcher(
        channels=channels,
        cooldown_secs=cooldown,
        enabled=enabled,
    )
