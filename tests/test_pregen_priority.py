"""Tests for priority pregeneration in TTSEngine.

Covers:
- Priority pregeneration calls _generate_to_file_unlocked for first 3 items
- Already cached items are skipped
- Remaining items are queued in background via pregenerate()
- Priority count is configurable
"""

from __future__ import annotations

import threading
import unittest.mock as mock

import pytest

from io_mcp.tts import TTSEngine


# ─── Helpers ─────────────────────────────────────────────────────────


def _make_engine(**kwargs) -> TTSEngine:
    """Create a TTSEngine with all binaries stubbed to None."""
    defaults = dict(local=True, speed=1.0, config=None)
    defaults.update(kwargs)
    with mock.patch("io_mcp.tts._find_binary", return_value=None):
        engine = TTSEngine(**defaults)
    return engine


# ─── pregenerate_priority ─────────────────────────────────────────────


class TestPregeneratePriority:
    """Tests for the priority pregeneration method."""

    def test_generates_first_3_synchronously(self):
        """Priority items (first 3) should be generated via _generate_to_file_unlocked."""
        engine = _make_engine()
        generated = []

        def fake_generate(text, **kwargs):
            generated.append(text)
            return f"/tmp/{text}.wav"

        with mock.patch.object(engine, "_generate_to_file_unlocked", side_effect=fake_generate):
            with mock.patch.object(engine, "pregenerate") as mock_pregen:
                engine.pregenerate_priority(
                    ["alpha", "beta", "gamma", "delta", "epsilon"],
                    priority_count=3,
                )

        # First 3 should have been generated directly
        assert generated == ["alpha", "beta", "gamma"]

    def test_remaining_items_queued_via_pregenerate(self):
        """Items beyond priority_count should be passed to pregenerate()."""
        engine = _make_engine()

        def fake_generate(text, **kwargs):
            return f"/tmp/{text}.wav"

        with mock.patch.object(engine, "_generate_to_file_unlocked", side_effect=fake_generate):
            with mock.patch.object(engine, "pregenerate") as mock_pregen:
                engine.pregenerate_priority(
                    ["alpha", "beta", "gamma", "delta", "epsilon"],
                    priority_count=3,
                )

        # pregenerate() should have been called with the remaining items
        mock_pregen.assert_called_once()
        remaining = mock_pregen.call_args[0][0]
        assert set(remaining) == {"delta", "epsilon"}

    def test_skips_cached_priority_items(self):
        """Already cached items in the priority range should be skipped."""
        engine = _make_engine()

        # Pre-cache "beta" — is_cached checks both the cache dict and os.path.isfile.
        # For local mode engines _cache_key is simple, so we populate the dict
        # and mock isfile to return True only for the cached path.
        key = engine._cache_key("beta")
        engine._cache[key] = "/tmp/beta.wav"

        generated = []

        def fake_generate(text, **kwargs):
            generated.append(text)
            return f"/tmp/{text}.wav"

        def fake_isfile(path):
            return path == "/tmp/beta.wav"

        with mock.patch("os.path.isfile", side_effect=fake_isfile):
            with mock.patch.object(engine, "_generate_to_file_unlocked", side_effect=fake_generate):
                with mock.patch.object(engine, "pregenerate"):
                    engine.pregenerate_priority(
                        ["alpha", "beta", "gamma"],
                        priority_count=3,
                    )

        # "beta" should be skipped since it's cached
        assert "beta" not in generated
        assert "alpha" in generated
        assert "gamma" in generated

    def test_skips_cached_remaining_items(self):
        """Already cached items in the remaining range should not be passed to pregenerate()."""
        engine = _make_engine()

        # Pre-cache "delta"
        key = engine._cache_key("delta")
        engine._cache[key] = "/tmp/delta.wav"

        def fake_generate(text, **kwargs):
            return f"/tmp/{text}.wav"

        def fake_isfile(path):
            return path == "/tmp/delta.wav"

        with mock.patch("os.path.isfile", side_effect=fake_isfile):
            with mock.patch.object(engine, "_generate_to_file_unlocked", side_effect=fake_generate):
                with mock.patch.object(engine, "pregenerate") as mock_pregen:
                    engine.pregenerate_priority(
                        ["alpha", "beta", "gamma", "delta", "epsilon"],
                        priority_count=3,
                    )

        # pregenerate() should only get uncached remaining items
        remaining = mock_pregen.call_args[0][0]
        assert "delta" not in remaining
        assert "epsilon" in remaining

    def test_all_remaining_cached_skips_pregenerate(self):
        """If all remaining items are cached, pregenerate() should not be called."""
        engine = _make_engine()

        # Pre-cache all remaining items
        cached_paths = set()
        for text in ["delta", "epsilon"]:
            key = engine._cache_key(text)
            path = f"/tmp/{text}.wav"
            engine._cache[key] = path
            cached_paths.add(path)

        def fake_generate(text, **kwargs):
            return f"/tmp/{text}.wav"

        def fake_isfile(path):
            return path in cached_paths

        with mock.patch("os.path.isfile", side_effect=fake_isfile):
            with mock.patch.object(engine, "_generate_to_file_unlocked", side_effect=fake_generate):
                with mock.patch.object(engine, "pregenerate") as mock_pregen:
                    engine.pregenerate_priority(
                        ["alpha", "beta", "gamma", "delta", "epsilon"],
                        priority_count=3,
                    )

        # pregenerate() should NOT be called since all remaining are cached
        mock_pregen.assert_not_called()

    def test_configurable_priority_count(self):
        """priority_count parameter controls how many items get priority treatment."""
        engine = _make_engine()
        generated = []

        def fake_generate(text, **kwargs):
            generated.append(text)
            return f"/tmp/{text}.wav"

        with mock.patch.object(engine, "_generate_to_file_unlocked", side_effect=fake_generate):
            with mock.patch.object(engine, "pregenerate") as mock_pregen:
                engine.pregenerate_priority(
                    ["a", "b", "c", "d", "e"],
                    priority_count=2,
                )

        # Only first 2 should be generated synchronously
        assert generated == ["a", "b"]
        # Remaining 3 should go to pregenerate()
        remaining = mock_pregen.call_args[0][0]
        assert set(remaining) == {"c", "d", "e"}

    def test_priority_count_larger_than_list(self):
        """If priority_count >= len(texts), all items are priority-generated."""
        engine = _make_engine()
        generated = []

        def fake_generate(text, **kwargs):
            generated.append(text)
            return f"/tmp/{text}.wav"

        with mock.patch.object(engine, "_generate_to_file_unlocked", side_effect=fake_generate):
            with mock.patch.object(engine, "pregenerate") as mock_pregen:
                engine.pregenerate_priority(
                    ["a", "b"],
                    priority_count=5,
                )

        # Both items generated synchronously
        assert generated == ["a", "b"]
        # No remaining items, so pregenerate() should not be called
        mock_pregen.assert_not_called()

    def test_empty_list_is_noop(self):
        """Empty text list should be a no-op."""
        engine = _make_engine()

        with mock.patch.object(engine, "_generate_to_file_unlocked") as mock_gen:
            with mock.patch.object(engine, "pregenerate") as mock_pregen:
                engine.pregenerate_priority([])

        mock_gen.assert_not_called()
        mock_pregen.assert_not_called()

    def test_speed_override_passed_through(self):
        """speed_override should be forwarded to both priority and background generation."""
        engine = _make_engine()
        calls = []

        def fake_generate(text, **kwargs):
            calls.append(kwargs)
            return f"/tmp/{text}.wav"

        with mock.patch.object(engine, "_generate_to_file_unlocked", side_effect=fake_generate):
            with mock.patch.object(engine, "pregenerate") as mock_pregen:
                engine.pregenerate_priority(
                    ["a", "b", "c", "d"],
                    priority_count=2,
                    speed_override=1.5,
                )

        # Priority items should get speed_override
        for call_kwargs in calls:
            assert call_kwargs.get("speed_override") == 1.5

        # Background pregenerate() should also get speed_override
        assert mock_pregen.call_args[1].get("speed_override") == 1.5

    def test_increments_pregen_gen(self):
        """pregenerate_priority should increment the generation counter."""
        engine = _make_engine()
        initial_gen = engine._pregen_gen

        def fake_generate(text, **kwargs):
            return f"/tmp/{text}.wav"

        with mock.patch.object(engine, "_generate_to_file_unlocked", side_effect=fake_generate):
            with mock.patch.object(engine, "pregenerate"):
                engine.pregenerate_priority(["a", "b"])

        assert engine._pregen_gen > initial_gen

    def test_staleness_check_during_priority(self):
        """If _pregen_gen is bumped mid-generation, remaining priority items should be skipped."""
        engine = _make_engine()
        generated = []

        def fake_generate(text, **kwargs):
            # After first item, simulate a newer pregenerate call
            if len(generated) == 1:
                engine._pregen_gen += 1
            generated.append(text)
            return f"/tmp/{text}.wav"

        with mock.patch.object(engine, "_generate_to_file_unlocked", side_effect=fake_generate):
            with mock.patch.object(engine, "pregenerate"):
                engine.pregenerate_priority(
                    ["a", "b", "c"],
                    priority_count=3,
                )

        # "a" is generated first. Inside fake_generate for "b",
        # len(generated)==1 triggers the counter bump. But the staleness
        # check happens BEFORE calling _generate_to_file_unlocked, so "b"
        # is already being generated when the counter bumps. "c" is skipped
        # because the check fires before its generation.
        assert "a" in generated
        assert "b" in generated
        assert "c" not in generated
        assert len(generated) == 2  # "a" and "b", but not "c"
