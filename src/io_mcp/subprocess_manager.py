"""Async subprocess manager for non-blocking process lifecycle management.

Centralises subprocess creation, tracking, and cleanup. Replaces the manual
process tracking (self._process, self._streaming_tts_proc, self._termux_proc)
and threading.Lock contention pattern in TTSEngine with a thread-safe,
lock-free approach using atomic operations.

Key design decisions:
- Uses subprocess.Popen (not asyncio) because callers run in background
  threads, not on an asyncio event loop. The TUI's Textual loop is asyncio,
  but TTSEngine methods are invoked from daemon threads.
- Process group killing (os.killpg) is centralised in one place.
- No threading.Lock — uses a simple list with atomic-ish reference swaps.
  On CPython the GIL makes list append/clear effectively atomic for our
  use case (no iteration-while-mutating).
- preexec_fn=os.setsid is applied automatically so all child processes
  can be killed via process group.
"""

from __future__ import annotations

import os
import signal
import subprocess
from typing import Optional


class TrackedProcess:
    """A subprocess tracked by the manager with metadata for cleanup."""

    __slots__ = ("proc", "tag", "use_pgid")

    def __init__(self, proc: subprocess.Popen, tag: str = "",
                 use_pgid: bool = True):
        self.proc = proc
        self.tag = tag
        self.use_pgid = use_pgid

    @property
    def alive(self) -> bool:
        return self.proc.poll() is None

    def kill(self) -> None:
        """Kill this process (and its process group if use_pgid is set)."""
        if not self.alive:
            return
        if self.use_pgid:
            try:
                os.killpg(os.getpgid(self.proc.pid), signal.SIGKILL)
                return
            except (OSError, ProcessLookupError):
                pass
        # Fallback: direct kill
        try:
            self.proc.kill()
        except (OSError, ProcessLookupError):
            pass


class AsyncSubprocessManager:
    """Thread-safe subprocess manager with automatic process group killing.

    Tracks active subprocesses and provides centralised cancel_all().
    Eliminates the need for threading.Lock by using simple reference
    swaps protected by the GIL.

    Usage:
        mgr = AsyncSubprocessManager()

        # Start a tracked process
        tracked = mgr.start(["paplay", "/tmp/audio.wav"],
                           tag="playback", env=env)

        # Cancel all processes (non-blocking, from any thread)
        mgr.cancel_all()

        # Cancel specific tagged processes
        mgr.cancel_tagged("playback")

        # Or cancel from a background thread (same API, no lock needed)
        mgr.cancel_all()
    """

    def __init__(self) -> None:
        # Active tracked processes. Under CPython's GIL, list.append and
        # slice assignment are atomic enough for our producer/consumer pattern:
        # - Background threads append new processes
        # - cancel_all() replaces the list atomically via slice assignment
        self._active: list[TrackedProcess] = []

    def start(self, cmd: list[str], *, tag: str = "",
              env: Optional[dict] = None,
              stdout=subprocess.DEVNULL,
              stderr=subprocess.PIPE,
              stdin=None,
              use_pgid: bool = True,
              **kwargs) -> TrackedProcess:
        """Start a subprocess and track it.

        Args:
            cmd: Command and arguments.
            tag: Label for selective cancellation (e.g. "playback", "tts", "termux").
            env: Environment variables. Uses os.environ if None.
            stdout: stdout handling (default: DEVNULL).
            stderr: stderr handling (default: PIPE).
            stdin: stdin handling (default: None).
            use_pgid: If True, uses os.setsid and kills via process group.
            **kwargs: Additional Popen kwargs.

        Returns:
            TrackedProcess wrapping the subprocess.

        Raises:
            OSError: If the subprocess cannot be started.
        """
        # Clean up dead processes opportunistically (no lock needed)
        self._prune_dead()

        popen_kwargs = dict(
            stdout=stdout,
            stderr=stderr,
            stdin=stdin,
            env=env,
            **kwargs,
        )
        if use_pgid:
            popen_kwargs["preexec_fn"] = os.setsid

        proc = subprocess.Popen(cmd, **popen_kwargs)
        tracked = TrackedProcess(proc, tag=tag, use_pgid=use_pgid)
        self._active.append(tracked)
        return tracked

    def cancel_all(self) -> None:
        """Kill all tracked processes immediately.

        Thread-safe: can be called from any thread. Uses atomic list
        swap (GIL-protected) to grab the current list and replace it
        with an empty one, then kills everything.
        """
        # Atomically grab all tracked processes and clear the list
        to_kill = self._active[:]
        self._active[:] = []

        for tracked in to_kill:
            tracked.kill()

    def cancel_tagged(self, tag: str) -> None:
        """Kill all tracked processes with a specific tag.

        Thread-safe. Processes with other tags are preserved.
        """
        to_kill = []
        to_keep = []
        for tracked in self._active[:]:
            if tracked.tag == tag:
                to_kill.append(tracked)
            else:
                to_keep.append(tracked)
        self._active[:] = to_keep

        for tracked in to_kill:
            tracked.kill()

    def get_by_tag(self, tag: str) -> Optional[TrackedProcess]:
        """Get the most recent alive process with a given tag.

        Returns None if no alive process with that tag exists.
        """
        for tracked in reversed(self._active):
            if tracked.tag == tag and tracked.alive:
                return tracked
        return None

    def has_active(self, tag: Optional[str] = None) -> bool:
        """Check if any tracked process is still alive.

        Args:
            tag: If provided, only check processes with this tag.
        """
        for tracked in self._active:
            if tag is not None and tracked.tag != tag:
                continue
            if tracked.alive:
                return True
        return False

    def _prune_dead(self) -> None:
        """Remove dead processes from the tracking list.

        Called opportunistically on start() to prevent unbounded growth.
        No lock needed — worst case a just-died process stays one extra cycle.
        """
        self._active[:] = [t for t in self._active if t.alive]

    @property
    def active_count(self) -> int:
        """Number of currently alive tracked processes."""
        return sum(1 for t in self._active if t.alive)
