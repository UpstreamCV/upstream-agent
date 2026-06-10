"""Logging configuration and secret redaction.

Named ``logging_setup`` rather than ``logging`` to avoid shadowing the
stdlib :mod:`logging` module.

Exposes:

* :func:`setup_logging` — configure a rotating file handler under
  ``~/.upstream/logs/agent.log`` plus a stderr handler. Each handler gets
  a shared :class:`TokenRedactionFilter` instance. Filters live on
  handlers (not the root logger) because Python's logging hierarchy only
  consults a logger's filters for records emitted *at* that logger;
  records that propagate up from child loggers bypass parent filters but
  still go through every handler's filter chain.
* :class:`TokenRedactionFilter` — replaces known sensitive substrings and
  common token patterns (``Bearer ...``, ``token=...``, JWT-shaped
  strings, ``Authorization: ...``) with ``****``.
* :func:`redact_token` — pure helper used by CLI output to display a
  token hint such as ``****abcd`` without ever printing the full value.
"""

from __future__ import annotations

import logging
import re
import threading
from collections.abc import Iterable
from logging.handlers import RotatingFileHandler
from pathlib import Path

DEFAULT_LOG_DIR: Path = Path.home() / ".upstream" / "logs"

_LEVELS: dict[str, int] = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
}

_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(Bearer\s+)\S+", re.IGNORECASE), r"\1****"),
    (re.compile(r"(Authorization:\s*)\S+", re.IGNORECASE), r"\1****"),
    (re.compile(r"(\btoken\s*[=:]\s*)\S+", re.IGNORECASE), r"\1****"),
    (re.compile(r"(\bapi[_-]?key\s*[=:]\s*)\S+", re.IGNORECASE), r"\1****"),
    (re.compile(r"(\bpassword\s*[=:]\s*)\S+", re.IGNORECASE), r"\1****"),
    (re.compile(r"(\bsecret\s*[=:]\s*)\S+", re.IGNORECASE), r"\1****"),
    (re.compile(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"), "****"),
)


def redact_token(token: str | None) -> str:
    """Return a non-secret display hint for ``token``.

    Examples:
        >>> redact_token(None)
        '<unset>'
        >>> redact_token('')
        '<unset>'
        >>> redact_token('abc')
        '****'
        >>> redact_token('sk_live_abcd1234')
        '****1234'
    """

    if not token:
        return "<unset>"
    if len(token) <= 8:
        return "****"
    return f"****{token[-4:]}"


class RingBufferHandler(logging.Handler):
    """In-memory log handler backing the interactive shell's ``logs`` view.

    Keeps the last ``capacity`` formatted records in a deque so log
    output never spams the interactive terminal; the shell renders
    :meth:`tail` on demand. Attach via ``setup_logging(extra_handlers=...)``
    so it gets the shared :class:`TokenRedactionFilter` like every other
    handler.
    """

    def __init__(self, capacity: int = 500) -> None:
        super().__init__()
        from collections import deque  # noqa: PLC0415 - tiny, local

        self._records: deque[str] = deque(maxlen=capacity)
        self._records_lock = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            line = self.format(record)
        except Exception:  # pragma: no cover - formatting must never raise
            line = str(record.msg)
        with self._records_lock:
            self._records.append(line)

    def tail(self, n: int = 50) -> list[str]:
        """Return the most recent ``n`` formatted log lines, oldest first."""

        with self._records_lock:
            lines = list(self._records)
        if n <= 0:
            return []
        return lines[-n:]

    def __len__(self) -> int:
        with self._records_lock:
            return len(self._records)


class TokenRedactionFilter(logging.Filter):
    """Logging filter that scrubs known secrets and token-shaped patterns.

    The filter mutates ``record.msg`` to the fully formatted, redacted
    string and clears ``record.args`` so downstream handlers cannot
    re-format the original arguments.
    """

    def __init__(self, sensitive_values: Iterable[str] = ()) -> None:
        super().__init__()
        self._sensitive: tuple[str, ...] = tuple(v for v in sensitive_values if v)

    def _redact(self, text: str) -> str:
        for value in self._sensitive:
            if value in text:
                text = text.replace(value, "****")
        for pattern, repl in _PATTERNS:
            text = pattern.sub(repl, text)
        return text

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        try:
            message = record.getMessage()
        except Exception:
            message = str(record.msg)
        record.msg = self._redact(message)
        record.args = None
        return True


def setup_logging(
    level: str = "info",
    log_dir: Path = DEFAULT_LOG_DIR,
    sensitive_values: Iterable[str] = (),
    *,
    console: bool = True,
    extra_handlers: Iterable[logging.Handler] = (),
) -> None:
    """Configure root logging for the agent.

    Creates ``log_dir`` if missing, attaches a rotating file handler
    (``agent.log``, 5 MB × 3 backups) and a stderr handler, and installs
    a :class:`TokenRedactionFilter` on each handler. The same filter
    instance is shared between handlers — the second invocation sees
    already-redacted text and is a no-op.

    ``console=False`` skips the stderr handler — the interactive shell
    uses this so background-stream logs land in the file and the ring
    buffer instead of scrolling over the prompt. Every handler in
    ``extra_handlers`` gets the shared redaction filter and formatter
    attached before being installed on the root logger.

    Idempotent: existing root-logger handlers are cleared on each call so
    the CLI can call this multiple times during a single invocation
    (e.g., before and after loading config) without accumulating
    handlers.
    """

    log_dir.mkdir(parents=True, exist_ok=True)

    numeric_level = _LEVELS.get(level.lower(), logging.INFO)

    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)

    root.setLevel(numeric_level)

    formatter = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )
    redaction_filter = TokenRedactionFilter(sensitive_values)

    file_handler = RotatingFileHandler(
        log_dir / "agent.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    file_handler.addFilter(redaction_filter)
    root.addHandler(file_handler)

    if console:
        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(formatter)
        stream_handler.addFilter(redaction_filter)
        root.addHandler(stream_handler)

    for handler in extra_handlers:
        handler.setFormatter(formatter)
        handler.addFilter(redaction_filter)
        root.addHandler(handler)
