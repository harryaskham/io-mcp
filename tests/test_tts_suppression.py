"""Tests for TTS suppression notification and recovery announcement.

Covers:
- _notify_tts_suppressed plays error chime on first call
- Rate limiting prevents chime spam (second call within 10s skipped)
- Rate limiting allows chime after interval expires
- _notify_tts_suppressed updates TUI status via _on_tts_error callback
- Recovery plays success chime and speaks "Speech restored"
- _speak_streaming_once calls _notify_tts_suppressed when API unavailable
- speak_with_local_fallback calls _notify_tts_suppressed on cache miss when API unavailable
- reset_failure_counters resets suppression chime timer
"""

from __future__ import annotations

import struct
import time
import unittest.mock as mock

import pytest

from io_mcp.tts import TTSEngine


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


def _open_circuit_breaker(engine: TTSEngine) -> None:
    """Trip the circuit breaker by recording enough failures."""
    for _ in range(engine._api_fail_threshold):
        engine._record_api_gen_failure("HTTP 500")


# ─── _notify_tts_suppressed ─────────────────────────────────────────


class TestNotifyTtsSuppressed:
    """_notify_tts_suppressed plays error chime and updates TUI status."""

    def test_first_call_plays_error_chime(self):
        """First suppression notification should play the error chime."""
        engine = _make_engine()
        with mock.patch.object(engine, 'play_chime') as mock_chime:
            engine._notify_tts_suppressed()
            # play_chime is called in a background thread — wait for it
            deadline = time.time() + 2
            while mock_chime.call_count == 0 and time.time() < deadline:
                time.sleep(0.02)
            mock_chime.assert_called_once_with("error")

    def test_rate_limiting_prevents_spam(self):
        """Second call within 10s interval should NOT play chime."""
        engine = _make_engine()
        with mock.patch.object(engine, 'play_chime') as mock_chime:
            engine._notify_tts_suppressed()
            # Wait for the first chime thread to fire
            deadline = time.time() + 2
            while mock_chime.call_count == 0 and time.time() < deadline:
                time.sleep(0.02)
            assert mock_chime.call_count == 1

            # Second call immediately — should be rate-limited
            engine._notify_tts_suppressed()
            time.sleep(0.1)  # brief wait to ensure no thread fires
            assert mock_chime.call_count == 1  # still 1 — rate-limited

    def test_chime_plays_again_after_interval(self):
        """After the rate limit interval expires, chime should play again."""
        engine = _make_engine()
        # Use a short interval for testing
        engine._suppression_chime_interval = 0.1  # 100ms

        with mock.patch.object(engine, 'play_chime') as mock_chime:
            engine._notify_tts_suppressed()
            deadline = time.time() + 2
            while mock_chime.call_count == 0 and time.time() < deadline:
                time.sleep(0.02)
            assert mock_chime.call_count == 1

            # Wait for interval to expire
            time.sleep(0.15)

            engine._notify_tts_suppressed()
            deadline = time.time() + 2
            while mock_chime.call_count < 2 and time.time() < deadline:
                time.sleep(0.02)
            assert mock_chime.call_count == 2

    def test_updates_tui_status_line(self):
        """_notify_tts_suppressed calls _on_tts_error with 'TTS unavailable'."""
        engine = _make_engine()
        error_cb = mock.MagicMock()
        engine._on_tts_error = error_cb

        with mock.patch.object(engine, 'play_chime'):
            engine._notify_tts_suppressed()

        error_cb.assert_called_once_with("TTS unavailable")

    def test_updates_tui_status_even_when_rate_limited(self):
        """Even when chime is rate-limited, status line is always updated."""
        engine = _make_engine()
        error_cb = mock.MagicMock()
        engine._on_tts_error = error_cb

        with mock.patch.object(engine, 'play_chime'):
            engine._notify_tts_suppressed()
            engine._notify_tts_suppressed()

        # Called twice — status update is not rate-limited
        assert error_cb.call_count == 2

    def test_no_crash_without_error_callback(self):
        """Should not crash if _on_tts_error is not set."""
        engine = _make_engine()
        engine._on_tts_error = None
        with mock.patch.object(engine, 'play_chime'):
            engine._notify_tts_suppressed()  # should not raise


# ─── _notify_tts_recovered ──────────────────────────────────────────


class TestNotifyTtsRecovered:
    """Recovery notification plays success chime and speaks announcement."""

    def test_recovery_plays_success_chime(self):
        """Recovery should play the success chime."""
        engine = _make_engine()
        with mock.patch.object(engine, 'play_chime') as mock_chime, \
             mock.patch.object(engine, 'speak_async') as mock_speak:
            engine._notify_tts_recovered()
            # Wait for background thread
            deadline = time.time() + 2
            while mock_chime.call_count == 0 and time.time() < deadline:
                time.sleep(0.02)
            mock_chime.assert_called_once_with("success")

    def test_recovery_speaks_restored_message(self):
        """Recovery should speak 'Speech restored'."""
        engine = _make_engine()
        with mock.patch.object(engine, 'play_chime'), \
             mock.patch.object(engine, 'speak_async') as mock_speak:
            engine._notify_tts_recovered()
            # Wait for background thread (chime + 0.3s sleep + speak)
            deadline = time.time() + 3
            while mock_speak.call_count == 0 and time.time() < deadline:
                time.sleep(0.05)
            mock_speak.assert_called_once_with("Speech restored")

    def test_recovery_resets_suppression_chime_timer(self):
        """Recovery should reset the suppression chime timer."""
        engine = _make_engine()
        engine._last_suppression_chime_time = time.time()
        with mock.patch.object(engine, 'play_chime'), \
             mock.patch.object(engine, 'speak_async'):
            engine._notify_tts_recovered()
        assert engine._last_suppression_chime_time == 0


# ─── Integration: _speak_streaming_once calls _notify_tts_suppressed ─


class TestStreamingOnceCallsSuppression:
    """_speak_streaming_once uses _notify_tts_suppressed when API is down."""

    def test_api_unavailable_calls_notify(self):
        """When circuit breaker is open, _speak_streaming_once should call
        _notify_tts_suppressed instead of silently returning."""
        engine = _make_engine()
        _open_circuit_breaker(engine)

        with mock.patch.object(engine, '_notify_tts_suppressed') as mock_notify:
            result = engine._speak_streaming_once("hello", force=False)

        mock_notify.assert_called_once()
        assert result is None  # returns None (not "retry")

    def test_api_unavailable_with_force_bypasses_notify(self):
        """When force=True, circuit breaker is bypassed — no notification."""
        engine = _make_engine()
        _open_circuit_breaker(engine)

        with mock.patch.object(engine, '_notify_tts_suppressed') as mock_notify, \
             mock.patch.object(engine, '_start_playback'), \
             mock.patch("io_mcp.tts.AsyncSubprocessManager"):
            # Will fail for other reasons (no real binary), but should NOT
            # call _notify_tts_suppressed
            try:
                engine._speak_streaming_once("hello", force=True)
            except Exception:
                pass  # expected — no real binary
            mock_notify.assert_not_called()


# ─── Integration: speak_with_local_fallback calls _notify_tts_suppressed


class TestSpeakWithLocalFallbackCallsSuppression:
    """speak_with_local_fallback uses _notify_tts_suppressed on cache miss
    when API is down."""

    def test_cache_miss_api_unavailable_calls_notify(self):
        """Cache miss + circuit breaker open → _notify_tts_suppressed."""
        engine = _make_engine()
        _open_circuit_breaker(engine)

        with mock.patch.object(engine, '_notify_tts_suppressed') as mock_notify:
            engine.speak_with_local_fallback("uncached text")

        mock_notify.assert_called_once()

    def test_cache_hit_api_unavailable_plays_normally(self):
        """Cache hit should play normally even when circuit breaker is open."""
        engine = _make_engine()
        _open_circuit_breaker(engine)

        # Prime the cache
        key = engine._cache_key("cached text")
        import tempfile, os
        fake_wav = os.path.join(tempfile.gettempdir(), "test_cached.wav")
        with open(fake_wav, "wb") as f:
            f.write(b"RIFF" + b"\x00" * 40 + b"data")
        engine._cache[key] = fake_wav

        with mock.patch.object(engine, '_notify_tts_suppressed') as mock_notify, \
             mock.patch.object(engine, 'stop_sync'), \
             mock.patch.object(engine, '_start_playback'):
            engine.speak_with_local_fallback("cached text")
            # Should NOT call notify — cache hit plays normally
            mock_notify.assert_not_called()

        # Cleanup
        try:
            os.unlink(fake_wav)
        except OSError:
            pass

    def test_cache_miss_api_available_calls_speak_async(self):
        """Cache miss + API available → normal speak_async, no notification."""
        engine = _make_engine()
        # API is healthy — no failures

        with mock.patch.object(engine, '_notify_tts_suppressed') as mock_notify, \
             mock.patch.object(engine, 'speak_async') as mock_speak:
            engine.speak_with_local_fallback("new text")

        mock_notify.assert_not_called()
        mock_speak.assert_called_once()


# ─── Integration: recovery probe triggers recovered notification ─────


class TestRecoveryProbeTriggersNotification:
    """When recovery probe succeeds, _notify_tts_recovered is called."""

    def test_successful_probe_calls_recovered(self):
        """Probe success should trigger the recovery notification."""
        engine = _make_engine()
        _open_circuit_breaker(engine)

        # Create valid WAV data for the probe
        sample_rate = 24000
        raw = struct.pack("<100h", *([0] * 100))
        data_size = len(raw)
        wav_data = (
            b"RIFF"
            + struct.pack("<I", 36 + data_size)
            + b"WAVE"
            + b"fmt "
            + struct.pack("<IHHIIHH", 16, 1, 1, sample_rate, sample_rate * 2, 2, 16)
            + b"data"
            + struct.pack("<I", data_size)
            + raw
        )

        def fake_run(cmd, stdout=None, stderr=None, env=None, timeout=None):
            if stdout and hasattr(stdout, 'write'):
                stdout.write(wav_data)
            result = mock.MagicMock()
            result.returncode = 0
            return result

        with mock.patch("subprocess.run", side_effect=fake_run), \
             mock.patch.object(engine, '_notify_tts_recovered') as mock_recovered:
            engine._api_gen_last_failure = time.time() - 61
            engine._spawn_recovery_probe()

            # Wait for probe thread
            deadline = time.time() + 5
            while engine._api_gen_probe_in_progress and time.time() < deadline:
                time.sleep(0.05)

            mock_recovered.assert_called_once()

    def test_failed_probe_does_not_call_recovered(self):
        """Probe failure should NOT trigger recovery notification."""
        engine = _make_engine()
        _open_circuit_breaker(engine)

        def fake_run(cmd, stdout=None, stderr=None, env=None, timeout=None):
            if stdout and hasattr(stdout, 'write'):
                stdout.write(b"")
            result = mock.MagicMock()
            result.returncode = 1
            result.stderr = b"still broken"
            return result

        with mock.patch("subprocess.run", side_effect=fake_run), \
             mock.patch.object(engine, '_notify_tts_recovered') as mock_recovered:
            engine._spawn_recovery_probe()

            deadline = time.time() + 5
            while engine._api_gen_probe_in_progress and time.time() < deadline:
                time.sleep(0.05)

            mock_recovered.assert_not_called()


# ─── reset_failure_counters resets chime timer ───────────────────────


class TestResetFailureCountersResetsChimeTimer:
    """reset_failure_counters also resets the suppression chime timer."""

    def test_reset_clears_chime_timer(self):
        engine = _make_engine()
        engine._last_suppression_chime_time = time.time()
        engine.reset_failure_counters()
        assert engine._last_suppression_chime_time == 0
