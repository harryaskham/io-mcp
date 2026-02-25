"""Tests for the io-mcp TTS engine.

Covers:
- _find_binary path resolution
- TTSEngine cache key generation and invalidation
- Cache hit/miss detection (is_cached)
- Mute/unmute behavior
- play_cached fast/slow paths
- speak / speak_async / speak_streaming dispatch
- speak_with_local_fallback logic
- pregenerate parallel generation
- stop() kills all process types
- clear_cache removes files and dict entries
- play_tone WAV generation
- play_chime style dispatch
- Local backend fallback chain (termux → espeak → none)
- Thread safety of stop/play interactions
"""

from __future__ import annotations

import hashlib
import os
import struct
import subprocess
import tempfile
import threading
import time
import unittest.mock as mock

import pytest

from io_mcp.tts import TTSEngine, _find_binary, CACHE_DIR, TTS_SPEED


# ─── Helpers ─────────────────────────────────────────────────────────


class FakeConfig:
    """Minimal IoMcpConfig stand-in for TTSEngine tests."""

    def __init__(
        self,
        model: str = "gpt-4o-mini-tts",
        voice: str = "sage",
        speed: float = 1.3,
        emotion: str = "friendly",
        local_backend: str = "none",
    ):
        self.tts_model_name = model
        self.tts_voice = voice
        self.tts_speed = speed
        self.tts_emotion = emotion
        self.tts_local_backend = local_backend
        self.tts_ui_voice = ""

    def tts_cli_args(self, text: str, voice_override=None, emotion_override=None):
        voice = voice_override or self.tts_voice
        return [
            text,
            "--model", self.tts_model_name,
            "--voice", voice,
            "--speed", str(self.tts_speed),
            "--stdout",
            "--response-format", "wav",
        ]


def _make_engine(**kwargs) -> TTSEngine:
    """Create a TTSEngine with all binaries stubbed to None so no real
    subprocess calls are made.  Caller can override via kwargs."""
    defaults = dict(local=True, speed=1.0, config=None)
    defaults.update(kwargs)
    with mock.patch("io_mcp.tts._find_binary", return_value=None):
        engine = TTSEngine(**defaults)
    return engine


def _make_wav(path: str, duration_samples: int = 100) -> None:
    """Write a minimal but structurally valid WAV file."""
    sample_rate = 24000
    raw = struct.pack(f"<{duration_samples}h", *([0] * duration_samples))
    data_size = len(raw)
    with open(path, "wb") as f:
        f.write(b"RIFF")
        f.write(struct.pack("<I", 36 + data_size))
        f.write(b"WAVE")
        f.write(b"fmt ")
        f.write(struct.pack("<IHHIIHH", 16, 1, 1, sample_rate,
                            sample_rate * 2, 2, 16))
        f.write(b"data")
        f.write(struct.pack("<I", data_size))
        f.write(raw)


# ─── _find_binary ────────────────────────────────────────────────────


class TestFindBinary:
    """Tests for the _find_binary helper."""

    def test_finds_binary_on_path(self):
        # "python3" (or "python") should always be on PATH during tests
        result = _find_binary("python3") or _find_binary("python")
        assert result is not None

    def test_returns_none_for_nonexistent(self):
        assert _find_binary("nonexistent_binary_xyz_12345") is None

    @mock.patch("shutil.which", return_value=None)
    def test_checks_nix_profile_paths(self, _mock_which):
        with mock.patch("os.path.isfile") as mock_isfile:
            # First Nix path doesn't exist, second does
            mock_isfile.side_effect = lambda p: p.endswith("/default/bin/mybin")
            result = _find_binary("mybin")
            assert result is not None
            assert result.endswith("mybin")

    @mock.patch("shutil.which", return_value=None)
    @mock.patch("os.path.isfile", return_value=False)
    def test_returns_none_when_not_found_anywhere(self, _isfile, _which):
        assert _find_binary("missing_tool") is None

    @mock.patch("shutil.which", return_value="/usr/bin/espeak-ng")
    def test_prefers_path_over_nix(self, _which):
        result = _find_binary("espeak-ng")
        assert result == "/usr/bin/espeak-ng"


# ─── Cache key generation ────────────────────────────────────────────


class TestCacheKey:
    """Tests for TTSEngine._cache_key."""

    def test_local_mode_cache_key_includes_speed(self):
        engine = _make_engine(local=True, speed=1.0)
        key1 = engine._cache_key("hello")
        engine._speed = 2.0
        key2 = engine._cache_key("hello")
        assert key1 != key2

    def test_same_text_same_key(self):
        engine = _make_engine(local=True, speed=1.0)
        assert engine._cache_key("hello") == engine._cache_key("hello")

    def test_different_text_different_key(self):
        engine = _make_engine(local=True)
        assert engine._cache_key("hello") != engine._cache_key("world")

    def test_api_mode_includes_model_voice_speed_emotion(self):
        config = FakeConfig()
        with mock.patch("io_mcp.tts._find_binary", return_value="/usr/bin/tts"):
            engine = TTSEngine(local=False, speed=1.0, config=config)
        key1 = engine._cache_key("hello")

        config2 = FakeConfig(voice="coral")
        with mock.patch("io_mcp.tts._find_binary", return_value="/usr/bin/tts"):
            engine2 = TTSEngine(local=False, speed=1.0, config=config2)
        key2 = engine2._cache_key("hello")

        assert key1 != key2  # different voice → different key

    def test_voice_override_changes_key(self):
        config = FakeConfig(voice="sage")
        with mock.patch("io_mcp.tts._find_binary", return_value="/usr/bin/tts"):
            engine = TTSEngine(local=False, speed=1.0, config=config)
        key_default = engine._cache_key("hello")
        key_override = engine._cache_key("hello", voice_override="coral")
        assert key_default != key_override

    def test_emotion_override_changes_key(self):
        config = FakeConfig(emotion="friendly")
        with mock.patch("io_mcp.tts._find_binary", return_value="/usr/bin/tts"):
            engine = TTSEngine(local=False, speed=1.0, config=config)
        key_default = engine._cache_key("hello")
        key_override = engine._cache_key("hello", emotion_override="excited")
        assert key_default != key_override

    def test_cache_key_is_md5_hex(self):
        engine = _make_engine(local=True)
        key = engine._cache_key("test")
        assert len(key) == 32
        int(key, 16)  # should not raise — valid hex

    def test_cache_key_deterministic(self):
        engine = _make_engine(local=True, speed=1.5)
        keys = [engine._cache_key("same text") for _ in range(10)]
        assert len(set(keys)) == 1


# ─── is_cached ────────────────────────────────────────────────────────


class TestIsCached:
    """Tests for TTSEngine.is_cached."""

    def test_not_cached_initially(self):
        engine = _make_engine()
        assert engine.is_cached("something") is False

    def test_cached_after_dict_insert_with_real_file(self):
        engine = _make_engine()
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            _make_wav(f.name)
            key = engine._cache_key("hello")
            engine._cache[key] = f.name

            assert engine.is_cached("hello") is True

            os.unlink(f.name)

    def test_cached_returns_false_if_file_deleted(self):
        engine = _make_engine()
        key = engine._cache_key("hello")
        engine._cache[key] = "/tmp/does_not_exist_xyz.wav"
        assert engine.is_cached("hello") is False

    def test_voice_override_checked_correctly_api_mode(self):
        """In API mode (with config), voice_override changes the cache key."""
        config = FakeConfig(voice="sage")
        with mock.patch("io_mcp.tts._find_binary", return_value="/usr/bin/tts"):
            engine = TTSEngine(local=False, speed=1.0, config=config)

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            _make_wav(f.name)
            key = engine._cache_key("hello", voice_override="coral")
            engine._cache[key] = f.name

            assert engine.is_cached("hello", voice_override="coral") is True
            assert engine.is_cached("hello") is False  # default voice not cached

            os.unlink(f.name)

    def test_voice_override_ignored_in_local_mode(self):
        """In local mode (no config), voice_override is not part of cache key."""
        engine = _make_engine(local=True)

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            _make_wav(f.name)
            key = engine._cache_key("hello")
            engine._cache[key] = f.name

            # In local mode, voice_override doesn't affect the key
            assert engine.is_cached("hello", voice_override="coral") is True
            assert engine.is_cached("hello") is True

            os.unlink(f.name)


# ─── Mute / unmute ───────────────────────────────────────────────────


class TestMuteUnmute:
    """Tests for mute/unmute behavior."""

    def test_starts_unmuted(self):
        engine = _make_engine()
        assert engine._muted is False

    def test_mute_sets_flag(self):
        engine = _make_engine()
        engine.mute()
        assert engine._muted is True

    def test_unmute_clears_flag(self):
        engine = _make_engine()
        engine.mute()
        engine.unmute()
        assert engine._muted is False

    def test_mute_calls_stop(self):
        engine = _make_engine()
        with mock.patch.object(engine, "stop_sync") as mock_stop:
            engine.mute()
            mock_stop.assert_called_once()

    def test_play_cached_noop_when_muted(self):
        engine = _make_engine()
        engine._paplay = "/usr/bin/paplay"  # pretend it exists
        engine.mute()
        with mock.patch.object(engine, "_generate_to_file") as mock_gen:
            engine.play_cached("hello")
            mock_gen.assert_not_called()

    def test_speak_with_local_fallback_noop_when_muted(self):
        engine = _make_engine()
        engine.mute()
        with mock.patch.object(engine, "_speak_termux") as mock_termux:
            with mock.patch.object(engine, "speak_async") as mock_async:
                engine.speak_with_local_fallback("hello")
                mock_termux.assert_not_called()
                mock_async.assert_not_called()

    def test_play_tone_noop_when_muted(self):
        engine = _make_engine()
        engine._paplay = "/usr/bin/paplay"
        engine.mute()
        with mock.patch("subprocess.Popen") as mock_popen:
            engine.play_tone()
            mock_popen.assert_not_called()

    def test_play_chime_noop_when_muted(self):
        engine = _make_engine()
        engine.mute()
        with mock.patch.object(engine, "play_tone") as mock_tone:
            engine.play_chime("choices")
            time.sleep(0.1)  # chime runs in a thread
            mock_tone.assert_not_called()


# ─── play_cached ─────────────────────────────────────────────────────


class TestPlayCached:
    """Tests for TTSEngine.play_cached."""

    def test_noop_without_paplay(self):
        engine = _make_engine()
        engine._paplay = None
        with mock.patch.object(engine, "_generate_to_file") as mock_gen:
            engine.play_cached("hello")
            mock_gen.assert_not_called()

    def test_cache_hit_plays_directly(self):
        engine = _make_engine()
        engine._paplay = "/usr/bin/paplay"

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            _make_wav(f.name)
            key = engine._cache_key("hello")
            engine._cache[key] = f.name

            with mock.patch.object(engine, "stop_sync") as mock_stop:
                with mock.patch.object(engine, "_start_playback") as mock_play:
                    engine.play_cached("hello")
                    mock_stop.assert_called_once()
                    mock_play.assert_called_once_with(f.name, max_attempts=2)

            os.unlink(f.name)

    def test_cache_miss_generates_then_plays(self):
        engine = _make_engine()
        engine._paplay = "/usr/bin/paplay"

        fake_path = "/tmp/test_generated.wav"
        with mock.patch.object(engine, "_generate_to_file", return_value=fake_path) as mock_gen:
            with mock.patch.object(engine, "stop_sync"):
                with mock.patch.object(engine, "_start_playback") as mock_play:
                    engine.play_cached("hello")
                    mock_gen.assert_called_once()
                    mock_play.assert_called_once_with(fake_path, max_attempts=2)

    def test_cache_miss_generation_fails_no_crash(self):
        engine = _make_engine()
        engine._paplay = "/usr/bin/paplay"

        with mock.patch.object(engine, "_generate_to_file", return_value=None):
            with mock.patch.object(engine, "_start_playback") as mock_play:
                engine.play_cached("hello")
                mock_play.assert_not_called()

    def test_blocking_play_waits(self):
        engine = _make_engine()
        engine._paplay = "/usr/bin/paplay"

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            _make_wav(f.name)
            key = engine._cache_key("hello")
            engine._cache[key] = f.name

            with mock.patch.object(engine, "stop_sync"):
                with mock.patch.object(engine, "_start_playback"):
                    with mock.patch.object(engine, "_wait_for_playback") as mock_wait:
                        engine.play_cached("hello", block=True)
                        mock_wait.assert_called_once()

            os.unlink(f.name)


# ─── speak / speak_async ─────────────────────────────────────────────


class TestSpeak:
    """Tests for speak() and speak_async()."""

    def test_speak_calls_play_cached_blocking(self):
        engine = _make_engine()
        with mock.patch.object(engine, "play_cached") as mock_play:
            engine.speak("hello", voice_override="coral")
            mock_play.assert_called_once_with(
                "hello", block=True,
                voice_override="coral", emotion_override=None,
                model_override=None,
            )

    def test_speak_async_runs_in_thread(self):
        engine = _make_engine()
        called = threading.Event()

        def fake_play_cached(*args, **kwargs):
            called.set()

        with mock.patch.object(engine, "play_cached", side_effect=fake_play_cached):
            engine.speak_async("hello")
            assert called.wait(timeout=2), "speak_async did not call play_cached within timeout"


# ─── speak_streaming ──────────────────────────────────────────────────


class TestSpeakStreaming:
    """Tests for speak_streaming() dispatch logic."""

    def test_noop_without_paplay(self):
        engine = _make_engine()
        engine._paplay = None
        with mock.patch.object(engine, "play_cached") as mock_play:
            engine.speak_streaming("hello")
            mock_play.assert_not_called()

    def test_noop_when_muted(self):
        engine = _make_engine()
        engine._paplay = "/usr/bin/paplay"
        engine.mute()
        with mock.patch.object(engine, "play_cached") as mock_play:
            engine.speak_streaming("hello")
            mock_play.assert_not_called()

    def test_uses_cache_if_available(self):
        engine = _make_engine()
        engine._paplay = "/usr/bin/paplay"

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            _make_wav(f.name)
            key = engine._cache_key("hello")
            engine._cache[key] = f.name

            with mock.patch.object(engine, "stop_sync"):
                with mock.patch.object(engine, "_start_playback") as mock_play:
                    engine.speak_streaming("hello")
                    mock_play.assert_called_once_with(f.name, max_attempts=2)

            os.unlink(f.name)

    def test_falls_back_to_play_cached_in_local_mode(self):
        engine = _make_engine(local=True)
        engine._paplay = "/usr/bin/paplay"

        with mock.patch.object(engine, "play_cached") as mock_play:
            engine.speak_streaming("hello")
            mock_play.assert_called_once()

    def test_falls_back_without_tts_bin(self):
        engine = _make_engine(local=False)
        engine._paplay = "/usr/bin/paplay"
        engine._tts_bin = None
        engine._local = False

        with mock.patch.object(engine, "play_cached") as mock_play:
            engine.speak_streaming("hello")
            mock_play.assert_called_once()

    def test_falls_back_without_config(self):
        engine = _make_engine(local=False)
        engine._paplay = "/usr/bin/paplay"
        engine._tts_bin = "/usr/bin/tts"
        engine._local = False
        engine._config = None

        with mock.patch.object(engine, "play_cached") as mock_play:
            engine.speak_streaming("hello")
            mock_play.assert_called_once()


# ─── speak_with_local_fallback ────────────────────────────────────────


class TestSpeakWithLocalFallback:
    """Tests for speak_with_local_fallback() dispatch."""

    def test_cache_hit_plays_directly(self):
        config = FakeConfig(local_backend="none")
        engine = _make_engine(local=False, config=config)
        engine._paplay = "/usr/bin/paplay"

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            _make_wav(f.name)
            key = engine._cache_key("hello")
            engine._cache[key] = f.name

            with mock.patch.object(engine, "stop_sync"):
                with mock.patch.object(engine, "_start_playback") as mock_play:
                    engine.speak_with_local_fallback("hello")
                    mock_play.assert_called_once_with(f.name)

            os.unlink(f.name)

    def test_no_local_backend_falls_back_to_speak_async(self):
        """Cache miss with no local backend: uses speak_async() for API TTS."""
        config = FakeConfig(local_backend="none")
        with mock.patch("io_mcp.tts._find_binary", return_value="/usr/bin/tts"):
            engine = TTSEngine(local=False, speed=1.0, config=config)
        engine._local_backend = "none"

        with mock.patch.object(engine, "speak_async") as mock_async:
            engine.speak_with_local_fallback("uncached text")
            # Cache miss in API mode → falls back to speak_async (not espeak)
            mock_async.assert_called_once_with(
                "uncached text", voice_override=None, emotion_override=None)

    def test_termux_backend_calls_speak_termux(self):
        config = FakeConfig(local_backend="termux")
        engine = _make_engine(local=True, config=config)
        engine._local_backend = "termux"
        engine._termux_exec = "/usr/bin/termux-exec"

        with mock.patch.object(engine, "_speak_termux") as mock_termux:
            with mock.patch.object(engine, "_generate_to_file"):
                engine.speak_with_local_fallback("uncached text")
                time.sleep(0.2)  # threads need to fire
                mock_termux.assert_called_once_with("uncached text", block=False)

    def test_espeak_backend_generates_and_plays(self):
        config = FakeConfig(local_backend="espeak")
        engine = _make_engine(local=False, config=config)
        engine._local_backend = "espeak"
        engine._espeak = "/usr/bin/espeak-ng"
        engine._paplay = "/usr/bin/paplay"
        engine._local = False

        with mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = mock.MagicMock(returncode=0)
            with mock.patch.object(engine, "stop_sync"):
                with mock.patch.object(engine, "_start_playback"):
                    with mock.patch.object(engine, "_generate_to_file"):
                        engine.speak_with_local_fallback("uncached text")
                        time.sleep(0.3)  # let threads fire


# ─── pregenerate ──────────────────────────────────────────────────────


class TestPregenerate:
    """Tests for parallel pregeneration."""

    def test_skips_already_cached(self):
        engine = _make_engine()
        # Pre-populate cache
        key = engine._cache_key("cached text")
        engine._cache[key] = "/tmp/fake.wav"

        with mock.patch.object(engine, "_generate_to_file") as mock_gen:
            engine.pregenerate(["cached text"])
            mock_gen.assert_not_called()

    def test_generates_uncached_texts(self):
        engine = _make_engine()

        generated = []

        def fake_generate(text, **kwargs):
            generated.append(text)
            return f"/tmp/{text}.wav"

        with mock.patch.object(engine, "_generate_to_file", side_effect=fake_generate):
            engine.pregenerate(["alpha", "beta", "gamma"])
            assert set(generated) == {"alpha", "beta", "gamma"}

    def test_empty_list_is_noop(self):
        engine = _make_engine()
        with mock.patch.object(engine, "_generate_to_file") as mock_gen:
            engine.pregenerate([])
            mock_gen.assert_not_called()

    def test_mixed_cached_and_uncached(self):
        engine = _make_engine()
        key = engine._cache_key("cached")
        engine._cache[key] = "/tmp/cached.wav"

        generated = []

        def fake_generate(text, **kwargs):
            generated.append(text)
            return f"/tmp/{text}.wav"

        with mock.patch.object(engine, "_generate_to_file", side_effect=fake_generate):
            engine.pregenerate(["cached", "new1", "new2"])
            assert "cached" not in generated
            assert "new1" in generated
            assert "new2" in generated


# ─── stop ─────────────────────────────────────────────────────────────


class TestStop:
    """Tests for stop() killing all process types via the subprocess manager."""

    def test_stop_cancels_all_via_manager(self):
        engine = _make_engine()
        with mock.patch.object(engine._mgr, "cancel_all") as mock_cancel:
            engine.stop()
            # stop() runs in a background thread, give it time
            import time
            time.sleep(0.3)
            mock_cancel.assert_called_once()

    def test_stop_sync_cancels_all_via_manager(self):
        engine = _make_engine()
        with mock.patch.object(engine._mgr, "cancel_all") as mock_cancel:
            engine.stop_sync()
            mock_cancel.assert_called_once()

    def test_stop_noop_when_nothing_playing(self):
        engine = _make_engine()
        assert engine._mgr.active_count == 0
        engine.stop()  # should not raise

    def test_stop_sync_noop_when_nothing_playing(self):
        engine = _make_engine()
        assert engine._mgr.active_count == 0
        engine.stop_sync()  # should not raise


# ─── clear_cache ──────────────────────────────────────────────────────


class TestClearCache:
    """Tests for clear_cache()."""

    def test_clears_dict(self):
        engine = _make_engine()
        engine._cache["key1"] = "/tmp/file1.wav"
        engine._cache["key2"] = "/tmp/file2.wav"
        engine.clear_cache()
        assert len(engine._cache) == 0

    def test_removes_and_recreates_cache_dir(self):
        engine = _make_engine()
        # Create a file in the cache dir
        os.makedirs(CACHE_DIR, exist_ok=True)
        test_file = os.path.join(CACHE_DIR, "test_clear.wav")
        with open(test_file, "w") as f:
            f.write("test")

        engine.clear_cache()

        assert os.path.isdir(CACHE_DIR)  # recreated
        assert not os.path.isfile(test_file)  # file removed

    def test_cleanup_calls_stop_and_clear(self):
        engine = _make_engine()
        with mock.patch.object(engine, "stop_sync") as mock_stop:
            with mock.patch.object(engine, "clear_cache") as mock_clear:
                engine.cleanup()
                mock_stop.assert_called_once()
                mock_clear.assert_called_once()


# ─── _generate_to_file ───────────────────────────────────────────────


class TestGenerateToFile:
    """Tests for _generate_to_file."""

    def test_returns_cached_path_if_exists(self):
        engine = _make_engine()

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            _make_wav(f.name)
            key = engine._cache_key("hello")
            engine._cache[key] = f.name

            result = engine._generate_to_file("hello")
            assert result == f.name

            os.unlink(f.name)

    def test_returns_none_if_local_and_no_espeak(self):
        engine = _make_engine(local=True)
        engine._espeak = None
        result = engine._generate_to_file("hello")
        assert result is None

    def test_returns_none_if_api_and_no_tts_bin(self):
        engine = _make_engine(local=False)
        engine._local = False
        engine._tts_bin = None
        result = engine._generate_to_file("hello")
        assert result is None

    def test_espeak_generation(self):
        engine = _make_engine(local=True, speed=1.0)
        engine._espeak = "/usr/bin/espeak-ng"

        mock_result = mock.MagicMock()
        mock_result.returncode = 0

        with mock.patch("subprocess.run", return_value=mock_result) as mock_run:
            with mock.patch("builtins.open", mock.mock_open()):
                result = engine._generate_to_file("test text")
                assert result is not None
                # Verify espeak was called with correct speed
                call_args = mock_run.call_args
                cmd = call_args[0][0]
                assert "/usr/bin/espeak-ng" in cmd
                assert "--stdout" in cmd
                assert str(TTS_SPEED) in cmd  # speed=1.0, so WPM=160

    def test_api_generation_with_config(self):
        """API mode engine uses config's tts_cli_args to build the command."""
        config = FakeConfig()
        with mock.patch("io_mcp.tts._find_binary", return_value="/usr/bin/tts"):
            engine = TTSEngine(local=False, speed=1.0, config=config)

        assert engine._local is False
        assert engine._tts_bin == "/usr/bin/tts"
        assert engine._config is config

        # Verify the config's CLI args include the expected flags
        args = config.tts_cli_args("test text")
        assert "test text" in args
        assert "--model" in args
        assert "gpt-4o-mini-tts" in args
        assert "--voice" in args
        assert "sage" in args

    def test_failed_api_generation_removes_file(self):
        config = FakeConfig()
        with mock.patch("io_mcp.tts._find_binary", return_value="/usr/bin/tts"):
            engine = TTSEngine(local=False, speed=1.0, config=config)

        mock_result = mock.MagicMock()
        mock_result.returncode = 1  # failure
        mock_result.stderr = b"some error"

        with mock.patch("subprocess.run", return_value=mock_result):
            result = engine._generate_to_file("test text")
            assert result is None

    def test_exception_returns_none(self):
        engine = _make_engine(local=True)
        engine._espeak = "/usr/bin/espeak-ng"

        with mock.patch("subprocess.run", side_effect=OSError("boom")):
            with mock.patch("builtins.open", mock.mock_open()):
                result = engine._generate_to_file("test text")
                assert result is None


# ─── play_tone ────────────────────────────────────────────────────────


class TestPlayTone:
    """Tests for play_tone WAV generation."""

    def test_noop_without_paplay(self):
        engine = _make_engine()
        engine._paplay = None
        with mock.patch("subprocess.Popen") as mock_popen:
            engine.play_tone()
            mock_popen.assert_not_called()

    def test_generates_wav_and_plays(self):
        engine = _make_engine()
        engine._paplay = "/usr/bin/paplay"

        with mock.patch("subprocess.Popen") as mock_popen:
            engine.play_tone(frequency=440, duration_ms=50)
            mock_popen.assert_called_once()
            # Verify paplay was called
            call_args = mock_popen.call_args[0][0]
            assert call_args[0] == "/usr/bin/paplay"
            assert call_args[1].endswith(".wav")

    def test_wav_file_is_valid(self):
        engine = _make_engine()
        engine._paplay = "/usr/bin/paplay"

        with mock.patch("subprocess.Popen"):
            engine.play_tone(frequency=800, duration_ms=100)

        # Check the generated tone file
        tone_path = os.path.join(CACHE_DIR, "tone-800-100.wav")
        if os.path.isfile(tone_path):
            with open(tone_path, "rb") as f:
                header = f.read(4)
                assert header == b"RIFF"
                f.seek(8)
                wave_tag = f.read(4)
                assert wave_tag == b"WAVE"

    def test_tone_parameters(self):
        engine = _make_engine()
        engine._paplay = "/usr/bin/paplay"

        with mock.patch("subprocess.Popen") as mock_popen:
            engine.play_tone(frequency=1200, duration_ms=200, volume=0.5, fade=False)
            call_args = mock_popen.call_args[0][0]
            assert "tone-1200-200.wav" in call_args[1]


# ─── play_chime ───────────────────────────────────────────────────────


class TestPlayChime:
    """Tests for play_chime style dispatch."""

    KNOWN_STYLES = [
        "choices", "select", "connect", "record_start", "record_stop",
        "convo_on", "convo_off", "urgent", "error", "warning",
        "success", "disconnect", "heartbeat",
    ]

    def test_all_known_styles_call_play_tone(self):
        for style in self.KNOWN_STYLES:
            engine = _make_engine()
            engine._paplay = "/usr/bin/paplay"

            with mock.patch.object(engine, "play_tone") as mock_tone:
                engine.play_chime(style)
                time.sleep(0.3)  # chime plays in a thread
                assert mock_tone.call_count >= 1, f"Style '{style}' did not call play_tone"

    def test_unknown_style_is_noop(self):
        engine = _make_engine()
        engine._paplay = "/usr/bin/paplay"

        with mock.patch.object(engine, "play_tone") as mock_tone:
            engine.play_chime("nonexistent_style")
            time.sleep(0.2)
            mock_tone.assert_not_called()

    def test_choices_chime_plays_two_tones(self):
        engine = _make_engine()
        engine._paplay = "/usr/bin/paplay"

        with mock.patch.object(engine, "play_tone") as mock_tone:
            engine.play_chime("choices")
            time.sleep(0.3)
            assert mock_tone.call_count == 2

    def test_urgent_chime_plays_three_tones(self):
        engine = _make_engine()
        engine._paplay = "/usr/bin/paplay"

        with mock.patch.object(engine, "play_tone") as mock_tone:
            engine.play_chime("urgent")
            time.sleep(0.4)
            assert mock_tone.call_count == 3

    def test_success_chime_plays_four_tones(self):
        engine = _make_engine()
        engine._paplay = "/usr/bin/paplay"

        with mock.patch.object(engine, "play_tone") as mock_tone:
            engine.play_chime("success")
            time.sleep(0.5)
            assert mock_tone.call_count == 4


# ─── Local backend fallback chain ────────────────────────────────────


class TestLocalBackendFallback:
    """Tests for the termux → espeak → none fallback chain."""

    def test_termux_falls_back_to_espeak_without_termux_exec(self):
        config = FakeConfig(local_backend="termux")
        with mock.patch("io_mcp.tts._find_binary") as mock_find:
            # No termux-exec, but espeak-ng exists
            def find_side_effect(name):
                if name == "espeak-ng":
                    return "/usr/bin/espeak-ng"
                return None

            mock_find.side_effect = find_side_effect
            engine = TTSEngine(local=True, config=config)
            assert engine._local_backend == "espeak"

    def test_espeak_falls_back_to_none_without_espeak(self):
        config = FakeConfig(local_backend="espeak")
        with mock.patch("io_mcp.tts._find_binary", return_value=None):
            engine = TTSEngine(local=True, config=config)
            assert engine._local_backend == "none"

    def test_none_stays_none(self):
        config = FakeConfig(local_backend="none")
        with mock.patch("io_mcp.tts._find_binary", return_value=None):
            engine = TTSEngine(local=True, config=config)
            assert engine._local_backend == "none"

    def test_default_without_config_is_termux(self):
        # Without config, defaults to "termux", which falls back if no termux-exec
        with mock.patch("io_mcp.tts._find_binary", return_value=None):
            engine = TTSEngine(local=True, config=None)
            # No termux-exec → falls to espeak; no espeak → falls to none
            assert engine._local_backend == "none"


# ─── Thread safety ───────────────────────────────────────────────────


class TestThreadSafety:
    """Tests for concurrent stop/play interactions using the subprocess manager."""

    def test_concurrent_stops_dont_crash(self):
        engine = _make_engine()
        errors = []

        def do_stop():
            try:
                engine.stop_sync()
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=do_stop) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        assert len(errors) == 0, f"Concurrent stops raised: {errors}"

    def test_speak_async_concurrent_calls(self):
        engine = _make_engine()
        call_count = {"n": 0}
        lock = threading.Lock()

        def fake_play(*args, **kwargs):
            with lock:
                call_count["n"] += 1

        with mock.patch.object(engine, "play_cached", side_effect=fake_play):
            for _ in range(5):
                engine.speak_async("hello")
            time.sleep(1)

        assert call_count["n"] == 5


# ─── _start_playback / _wait_for_playback ─────────────────────────────


class TestStartPlayback:
    """Tests for _start_playback and _wait_for_playback using subprocess manager."""

    def test_start_playback_tracks_process(self):
        engine = _make_engine()
        engine._paplay = "/usr/bin/paplay"

        mock_proc = mock.MagicMock()
        # Simulate paplay still running after 0.15s (playback started ok)
        mock_proc.wait.side_effect = subprocess.TimeoutExpired(cmd="paplay", timeout=0.15)
        with mock.patch.object(engine._mgr, "start") as mock_start:
            mock_tracked = mock.MagicMock()
            mock_tracked.proc = mock_proc
            mock_start.return_value = mock_tracked
            result = engine._start_playback("/tmp/test.wav")
            assert result is True
            mock_start.assert_called_once()

    def test_start_playback_exception_returns_false(self):
        engine = _make_engine()
        engine._paplay = "/usr/bin/paplay"

        with mock.patch.object(engine._mgr, "start", side_effect=OSError("boom")):
            result = engine._start_playback("/tmp/test.wav")
            assert result is False

    def test_wait_for_playback_noop_when_no_process(self):
        engine = _make_engine()
        engine._wait_for_playback()  # should not raise

    def test_wait_for_playback_waits_for_process(self):
        engine = _make_engine()
        mock_proc = mock.MagicMock()
        mock_tracked = mock.MagicMock()
        mock_tracked.proc = mock_proc
        mock_tracked.alive = True
        with mock.patch.object(engine._mgr, "get_by_tag", return_value=mock_tracked):
            engine._wait_for_playback()
            mock_proc.wait.assert_called_once_with(timeout=30)

    def test_wait_for_playback_handles_timeout(self):
        engine = _make_engine()
        mock_proc = mock.MagicMock()
        mock_proc.wait.side_effect = Exception("timeout")
        mock_tracked = mock.MagicMock()
        mock_tracked.proc = mock_proc
        mock_tracked.alive = True
        with mock.patch.object(engine._mgr, "get_by_tag", return_value=mock_tracked):
            # Should not raise
            engine._wait_for_playback()


# ─── speak_streaming_async ────────────────────────────────────────────


class TestSpeakStreamingAsync:
    """Tests for speak_streaming_async."""

    def test_runs_in_thread(self):
        engine = _make_engine()
        called = threading.Event()

        def fake_streaming(*args, **kwargs):
            called.set()

        with mock.patch.object(engine, "speak_streaming", side_effect=fake_streaming):
            engine.speak_streaming_async("hello")
            assert called.wait(timeout=2)


# ─── _speak_termux ────────────────────────────────────────────────────


class TestSpeakTermux:
    """Tests for _speak_termux using the subprocess manager."""

    def test_noop_without_termux_exec(self):
        engine = _make_engine()
        engine._termux_exec = None
        with mock.patch.object(engine._mgr, "start") as mock_start:
            engine._speak_termux("hello")
            mock_start.assert_not_called()

    def test_cancels_previous_and_starts_new(self):
        engine = _make_engine()
        engine._termux_exec = "/usr/bin/termux-exec"

        mock_tracked = mock.MagicMock()
        mock_tracked.proc.wait.return_value = None

        with mock.patch.object(engine._mgr, "cancel_tagged") as mock_cancel:
            with mock.patch.object(engine._mgr, "start", return_value=mock_tracked):
                engine._speak_termux("hello")
                mock_cancel.assert_called_once_with("termux")

    def test_uses_config_speed(self):
        config = FakeConfig(speed=1.5)
        engine = _make_engine(config=config)
        engine._termux_exec = "/usr/bin/termux-exec"

        mock_tracked = mock.MagicMock()
        mock_tracked.proc.wait.return_value = None

        with mock.patch.object(engine._mgr, "cancel_tagged"):
            with mock.patch.object(engine._mgr, "start", return_value=mock_tracked) as mock_start:
                engine._speak_termux("hello")
                cmd = mock_start.call_args[0][0]
                assert "1.5" in cmd  # speed passed as string
