"""Tests for TTS API error recovery improvements.

Covers:
- Recovery probe spawning after circuit breaker cooldown
- Recovery probe success resets failure counters
- Recovery probe failure restarts cooldown
- No thundering herd: probe_in_progress prevents duplicate probes
- Failure reason tracking (_api_gen_last_error)
- api_health property returns correct state
- api_health exposed in get_settings response
- reset_failure_counters clears error and probe state
"""

from __future__ import annotations

import os
import struct
import subprocess
import tempfile
import threading
import time
import unittest.mock as mock

import pytest

from io_mcp.tts import TTSEngine, CACHE_DIR, WAV_HEADER_SIZE


# ─── Helpers ─────────────────────────────────────────────────────────


class FakeConfig:
    """Minimal IoMcpConfig stand-in for TTSEngine tests."""

    def __init__(self):
        self.tts_model_name = "gpt-4o-mini-tts"
        self.tts_voice = "sage"
        self.tts_voice_preset = "sage"
        self.tts_speed = 1.3
        self.tts_emotion = "friendly"
        self.tts_local_backend = "none"
        self.tts_ui_voice = ""
        self.tts_ui_voice_preset = ""
        self.chimes_enabled = True

    def tts_speed_for(self, context: str) -> float:
        return self.tts_speed

    def tts_cli_args(self, text: str, voice_override=None, emotion_override=None,
                     model_override=None, speed_override=None):
        return [
            text,
            "--model", self.tts_model_name,
            "--voice", voice_override or self.tts_voice,
            "--speed", str(speed_override if speed_override is not None else self.tts_speed),
            "--stdout",
            "--response-format", "wav",
        ]


def _make_engine(**kwargs) -> TTSEngine:
    """Create a TTSEngine with all binaries stubbed to None."""
    defaults = dict(local=False, speed=1.0, config=FakeConfig())
    defaults.update(kwargs)
    with mock.patch("io_mcp.tts._find_binary", return_value=None):
        engine = TTSEngine(**defaults)
    # Stub the tts binary so API path is used.
    # Also undo the constructor's fallback to local mode.
    engine._tts_bin = "/usr/bin/tts"
    engine._paplay = "/usr/bin/paplay"
    engine._local = False
    return engine


def _make_wav_bytes(duration_samples: int = 100) -> bytes:
    """Create minimal valid WAV file bytes."""
    sample_rate = 24000
    raw = struct.pack(f"<{duration_samples}h", *([0] * duration_samples))
    data_size = len(raw)
    header = (
        b"RIFF"
        + struct.pack("<I", 36 + data_size)
        + b"WAVE"
        + b"fmt "
        + struct.pack("<IHHIIHH", 16, 1, 1, sample_rate, sample_rate * 2, 2, 16)
        + b"data"
        + struct.pack("<I", data_size)
    )
    return header + raw


# ─── Circuit breaker: failure reason tracking ────────────────────────


class TestFailureReasonTracking:
    """_record_api_gen_failure stores the reason string."""

    def test_failure_records_reason(self):
        engine = _make_engine()
        engine._record_api_gen_failure("HTTP 500")
        assert engine._api_gen_last_error == "HTTP 500"
        assert engine._api_gen_consecutive_failures == 1

    def test_failure_without_reason_keeps_previous(self):
        engine = _make_engine()
        engine._record_api_gen_failure("connection refused")
        engine._record_api_gen_failure()  # no reason
        assert engine._api_gen_last_error == "connection refused"
        assert engine._api_gen_consecutive_failures == 2

    def test_failure_with_new_reason_overwrites(self):
        engine = _make_engine()
        engine._record_api_gen_failure("timeout")
        engine._record_api_gen_failure("HTTP 503")
        assert engine._api_gen_last_error == "HTTP 503"

    def test_success_clears_error(self):
        engine = _make_engine()
        engine._record_api_gen_failure("timeout")
        engine._record_api_gen_success()
        assert engine._api_gen_last_error is None
        assert engine._api_gen_consecutive_failures == 0

    def test_reset_clears_error_and_probe(self):
        engine = _make_engine()
        engine._record_api_gen_failure("HTTP 500")
        engine._api_gen_probe_in_progress = True
        engine.reset_failure_counters()
        assert engine._api_gen_last_error is None
        assert engine._api_gen_consecutive_failures == 0
        assert engine._api_gen_probe_in_progress is False


# ─── api_health property ─────────────────────────────────────────────


class TestApiHealthProperty:
    """api_health returns correct circuit breaker state."""

    def test_healthy_state(self):
        engine = _make_engine()
        health = engine.api_health
        assert health["available"] is True
        assert health["consecutive_failures"] == 0
        assert health["last_error"] is None
        assert health["cooldown_remaining_seconds"] is None
        assert health["probe_in_progress"] is False

    def test_partial_failures_still_available(self):
        engine = _make_engine()
        engine._record_api_gen_failure("HTTP 500")
        engine._record_api_gen_failure("HTTP 500")
        health = engine.api_health
        assert health["available"] is True  # below threshold (3)
        assert health["consecutive_failures"] == 2
        assert health["last_error"] == "HTTP 500"
        assert health["cooldown_remaining_seconds"] is None

    def test_circuit_open_with_cooldown(self):
        engine = _make_engine()
        for i in range(3):
            engine._record_api_gen_failure("timeout")
        health = engine.api_health
        assert health["available"] is False
        assert health["consecutive_failures"] == 3
        assert health["last_error"] == "timeout"
        # Cooldown should be close to 60 seconds
        assert health["cooldown_remaining_seconds"] is not None
        assert 55 < health["cooldown_remaining_seconds"] <= 60

    def test_circuit_open_cooldown_expired(self):
        engine = _make_engine()
        for i in range(3):
            engine._record_api_gen_failure("timeout")
        # Simulate cooldown expiry
        engine._api_gen_last_failure = time.time() - 61
        health = engine.api_health
        assert health["available"] is False  # still false until probe succeeds
        assert health["cooldown_remaining_seconds"] == 0.0

    def test_probe_in_progress_shown(self):
        engine = _make_engine()
        for i in range(3):
            engine._record_api_gen_failure("timeout")
        engine._api_gen_probe_in_progress = True
        health = engine.api_health
        assert health["probe_in_progress"] is True


# ─── Recovery probe ──────────────────────────────────────────────────


class TestRecoveryProbe:
    """After cooldown, a probe runs before re-enabling the circuit."""

    def test_circuit_stays_closed_during_cooldown(self):
        """During cooldown, _api_gen_available returns False and no probe spawns."""
        engine = _make_engine()
        for i in range(3):
            engine._record_api_gen_failure("timeout")
        # Still in cooldown
        assert engine._api_gen_available() is False
        assert engine._api_gen_probe_in_progress is False

    def test_probe_spawns_after_cooldown(self):
        """After cooldown expires, _api_gen_available spawns a probe thread."""
        engine = _make_engine()
        for i in range(3):
            engine._record_api_gen_failure("timeout")

        # Expire the cooldown
        engine._api_gen_last_failure = time.time() - 61

        # Mock _spawn_recovery_probe to just set the flag
        with mock.patch.object(engine, '_spawn_recovery_probe') as mock_probe:
            result = engine._api_gen_available()
            assert result is False  # still returns False until probe succeeds
            mock_probe.assert_called_once()

    def test_no_duplicate_probes(self):
        """If a probe is already running, don't spawn another."""
        engine = _make_engine()
        for i in range(3):
            engine._record_api_gen_failure("timeout")
        engine._api_gen_last_failure = time.time() - 61
        engine._api_gen_probe_in_progress = True

        with mock.patch.object(engine, '_spawn_recovery_probe') as mock_probe:
            engine._api_gen_available()
            mock_probe.assert_not_called()

    def test_probe_success_resets_circuit(self):
        """Successful probe resets failure counters and clears error."""
        engine = _make_engine()
        for i in range(3):
            engine._record_api_gen_failure("HTTP 500")

        # Create a valid WAV file for the probe to "generate"
        wav_data = _make_wav_bytes()

        def fake_run(cmd, stdout=None, stderr=None, env=None, timeout=None):
            """Mock subprocess.run that writes valid WAV data."""
            if stdout and hasattr(stdout, 'write'):
                stdout.write(wav_data)
            result = mock.MagicMock()
            result.returncode = 0
            return result

        with mock.patch("subprocess.run", side_effect=fake_run):
            engine._api_gen_last_failure = time.time() - 61
            engine._spawn_recovery_probe()

            # Wait for the probe thread to complete (inside mock context)
            deadline = time.time() + 5
            while engine._api_gen_probe_in_progress and time.time() < deadline:
                time.sleep(0.05)

        assert engine._api_gen_consecutive_failures == 0
        assert engine._api_gen_last_error is None
        assert engine._api_gen_probe_in_progress is False
        assert engine._api_gen_available() is True

    def test_probe_failure_restarts_cooldown(self):
        """Failed probe updates last_failure time and keeps circuit open."""
        engine = _make_engine()
        for i in range(3):
            engine._record_api_gen_failure("HTTP 500")
        old_failure_time = engine._api_gen_last_failure

        def fake_run(cmd, stdout=None, stderr=None, env=None, timeout=None):
            """Mock subprocess.run that fails."""
            if stdout and hasattr(stdout, 'write'):
                stdout.write(b"")  # empty file
            result = mock.MagicMock()
            result.returncode = 1
            result.stderr = b"API key invalid"
            return result

        with mock.patch("subprocess.run", side_effect=fake_run):
            engine._spawn_recovery_probe()

            # Wait for the probe thread to complete (inside mock context)
            deadline = time.time() + 5
            while engine._api_gen_probe_in_progress and time.time() < deadline:
                time.sleep(0.05)

        # Circuit should still be open with updated failure time
        assert engine._api_gen_consecutive_failures == 3  # not changed
        assert engine._api_gen_last_failure > old_failure_time
        assert "probe failed" in engine._api_gen_last_error
        assert engine._api_gen_probe_in_progress is False

    def test_probe_timeout_restarts_cooldown(self):
        """Timed-out probe updates error and restarts cooldown."""
        engine = _make_engine()
        for i in range(3):
            engine._record_api_gen_failure("HTTP 500")

        def fake_run(cmd, stdout=None, stderr=None, env=None, timeout=None):
            raise subprocess.TimeoutExpired(cmd, timeout)

        with mock.patch("subprocess.run", side_effect=fake_run):
            engine._spawn_recovery_probe()

            deadline = time.time() + 5
            while engine._api_gen_probe_in_progress and time.time() < deadline:
                time.sleep(0.05)

        assert engine._api_gen_last_error == "probe timed out"
        assert engine._api_gen_probe_in_progress is False

    def test_probe_exception_restarts_cooldown(self):
        """Exception in probe updates error and restarts cooldown."""
        engine = _make_engine()
        for i in range(3):
            engine._record_api_gen_failure("HTTP 500")

        def fake_run(cmd, stdout=None, stderr=None, env=None, timeout=None):
            raise ConnectionError("Connection refused")

        with mock.patch("subprocess.run", side_effect=fake_run):
            engine._spawn_recovery_probe()

            deadline = time.time() + 5
            while engine._api_gen_probe_in_progress and time.time() < deadline:
                time.sleep(0.05)

        assert "probe exception" in engine._api_gen_last_error
        assert "Connection refused" in engine._api_gen_last_error
        assert engine._api_gen_probe_in_progress is False

    def test_probe_without_tts_bin_is_noop(self):
        """If _tts_bin is None, probe exits without attempting subprocess."""
        engine = _make_engine()
        engine._tts_bin = None
        for i in range(3):
            engine._record_api_gen_failure("HTTP 500")

        with mock.patch("subprocess.run") as mock_run:
            engine._spawn_recovery_probe()

            deadline = time.time() + 5
            while engine._api_gen_probe_in_progress and time.time() < deadline:
                time.sleep(0.05)

        mock_run.assert_not_called()
        assert engine._api_gen_probe_in_progress is False

    def test_probe_cleans_up_temp_file(self):
        """Probe removes its temp WAV file regardless of result."""
        engine = _make_engine()
        for i in range(3):
            engine._record_api_gen_failure("HTTP 500")

        probe_path = os.path.join(tempfile.gettempdir(), "io-mcp-tts-probe.wav")

        def fake_run(cmd, stdout=None, stderr=None, env=None, timeout=None):
            if stdout and hasattr(stdout, 'write'):
                stdout.write(b"bad data")
            result = mock.MagicMock()
            result.returncode = 1
            result.stderr = b"error"
            return result

        with mock.patch("subprocess.run", side_effect=fake_run):
            engine._spawn_recovery_probe()

            deadline = time.time() + 5
            while engine._api_gen_probe_in_progress and time.time() < deadline:
                time.sleep(0.05)

        # Probe should have cleaned up
        assert not os.path.exists(probe_path)


# ─── Failure reasons propagated from _generate_to_file ───────────────


class TestGenerateToFileFailureReasons:
    """_generate_to_file passes meaningful reasons to _record_api_gen_failure."""

    def test_cli_failure_records_exit_code_and_stderr(self):
        """When tts CLI exits non-zero, the reason includes exit code and stderr."""
        engine = _make_engine()

        def fake_run(cmd, stdout=None, stderr=None, env=None, timeout=None):
            result = mock.MagicMock()
            result.returncode = 1
            result.stderr = b"Invalid API key"
            return result

        with mock.patch("subprocess.run", side_effect=fake_run):
            result = engine._generate_to_file("hello")

        assert result is None
        assert engine._api_gen_consecutive_failures == 1
        assert "exit code 1" in engine._api_gen_last_error
        assert "Invalid API key" in engine._api_gen_last_error

    def test_timeout_records_reason(self):
        """Timeout is recorded as the failure reason."""
        engine = _make_engine()

        def fake_run(cmd, stdout=None, stderr=None, env=None, timeout=None):
            raise subprocess.TimeoutExpired(cmd, timeout)

        with mock.patch("subprocess.run", side_effect=fake_run):
            result = engine._generate_to_file("hello")

        assert result is None
        assert engine._api_gen_last_error == "timeout"

    def test_exception_records_reason(self):
        """General exception records the error message."""
        engine = _make_engine()

        def fake_run(cmd, stdout=None, stderr=None, env=None, timeout=None):
            raise OSError("No such file or directory")

        with mock.patch("subprocess.run", side_effect=fake_run):
            result = engine._generate_to_file("hello")

        assert result is None
        assert "exception" in engine._api_gen_last_error
        assert "No such file" in engine._api_gen_last_error

    def test_invalid_wav_records_reason(self):
        """Invalid WAV output records the file size in the reason."""
        engine = _make_engine()

        def fake_run(cmd, stdout=None, stderr=None, env=None, timeout=None):
            if stdout and hasattr(stdout, 'write'):
                stdout.write(b"tiny")  # WAV too small
            result = mock.MagicMock()
            result.returncode = 0
            return result

        with mock.patch("subprocess.run", side_effect=fake_run):
            result = engine._generate_to_file("hello")

        assert result is None
        assert "invalid WAV" in engine._api_gen_last_error

    def test_missing_binary_records_reason(self):
        """Missing tts binary records the reason."""
        engine = _make_engine()
        engine._tts_bin = None

        result = engine._generate_to_file("hello")
        assert result is None
        assert engine._api_gen_last_error == "tts binary not found"


# ─── Integration: circuit breaker full cycle ─────────────────────────


class TestCircuitBreakerFullCycle:
    """End-to-end circuit breaker: failures → open → cooldown → probe → recovery."""

    def test_full_cycle(self):
        engine = _make_engine()

        # 1. Three failures open the circuit
        engine._record_api_gen_failure("HTTP 500")
        engine._record_api_gen_failure("HTTP 500")
        engine._record_api_gen_failure("HTTP 500")
        assert engine._api_gen_available() is False

        health = engine.api_health
        assert health["available"] is False
        assert health["last_error"] == "HTTP 500"
        assert health["cooldown_remaining_seconds"] > 50

        # 2. During cooldown, stays closed — no probe
        assert engine._api_gen_available() is False
        assert engine._api_gen_probe_in_progress is False

        # 3. After cooldown, probe spawns
        engine._api_gen_last_failure = time.time() - 61
        wav_data = _make_wav_bytes()

        def fake_run(cmd, stdout=None, stderr=None, env=None, timeout=None):
            if stdout and hasattr(stdout, 'write'):
                stdout.write(wav_data)
            result = mock.MagicMock()
            result.returncode = 0
            return result

        with mock.patch("subprocess.run", side_effect=fake_run):
            # This should spawn the probe
            result = engine._api_gen_available()
            assert result is False  # not yet available

            # Wait for probe to complete (inside mock context)
            deadline = time.time() + 5
            while engine._api_gen_probe_in_progress and time.time() < deadline:
                time.sleep(0.05)

        # 4. After successful probe, circuit closes
        assert engine._api_gen_available() is True
        health = engine.api_health
        assert health["available"] is True
        assert health["consecutive_failures"] == 0
        assert health["last_error"] is None
