"""Logging configuration and secret redaction.

Named ``logging_setup`` rather than ``logging`` to avoid shadowing the
stdlib :mod:`logging` module.

This module exposes:

* :func:`setup_logging` — configure a rotating file handler under
  ``~/.upstream/logs/agent.log`` plus a stderr handler, both filtered for
  secret redaction.
* :class:`TokenRedactionFilter` — a :class:`logging.Filter` that replaces
  known sensitive substrings and common token patterns in log records.
* :func:`redact_token` — a pure helper used by CLI output to display a
  non-secret hint (``****abcd``) for a configured token.

Step 3 of Milestone 1 introduces only :func:`redact_token`. The filter and
the ``setup_logging`` function are completed in the next step.
"""

from __future__ import annotations


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
