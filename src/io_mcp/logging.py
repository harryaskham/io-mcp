"""Structured logging for io-mcp.

Replaces ad-hoc ``open('/tmp/...', 'a')`` writes with a proper logging
pipeline using Python's ``logging`` module.  Features:

* **RotatingFileHandler** – 5 MB max, 3 backups.
* **Structured JSON** – each line is a JSON object with ``timestamp``,
  ``level``, ``logger``, ``message``, and optional ``context`` fields.
* **Log-level differentiation** – DEBUG for cache hits, INFO for speech
  events, WARNING for failures, ERROR for crashes.
* **Backwards-compatible file locations** – writes to the same ``/tmp/``
  paths so ``get_logs``, the TUI system-log viewer, and proxy crash
  diagnostics keep working.
* **Context support** – callers can pass ``session_id``, ``tool_name``,
  ``text_preview``, timing data, etc. via the ``extra`` dict.
"""

from __future__ import annotations

import json
import logging
import os
import time
from logging.handlers import RotatingFileHandler
from typing import Any, Optional


# ── Log file paths (unchanged from ad-hoc locations) ─────────────────

TUI_ERROR_LOG = "/tmp/io-mcp-tui-error.log"
TOOL_ERROR_LOG = "/tmp/io-mcp-tool-error.log"
SERVER_LOG = "/tmp/io-mcp-server.log"
PROXY_LOG = "/tmp/io-mcp-proxy.log"

# ── Rotation settings ────────────────────────────────────────────────

MAX_BYTES = 5 * 1024 * 1024  # 5 MB
BACKUP_COUNT = 3


class _JsonFormatter(logging.Formatter):
    """Emit each log record as a single-line JSON object.

    Fields:
        timestamp  – ISO-8601 with milliseconds
        level      – DEBUG / INFO / WARNING / ERROR / CRITICAL
        logger     – logger name
        message    – the log message
        context    – optional dict with session_id, tool_name, etc.
    """

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "timestamp": self.formatTime(record, datefmt="%Y-%m-%dT%H:%M:%S.") + f"{int(record.msecs):03d}",
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        # Merge caller-supplied context
        ctx = getattr(record, "context", None)
        if ctx:
            entry["context"] = ctx
        # Include exception info if present
        if record.exc_info and record.exc_info[0] is not None:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str)


class _PlainFormatter(logging.Formatter):
    """Simple one-line formatter for the server/proxy log."""

    def format(self, record: logging.LogRecord) -> str:
        ts = self.formatTime(record, datefmt="%Y-%m-%d %H:%M:%S")
        return f"[{ts}] {record.levelname}: {record.getMessage()}"


def _make_handler(
    path: str,
    formatter: logging.Formatter,
    max_bytes: int = MAX_BYTES,
    backup_count: int = BACKUP_COUNT,
) -> RotatingFileHandler:
    """Create a RotatingFileHandler that writes to *path*."""
    os.makedirs(os.path.dirname(path) or "/tmp", exist_ok=True)
    handler = RotatingFileHandler(
        path, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
    )
    handler.setFormatter(formatter)
    return handler


# ── Public helpers ────────────────────────────────────────────────────

_configured: set[str] = set()


def get_logger(
    name: str,
    log_file: str = TUI_ERROR_LOG,
    level: int = logging.DEBUG,
    *,
    json_format: bool = True,
) -> logging.Logger:
    """Return a logger that writes structured JSON to *log_file*.

    Calling this multiple times with the same *name* returns the same
    logger (standard ``logging`` behaviour) but only adds the handler
    once.

    Parameters
    ----------
    name:
        Logger name (e.g. ``"io-mcp.tts"``, ``"io-mcp.tui"``).
    log_file:
        Absolute path to the log file.  Defaults to the TUI error log.
    level:
        Minimum level for this logger.
    json_format:
        ``True`` → JSON lines (default, machine-parseable).
        ``False`` → plain-text lines (for server/proxy startup logs).
    """
    logger = logging.getLogger(name)
    key = f"{name}:{log_file}"
    if key not in _configured:
        fmt = _JsonFormatter() if json_format else _PlainFormatter()
        handler = _make_handler(log_file, fmt)
        handler.setLevel(level)
        logger.addHandler(handler)
        logger.setLevel(level)
        _configured.add(key)
    return logger


def log_context(
    *,
    session_id: str = "",
    tool_name: str = "",
    text_preview: str = "",
    duration_ms: float | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """Build a context dict for structured log entries.

    Usage::

        logger.warning("TTS failed", extra={"context": log_context(
            session_id="abc", tool_name="speak", text_preview="Hello..."
        )})
    """
    ctx: dict[str, Any] = {}
    if session_id:
        ctx["session_id"] = session_id
    if tool_name:
        ctx["tool_name"] = tool_name
    if text_preview:
        ctx["text_preview"] = text_preview[:80]
    if duration_ms is not None:
        ctx["duration_ms"] = round(duration_ms, 1)
    ctx.update(extra)
    return ctx


def parse_log_line(line: str) -> dict[str, Any] | None:
    """Try to parse a structured JSON log line.

    Returns ``None`` for non-JSON lines (legacy/plain-text entries)
    so readers can gracefully handle mixed-format logs.
    """
    line = line.strip()
    if not line:
        return None
    try:
        return json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return None


def read_log_tail(path: str, lines: int = 50) -> list[str]:
    """Read the last *lines* lines from a log file.

    Returns an empty list if the file does not exist.  Each entry is
    a raw string — callers can pass through ``parse_log_line`` to get
    structured data.
    """
    try:
        with open(path, "r") as f:
            content = f.read().strip()
        if not content:
            return []
        return content.split("\n")[-lines:]
    except FileNotFoundError:
        return []
    except Exception:
        return []
