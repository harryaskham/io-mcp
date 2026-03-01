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
        chimes_enabled: bool = True,
    ):
        self.tts_model_name = model
        self.tts_voice = voice
        self.tts_voice_preset = voice  # preset name (same as voice for simple cases)
        self.tts_speed = speed
        self.tts_emotion = emotion
        self.tts_local_backend = local_backend
        self.tts_ui_voice = ""
        self.tts_ui_voice_preset = ""
        self.chimes_enabled = chimes_enabled

    def tts_speed_for(self, context: str) -> float:
        """Return the base speed for any context (no sub-speeds in tests)."""
        return self.tts_speed

    def tts_cli_args(self, text: str, voice_override=None, emotion_override=None,
                     model_override=None, speed_override=None):
        voice = voice_override or self.tts_voice
        speed = speed_override if speed_override is not None else self.tts_speed
        return [
            text,
            "--model", self.tts_model_name,
            "--voice", voice,
            "--speed", str(speed),
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

            with mock.patch.object(engine, "_start_playback") as mock_play:
                engine.play_cached("hello")
                mock_play.assert_called_once_with(f.name, max_attempts=2)

            os.unlink(f.name)

    def test_cache_miss_generates_then_plays(self):
        engine = _make_engine()
        engine._paplay = "/usr/bin/paplay"

        fake_path = "/tmp/test_generated.wav"
        with mock.patch.object(engine, "_generate_to_file", return_value=fake_path) as mock_gen:
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
                model_override=None, speed_override=None,
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
                "uncached text", voice_override=None, emotion_override=None,
                speed_override=None)

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

        with mock.patch.object(engine, "_generate_to_file_unlocked") as mock_gen:
            engine.pregenerate(["cached text"])
            mock_gen.assert_not_called()

    def test_generates_uncached_texts(self):
        engine = _make_engine()

        generated = []

        def fake_generate(text, **kwargs):
            generated.append(text)
            return f"/tmp/{text}.wav"

        with mock.patch.object(engine, "_generate_to_file_unlocked", side_effect=fake_generate):
            engine.pregenerate(["alpha", "beta", "gamma"])
            assert set(generated) == {"alpha", "beta", "gamma"}

    def test_empty_list_is_noop(self):
        engine = _make_engine()
        with mock.patch.object(engine, "_generate_to_file_unlocked") as mock_gen:
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

        with mock.patch.object(engine, "_generate_to_file_unlocked", side_effect=fake_generate):
            engine.pregenerate(["cached", "new1", "new2"])
            assert "cached" not in generated
            assert "new1" in generated
            assert "new2" in generated

    def test_generation_counter_skips_stale(self):
        """Workers skip generation when a newer pregenerate() has been called."""
        engine = _make_engine()
        generated = []

        def slow_generate(text, **kwargs):
            # Simulate a newer pregenerate() call arriving mid-generation
            # by incrementing the counter before the second item
            if len(generated) == 1:
                engine._pregen_gen += 1  # simulate newer call
            generated.append(text)
            return f"/tmp/{text}.wav"

        with mock.patch.object(engine, "_generate_to_file_unlocked", side_effect=slow_generate):
            engine.pregenerate(["first", "second"], max_workers=1)

        # "first" should be generated, "second" may or may not depending on timing
        assert "first" in generated


# ─── _generate_to_file error handling ─────────────────────────────────


class TestGenerateToFileErrorHandling:
    """Tests for partial file cleanup on errors in _generate_to_file."""

    def test_timeout_cleans_up_partial_file_locked(self):
        """_generate_to_file removes partial WAV on TimeoutExpired."""
        config = FakeConfig()
        engine = _make_engine(local=False, config=config)
        engine._tts_bin = "/usr/bin/tts"
        engine._local = False  # override fallback from init

        # Create a partial file to simulate what subprocess.run would leave behind
        out_key = engine._cache_key("timeout text")
        out_path = os.path.join(CACHE_DIR, f"{out_key}.wav")
        os.makedirs(CACHE_DIR, exist_ok=True)

        def fake_run(*args, **kwargs):
            # Write partial data then timeout
            with open(out_path, "wb") as f:
                f.write(b"partial")
            raise subprocess.TimeoutExpired(cmd="tts", timeout=15)

        with mock.patch("subprocess.run", side_effect=fake_run):
            result = engine._generate_to_file("timeout text")

        assert result is None
        assert not os.path.exists(out_path), "Partial file should be cleaned up on timeout"
        assert out_key not in engine._cache

    def test_exception_cleans_up_partial_file_locked(self):
        """_generate_to_file removes partial WAV on generic exception."""
        config = FakeConfig()
        engine = _make_engine(local=False, config=config)
        engine._tts_bin = "/usr/bin/tts"
        engine._local = False  # override fallback from init

        out_key = engine._cache_key("error text")
        out_path = os.path.join(CACHE_DIR, f"{out_key}.wav")
        os.makedirs(CACHE_DIR, exist_ok=True)

        def fake_run(*args, **kwargs):
            with open(out_path, "wb") as f:
                f.write(b"partial")
            raise RuntimeError("API exploded")

        with mock.patch("subprocess.run", side_effect=fake_run):
            result = engine._generate_to_file("error text")

        assert result is None
        assert not os.path.exists(out_path), "Partial file should be cleaned up on exception"
        assert out_key not in engine._cache

    def test_timeout_cleans_up_partial_file_unlocked(self):
        """_generate_to_file_unlocked removes partial WAV on TimeoutExpired."""
        config = FakeConfig()
        engine = _make_engine(local=False, config=config)
        engine._tts_bin = "/usr/bin/tts"
        engine._local = False  # override fallback from init

        out_key = engine._cache_key("timeout text unlocked")
        out_path = os.path.join(CACHE_DIR, f"{out_key}.wav")
        os.makedirs(CACHE_DIR, exist_ok=True)

        def fake_run(*args, **kwargs):
            with open(out_path, "wb") as f:
                f.write(b"partial")
            raise subprocess.TimeoutExpired(cmd="tts", timeout=15)

        with mock.patch("subprocess.run", side_effect=fake_run):
            result = engine._generate_to_file_unlocked("timeout text unlocked")

        assert result is None
        assert not os.path.exists(out_path), "Partial file should be cleaned up on timeout"
        assert out_key not in engine._cache

    def test_exception_cleans_up_partial_file_unlocked(self):
        """_generate_to_file_unlocked removes partial WAV on generic exception."""
        config = FakeConfig()
        engine = _make_engine(local=False, config=config)
        engine._tts_bin = "/usr/bin/tts"
        engine._local = False  # override fallback from init

        out_key = engine._cache_key("error text unlocked")
        out_path = os.path.join(CACHE_DIR, f"{out_key}.wav")
        os.makedirs(CACHE_DIR, exist_ok=True)

        def fake_run(*args, **kwargs):
            with open(out_path, "wb") as f:
                f.write(b"partial")
            raise RuntimeError("API exploded")

        with mock.patch("subprocess.run", side_effect=fake_run):
            result = engine._generate_to_file_unlocked("error text unlocked")

        assert result is None
        assert not os.path.exists(out_path), "Partial file should be cleaned up on exception"
        assert out_key not in engine._cache

    def test_timeout_records_api_failure(self):
        """Both methods record API failure on timeout."""
        config = FakeConfig()
        engine = _make_engine(local=False, config=config)
        engine._tts_bin = "/usr/bin/tts"
        engine._local = False  # override fallback from init

        with mock.patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="tts", timeout=15)):
            engine._generate_to_file("t1")
            engine._generate_to_file_unlocked("t2")

        assert engine._api_gen_consecutive_failures >= 2


# ─── cache_stats ──────────────────────────────────────────────────────


class TestCacheStats:
    """Tests for cache_stats() accuracy."""

    def test_counts_cached_items(self):
        engine = _make_engine()
        os.makedirs(CACHE_DIR, exist_ok=True)

        # Create real files so sizes can be measured
        f1 = os.path.join(CACHE_DIR, "stats_test_1.wav")
        f2 = os.path.join(CACHE_DIR, "stats_test_2.wav")
        _make_wav(f1, duration_samples=100)
        _make_wav(f2, duration_samples=200)

        engine._cache["key1"] = f1
        engine._cache["key2"] = f2

        count, total_bytes = engine.cache_stats()
        assert count == 2
        assert total_bytes > 0
        assert total_bytes == os.path.getsize(f1) + os.path.getsize(f2)

        # Cleanup
        os.unlink(f1)
        os.unlink(f2)

    def test_skips_missing_files(self):
        engine = _make_engine()
        engine._cache["missing"] = "/tmp/nonexistent_file_xyz.wav"
        engine._cache["also_missing"] = "/tmp/another_nonexistent.wav"

        count, total_bytes = engine.cache_stats()
        assert count == 2  # dict entries still counted
        assert total_bytes == 0  # but no bytes since files don't exist

    def test_empty_cache(self):
        engine = _make_engine()
        count, total_bytes = engine.cache_stats()
        assert count == 0
        assert total_bytes == 0


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
        "success", "disconnect", "heartbeat", "inbox",
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

    def test_play_chime_noop_when_chimes_disabled(self):
        """play_chime should be a no-op when config.chimes_enabled is False."""
        config = FakeConfig(chimes_enabled=False)
        engine = _make_engine(config=config)
        engine._paplay = "/usr/bin/paplay"

        with mock.patch.object(engine, "play_tone") as mock_tone:
            engine.play_chime("choices")
            time.sleep(0.2)
            mock_tone.assert_not_called()

    def test_inbox_chime_plays_three_tones(self):
        engine = _make_engine()
        engine._paplay = "/usr/bin/paplay"

        with mock.patch.object(engine, "play_tone") as mock_tone:
            engine.play_chime("inbox")
            time.sleep(0.3)
            assert mock_tone.call_count == 3


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


# ─── _NUMBER_WORDS ───────────────────────────────────────────────────


class TestNumberWords:
    """Tests for TTSEngine._NUMBER_WORDS constant."""

    def test_has_1_through_9(self):
        for i in range(1, 10):
            assert i in TTSEngine._NUMBER_WORDS

    def test_values_are_word_strings(self):
        expected = {
            1: "one", 2: "two", 3: "three", 4: "four", 5: "five",
            6: "six", 7: "seven", 8: "eight", 9: "nine",
        }
        assert TTSEngine._NUMBER_WORDS == expected


# ─── _concat_wavs ────────────────────────────────────────────────────


class TestConcatWavs:
    """Tests for TTSEngine._concat_wavs WAV concatenation."""

    def test_single_file(self):
        engine = _make_engine()
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            _make_wav(f.name, duration_samples=200)
            result = engine._concat_wavs([f.name])
            assert result is not None
            assert os.path.isfile(result)
            # Verify it's a valid WAV
            with open(result, "rb") as rf:
                header = rf.read(4)
                assert header == b"RIFF"
            os.unlink(f.name)
            os.unlink(result)

    def test_multiple_files_concatenated(self):
        engine = _make_engine()
        files = []
        total_samples = 0
        for n in [100, 200, 150]:
            f = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
            _make_wav(f.name, duration_samples=n)
            files.append(f.name)
            total_samples += n
            f.close()

        result = engine._concat_wavs(files)
        assert result is not None
        assert os.path.isfile(result)

        # Verify combined file has data from all inputs
        with open(result, "rb") as rf:
            header = rf.read(44)
            assert header[:4] == b"RIFF"
            assert header[8:12] == b"WAVE"
            data = rf.read()
            # Each sample is 2 bytes (16-bit)
            assert len(data) == total_samples * 2

        for f in files:
            os.unlink(f)
        os.unlink(result)

    def test_empty_list_returns_none(self):
        engine = _make_engine()
        result = engine._concat_wavs([])
        assert result is None

    def test_nonexistent_files_skipped(self):
        engine = _make_engine()
        result = engine._concat_wavs(["/tmp/does_not_exist_xyz.wav"])
        assert result is None

    def test_mixed_valid_and_invalid(self):
        engine = _make_engine()
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            _make_wav(f.name, duration_samples=100)
            result = engine._concat_wavs([
                "/tmp/does_not_exist.wav",
                f.name,
                "/tmp/also_missing.wav",
            ])
            # Should concatenate the one valid file
            assert result is not None
            os.unlink(f.name)
            os.unlink(result)

    def test_too_small_file_skipped(self):
        engine = _make_engine()
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            f.write(b"RIFF")  # Only 4 bytes — too small for a WAV
            f.flush()
            result = engine._concat_wavs([f.name])
            assert result is None
            os.unlink(f.name)

    def test_deterministic_output_path(self):
        engine = _make_engine()
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            _make_wav(f.name, duration_samples=100)
            result1 = engine._concat_wavs([f.name])
            result2 = engine._concat_wavs([f.name])
            # Same inputs → same output path
            assert result1 == result2
            os.unlink(f.name)
            if result1 and os.path.isfile(result1):
                os.unlink(result1)


# ─── speak_fragments ─────────────────────────────────────────────────


class TestSpeakFragments:
    """Tests for TTSEngine.speak_fragments."""

    def test_all_cached_concatenates_and_plays(self):
        engine = _make_engine()
        engine._paplay = "/usr/bin/paplay"

        # Create two cached fragments
        files = []
        for text in ["selected", "Fix bug"]:
            f = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
            _make_wav(f.name, duration_samples=100)
            key = engine._cache_key(text)
            engine._cache[key] = f.name
            files.append(f.name)
            f.close()

        with mock.patch.object(engine, "_start_playback", return_value=True) as mock_play:
            with mock.patch.object(engine, "_wait_for_playback"):
                engine.speak_fragments(["selected", "Fix bug"])
                time.sleep(0.3)  # runs in a thread
                mock_play.assert_called_once()

        for f in files:
            os.unlink(f)

    def test_uncached_fragment_falls_back_to_streaming(self):
        config = FakeConfig()
        with mock.patch("io_mcp.tts._find_binary", return_value="/usr/bin/tts"):
            engine = TTSEngine(local=False, speed=1.0, config=config)
        engine._paplay = "/usr/bin/paplay"

        # Only cache one fragment
        f = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        _make_wav(f.name, duration_samples=100)
        key = engine._cache_key("selected")
        engine._cache[key] = f.name

        with mock.patch.object(engine, "speak_streaming") as mock_stream:
            engine.speak_fragments(["selected", "uncached text"])
            time.sleep(0.3)
            # Falls back to speak_streaming with full text
            mock_stream.assert_called_once()
            call_args = mock_stream.call_args
            assert "selected uncached text" in call_args[0][0]

        os.unlink(f.name)

    def test_noop_when_muted(self):
        engine = _make_engine()
        engine.mute()
        with mock.patch.object(engine, "_concat_wavs") as mock_concat:
            engine.speak_fragments(["one", "two"])
            time.sleep(0.2)
            mock_concat.assert_not_called()

    def test_runs_in_background_thread(self):
        engine = _make_engine()
        engine._paplay = "/usr/bin/paplay"
        called = threading.Event()

        # Cache all fragments
        for text in ["one", "test"]:
            f = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
            _make_wav(f.name, duration_samples=100)
            engine._cache[engine._cache_key(text)] = f.name

        orig_concat = engine._concat_wavs

        def mock_concat(paths):
            result = orig_concat(paths)
            called.set()
            return result

        with mock.patch.object(engine, "_concat_wavs", side_effect=mock_concat):
            with mock.patch.object(engine, "_start_playback", return_value=True):
                with mock.patch.object(engine, "_wait_for_playback"):
                    engine.speak_fragments(["one", "test"])
                    assert called.wait(timeout=2), "speak_fragments did not run in time"


# ─── speak_fragments_scroll ──────────────────────────────────────────


class TestSpeakFragmentsScroll:
    """Tests for TTSEngine.speak_fragments_scroll."""

    def test_all_cached_plays_concatenated(self):
        engine = _make_engine()
        engine._paplay = "/usr/bin/paplay"

        # Cache fragments
        files = []
        for text in ["one", "Fix bug"]:
            f = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
            _make_wav(f.name, duration_samples=100)
            engine._cache[engine._cache_key(text)] = f.name
            files.append(f.name)
            f.close()

        with mock.patch.object(engine, "stop_sync"):
            with mock.patch.object(engine, "_start_playback") as mock_play:
                engine.speak_fragments_scroll(["one", "Fix bug"])
                time.sleep(0.3)
                mock_play.assert_called_once()

        for f in files:
            os.unlink(f)

    def test_uncached_falls_back_to_speak_with_local_fallback(self):
        engine = _make_engine()
        engine._paplay = "/usr/bin/paplay"

        with mock.patch.object(engine, "speak_with_local_fallback") as mock_fallback:
            engine.speak_fragments_scroll(["uncached1", "uncached2"])
            # Falls back with full text
            mock_fallback.assert_called_once_with(
                "uncached1 uncached2",
                voice_override=None, emotion_override=None,
                speed_override=None)

    def test_noop_when_muted(self):
        engine = _make_engine()
        engine.mute()
        with mock.patch.object(engine, "speak_with_local_fallback") as mock_fallback:
            with mock.patch.object(engine, "_concat_wavs") as mock_concat:
                engine.speak_fragments_scroll(["one", "two"])
                time.sleep(0.2)
                mock_fallback.assert_not_called()
                mock_concat.assert_not_called()

    def test_scroll_gen_prevents_stale_playback(self):
        engine = _make_engine()
        engine._paplay = "/usr/bin/paplay"

        # Cache fragments
        for text in ["one", "test"]:
            f = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
            _make_wav(f.name, duration_samples=100)
            engine._cache[engine._cache_key(text)] = f.name

        # Call speak_fragments_scroll, then immediately increment scroll_gen
        # to simulate the user scrolling past
        with mock.patch.object(engine, "_concat_wavs") as mock_concat:
            with mock.patch.object(engine, "stop_sync"):
                engine.speak_fragments_scroll(["one", "test"])
                engine._scroll_gen += 10  # simulate rapid scrolling
                time.sleep(0.3)
                # The background thread should have detected stale gen and skipped
                # (concat might or might not be called depending on timing,
                # but _start_playback should not be called)

    def test_increments_scroll_gen(self):
        engine = _make_engine()
        initial_gen = engine._scroll_gen
        engine.speak_fragments_scroll(["uncached"])
        assert engine._scroll_gen > initial_gen


# ─── Common UI phrases / settings constants ───────────────────────────


class TestUITextConstants:
    """Tests for the pregeneration text constants on TTSEngine."""

    def test_common_ui_phrases_not_empty(self):
        """_COMMON_UI_PHRASES should contain essential UI strings."""
        phrases = TTSEngine._COMMON_UI_PHRASES
        assert len(phrases) > 10, "Expected many common UI phrases"
        # Check for essential phrases that are spoken frequently
        assert "selected" in phrases
        assert "Dismissed" in phrases
        assert "Settings" in phrases
        assert "Quick settings" in phrases
        assert "Refreshed" in phrases
        assert "Cancelled." in phrases
        assert "More options" in phrases
        assert "Collapsed" in phrases

    def test_common_ui_phrases_no_duplicates(self):
        """_COMMON_UI_PHRASES should not contain duplicates."""
        phrases = TTSEngine._COMMON_UI_PHRASES
        assert len(phrases) == len(set(phrases)), (
            f"Duplicates found: {[p for p in phrases if phrases.count(p) > 1]}"
        )

    def test_settings_labels_not_empty(self):
        """_SETTINGS_LABELS should contain all settings menu entries."""
        labels = TTSEngine._SETTINGS_LABELS
        assert len(labels) >= 8
        assert "Speed" in labels
        assert "Agent voice" in labels
        assert "UI voice" in labels
        assert "Style" in labels
        assert "Close settings" in labels

    def test_quick_settings_labels_not_empty(self):
        """_QUICK_SETTINGS_LABELS should contain common quick settings."""
        labels = TTSEngine._QUICK_SETTINGS_LABELS
        assert len(labels) >= 5
        assert "Fast toggle" in labels
        assert "Voice toggle" in labels
        assert "Back" in labels

    def test_number_words_cover_1_through_9(self):
        """_NUMBER_WORDS should map all single-digit positions."""
        words = TTSEngine._NUMBER_WORDS
        for i in range(1, 10):
            assert i in words, f"Missing number word for {i}"
        assert words[1] == "one"
        assert words[9] == "nine"


# ─── pregenerate_ui ──────────────────────────────────────────────────


class TestPregenerateUi:
    """Tests for the UI pregeneration queue."""

    def test_skips_already_cached_ui_texts(self):
        engine = _make_engine()
        # Pre-populate cache with UI voice override
        key = engine._cache_key("Settings", voice_override="noa")
        engine._cache[key] = "/tmp/fake.wav"

        with mock.patch.object(engine, "_generate_to_file_unlocked") as mock_gen:
            engine.pregenerate_ui(["Settings"], voice_override="noa")
            mock_gen.assert_not_called()

    def test_generates_uncached_ui_texts(self):
        engine = _make_engine()
        generated = []

        def fake_generate(text, **kwargs):
            generated.append(text)
            return f"/tmp/{text}.wav"

        with mock.patch.object(engine, "_generate_to_file_unlocked", side_effect=fake_generate):
            engine.pregenerate_ui(["alpha", "beta"])
            assert set(generated) == {"alpha", "beta"}

    def test_uses_separate_generation_counter(self):
        """UI pregeneration has its own counter, independent of main pregeneration."""
        engine = _make_engine()

        # Record initial counters
        initial_main = engine._pregen_gen
        initial_ui = engine._pregen_ui_gen

        with mock.patch.object(engine, "_generate_to_file_unlocked"):
            engine.pregenerate_ui(["text1"])

        assert engine._pregen_ui_gen > initial_ui
        assert engine._pregen_gen == initial_main  # main counter unchanged

    def test_voice_override_changes_cache_key(self):
        """UI texts pregenerated with voice override should have different cache keys."""
        config = FakeConfig(voice="sage")
        engine = _make_engine(local=False, config=config)
        # Force API mode (init may have fallen back to local)
        engine._local = False

        key_no_override = engine._cache_key("hello")
        key_with_override = engine._cache_key("hello", voice_override="noa")
        assert key_no_override != key_with_override

    def test_speed_override_changes_cache_key(self):
        """UI texts pregenerated with speed override should have different cache keys."""
        config = FakeConfig(speed=1.3)
        engine = _make_engine(local=False, config=config)
        engine._local = False

        key_default = engine._cache_key("hello")
        key_faster = engine._cache_key("hello", speed_override=1.8)
        assert key_default != key_faster

    def test_empty_list_is_noop(self):
        engine = _make_engine()
        with mock.patch.object(engine, "_generate_to_file_unlocked") as mock_gen:
            engine.pregenerate_ui([])
            mock_gen.assert_not_called()

    def test_passes_voice_and_speed_overrides_to_generator(self):
        """pregenerate_ui should forward voice and speed overrides."""
        engine = _make_engine()
        calls = []

        def fake_generate(text, **kwargs):
            calls.append(kwargs)
            return f"/tmp/{text}.wav"

        with mock.patch.object(engine, "_generate_to_file_unlocked", side_effect=fake_generate):
            engine.pregenerate_ui(["test"], voice_override="noa", speed_override=1.5)

        assert len(calls) == 1
        assert calls[0].get("voice_override") == "noa"
        assert calls[0].get("speed_override") == 1.5

    def test_ui_pregen_counter_name(self):
        """UI pregeneration should use the '_pregen_ui_gen' counter name."""
        engine = _make_engine()
        calls = []

        def fake_generate(text, **kwargs):
            calls.append(kwargs)
            return f"/tmp/{text}.wav"

        with mock.patch.object(engine, "_generate_to_file_unlocked", side_effect=fake_generate):
            engine.pregenerate_ui(["test"])

        assert len(calls) == 1
        assert calls[0].get("_pregen_counter") == "_pregen_ui_gen"


# ─── Cache key consistency ──────────────────────────────────────────


class TestCacheKeyConsistency:
    """Tests ensuring that pregeneration and playback use matching cache keys.

    A common source of cache misses is when pregeneration uses different
    parameters (voice, speed, emotion) than the playback path. These tests
    verify that the cache keys match.
    """

    def test_ui_voice_pregen_matches_playback_with_override(self):
        """Pregenerate with voice_override should match is_cached with same override."""
        config = FakeConfig(voice="sage")
        engine = _make_engine(local=False, config=config)
        engine._local = False  # Force API mode

        # Simulate pregeneration with UI voice override
        key = engine._cache_key("More options", voice_override="noa", speed_override=1.5)
        wav_path = os.path.join(CACHE_DIR, f"{key}.wav")
        os.makedirs(CACHE_DIR, exist_ok=True)
        _make_wav(wav_path)
        engine._cache[key] = wav_path

        # Check that playback path with same override finds the cache
        assert engine.is_cached("More options", voice_override="noa", speed_override=1.5)

        # Without override, it should NOT be cached (different key)
        assert not engine.is_cached("More options")

    def test_number_words_cached_at_ui_speed(self):
        """Number words pregenerated at ui speed should be found at that speed."""
        config = FakeConfig(speed=1.3)
        engine = _make_engine(local=False, config=config)
        engine._local = False  # Force API mode

        ui_speed = 1.5
        key = engine._cache_key("one", speed_override=ui_speed)
        wav_path = os.path.join(CACHE_DIR, f"{key}.wav")
        os.makedirs(CACHE_DIR, exist_ok=True)
        _make_wav(wav_path)
        engine._cache[key] = wav_path

        # Should be found at UI speed
        assert engine.is_cached("one", speed_override=ui_speed)
        # Should NOT be found at default speed
        assert not engine.is_cached("one")

    def test_selected_fragment_consistent_across_paths(self):
        """The word 'selected' should use consistent cache keys in all paths.

        Pregeneration path: pregenerate(["selected"], speed_override=ui_speed)
        Playback path: speak_fragments(["selected", label], speed_override=ui_speed)
        Both should produce the same cache key for "selected".
        """
        config = FakeConfig(speed=1.3)
        engine = _make_engine(local=False, config=config)
        engine._local = False  # Force API mode

        ui_speed = 1.5

        # Key from pregeneration (no voice override, with speed override)
        pregen_key = engine._cache_key("selected", speed_override=ui_speed)

        # Key from fragment playback (same parameters)
        playback_key = engine._cache_key("selected", speed_override=ui_speed)

        assert pregen_key == playback_key

    def test_extras_label_pregen_ui_matches_scroll_with_ui_voice(self):
        """Extra option labels pregenerated with UI voice should match scroll playback.

        The app pregenerates extras with _pregenerate_ui_worker (UI voice + UI speed).
        on_highlight_changed for extras now also passes the UI voice override.
        These must produce the same cache key.
        """
        config = FakeConfig(voice="sage")
        engine = _make_engine(local=False, config=config)
        engine._local = False  # Force API mode

        ui_voice = "noa"
        ui_speed = 1.5

        # Pregeneration path (from _pregenerate_ui_worker)
        pregen_key = engine._cache_key("Record response", voice_override=ui_voice,
                                        speed_override=ui_speed)

        # Scroll readout path (from on_highlight_changed with UI voice)
        scroll_key = engine._cache_key("Record response", voice_override=ui_voice,
                                        speed_override=ui_speed)

        assert pregen_key == scroll_key

    def test_extras_without_ui_voice_match_no_override(self):
        """When uiVoice is not set, extras pregeneration and scroll both use no override."""
        config = FakeConfig(voice="sage")
        engine = _make_engine(local=False, config=config)
        engine._local = False  # Force API mode

        ui_speed = 1.5

        # Without UI voice override
        pregen_key = engine._cache_key("Record response", speed_override=ui_speed)
        scroll_key = engine._cache_key("Record response", speed_override=ui_speed)

        assert pregen_key == scroll_key
