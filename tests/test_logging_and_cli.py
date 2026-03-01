"""Tests for the io-mcp logging module and CLI tool (io-mcp-msg).

Logging tests cover:
- get_logger() — creates loggers with correct level, handlers, formatting
- _JsonFormatter — structured JSON output with context and exception fields
- _PlainFormatter — simple one-line text output
- _make_handler — RotatingFileHandler creation
- read_log_tail() — reads last N lines, handles missing/empty files
- log_context() — builds context dicts with truncation and extras
- parse_log_line() — parses JSON lines, handles non-JSON gracefully
- Log file path constants (TUI_ERROR_LOG, TOOL_ERROR_LOG, etc.)
- Idempotent handler registration (calling get_logger twice)

CLI tests cover:
- Argument parsing (message, --host, --port, -s, --active, --list, --health)
- Health check (--health)
- Session listing (--list) — with and without sessions
- Message sending — broadcast, active, specific session
- Stdin piping when no positional args
- Error handling — connection errors, HTTP errors
- Missing message shows help and exits
"""

from __future__ import annotations

import io
import json
import logging
import os
import sys
import tempfile
import textwrap
import unittest.mock as mock

import pytest

from io_mcp.logging import (
    BACKUP_COUNT,
    MAX_BYTES,
    PROXY_LOG,
    SERVER_LOG,
    TOOL_ERROR_LOG,
    TUI_ERROR_LOG,
    _JsonFormatter,
    _PlainFormatter,
    _configured,
    _make_handler,
    get_logger,
    log_context,
    parse_log_line,
    read_log_tail,
)
from io_mcp.cli import main, _api_get, _api_post


# ===========================================================================
# Logging module tests
# ===========================================================================


class TestLogFilePaths:
    """Verify the well-known log file path constants."""

    def test_tui_error_log_path(self):
        assert TUI_ERROR_LOG == "/tmp/io-mcp-tui-error.log"

    def test_tool_error_log_path(self):
        assert TOOL_ERROR_LOG == "/tmp/io-mcp-tool-error.log"

    def test_server_log_path(self):
        assert SERVER_LOG == "/tmp/io-mcp-server.log"

    def test_proxy_log_path(self):
        assert PROXY_LOG == "/tmp/io-mcp-proxy.log"


class TestRotationSettings:
    """Verify rotation constants."""

    def test_max_bytes(self):
        assert MAX_BYTES == 5 * 1024 * 1024  # 5 MB

    def test_backup_count(self):
        assert BACKUP_COUNT == 3


class TestJsonFormatter:
    """Tests for the _JsonFormatter class."""

    def setup_method(self):
        self.fmt = _JsonFormatter()

    def _make_record(self, msg="test message", level=logging.INFO, name="test.logger", **kwargs):
        record = logging.LogRecord(
            name=name,
            level=level,
            pathname="test.py",
            lineno=42,
            msg=msg,
            args=(),
            exc_info=None,
        )
        for k, v in kwargs.items():
            setattr(record, k, v)
        return record

    def test_basic_json_output(self):
        record = self._make_record("hello world")
        output = self.fmt.format(record)
        parsed = json.loads(output)

        assert parsed["level"] == "INFO"
        assert parsed["logger"] == "test.logger"
        assert parsed["message"] == "hello world"
        assert "timestamp" in parsed

    def test_timestamp_format(self):
        record = self._make_record()
        output = self.fmt.format(record)
        parsed = json.loads(output)
        ts = parsed["timestamp"]
        # Should look like 2026-03-01T12:34:56.789
        assert "T" in ts
        assert "." in ts

    def test_context_included(self):
        record = self._make_record(context={"session_id": "abc", "tool_name": "speak"})
        output = self.fmt.format(record)
        parsed = json.loads(output)

        assert parsed["context"]["session_id"] == "abc"
        assert parsed["context"]["tool_name"] == "speak"

    def test_no_context_when_absent(self):
        record = self._make_record()
        output = self.fmt.format(record)
        parsed = json.loads(output)
        assert "context" not in parsed

    def test_empty_context_not_included(self):
        """An empty dict is falsy, so it should not appear."""
        record = self._make_record(context={})
        output = self.fmt.format(record)
        parsed = json.loads(output)
        assert "context" not in parsed

    def test_exception_info_included(self):
        try:
            raise ValueError("boom")
        except ValueError:
            exc_info = sys.exc_info()

        record = self._make_record()
        record.exc_info = exc_info
        output = self.fmt.format(record)
        parsed = json.loads(output)

        assert "exception" in parsed
        assert "ValueError: boom" in parsed["exception"]

    def test_no_exception_when_none(self):
        record = self._make_record()
        record.exc_info = (None, None, None)
        output = self.fmt.format(record)
        parsed = json.loads(output)
        assert "exception" not in parsed

    def test_all_log_levels(self):
        for level, name in [
            (logging.DEBUG, "DEBUG"),
            (logging.INFO, "INFO"),
            (logging.WARNING, "WARNING"),
            (logging.ERROR, "ERROR"),
            (logging.CRITICAL, "CRITICAL"),
        ]:
            record = self._make_record(level=level)
            output = self.fmt.format(record)
            parsed = json.loads(output)
            assert parsed["level"] == name

    def test_message_with_args_formatting(self):
        """LogRecord with % formatting args should produce the formatted message."""
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="test.py",
            lineno=1,
            msg="value is %d",
            args=(42,),
            exc_info=None,
        )
        output = self.fmt.format(record)
        parsed = json.loads(output)
        assert parsed["message"] == "value is 42"

    def test_output_is_single_line(self):
        record = self._make_record("line1\nline2\nline3")
        output = self.fmt.format(record)
        # JSON serialization should escape newlines
        assert "\n" not in output


class TestPlainFormatter:
    """Tests for the _PlainFormatter class."""

    def setup_method(self):
        self.fmt = _PlainFormatter()

    def test_basic_format(self):
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="test.py",
            lineno=1,
            msg="server started",
            args=(),
            exc_info=None,
        )
        output = self.fmt.format(record)
        # Should look like: [2026-03-01 12:34:56] INFO: server started
        assert "] INFO: server started" in output
        assert output.startswith("[")

    def test_warning_level(self):
        record = logging.LogRecord(
            name="test",
            level=logging.WARNING,
            pathname="test.py",
            lineno=1,
            msg="something wrong",
            args=(),
            exc_info=None,
        )
        output = self.fmt.format(record)
        assert "WARNING: something wrong" in output


class TestMakeHandler:
    """Tests for the _make_handler function."""

    def test_creates_rotating_handler(self, tmp_path):
        log_file = str(tmp_path / "test.log")
        fmt = _PlainFormatter()
        handler = _make_handler(log_file, fmt)

        from logging.handlers import RotatingFileHandler
        assert isinstance(handler, RotatingFileHandler)
        handler.close()

    def test_handler_has_formatter(self, tmp_path):
        log_file = str(tmp_path / "test.log")
        fmt = _JsonFormatter()
        handler = _make_handler(log_file, fmt)

        assert handler.formatter is fmt
        handler.close()

    def test_custom_max_bytes(self, tmp_path):
        log_file = str(tmp_path / "test.log")
        handler = _make_handler(log_file, _PlainFormatter(), max_bytes=1024)

        assert handler.maxBytes == 1024
        handler.close()

    def test_custom_backup_count(self, tmp_path):
        log_file = str(tmp_path / "test.log")
        handler = _make_handler(log_file, _PlainFormatter(), backup_count=5)

        assert handler.backupCount == 5
        handler.close()

    def test_creates_parent_dirs(self, tmp_path):
        log_file = str(tmp_path / "subdir" / "deep" / "test.log")
        handler = _make_handler(log_file, _PlainFormatter())

        assert os.path.isdir(str(tmp_path / "subdir" / "deep"))
        handler.close()


class TestGetLogger:
    """Tests for the get_logger() function."""

    def setup_method(self):
        # Save and restore _configured state to avoid cross-test pollution
        self._saved_configured = _configured.copy()

    def teardown_method(self):
        _configured.clear()
        _configured.update(self._saved_configured)

    def test_returns_logger(self, tmp_path):
        log_file = str(tmp_path / "test_get_logger.log")
        logger = get_logger("test.get_logger.basic", log_file)
        assert isinstance(logger, logging.Logger)
        assert logger.name == "test.get_logger.basic"
        # Cleanup
        for h in logger.handlers[:]:
            h.close()
            logger.removeHandler(h)

    def test_logger_level(self, tmp_path):
        log_file = str(tmp_path / "test_level.log")
        logger = get_logger("test.get_logger.level", log_file, level=logging.WARNING)
        assert logger.level == logging.WARNING
        for h in logger.handlers[:]:
            h.close()
            logger.removeHandler(h)

    def test_json_format_by_default(self, tmp_path):
        log_file = str(tmp_path / "test_json.log")
        logger = get_logger("test.get_logger.json_default", log_file)
        # Handler should have _JsonFormatter
        assert any(isinstance(h.formatter, _JsonFormatter) for h in logger.handlers)
        for h in logger.handlers[:]:
            h.close()
            logger.removeHandler(h)

    def test_plain_format(self, tmp_path):
        log_file = str(tmp_path / "test_plain.log")
        logger = get_logger("test.get_logger.plain", log_file, json_format=False)
        assert any(isinstance(h.formatter, _PlainFormatter) for h in logger.handlers)
        for h in logger.handlers[:]:
            h.close()
            logger.removeHandler(h)

    def test_no_propagation(self, tmp_path):
        log_file = str(tmp_path / "test_prop.log")
        logger = get_logger("test.get_logger.no_prop", log_file)
        assert logger.propagate is False
        for h in logger.handlers[:]:
            h.close()
            logger.removeHandler(h)

    def test_idempotent_handler_registration(self, tmp_path):
        log_file = str(tmp_path / "test_idempotent.log")
        name = "test.get_logger.idempotent"
        logger1 = get_logger(name, log_file)
        handler_count = len(logger1.handlers)
        logger2 = get_logger(name, log_file)
        assert logger1 is logger2
        assert len(logger2.handlers) == handler_count
        for h in logger1.handlers[:]:
            h.close()
            logger1.removeHandler(h)

    def test_different_files_add_handlers(self, tmp_path):
        name = "test.get_logger.multi_file"
        log1 = str(tmp_path / "log1.log")
        log2 = str(tmp_path / "log2.log")
        logger = get_logger(name, log1)
        initial = len(logger.handlers)
        get_logger(name, log2)
        assert len(logger.handlers) == initial + 1
        for h in logger.handlers[:]:
            h.close()
            logger.removeHandler(h)

    def test_writes_json_to_file(self, tmp_path):
        log_file = str(tmp_path / "test_write.log")
        logger = get_logger("test.get_logger.write", log_file)
        logger.info("hello from test")
        # Flush handlers
        for h in logger.handlers:
            h.flush()
        content = open(log_file).read().strip()
        parsed = json.loads(content)
        assert parsed["message"] == "hello from test"
        assert parsed["level"] == "INFO"
        for h in logger.handlers[:]:
            h.close()
            logger.removeHandler(h)

    def test_writes_plain_to_file(self, tmp_path):
        log_file = str(tmp_path / "test_write_plain.log")
        logger = get_logger("test.get_logger.write_plain", log_file, json_format=False)
        logger.warning("plain warning")
        for h in logger.handlers:
            h.flush()
        content = open(log_file).read().strip()
        assert "WARNING: plain warning" in content
        for h in logger.handlers[:]:
            h.close()
            logger.removeHandler(h)

    def test_context_written_to_file(self, tmp_path):
        log_file = str(tmp_path / "test_ctx_write.log")
        logger = get_logger("test.get_logger.ctx_write", log_file)
        logger.info("with context", extra={"context": {"session_id": "s123"}})
        for h in logger.handlers:
            h.flush()
        content = open(log_file).read().strip()
        parsed = json.loads(content)
        assert parsed["context"]["session_id"] == "s123"
        for h in logger.handlers[:]:
            h.close()
            logger.removeHandler(h)


class TestLogContext:
    """Tests for the log_context() helper."""

    def test_empty_context(self):
        ctx = log_context()
        assert ctx == {}

    def test_session_id(self):
        ctx = log_context(session_id="abc-123")
        assert ctx == {"session_id": "abc-123"}

    def test_tool_name(self):
        ctx = log_context(tool_name="speak")
        assert ctx == {"tool_name": "speak"}

    def test_text_preview_truncation(self):
        long_text = "x" * 200
        ctx = log_context(text_preview=long_text)
        assert len(ctx["text_preview"]) == 80

    def test_text_preview_short(self):
        ctx = log_context(text_preview="short")
        assert ctx["text_preview"] == "short"

    def test_duration_ms(self):
        ctx = log_context(duration_ms=123.456)
        assert ctx["duration_ms"] == 123.5

    def test_duration_ms_none(self):
        ctx = log_context(duration_ms=None)
        assert "duration_ms" not in ctx

    def test_extra_kwargs(self):
        ctx = log_context(cache_hit=True, voice="sage")
        assert ctx["cache_hit"] is True
        assert ctx["voice"] == "sage"

    def test_all_fields(self):
        ctx = log_context(
            session_id="s1",
            tool_name="present_choices",
            text_preview="Pick one",
            duration_ms=50.0,
            count=3,
        )
        assert ctx == {
            "session_id": "s1",
            "tool_name": "present_choices",
            "text_preview": "Pick one",
            "duration_ms": 50.0,
            "count": 3,
        }

    def test_empty_strings_excluded(self):
        ctx = log_context(session_id="", tool_name="", text_preview="")
        assert ctx == {}


class TestParseLogLine:
    """Tests for parse_log_line()."""

    def test_valid_json(self):
        line = json.dumps({"level": "INFO", "message": "test"})
        result = parse_log_line(line)
        assert result == {"level": "INFO", "message": "test"}

    def test_non_json_line(self):
        assert parse_log_line("[2024-01-01] INFO: plain text") is None

    def test_empty_string(self):
        assert parse_log_line("") is None

    def test_whitespace_only(self):
        assert parse_log_line("   \n  ") is None

    def test_strips_whitespace(self):
        line = '  {"key": "value"}  '
        result = parse_log_line(line)
        assert result == {"key": "value"}

    def test_partial_json(self):
        assert parse_log_line('{"incomplete": ') is None

    def test_complex_json(self):
        entry = {
            "timestamp": "2026-03-01T12:00:00.000",
            "level": "ERROR",
            "logger": "io-mcp.tts",
            "message": "TTS failed",
            "context": {"session_id": "abc", "duration_ms": 42.5},
            "exception": "Traceback...",
        }
        line = json.dumps(entry)
        result = parse_log_line(line)
        assert result == entry


class TestReadLogTail:
    """Tests for read_log_tail()."""

    def test_reads_last_n_lines(self, tmp_path):
        log_file = str(tmp_path / "test.log")
        lines = [f"line {i}" for i in range(100)]
        with open(log_file, "w") as f:
            f.write("\n".join(lines))

        result = read_log_tail(log_file, lines=10)
        assert len(result) == 10
        assert result[0] == "line 90"
        assert result[-1] == "line 99"

    def test_reads_all_when_fewer_lines(self, tmp_path):
        log_file = str(tmp_path / "test.log")
        with open(log_file, "w") as f:
            f.write("a\nb\nc")

        result = read_log_tail(log_file, lines=50)
        assert result == ["a", "b", "c"]

    def test_missing_file_returns_empty(self):
        result = read_log_tail("/tmp/nonexistent_io_mcp_test_file.log")
        assert result == []

    def test_empty_file_returns_empty(self, tmp_path):
        log_file = str(tmp_path / "empty.log")
        with open(log_file, "w") as f:
            f.write("")
        result = read_log_tail(log_file)
        assert result == []

    def test_whitespace_only_file(self, tmp_path):
        log_file = str(tmp_path / "ws.log")
        with open(log_file, "w") as f:
            f.write("   \n  \n   ")
        # After strip, this is empty
        result = read_log_tail(log_file)
        assert result == []

    def test_default_lines_50(self, tmp_path):
        log_file = str(tmp_path / "big.log")
        lines = [f"entry {i}" for i in range(200)]
        with open(log_file, "w") as f:
            f.write("\n".join(lines))

        result = read_log_tail(log_file)
        assert len(result) == 50
        assert result[0] == "entry 150"
        assert result[-1] == "entry 199"

    def test_single_line_file(self, tmp_path):
        log_file = str(tmp_path / "single.log")
        with open(log_file, "w") as f:
            f.write("only line")
        result = read_log_tail(log_file, lines=10)
        assert result == ["only line"]

    def test_permission_error_returns_empty(self, tmp_path):
        """Generic exceptions return empty list (except FileNotFoundError)."""
        log_file = str(tmp_path / "noperm.log")
        with open(log_file, "w") as f:
            f.write("data")
        # Mock open to raise a generic OSError
        with mock.patch("builtins.open", side_effect=PermissionError("denied")):
            result = read_log_tail(log_file)
        assert result == []


# ===========================================================================
# CLI tool tests
# ===========================================================================


class TestApiGet:
    """Tests for _api_get helper."""

    def test_successful_get(self):
        response_data = {"status": "ok"}
        mock_resp = mock.MagicMock()
        mock_resp.read.return_value = json.dumps(response_data).encode()
        mock_resp.__enter__ = mock.MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = mock.MagicMock(return_value=False)

        with mock.patch("io_mcp.cli.urllib.request.urlopen", return_value=mock_resp):
            result = _api_get("http://localhost:8445", "/api/health")
        assert result == {"status": "ok"}

    def test_connection_error_exits(self):
        import urllib.error
        with mock.patch(
            "io_mcp.cli.urllib.request.urlopen",
            side_effect=urllib.error.URLError("Connection refused"),
        ):
            with pytest.raises(SystemExit) as exc_info:
                _api_get("http://localhost:8445", "/api/health")
            assert exc_info.value.code == 1


class TestApiPost:
    """Tests for _api_post helper."""

    def test_successful_post(self):
        response_data = {"count": 2}
        mock_resp = mock.MagicMock()
        mock_resp.read.return_value = json.dumps(response_data).encode()
        mock_resp.__enter__ = mock.MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = mock.MagicMock(return_value=False)

        with mock.patch("io_mcp.cli.urllib.request.urlopen", return_value=mock_resp):
            result = _api_post("http://localhost:8445", "/api/message", {"text": "hi"})
        assert result == {"count": 2}

    def test_http_error_json_body(self):
        import urllib.error
        err_body = json.dumps({"error": "session not found"}).encode()
        http_err = urllib.error.HTTPError(
            "http://x", 404, "Not Found", {}, io.BytesIO(err_body)
        )
        with mock.patch("io_mcp.cli.urllib.request.urlopen", side_effect=http_err):
            with pytest.raises(SystemExit) as exc_info:
                _api_post("http://localhost:8445", "/api/sessions/xyz/message", {"text": "hi"})
            assert exc_info.value.code == 1

    def test_http_error_plain_body(self):
        import urllib.error
        http_err = urllib.error.HTTPError(
            "http://x", 500, "Server Error", {}, io.BytesIO(b"Internal failure")
        )
        with mock.patch("io_mcp.cli.urllib.request.urlopen", side_effect=http_err):
            with pytest.raises(SystemExit) as exc_info:
                _api_post("http://localhost:8445", "/api/message", {"text": "hi"})
            assert exc_info.value.code == 1

    def test_connection_error_exits(self):
        import urllib.error
        with mock.patch(
            "io_mcp.cli.urllib.request.urlopen",
            side_effect=urllib.error.URLError("Connection refused"),
        ):
            with pytest.raises(SystemExit) as exc_info:
                _api_post("http://localhost:8445", "/api/message", {"text": "hi"})
            assert exc_info.value.code == 1


class TestCliMain:
    """Tests for the main() CLI entry point."""

    def _run_main(self, args: list[str], stdin_text: str | None = None):
        """Helper to run main() with mocked sys.argv and optional stdin."""
        with mock.patch("sys.argv", ["io-mcp-msg"] + args):
            if stdin_text is not None:
                with mock.patch("sys.stdin", io.StringIO(stdin_text)):
                    with mock.patch("sys.stdin") as mock_stdin:
                        mock_stdin.isatty.return_value = False
                        mock_stdin.read.return_value = stdin_text
                        return main()
            return main()

    # ── --health ───────────────────────────────────────────────────────

    def test_health_check(self, capsys):
        health_data = {"status": "ok", "backend": True}
        with mock.patch("io_mcp.cli._api_get", return_value=health_data) as m:
            self._run_main(["--health"])
        m.assert_called_once_with("http://127.0.0.1:8445", "/api/health")
        output = capsys.readouterr().out
        parsed = json.loads(output)
        assert parsed["status"] == "ok"

    def test_health_custom_host_port(self, capsys):
        with mock.patch("io_mcp.cli._api_get", return_value={"status": "ok"}) as m:
            self._run_main(["--health", "--host", "192.168.1.5", "--port", "9000"])
        m.assert_called_once_with("http://192.168.1.5:9000", "/api/health")

    # ── --list ─────────────────────────────────────────────────────────

    def test_list_sessions_empty(self, capsys):
        with mock.patch("io_mcp.cli._api_get", return_value={"sessions": []}) as m:
            self._run_main(["--list"])
        m.assert_called_once_with("http://127.0.0.1:8445", "/api/sessions")
        assert "No active sessions" in capsys.readouterr().out

    def test_list_sessions_with_entries(self, capsys):
        sessions = {
            "sessions": [
                {"id": "abc123456789def", "name": "Code Review", "active": True},
                {"id": "xyz987654321uvw", "name": "Tests", "active": False},
            ]
        }
        with mock.patch("io_mcp.cli._api_get", return_value=sessions):
            self._run_main(["--list"])
        output = capsys.readouterr().out
        assert "abc123456789" in output
        assert "Code Review" in output
        assert "choices" in output  # 🟢 choices
        assert "working" in output  # ⏳ working

    # ── message sending ────────────────────────────────────────────────

    def test_broadcast_message(self, capsys):
        with mock.patch("io_mcp.cli._api_post", return_value={"count": 3}) as m:
            self._run_main(["hello", "world"])
        m.assert_called_once_with(
            "http://127.0.0.1:8445",
            "/api/message",
            {"text": "hello world", "target": "all"},
        )
        assert "3 sessions" in capsys.readouterr().out

    def test_broadcast_single_session(self, capsys):
        with mock.patch("io_mcp.cli._api_post", return_value={"count": 1}) as m:
            self._run_main(["check this"])
        output = capsys.readouterr().out
        assert "1 session" in output
        # Should NOT be plural
        assert "1 sessions" not in output

    def test_active_message(self, capsys):
        with mock.patch("io_mcp.cli._api_post", return_value={"count": 1}) as m:
            self._run_main(["--active", "look at this"])
        m.assert_called_once_with(
            "http://127.0.0.1:8445",
            "/api/message",
            {"text": "look at this", "target": "active"},
        )

    def test_specific_session_message(self, capsys):
        with mock.patch("io_mcp.cli._api_post", return_value={"pending": 2}) as m:
            self._run_main(["-s", "abc123", "do this"])
        m.assert_called_once_with(
            "http://127.0.0.1:8445",
            "/api/sessions/abc123/message",
            {"text": "do this"},
        )
        output = capsys.readouterr().out
        assert "abc123" in output
        assert "2 pending" in output

    # ── stdin piping ───────────────────────────────────────────────────

    def test_stdin_piping(self, capsys):
        with mock.patch("io_mcp.cli._api_post", return_value={"count": 1}) as m:
            with mock.patch("sys.argv", ["io-mcp-msg"]):
                with mock.patch("sys.stdin") as mock_stdin:
                    mock_stdin.isatty.return_value = False
                    mock_stdin.read.return_value = "piped message\n"
                    main()
        m.assert_called_once_with(
            "http://127.0.0.1:8445",
            "/api/message",
            {"text": "piped message", "target": "all"},
        )

    # ── no message ─────────────────────────────────────────────────────

    def test_no_message_no_stdin_exits(self):
        with mock.patch("sys.argv", ["io-mcp-msg"]):
            with mock.patch("sys.stdin") as mock_stdin:
                mock_stdin.isatty.return_value = True
                with pytest.raises(SystemExit) as exc_info:
                    main()
                assert exc_info.value.code == 1

    # ── custom host/port for messages ──────────────────────────────────

    def test_custom_host_port_message(self, capsys):
        with mock.patch("io_mcp.cli._api_post", return_value={"count": 1}) as m:
            self._run_main(["--host", "10.0.0.1", "--port", "9999", "msg"])
        m.assert_called_once_with(
            "http://10.0.0.1:9999",
            "/api/message",
            {"text": "msg", "target": "all"},
        )

    # ── multi-word messages ────────────────────────────────────────────

    def test_multi_word_message_joined(self, capsys):
        with mock.patch("io_mcp.cli._api_post", return_value={"count": 1}) as m:
            self._run_main(["check", "the", "auth", "module"])
        call_args = m.call_args
        assert call_args[0][2]["text"] == "check the auth module"


class TestCliArgParsing:
    """Tests for argument parsing without actually making API calls."""

    def test_default_host(self):
        with mock.patch("sys.argv", ["io-mcp-msg", "--health"]):
            with mock.patch("io_mcp.cli._api_get", return_value={"status": "ok"}):
                main()
        # If we got here without error, default host worked

    def test_session_flag_short(self):
        """Verify -s is accepted as short form for --session."""
        with mock.patch("sys.argv", ["io-mcp-msg", "-s", "sess123", "hello"]):
            with mock.patch("io_mcp.cli._api_post", return_value={"pending": 1}):
                main()

    def test_session_flag_long(self):
        """Verify --session is accepted."""
        with mock.patch("sys.argv", ["io-mcp-msg", "--session", "sess123", "hello"]):
            with mock.patch("io_mcp.cli._api_post", return_value={"pending": 1}):
                main()

    def test_list_dest_is_list_sessions(self):
        """The --list flag maps to args.list_sessions."""
        with mock.patch("sys.argv", ["io-mcp-msg", "--list"]):
            with mock.patch("io_mcp.cli._api_get", return_value={"sessions": []}):
                main()

    def test_port_type_int(self):
        """Port should be parsed as integer."""
        with mock.patch("sys.argv", ["io-mcp-msg", "--port", "9999", "--health"]):
            with mock.patch("io_mcp.cli._api_get", return_value={}) as m:
                main()
        # Verify it was used as int in the URL
        m.assert_called_once_with("http://127.0.0.1:9999", "/api/health")


class TestCliListSessionsFormatting:
    """Detailed tests for --list output formatting."""

    def test_unnamed_session(self, capsys):
        sessions = {"sessions": [{"id": "abc123456789", "active": False}]}
        with mock.patch("io_mcp.cli._api_get", return_value=sessions):
            with mock.patch("sys.argv", ["io-mcp-msg", "--list"]):
                main()
        output = capsys.readouterr().out
        assert "(unnamed)" in output

    def test_active_session_indicator(self, capsys):
        sessions = {"sessions": [{"id": "abc123456789", "name": "Test", "active": True}]}
        with mock.patch("io_mcp.cli._api_get", return_value=sessions):
            with mock.patch("sys.argv", ["io-mcp-msg", "--list"]):
                main()
        output = capsys.readouterr().out
        assert "choices" in output

    def test_working_session_indicator(self, capsys):
        sessions = {"sessions": [{"id": "abc123456789", "name": "Test", "active": False}]}
        with mock.patch("io_mcp.cli._api_get", return_value=sessions):
            with mock.patch("sys.argv", ["io-mcp-msg", "--list"]):
                main()
        output = capsys.readouterr().out
        assert "working" in output

    def test_session_id_truncated_to_12(self, capsys):
        full_id = "abcdefghijklmnopqrstuvwxyz"
        sessions = {"sessions": [{"id": full_id, "name": "X", "active": False}]}
        with mock.patch("io_mcp.cli._api_get", return_value=sessions):
            with mock.patch("sys.argv", ["io-mcp-msg", "--list"]):
                main()
        output = capsys.readouterr().out
        assert full_id[:12] in output
