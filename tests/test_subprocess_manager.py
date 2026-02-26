"""Tests for the AsyncSubprocessManager.

Covers:
- Process creation and tracking
- Tag-based process management
- cancel_all() kills all tracked processes
- cancel_tagged() kills only matching processes
- get_by_tag() retrieves active processes
- has_active() checks for alive processes
- Dead process pruning
- Process group killing via TrackedProcess
- Thread safety of concurrent operations
"""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
import unittest.mock as mock

import pytest

from io_mcp.subprocess_manager import AsyncSubprocessManager, TrackedProcess


# ─── TrackedProcess ──────────────────────────────────────────────────


class TestTrackedProcess:
    """Tests for the TrackedProcess wrapper."""

    def test_alive_running_process(self):
        """alive returns True for a running process."""
        proc = subprocess.Popen(
            ["sleep", "10"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            preexec_fn=os.setsid,
        )
        try:
            tracked = TrackedProcess(proc, tag="test", use_pgid=True)
            assert tracked.alive is True
        finally:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (OSError, ProcessLookupError):
                proc.kill()
            proc.wait()

    def test_alive_dead_process(self):
        """alive returns False for a completed process."""
        proc = subprocess.Popen(
            ["true"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        proc.wait()
        tracked = TrackedProcess(proc, tag="test")
        assert tracked.alive is False

    def test_kill_running_process_with_pgid(self):
        """kill() uses process group killing when use_pgid=True."""
        proc = subprocess.Popen(
            ["sleep", "10"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            preexec_fn=os.setsid,
        )
        tracked = TrackedProcess(proc, tag="test", use_pgid=True)
        assert tracked.alive is True
        tracked.kill()
        proc.wait()
        assert tracked.alive is False

    def test_kill_running_process_without_pgid(self):
        """kill() falls back to proc.kill() when use_pgid=False."""
        proc = subprocess.Popen(
            ["sleep", "10"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        tracked = TrackedProcess(proc, tag="test", use_pgid=False)
        tracked.kill()
        proc.wait()
        assert tracked.alive is False

    def test_kill_already_dead_is_noop(self):
        """kill() is a no-op on an already dead process."""
        proc = subprocess.Popen(
            ["true"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        proc.wait()
        tracked = TrackedProcess(proc, tag="test")
        tracked.kill()  # Should not raise

    def test_tag_and_use_pgid_defaults(self):
        """TrackedProcess stores tag and defaults use_pgid to True."""
        proc = mock.MagicMock()
        proc.poll.return_value = 0
        tracked = TrackedProcess(proc, tag="mytag")
        assert tracked.tag == "mytag"
        assert tracked.use_pgid is True


# ─── AsyncSubprocessManager ─────────────────────────────────────────


class TestManagerStart:
    """Tests for starting and tracking processes."""

    def test_start_creates_tracked_process(self):
        """start() returns a TrackedProcess with the subprocess."""
        mgr = AsyncSubprocessManager()
        tracked = mgr.start(["sleep", "10"], tag="test")
        try:
            assert isinstance(tracked, TrackedProcess)
            assert tracked.alive is True
            assert tracked.tag == "test"
        finally:
            tracked.kill()
            tracked.proc.wait()

    def test_start_applies_setsid_by_default(self):
        """start() sets preexec_fn=os.setsid by default for pgid killing."""
        mgr = AsyncSubprocessManager()
        tracked = mgr.start(["sleep", "10"], tag="test")
        try:
            # The process should be its own session leader
            assert os.getpgid(tracked.proc.pid) == tracked.proc.pid
        finally:
            tracked.kill()
            tracked.proc.wait()

    def test_start_tracks_in_active_list(self):
        """start() adds the process to the active list."""
        mgr = AsyncSubprocessManager()
        tracked = mgr.start(["sleep", "10"], tag="test")
        try:
            assert mgr.active_count == 1
        finally:
            tracked.kill()
            tracked.proc.wait()

    def test_start_raises_on_invalid_command(self):
        """start() raises OSError for non-existent commands."""
        mgr = AsyncSubprocessManager()
        with pytest.raises(FileNotFoundError):
            mgr.start(["nonexistent-binary-xyz"], tag="test")

    def test_start_prunes_dead_processes(self):
        """start() opportunistically removes dead processes."""
        mgr = AsyncSubprocessManager()
        # Start a fast-dying process
        t1 = mgr.start(["true"], tag="dead")
        t1.proc.wait()  # Wait for it to die
        assert len(mgr._active) == 1  # Still in list

        # Starting another process should prune the dead one
        t2 = mgr.start(["sleep", "10"], tag="alive")
        try:
            assert len(mgr._active) == 1  # Dead one pruned
            assert mgr._active[0].tag == "alive"
        finally:
            t2.kill()
            t2.proc.wait()


class TestManagerCancelAll:
    """Tests for cancel_all()."""

    def test_cancel_all_kills_all_processes(self):
        """cancel_all() kills all tracked processes."""
        mgr = AsyncSubprocessManager()
        procs = []
        for i in range(3):
            t = mgr.start(["sleep", "10"], tag=f"t{i}")
            procs.append(t)

        mgr.cancel_all()
        for t in procs:
            t.proc.wait()
            assert t.alive is False

    def test_cancel_all_clears_active_list(self):
        """cancel_all() empties the active list."""
        mgr = AsyncSubprocessManager()
        mgr.start(["sleep", "10"], tag="test")
        mgr.cancel_all()
        assert len(mgr._active) == 0

    def test_cancel_all_noop_when_empty(self):
        """cancel_all() is a no-op when no processes are tracked."""
        mgr = AsyncSubprocessManager()
        mgr.cancel_all()  # Should not raise

    def test_cancel_all_handles_already_dead(self):
        """cancel_all() gracefully handles already-dead processes."""
        mgr = AsyncSubprocessManager()
        t = mgr.start(["true"], tag="test")
        t.proc.wait()  # Already dead
        mgr.cancel_all()  # Should not raise


class TestManagerCancelTagged:
    """Tests for cancel_tagged()."""

    def test_cancel_tagged_kills_matching(self):
        """cancel_tagged() kills only processes with the given tag."""
        mgr = AsyncSubprocessManager()
        t1 = mgr.start(["sleep", "10"], tag="kill_me")
        t2 = mgr.start(["sleep", "10"], tag="keep_me")

        mgr.cancel_tagged("kill_me")
        t1.proc.wait()
        assert t1.alive is False
        assert t2.alive is True

        # Clean up
        t2.kill()
        t2.proc.wait()

    def test_cancel_tagged_preserves_others(self):
        """cancel_tagged() keeps processes with other tags in the active list."""
        mgr = AsyncSubprocessManager()
        mgr.start(["sleep", "10"], tag="a")
        t2 = mgr.start(["sleep", "10"], tag="b")
        mgr.start(["sleep", "10"], tag="a")

        mgr.cancel_tagged("a")
        # Only "b" should remain
        alive = [t for t in mgr._active if t.alive]
        assert len(alive) == 1
        assert alive[0].tag == "b"

        # Clean up
        mgr.cancel_all()
        t2.proc.wait()

    def test_cancel_tagged_noop_for_unknown_tag(self):
        """cancel_tagged() is a no-op for non-existent tags."""
        mgr = AsyncSubprocessManager()
        t = mgr.start(["sleep", "10"], tag="real")
        mgr.cancel_tagged("fake")
        assert t.alive is True
        t.kill()
        t.proc.wait()


class TestManagerGetByTag:
    """Tests for get_by_tag()."""

    def test_returns_alive_process(self):
        """get_by_tag() returns the most recent alive process."""
        mgr = AsyncSubprocessManager()
        t = mgr.start(["sleep", "10"], tag="playback")
        result = mgr.get_by_tag("playback")
        assert result is t
        t.kill()
        t.proc.wait()

    def test_returns_none_when_dead(self):
        """get_by_tag() returns None when all matching processes are dead."""
        mgr = AsyncSubprocessManager()
        t = mgr.start(["true"], tag="playback")
        t.proc.wait()
        assert mgr.get_by_tag("playback") is None

    def test_returns_none_for_unknown_tag(self):
        """get_by_tag() returns None for non-existent tags."""
        mgr = AsyncSubprocessManager()
        assert mgr.get_by_tag("nonexistent") is None

    def test_returns_most_recent(self):
        """get_by_tag() returns the most recent alive process with that tag."""
        mgr = AsyncSubprocessManager()
        t1 = mgr.start(["sleep", "10"], tag="playback")
        t2 = mgr.start(["sleep", "10"], tag="playback")
        result = mgr.get_by_tag("playback")
        assert result is t2
        mgr.cancel_all()
        t1.proc.wait()
        t2.proc.wait()


class TestManagerHasActive:
    """Tests for has_active()."""

    def test_no_processes(self):
        """has_active() returns False when no processes are tracked."""
        mgr = AsyncSubprocessManager()
        assert mgr.has_active() is False

    def test_alive_process(self):
        """has_active() returns True when processes are alive."""
        mgr = AsyncSubprocessManager()
        t = mgr.start(["sleep", "10"], tag="test")
        assert mgr.has_active() is True
        t.kill()
        t.proc.wait()

    def test_dead_process(self):
        """has_active() returns False when all processes are dead."""
        mgr = AsyncSubprocessManager()
        t = mgr.start(["true"], tag="test")
        t.proc.wait()
        assert mgr.has_active() is False

    def test_with_tag_filter(self):
        """has_active() filters by tag when provided."""
        mgr = AsyncSubprocessManager()
        t = mgr.start(["sleep", "10"], tag="alpha")
        assert mgr.has_active(tag="alpha") is True
        assert mgr.has_active(tag="beta") is False
        t.kill()
        t.proc.wait()


class TestManagerActiveCount:
    """Tests for the active_count property."""

    def test_empty_manager(self):
        """active_count is 0 for empty manager."""
        mgr = AsyncSubprocessManager()
        assert mgr.active_count == 0

    def test_counts_alive_only(self):
        """active_count only counts alive processes."""
        mgr = AsyncSubprocessManager()
        t1 = mgr.start(["sleep", "10"], tag="alive")
        t2 = mgr.start(["true"], tag="dead")
        t2.proc.wait()
        assert mgr.active_count == 1
        t1.kill()
        t1.proc.wait()


class TestManagerThreadSafety:
    """Tests for thread safety of the manager."""

    def test_concurrent_start_and_cancel(self):
        """Concurrent start() and cancel_all() calls don't crash."""
        mgr = AsyncSubprocessManager()
        errors = []

        def start_many():
            for _ in range(10):
                try:
                    mgr.start(["true"], tag="test")
                except Exception as e:
                    errors.append(e)

        def cancel_many():
            for _ in range(10):
                try:
                    mgr.cancel_all()
                except Exception as e:
                    errors.append(e)

        threads = [
            threading.Thread(target=start_many),
            threading.Thread(target=cancel_many),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        assert len(errors) == 0

    def test_concurrent_cancel_tagged(self):
        """Concurrent cancel_tagged() calls don't crash."""
        mgr = AsyncSubprocessManager()
        errors = []

        # Start some processes first
        for i in range(5):
            mgr.start(["sleep", "10"], tag=f"tag{i % 2}")

        def cancel_tag0():
            for _ in range(5):
                try:
                    mgr.cancel_tagged("tag0")
                except Exception as e:
                    errors.append(e)

        def cancel_tag1():
            for _ in range(5):
                try:
                    mgr.cancel_tagged("tag1")
                except Exception as e:
                    errors.append(e)

        threads = [
            threading.Thread(target=cancel_tag0),
            threading.Thread(target=cancel_tag1),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        assert len(errors) == 0
        mgr.cancel_all()  # Clean up any stragglers
