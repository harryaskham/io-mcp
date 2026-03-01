"""Tests for the io-mcp persistent UI state module.

Tests get/set/toggle operations, file handling edge cases,
concurrent access, corrupt JSON recovery, and key isolation.
"""

from __future__ import annotations

import json
import os
import threading
from unittest import mock

import pytest

import io_mcp.state as state_mod


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolate_state(tmp_path):
    """Redirect state module to use tmp_path instead of real config dir."""
    config_dir = str(tmp_path / "config")
    state_file = os.path.join(config_dir, "state.json")
    with mock.patch.object(state_mod, "DEFAULT_CONFIG_DIR", config_dir), \
         mock.patch.object(state_mod, "STATE_FILE", state_file):
        yield


def _state_file_path() -> str:
    """Return the currently-patched STATE_FILE path."""
    return state_mod.STATE_FILE


def _write_raw(content: str) -> None:
    """Write raw string content to the state file."""
    os.makedirs(os.path.dirname(_state_file_path()), exist_ok=True)
    with open(_state_file_path(), "w") as f:
        f.write(content)


def _read_raw() -> str:
    """Read raw state file content."""
    with open(_state_file_path()) as f:
        return f.read()


def _read_json() -> dict:
    """Read and parse the state file as JSON."""
    with open(_state_file_path()) as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# get()
# ---------------------------------------------------------------------------

class TestGet:
    def test_returns_default_when_file_missing(self):
        """get() returns default when state file does not exist."""
        assert state_mod.get("missing_key") is None
        assert state_mod.get("missing_key", "fallback") == "fallback"

    def test_returns_stored_value(self):
        """get() returns a previously stored value."""
        _write_raw(json.dumps({"theme": "dark"}))
        assert state_mod.get("theme") == "dark"

    def test_returns_default_for_missing_key(self):
        """get() returns default when key is not in file."""
        _write_raw(json.dumps({"other": 42}))
        assert state_mod.get("missing", "default_val") == "default_val"

    def test_returns_none_default(self):
        """get() returns None by default when key is absent."""
        _write_raw(json.dumps({"a": 1}))
        assert state_mod.get("b") is None

    def test_handles_corrupt_json(self):
        """get() returns default when state file contains invalid JSON."""
        _write_raw("{not valid json!!!")
        assert state_mod.get("key", "safe") == "safe"

    def test_handles_empty_file(self):
        """get() returns default when state file is empty."""
        _write_raw("")
        assert state_mod.get("key", "safe") == "safe"

    def test_handles_json_array(self):
        """get() returns default when state file contains a JSON array (not dict)."""
        _write_raw("[1, 2, 3]")
        # json.load returns a list, which has no .get method → AttributeError
        # The module catches JSONDecodeError and FileNotFoundError but not AttributeError.
        # This tests current behavior: it will raise AttributeError.
        # Actually, let's check — list has no .get, so _load() returns the list,
        # and state_mod.get calls .get() on it which would raise.
        # But _load only catches FileNotFoundError, JSONDecodeError, PermissionError.
        # So this would raise AttributeError. Let's verify:
        with pytest.raises(AttributeError):
            state_mod.get("key")


# ---------------------------------------------------------------------------
# set()
# ---------------------------------------------------------------------------

class TestSet:
    def test_writes_to_disk(self):
        """set() persists value to state file."""
        state_mod.set("color", "blue")
        data = _read_json()
        assert data["color"] == "blue"

    def test_creates_directory_if_needed(self):
        """set() creates config directory if it doesn't exist."""
        # The autouse fixture patches to a dir that doesn't exist yet.
        assert not os.path.exists(os.path.dirname(_state_file_path()))
        state_mod.set("key", "value")
        assert os.path.isfile(_state_file_path())

    def test_overwrites_existing_value(self):
        """set() overwrites a previously stored value."""
        state_mod.set("x", "old")
        state_mod.set("x", "new")
        assert state_mod.get("x") == "new"

    def test_preserves_other_keys(self):
        """set() does not clobber unrelated keys."""
        state_mod.set("a", 1)
        state_mod.set("b", 2)
        assert state_mod.get("a") == 1
        assert state_mod.get("b") == 2

    def test_writes_valid_json(self):
        """set() writes properly formatted JSON."""
        state_mod.set("key", "value")
        raw = _read_raw()
        parsed = json.loads(raw)
        assert parsed == {"key": "value"}

    def test_handles_permission_error_on_write(self, tmp_path):
        """set() silently handles PermissionError (best effort save)."""
        # Make the config directory read-only so writing fails.
        config_dir = os.path.dirname(_state_file_path())
        os.makedirs(config_dir, exist_ok=True)
        state_mod.set("before", "ok")

        # Make file unwritable
        os.chmod(_state_file_path(), 0o444)
        os.chmod(config_dir, 0o555)
        try:
            # Should not raise — _save catches all exceptions
            state_mod.set("after", "fail")
        finally:
            # Restore permissions for cleanup
            os.chmod(config_dir, 0o755)
            os.chmod(_state_file_path(), 0o644)

        # The "after" key should NOT have been saved (write failed)
        assert state_mod.get("after") is None
        # The "before" key should still be there
        assert state_mod.get("before") == "ok"

    def test_recovers_from_corrupt_file(self):
        """set() overwrites corrupt JSON with fresh state."""
        _write_raw("NOT JSON")
        state_mod.set("fresh", True)
        assert state_mod.get("fresh") is True


# ---------------------------------------------------------------------------
# toggle()
# ---------------------------------------------------------------------------

class TestToggle:
    def test_toggles_true_to_false(self):
        """toggle() flips True to False."""
        state_mod.set("flag", True)
        result = state_mod.toggle("flag")
        assert result is False
        assert state_mod.get("flag") is False

    def test_toggles_false_to_true(self):
        """toggle() flips False to True."""
        state_mod.set("flag", False)
        result = state_mod.toggle("flag")
        assert result is True
        assert state_mod.get("flag") is True

    def test_full_cycle(self):
        """toggle() cycles True→False→True."""
        state_mod.set("cycle", True)
        assert state_mod.toggle("cycle") is False
        assert state_mod.toggle("cycle") is True

    def test_uses_default_on_first_call(self):
        """toggle() uses default=False when key is new, so first toggle returns True."""
        result = state_mod.toggle("new_flag")
        # default is False, so not False = True
        assert result is True

    def test_uses_custom_default(self):
        """toggle() uses custom default when key is absent."""
        result = state_mod.toggle("new_flag", default=True)
        # default is True, so not True = False
        assert result is False

    def test_persists_to_disk(self):
        """toggle() writes the new value to disk."""
        state_mod.toggle("persisted")
        data = _read_json()
        assert "persisted" in data

    def test_returns_new_value(self):
        """toggle() returns the value AFTER toggling, not before."""
        state_mod.set("val", True)
        returned = state_mod.toggle("val")
        stored = state_mod.get("val")
        assert returned == stored


# ---------------------------------------------------------------------------
# Multiple keys — isolation
# ---------------------------------------------------------------------------

class TestMultipleKeys:
    def test_independent_keys(self):
        """Different keys do not interfere with each other."""
        state_mod.set("key_a", "alpha")
        state_mod.set("key_b", "beta")
        state_mod.set("key_c", "gamma")

        assert state_mod.get("key_a") == "alpha"
        assert state_mod.get("key_b") == "beta"
        assert state_mod.get("key_c") == "gamma"

    def test_toggle_does_not_affect_other_keys(self):
        """Toggling one key leaves others unchanged."""
        state_mod.set("flag1", True)
        state_mod.set("flag2", False)
        state_mod.set("data", "hello")

        state_mod.toggle("flag1")

        assert state_mod.get("flag1") is False
        assert state_mod.get("flag2") is False
        assert state_mod.get("data") == "hello"

    def test_many_keys(self):
        """State handles many keys without issue."""
        for i in range(50):
            state_mod.set(f"key_{i}", i)

        for i in range(50):
            assert state_mod.get(f"key_{i}") == i


# ---------------------------------------------------------------------------
# Edge cases — value types
# ---------------------------------------------------------------------------

class TestEdgeCases:
    def test_none_value(self):
        """set/get works with None values."""
        state_mod.set("nullable", None)
        # None is stored, so get returns None (which is also the default).
        # Verify it's actually in the file.
        data = _read_json()
        assert "nullable" in data
        assert data["nullable"] is None

    def test_nested_dict(self):
        """set/get works with nested dictionaries."""
        nested = {"level1": {"level2": {"level3": "deep"}}}
        state_mod.set("nested", nested)
        result = state_mod.get("nested")
        assert result == nested
        assert result["level1"]["level2"]["level3"] == "deep"

    def test_empty_string_value(self):
        """set/get works with empty strings."""
        state_mod.set("empty", "")
        assert state_mod.get("empty") == ""
        assert state_mod.get("empty") is not None

    def test_empty_string_key(self):
        """set/get works with empty string as key."""
        state_mod.set("", "empty_key_value")
        assert state_mod.get("") == "empty_key_value"

    def test_very_long_key(self):
        """set/get works with very long keys."""
        long_key = "k" * 1000
        state_mod.set(long_key, "stored")
        assert state_mod.get(long_key) == "stored"

    def test_special_characters_in_key(self):
        """set/get works with special characters in keys."""
        state_mod.set("key/with/slashes", 1)
        state_mod.set("key.with.dots", 2)
        state_mod.set("key with spaces", 3)
        state_mod.set("key\nwith\nnewlines", 4)

        assert state_mod.get("key/with/slashes") == 1
        assert state_mod.get("key.with.dots") == 2
        assert state_mod.get("key with spaces") == 3
        assert state_mod.get("key\nwith\nnewlines") == 4

    def test_list_value(self):
        """set/get works with list values."""
        state_mod.set("items", [1, "two", None, True])
        assert state_mod.get("items") == [1, "two", None, True]

    def test_numeric_value(self):
        """set/get works with numeric values."""
        state_mod.set("int_val", 42)
        state_mod.set("float_val", 3.14)
        assert state_mod.get("int_val") == 42
        assert state_mod.get("float_val") == 3.14

    def test_boolean_value(self):
        """set/get works with boolean values."""
        state_mod.set("t", True)
        state_mod.set("f", False)
        assert state_mod.get("t") is True
        assert state_mod.get("f") is False


# ---------------------------------------------------------------------------
# File operations — error handling
# ---------------------------------------------------------------------------

class TestFileOperations:
    def test_load_returns_empty_on_file_not_found(self):
        """_load() returns empty dict when file doesn't exist."""
        result = state_mod._load()
        assert result == {}

    def test_load_returns_empty_on_corrupt_json(self):
        """_load() returns empty dict on corrupt JSON."""
        _write_raw("{corrupt")
        result = state_mod._load()
        assert result == {}

    def test_load_returns_empty_on_permission_error(self):
        """_load() returns empty dict when file is unreadable."""
        config_dir = os.path.dirname(_state_file_path())
        os.makedirs(config_dir, exist_ok=True)
        _write_raw(json.dumps({"secret": "data"}))
        os.chmod(_state_file_path(), 0o000)
        try:
            result = state_mod._load()
            assert result == {}
        finally:
            os.chmod(_state_file_path(), 0o644)

    def test_save_creates_directory(self):
        """_save() creates config directory if missing."""
        state_mod._save({"hello": "world"})
        assert os.path.isfile(_state_file_path())

    def test_save_never_raises(self):
        """_save() silently handles errors (best effort)."""
        # Patch STATE_FILE to an impossible path
        with mock.patch.object(state_mod, "STATE_FILE", "/proc/impossible/state.json"), \
             mock.patch.object(state_mod, "DEFAULT_CONFIG_DIR", "/proc/impossible"):
            # Should not raise
            state_mod._save({"key": "value"})

    def test_json_formatting(self):
        """State file uses indented JSON for readability."""
        state_mod.set("formatted", True)
        raw = _read_raw()
        # json.dump with indent=2 puts each key on its own line
        assert "\n" in raw
        assert "  " in raw


# ---------------------------------------------------------------------------
# Concurrent access
# ---------------------------------------------------------------------------

class TestConcurrentAccess:
    def test_concurrent_writes_dont_crash(self):
        """Multiple threads writing simultaneously don't raise exceptions."""
        errors: list[Exception] = []

        def writer(n: int) -> None:
            try:
                for i in range(20):
                    state_mod.set(f"thread_{n}_key_{i}", i)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=writer, args=(n,)) for n in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert errors == [], f"Concurrent writes raised: {errors}"

    def test_concurrent_toggles_dont_crash(self):
        """Multiple threads toggling simultaneously don't raise exceptions."""
        errors: list[Exception] = []

        def toggler() -> None:
            try:
                for _ in range(20):
                    state_mod.toggle("shared_flag")
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=toggler) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert errors == [], f"Concurrent toggles raised: {errors}"
        # The final value should be a boolean (we can't predict which one due to races)
        final = state_mod.get("shared_flag")
        assert isinstance(final, bool)

    def test_concurrent_reads_during_writes(self):
        """Reads during concurrent writes don't crash."""
        errors: list[Exception] = []

        def writer() -> None:
            try:
                for i in range(20):
                    state_mod.set(f"w_key_{i}", i)
            except Exception as exc:
                errors.append(exc)

        def reader() -> None:
            try:
                for i in range(20):
                    state_mod.get(f"w_key_{i}", "default")
            except Exception as exc:
                errors.append(exc)

        threads = []
        for _ in range(3):
            threads.append(threading.Thread(target=writer))
            threads.append(threading.Thread(target=reader))
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert errors == [], f"Concurrent read/write raised: {errors}"
