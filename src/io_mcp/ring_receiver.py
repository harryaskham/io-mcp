"""UDP receiver for ring-mods key events.

Vendored from ring-mods (github.com/harryaskham/ring-mods).

Listens for JSON datagrams from the ring-mods Android app and
dispatches them to a callback function. Runs as a background thread.

Protocol:
    Each UDP datagram is a JSON object with a "type" field:

    {"type": "key", "keycode": 20}           # Android keycode
    {"type": "text", "text": "j"}            # Text key name
    {"type": "scroll", "amount": 1}          # Scroll direction

Usage:
    from io_mcp.ring_receiver import RingReceiver

    def on_key(key: str):
        print(f"Ring event: {key}")

    receiver = RingReceiver(callback=on_key, port=5555)
    receiver.start()  # background thread
    # ... later ...
    receiver.stop()
"""

from __future__ import annotations

import json
import logging
import socket
import threading
from typing import Callable, Optional

log = logging.getLogger("io_mcp.ring_receiver")

# Android keycode → io-mcp key name
KEYCODE_MAP = {
    19: "k",       # DPAD_UP
    20: "j",       # DPAD_DOWN
    21: "h",       # DPAD_LEFT → prev tab
    22: "l",       # DPAD_RIGHT → next tab
    66: "enter",   # ENTER
    62: "space",   # SPACE
    67: "u",       # DEL → undo
    # Letter keycodes (when ring is configured to send j/k etc.)
    36: "j",       # KEYCODE_J
    39: "k",       # KEYCODE_K
    34: "h",       # KEYCODE_H
    40: "l",       # KEYCODE_L
    46: "s",       # KEYCODE_S
    32: "d",       # KEYCODE_D
    42: "n",       # KEYCODE_N
    41: "m",       # KEYCODE_M
    37: "i",       # KEYCODE_I
    49: "u",       # KEYCODE_U
}

# Valid text key names that can be forwarded directly
VALID_TEXT_KEYS = frozenset({
    "j", "k", "enter", "space", "u", "h", "l",
    "s", "d", "n", "m", "i",
})


class RingReceiver:
    """UDP listener for ring-mods key events.

    Runs a background daemon thread that listens for JSON datagrams
    and calls the provided callback with an io-mcp key name string.

    Args:
        callback: Called with key name (e.g. "j", "enter") on each event.
        port: UDP port to listen on (default 5555).
        host: Bind address (default "0.0.0.0").
    """

    def __init__(
        self,
        callback: Callable[[str], None],
        port: int = 5555,
        host: str = "0.0.0.0",
    ):
        self.callback = callback
        self.port = port
        self.host = host
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._socket: Optional[socket.socket] = None

    def start(self) -> None:
        """Start the UDP listener in a background daemon thread."""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._listen, daemon=True, name="ring-receiver"
        )
        self._thread.start()

    def stop(self) -> None:
        """Stop the UDP listener."""
        self._running = False
        if self._socket:
            try:
                self._socket.close()
            except OSError:
                pass

    @property
    def alive(self) -> bool:
        """Whether the listener thread is running."""
        return self._running and self._thread is not None and self._thread.is_alive()

    def _listen(self) -> None:
        """Main listener loop — runs in background thread."""
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._socket.bind((self.host, self.port))
        self._socket.settimeout(1.0)  # allow periodic stop checks
        log.info("Ring receiver listening on %s:%d", self.host, self.port)

        while self._running:
            try:
                data, addr = self._socket.recvfrom(1024)
                log.debug("UDP recv from %s: %s", addr, data[:200])
                self._handle(data)
            except socket.timeout:
                continue
            except OSError:
                break

        # Clean up
        try:
            self._socket.close()
        except OSError:
            pass

    def _handle(self, data: bytes) -> None:
        """Parse a JSON datagram and dispatch to callback."""
        try:
            event = json.loads(data.decode())
        except (json.JSONDecodeError, UnicodeDecodeError):
            return

        key = None
        evt_type = event.get("type", "")

        if evt_type == "key":
            keycode = event.get("keycode", 0)
            key = KEYCODE_MAP.get(keycode)
        elif evt_type == "text":
            text = event.get("text", "")
            if text in VALID_TEXT_KEYS:
                key = text
        elif evt_type == "scroll":
            amount = event.get("amount", 0)
            if amount > 0:
                key = "j"
            elif amount < 0:
                key = "k"

        if key:
            log.info("Ring key: %s (from %s event)", key, evt_type)
            try:
                self.callback(key)
            except Exception:
                pass
        else:
            log.debug("Ignoring %s event: %s", evt_type, event)
