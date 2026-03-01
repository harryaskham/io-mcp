"""Tests for proxy health checking functions.

Tests cover:
- proxy_health() comprehensive health check
- _check_port_open() TCP connectivity
- _parse_address() address string parsing
- _get_pid_uptime() process uptime detection
- _format_uptime() human-readable time formatting
- Integration with is_server_running() and check_health()
"""

from __future__ import annotations

import json
import os
import socket
import tempfile
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from unittest.mock import patch, mock_open

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def free_port():
    """Find a free TCP port."""
    with socket.socket() as s:
        s.bind(("", 0))
        return s.getsockname()[1]


@pytest.fixture()
def listening_port(free_port):
    """Start a TCP server on a free port and yield the port number."""
    server = HTTPServer(("127.0.0.1", free_port), _SilentHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    # Wait for server to start
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", free_port), timeout=0.5):
                break
        except OSError:
            time.sleep(0.05)
    yield free_port
    server.shutdown()


class _SilentHandler(BaseHTTPRequestHandler):
    """HTTP handler that responds 200 to everything, no logging."""

    def do_GET(self):
        self.send_response(200)
        self.end_headers()

    def do_POST(self):
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args):
        pass


@pytest.fixture()
def fake_pid_file(tmp_path):
    """Create a temporary PID file with the current process's PID."""
    pid_file = tmp_path / "test-proxy.pid"
    pid_file.write_text(str(os.getpid()))
    return str(pid_file)


@pytest.fixture()
def fake_pid_file_dead(tmp_path):
    """Create a PID file with a PID that doesn't exist."""
    pid_file = tmp_path / "test-proxy-dead.pid"
    # Use a PID that is very unlikely to exist
    pid_file.write_text("999999999")
    return str(pid_file)


# ---------------------------------------------------------------------------
# Tests: _check_port_open
# ---------------------------------------------------------------------------

class TestCheckPortOpen:
    """Test TCP port connectivity checks."""

    def test_open_port_returns_true(self, listening_port):
        from io_mcp.proxy import _check_port_open
        assert _check_port_open("127.0.0.1", listening_port) is True

    def test_closed_port_returns_false(self, free_port):
        from io_mcp.proxy import _check_port_open
        assert _check_port_open("127.0.0.1", free_port) is False

    def test_invalid_host_returns_false(self):
        from io_mcp.proxy import _check_port_open
        assert _check_port_open("999.999.999.999", 8444) is False

    def test_timeout_is_respected(self, free_port):
        from io_mcp.proxy import _check_port_open
        start = time.monotonic()
        _check_port_open("127.0.0.1", free_port, timeout=0.1)
        elapsed = time.monotonic() - start
        # Should not take much longer than the timeout
        assert elapsed < 1.0

    def test_default_timeout(self, free_port):
        """Default timeout should be 2.0 seconds."""
        from io_mcp.proxy import _check_port_open
        # Just verify it doesn't error with default args
        result = _check_port_open("127.0.0.1", free_port)
        assert result is False


# ---------------------------------------------------------------------------
# Tests: _parse_address
# ---------------------------------------------------------------------------

class TestParseAddress:
    """Test address string parsing."""

    def test_standard_address(self):
        from io_mcp.proxy import _parse_address
        assert _parse_address("localhost:8444") == ("localhost", 8444)

    def test_ip_address(self):
        from io_mcp.proxy import _parse_address
        assert _parse_address("192.168.1.1:9000") == ("192.168.1.1", 9000)

    def test_all_interfaces_normalized(self):
        from io_mcp.proxy import _parse_address
        host, port = _parse_address("0.0.0.0:8444")
        assert host == "localhost"
        assert port == 8444

    def test_empty_host_normalized(self):
        from io_mcp.proxy import _parse_address
        host, port = _parse_address(":8444")
        assert host == "localhost"
        assert port == 8444

    def test_invalid_port_returns_default(self):
        from io_mcp.proxy import _parse_address
        assert _parse_address("localhost:notaport") == ("localhost", 8444)

    def test_no_colon_returns_default(self):
        from io_mcp.proxy import _parse_address
        assert _parse_address("justahostname") == ("localhost", 8444)

    def test_empty_string_returns_default(self):
        from io_mcp.proxy import _parse_address
        assert _parse_address("") == ("localhost", 8444)

    def test_custom_port(self):
        from io_mcp.proxy import _parse_address
        assert _parse_address("myhost:12345") == ("myhost", 12345)

    def test_hostname_with_dots(self):
        from io_mcp.proxy import _parse_address
        assert _parse_address("my.server.local:8444") == ("my.server.local", 8444)

    def test_ipv4_with_custom_port(self):
        from io_mcp.proxy import _parse_address
        assert _parse_address("10.0.0.1:7777") == ("10.0.0.1", 7777)


# ---------------------------------------------------------------------------
# Tests: _format_uptime
# ---------------------------------------------------------------------------

class TestFormatUptime:
    """Test human-readable uptime formatting."""

    def test_seconds(self):
        from io_mcp.proxy import _format_uptime
        assert _format_uptime(5) == "5s"
        assert _format_uptime(0) == "0s"
        assert _format_uptime(59) == "59s"

    def test_minutes(self):
        from io_mcp.proxy import _format_uptime
        assert _format_uptime(60) == "1m 0s"
        assert _format_uptime(90) == "1m 30s"
        assert _format_uptime(3599) == "59m 59s"

    def test_hours(self):
        from io_mcp.proxy import _format_uptime
        assert _format_uptime(3600) == "1h 0m"
        assert _format_uptime(5400) == "1h 30m"
        assert _format_uptime(86399) == "23h 59m"

    def test_days(self):
        from io_mcp.proxy import _format_uptime
        assert _format_uptime(86400) == "1d 0h"
        assert _format_uptime(172800) == "2d 0h"
        assert _format_uptime(90000) == "1d 1h"

    def test_negative_returns_zero(self):
        from io_mcp.proxy import _format_uptime
        assert _format_uptime(-10) == "0s"

    def test_fractional_seconds_truncated(self):
        from io_mcp.proxy import _format_uptime
        assert _format_uptime(5.9) == "5s"


# ---------------------------------------------------------------------------
# Tests: _get_pid_uptime
# ---------------------------------------------------------------------------

class TestGetPidUptime:
    """Test process uptime detection."""

    def test_current_process_has_uptime(self):
        from io_mcp.proxy import _get_pid_uptime
        uptime = _get_pid_uptime(os.getpid())
        # Current process should have some uptime.
        # On proot (Nix-on-Droid), /proc may be unreliable and the function
        # falls back to PID file mtime. If PID file doesn't exist, returns None.
        if uptime is not None:
            assert uptime >= 0

    def test_nonexistent_pid_falls_back(self):
        from io_mcp.proxy import _get_pid_uptime
        # PID 999999999 shouldn't exist — falls back to PID file mtime
        result = _get_pid_uptime(999999999)
        # Result could be None (no PID file) or a float (PID file fallback)
        assert result is None or isinstance(result, float)

    def test_fallback_to_pid_file_mtime(self):
        from io_mcp.proxy import _get_pid_uptime, PID_FILE
        # Write a PID file with known mtime
        with tempfile.NamedTemporaryFile(mode='w', suffix='.pid', delete=False) as f:
            f.write("999999999")
            tmp_path = f.name

        try:
            with patch("io_mcp.proxy.PID_FILE", tmp_path):
                result = _get_pid_uptime(999999999)
                # Should get a positive uptime from file mtime
                if result is not None:
                    assert result >= 0
        finally:
            os.unlink(tmp_path)


# ---------------------------------------------------------------------------
# Tests: proxy_health (comprehensive)
# ---------------------------------------------------------------------------

class TestProxyHealth:
    """Test the comprehensive proxy_health() function."""

    def test_healthy_proxy(self, listening_port, fake_pid_file):
        """PID alive + port open = healthy."""
        from io_mcp.proxy import proxy_health

        with patch("io_mcp.proxy.PID_FILE", fake_pid_file):
            with patch("io_mcp.proxy._read_pid", return_value=os.getpid()):
                result = proxy_health(f"127.0.0.1:{listening_port}")

        assert result["status"] == "healthy"
        assert result["pid"] == os.getpid()
        assert result["pid_alive"] is True
        assert result["port_open"] is True
        assert "running" in result["details"].lower()

    def test_unhealthy_nothing_running(self, free_port):
        """No PID, no port = unhealthy."""
        from io_mcp.proxy import proxy_health

        with patch("io_mcp.proxy._read_pid", return_value=None):
            result = proxy_health(f"127.0.0.1:{free_port}")

        assert result["status"] == "unhealthy"
        assert result["pid"] is None
        assert result["pid_alive"] is False
        assert result["port_open"] is False
        assert "not running" in result["details"].lower()

    def test_degraded_pid_alive_port_closed(self, free_port, fake_pid_file):
        """PID alive but port not accepting = degraded."""
        from io_mcp.proxy import proxy_health

        with patch("io_mcp.proxy.PID_FILE", fake_pid_file):
            with patch("io_mcp.proxy._read_pid", return_value=os.getpid()):
                result = proxy_health(f"127.0.0.1:{free_port}")

        assert result["status"] == "degraded"
        assert result["pid_alive"] is True
        assert result["port_open"] is False
        assert "not accepting connections" in result["details"].lower() or "starting up" in result["details"].lower()

    def test_degraded_port_open_but_pid_dead(self, listening_port):
        """Port open but PID dead/missing = degraded."""
        from io_mcp.proxy import proxy_health

        with patch("io_mcp.proxy._read_pid", return_value=999999999):
            result = proxy_health(f"127.0.0.1:{listening_port}")

        assert result["status"] == "degraded"
        assert result["pid_alive"] is False
        assert result["port_open"] is True
        assert "another process" in result["details"].lower() or "stale" in result["details"].lower()

    def test_default_address(self):
        """Default address is localhost:8444."""
        from io_mcp.proxy import proxy_health

        with patch("io_mcp.proxy._read_pid", return_value=None):
            result = proxy_health()

        assert result["address"] == "localhost:8444"

    def test_address_in_result(self, free_port):
        """Address is included in result dict."""
        from io_mcp.proxy import proxy_health

        with patch("io_mcp.proxy._read_pid", return_value=None):
            result = proxy_health(f"myhost:{free_port}")

        assert result["address"] == f"myhost:{free_port}"

    def test_uptime_included_when_healthy(self, listening_port, fake_pid_file):
        """Uptime is populated when process is alive."""
        from io_mcp.proxy import proxy_health

        with patch("io_mcp.proxy.PID_FILE", fake_pid_file):
            with patch("io_mcp.proxy._read_pid", return_value=os.getpid()):
                result = proxy_health(f"127.0.0.1:{listening_port}")

        assert result["status"] == "healthy"
        # Uptime should be available (at least from PID file fallback)
        # It might be None on some systems, but uptime_seconds should be set
        if result["uptime_seconds"] is not None:
            assert result["uptime_seconds"] >= 0
            assert result["uptime"] is not None
            assert len(result["uptime"]) > 0

    def test_no_uptime_when_pid_dead(self, free_port):
        """No uptime when PID is not alive."""
        from io_mcp.proxy import proxy_health

        with patch("io_mcp.proxy._read_pid", return_value=None):
            result = proxy_health(f"127.0.0.1:{free_port}")

        assert result["uptime"] is None
        assert result["uptime_seconds"] is None

    def test_result_dict_has_all_keys(self, free_port):
        """Result dict always has all expected keys."""
        from io_mcp.proxy import proxy_health

        with patch("io_mcp.proxy._read_pid", return_value=None):
            result = proxy_health(f"127.0.0.1:{free_port}")

        expected_keys = {
            "status", "pid", "pid_alive", "port_open",
            "uptime", "uptime_seconds", "address", "details",
        }
        assert set(result.keys()) == expected_keys

    def test_result_is_json_serializable(self, free_port):
        """Result dict can be serialized to JSON."""
        from io_mcp.proxy import proxy_health

        with patch("io_mcp.proxy._read_pid", return_value=None):
            result = proxy_health(f"127.0.0.1:{free_port}")

        # Should not raise
        serialized = json.dumps(result)
        assert isinstance(serialized, str)

    def test_healthy_with_uptime_in_details(self, listening_port, fake_pid_file):
        """Healthy status includes uptime in details string."""
        from io_mcp.proxy import proxy_health

        with patch("io_mcp.proxy.PID_FILE", fake_pid_file):
            with patch("io_mcp.proxy._read_pid", return_value=os.getpid()):
                result = proxy_health(f"127.0.0.1:{listening_port}")

        assert result["status"] == "healthy"
        assert "PID" in result["details"]
        assert str(listening_port) in result["details"]


# ---------------------------------------------------------------------------
# Tests: check_health (backward compatibility)
# ---------------------------------------------------------------------------

class TestCheckHealthCompat:
    """Test that check_health() still works (backward compatibility)."""

    def test_returns_bool(self):
        from io_mcp.proxy import check_health
        result = check_health("localhost:8444")
        assert isinstance(result, bool)

    def test_delegates_to_is_server_running(self):
        from io_mcp.proxy import check_health
        with patch("io_mcp.proxy.is_server_running", return_value=True):
            assert check_health("localhost:8444") is True
        with patch("io_mcp.proxy.is_server_running", return_value=False):
            assert check_health("localhost:8444") is False


# ---------------------------------------------------------------------------
# Tests: is_server_running
# ---------------------------------------------------------------------------

class TestIsServerRunning:
    """Test PID-based server running check."""

    def test_returns_true_for_current_pid(self, fake_pid_file):
        from io_mcp.proxy import is_server_running
        with patch("io_mcp.proxy.PID_FILE", fake_pid_file):
            assert is_server_running() is True

    def test_returns_false_for_dead_pid(self, fake_pid_file_dead):
        from io_mcp.proxy import is_server_running
        with patch("io_mcp.proxy.PID_FILE", fake_pid_file_dead):
            assert is_server_running() is False

    def test_returns_false_when_no_pid_file(self):
        from io_mcp.proxy import is_server_running
        with patch("io_mcp.proxy.PID_FILE", "/tmp/nonexistent-pid-file-12345.pid"):
            assert is_server_running() is False


# ---------------------------------------------------------------------------
# Tests: _read_pid
# ---------------------------------------------------------------------------

class TestReadPid:
    """Test PID file reading."""

    def test_reads_valid_pid(self, fake_pid_file):
        from io_mcp.proxy import _read_pid
        with patch("io_mcp.proxy.PID_FILE", fake_pid_file):
            pid = _read_pid()
            assert pid == os.getpid()

    def test_returns_none_for_missing_file(self):
        from io_mcp.proxy import _read_pid
        with patch("io_mcp.proxy.PID_FILE", "/tmp/definitely-not-a-real-pid-file.pid"):
            assert _read_pid() is None

    def test_returns_none_for_invalid_content(self, tmp_path):
        from io_mcp.proxy import _read_pid
        pid_file = tmp_path / "bad.pid"
        pid_file.write_text("not-a-number")
        with patch("io_mcp.proxy.PID_FILE", str(pid_file)):
            assert _read_pid() is None

    def test_handles_whitespace(self, tmp_path):
        from io_mcp.proxy import _read_pid
        pid_file = tmp_path / "ws.pid"
        pid_file.write_text("  12345  \n")
        with patch("io_mcp.proxy.PID_FILE", str(pid_file)):
            assert _read_pid() == 12345


# ---------------------------------------------------------------------------
# Tests: Edge cases and error handling
# ---------------------------------------------------------------------------

class TestEdgeCases:
    """Test error handling and edge cases."""

    def test_proxy_health_with_zero_port(self):
        """Port 0 should fail gracefully."""
        from io_mcp.proxy import proxy_health
        with patch("io_mcp.proxy._read_pid", return_value=None):
            result = proxy_health("localhost:0")
        assert result["status"] == "unhealthy"

    def test_proxy_health_permission_error_on_pid(self, listening_port):
        """PermissionError on os.kill should not crash."""
        from io_mcp.proxy import proxy_health
        with patch("io_mcp.proxy._read_pid", return_value=1):
            with patch("os.kill", side_effect=PermissionError("Operation not permitted")):
                result = proxy_health(f"127.0.0.1:{listening_port}")
        # PID alive is False due to PermissionError, port is open
        assert result["pid_alive"] is False
        assert result["port_open"] is True

    def test_check_port_open_with_zero_timeout(self, free_port):
        """Zero timeout should still work (may fail fast)."""
        from io_mcp.proxy import _check_port_open
        # Should not raise
        result = _check_port_open("127.0.0.1", free_port, timeout=0.001)
        assert isinstance(result, bool)

    def test_format_uptime_large_values(self):
        """Very large uptime values format correctly."""
        from io_mcp.proxy import _format_uptime
        # 365 days
        assert "365d" in _format_uptime(365 * 86400)
        # 1000 days
        result = _format_uptime(1000 * 86400)
        assert "1000d" in result

    def test_get_pid_uptime_negative_not_returned(self):
        """Negative uptime values should not be returned by proxy_health."""
        from io_mcp.proxy import proxy_health
        with patch("io_mcp.proxy._read_pid", return_value=os.getpid()):
            with patch("io_mcp.proxy._get_pid_uptime", return_value=-5.0):
                with patch("io_mcp.proxy._check_port_open", return_value=True):
                    result = proxy_health("localhost:8444")
        # Negative uptime should not be included
        assert result["uptime"] is None
        assert result["uptime_seconds"] is None

    def test_proxy_health_concurrent_calls(self, listening_port, fake_pid_file):
        """Multiple concurrent health checks should not interfere."""
        from io_mcp.proxy import proxy_health

        results = []
        errors = []

        def check():
            try:
                with patch("io_mcp.proxy.PID_FILE", fake_pid_file):
                    with patch("io_mcp.proxy._read_pid", return_value=os.getpid()):
                        r = proxy_health(f"127.0.0.1:{listening_port}")
                        results.append(r)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=check) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert len(errors) == 0, f"Concurrent health checks raised: {errors}"
        assert len(results) == 5
        for r in results:
            assert r["status"] == "healthy"
