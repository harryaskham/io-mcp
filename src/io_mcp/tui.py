"""TUI for io-mcp: alt-screen display, raw input from /dev/tty, dwell timer.

This module reads input from /dev/tty (not stdin) so it works alongside
an MCP server that may use stdin/stdout for its own transport.
"""

from __future__ import annotations

import os
import sys
import termios
import threading
import time
import tty
from dataclasses import dataclass
from enum import Enum, auto
from queue import Queue
from typing import Optional

from .tts import TTSEngine


# ─── Configuration ───────────────────────────────────────────────────────────

DWELL_TIME = 5.0           # seconds to linger before auto-select
SCROLL_DEBOUNCE = 0.08     # seconds between scroll events
DISPLAY_REFRESH = 0.05     # 20fps


# ─── Events ──────────────────────────────────────────────────────────────────

class EventType(Enum):
    SCROLL = auto()
    ENTER = auto()
    DWELL_SELECT = auto()
    QUIT = auto()


@dataclass
class Event:
    type: EventType
    data: object = None


# ─── Raw Input from /dev/tty ─────────────────────────────────────────────────

class RawInput:
    """Reads raw input from /dev/tty in a background thread."""

    def __init__(self, queue: Queue):
        self._queue = queue
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._last_scroll = 0.0
        self._old_settings = None
        self._tty_fd: Optional[int] = None
        self._tty_file = None

    def start(self):
        self._tty_file = open("/dev/tty", "r")
        self._tty_fd = self._tty_file.fileno()
        self._old_settings = termios.tcgetattr(self._tty_fd)
        tty.setraw(self._tty_fd)
        self._running = True
        self._thread = threading.Thread(target=self._read_loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        if self._old_settings and self._tty_fd is not None:
            try:
                termios.tcsetattr(self._tty_fd, termios.TCSADRAIN, self._old_settings)
            except Exception:
                pass
        if self._tty_file:
            try:
                self._tty_file.close()
            except Exception:
                pass

    def _read_loop(self):
        assert self._tty_file is not None
        while self._running:
            try:
                ch = self._tty_file.read(1)
                if not ch:
                    continue

                if ch == "\x03":  # Ctrl+C
                    self._queue.put(Event(EventType.QUIT))
                    return
                elif ch in ("\r", "\n"):
                    self._queue.put(Event(EventType.ENTER))
                elif ch == "k":  # vim up
                    now = time.time()
                    if now - self._last_scroll >= SCROLL_DEBOUNCE:
                        self._last_scroll = now
                        self._queue.put(Event(EventType.SCROLL, -1))
                elif ch == "j":  # vim down
                    now = time.time()
                    if now - self._last_scroll >= SCROLL_DEBOUNCE:
                        self._last_scroll = now
                        self._queue.put(Event(EventType.SCROLL, 1))
                elif ch == "\x1b":  # Escape sequence
                    seq = self._tty_file.read(1)
                    if seq == "[":
                        code = self._tty_file.read(1)
                        now = time.time()
                        if code == "A":  # Up arrow
                            if now - self._last_scroll >= SCROLL_DEBOUNCE:
                                self._last_scroll = now
                                self._queue.put(Event(EventType.SCROLL, -1))
                        elif code == "B":  # Down arrow
                            if now - self._last_scroll >= SCROLL_DEBOUNCE:
                                self._last_scroll = now
                                self._queue.put(Event(EventType.SCROLL, 1))
                        elif code == "<":
                            # SGR mouse: \033[<btn;x;yM
                            buf = ""
                            while True:
                                mc = self._tty_file.read(1)
                                if mc in ("M", "m") or not mc:
                                    break
                                buf += mc
                            parts = buf.split(";")
                            if len(parts) >= 1 and mc == "M":
                                btn = int(parts[0])
                                if btn == 64:  # scroll up
                                    if now - self._last_scroll >= SCROLL_DEBOUNCE:
                                        self._last_scroll = now
                                        self._queue.put(Event(EventType.SCROLL, -1))
                                elif btn == 65:  # scroll down
                                    if now - self._last_scroll >= SCROLL_DEBOUNCE:
                                        self._last_scroll = now
                                        self._queue.put(Event(EventType.SCROLL, 1))
                    elif seq == "\x1b":  # Double-escape = quit
                        self._queue.put(Event(EventType.QUIT))
                        return
            except Exception:
                if self._running:
                    time.sleep(0.01)


# ─── Dwell Timer ─────────────────────────────────────────────────────────────

class DwellTimer:
    """Fires a dwell-select event after DWELL_TIME seconds of no scrolling."""

    def __init__(self, queue: Queue):
        self._queue = queue
        self._start_time: Optional[float] = None
        self._cancelled = False
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None

    def reset(self):
        with self._lock:
            self._start_time = time.time()
            self._cancelled = False
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, daemon=True)
                self._thread.start()

    def cancel(self):
        with self._lock:
            self._cancelled = True
            self._start_time = None

    def get_progress(self) -> float:
        with self._lock:
            if self._start_time is None or self._cancelled:
                return 0.0
            elapsed = time.time() - self._start_time
            return min(1.0, elapsed / DWELL_TIME)

    def _run(self):
        while True:
            time.sleep(0.05)
            with self._lock:
                if self._cancelled or self._start_time is None:
                    return
                elapsed = time.time() - self._start_time
                if elapsed >= DWELL_TIME:
                    self._queue.put(Event(EventType.DWELL_SELECT))
                    self._start_time = None
                    return


# ─── Display ─────────────────────────────────────────────────────────────────

class Display:
    """ANSI terminal display, writes to /dev/tty for alt screen."""

    CLEAR = "\033[2J\033[H"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    REVERSE = "\033[7m"
    RESET = "\033[0m"
    CYAN = "\033[36m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    MAGENTA = "\033[35m"
    ALT_ON = "\033[?1049h"
    ALT_OFF = "\033[?1049l"
    MOUSE_ON = "\033[?1000h\033[?1006h"
    MOUSE_OFF = "\033[?1000l\033[?1006l"

    def __init__(self):
        self._last_render = ""
        self._tty = None

    def _out(self, s: str):
        """Write to /dev/tty."""
        if self._tty is None:
            self._tty = open("/dev/tty", "w")
        self._tty.write(s)
        self._tty.flush()

    def enter(self):
        self._out(self.ALT_ON + "\033[?25l" + self.MOUSE_ON)

    def leave(self):
        self._out(self.MOUSE_OFF + "\033[?25h" + self.ALT_OFF)
        if self._tty:
            try:
                self._tty.close()
            except Exception:
                pass
            self._tty = None

    def _get_cols(self) -> int:
        try:
            return os.get_terminal_size().columns
        except Exception:
            return 80

    def _wrap(self, text: str, width: int) -> list[str]:
        if width < 10:
            width = 10
        words = text.split()
        lines: list[str] = []
        current = ""
        for word in words:
            if current and len(current) + 1 + len(word) > width:
                lines.append(current)
                current = word
            else:
                current = f"{current} {word}" if current else word
        if current:
            lines.append(current)
        return lines or [""]

    def render(
        self,
        preamble: str,
        choices: list[dict],
        index: int,
        dwell_progress: float,
        status: str = "",
    ):
        cols = self._get_cols()
        content_width = cols - 8
        rule = "─" * (cols - 4)

        lines: list[str] = [""]
        lines.append(f"  {self.CYAN}{self.BOLD}io-mcp{self.RESET}")
        lines.append(f"  {self.DIM}{rule}{self.RESET}")
        lines.append("")

        if preamble:
            for wl in self._wrap(preamble, content_width):
                lines.append(f"  {self.GREEN}{wl}{self.RESET}")
            lines.append("")

        for i, choice in enumerate(choices):
            label = choice.get("label", "???")
            summary = choice.get("summary", "")
            is_more = choice.get("_is_more", False)

            if i == index:
                lines.append(f"  {self.REVERSE}{self.BOLD} ▶ {label} {self.RESET}")
                if summary:
                    for wl in self._wrap(summary, content_width - 4):
                        lines.append(f"      {self.DIM}{wl}{self.RESET}")
                if dwell_progress > 0:
                    bar_width = 20
                    filled = int(bar_width * dwell_progress)
                    remaining = bar_width - filled
                    bar = "█" * filled + "░" * remaining
                    secs = DWELL_TIME * (1 - dwell_progress)
                    lines.append(
                        f"      {self.YELLOW}[{bar}] {secs:.1f}s{self.RESET}"
                    )
            else:
                if is_more:
                    lines.append(f"    {self.MAGENTA}⟳ {label}{self.RESET}")
                else:
                    lines.append(f"    {self.DIM}  {label}{self.RESET}")
            lines.append("")

        lines.append(f"  {self.DIM}{rule}{self.RESET}")
        if status:
            lines.append(f"  {self.YELLOW}{status}{self.RESET}")
        else:
            lines.append(
                f"  {self.DIM}↕ Scroll  ⏎ Select  j/k Navigate  "
                f"Ctrl+C Quit{self.RESET}"
            )
        lines.append("")

        output = self.CLEAR + "\n".join(lines)
        if output != self._last_render:
            self._out(output)
            self._last_render = output

    def show_status(self, msg: str):
        """Show a simple status/loading screen."""
        rule = "─" * (self._get_cols() - 4)
        output = (
            f"{self.CLEAR}\n"
            f"  {self.CYAN}{self.BOLD}io-mcp{self.RESET}\n"
            f"  {self.DIM}{rule}{self.RESET}\n\n"
            f"  {self.YELLOW}{msg}{self.RESET}\n\n"
            f"  {self.DIM}{rule}{self.RESET}\n"
        )
        self._out(output)
        self._last_render = output


# ─── TUI Controller ─────────────────────────────────────────────────────────

class TUI:
    """Manages the full TUI lifecycle: input, display, dwell, TTS.

    Used by the MCP server to present choices and get user selections.
    Runs in the background; the MCP server calls present_choices() which
    blocks until the user selects.
    """

    def __init__(self, local_tts: bool = False):
        self._event_queue: Queue = Queue()
        self._tts = TTSEngine(local=local_tts)
        self._input = RawInput(self._event_queue)
        self._dwell = DwellTimer(self._event_queue)
        self._display = Display()

        self._choices: list[dict] = []
        self._index = 0
        self._preamble = ""
        self._active = False  # True when presenting choices
        self._selection: Optional[dict] = None
        self._selection_event = threading.Event()
        self._running = False
        self._thread: Optional[threading.Thread] = None

    def start(self):
        """Start the TUI: enter alt screen, begin input capture."""
        self._display.enter()
        self._input.start()
        self._running = True
        self._display.show_status("Waiting for Claude...")
        self._thread = threading.Thread(target=self._event_loop, daemon=True)
        self._thread.start()

    def stop(self):
        """Stop the TUI: clean up terminal."""
        self._running = False
        self._dwell.cancel()
        self._tts.cleanup()
        self._input.stop()
        self._display.leave()

    def present_choices(
        self, preamble: str, choices: list[dict]
    ) -> dict:
        """Show choices and block until user selects one.

        Returns {"selected": label, "summary": summary}.
        Thread-safe — called from the MCP server's async context
        via run_in_executor or similar.
        """
        self._preamble = preamble
        self._choices = list(choices)
        self._index = 0
        self._selection = None
        self._selection_event.clear()
        self._active = True

        # Speak preamble
        self._tts.speak(preamble)

        # Start dwell timer
        self._dwell.reset()

        # Render initial state
        self._display.render(
            self._preamble, self._choices, self._index, 0.0
        )

        # Block until user selects
        self._selection_event.wait()
        self._active = False

        return self._selection or {"selected": "timeout", "summary": ""}

    def speak(self, text: str) -> None:
        """Speak text via TTS (non-blocking)."""
        self._tts.speak(text)

    def _event_loop(self):
        """Background event loop: process input and refresh display."""
        from queue import Empty

        while self._running:
            try:
                event = self._event_queue.get(timeout=DISPLAY_REFRESH)
                self._handle_event(event)
            except Empty:
                pass

            # Refresh display for dwell progress
            if self._active and self._choices:
                progress = self._dwell.get_progress()
                self._display.render(
                    self._preamble, self._choices, self._index, progress
                )

    def _handle_event(self, event: Event):
        if event.type == EventType.QUIT:
            self._running = False
            # Unblock any waiting present_choices
            if self._active:
                self._selection = {"selected": "quit", "summary": "User quit"}
                self._selection_event.set()

        elif event.type == EventType.SCROLL and self._active:
            self._handle_scroll(event.data)

        elif event.type == EventType.ENTER and self._active:
            self._handle_select()

        elif event.type == EventType.DWELL_SELECT and self._active:
            self._handle_select()

    def _handle_scroll(self, direction: int):
        if not self._choices:
            return
        old_index = self._index
        self._index = max(0, min(len(self._choices) - 1, self._index + direction))
        if self._index != old_index:
            label = self._choices[self._index].get("label", "")
            self._tts.speak(label)
            self._dwell.reset()

    def _handle_select(self):
        if not self._choices or self._index >= len(self._choices):
            return
        chosen = self._choices[self._index]
        self._dwell.cancel()
        self._tts.stop()

        label = chosen.get("label", "")
        summary = chosen.get("summary", "")
        self._tts.speak(f"Selected: {label}")

        self._selection = {"selected": label, "summary": summary}
        self._selection_event.set()

        # Show waiting state
        self._display.show_status(f"Selected: {label} — waiting for Claude...")
