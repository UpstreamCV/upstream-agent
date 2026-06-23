"""Local stdlib HTTP receiver for the Upstream Collector.

A :class:`http.server.ThreadingHTTPServer` (no third-party server framework)
bound to ``127.0.0.1:8756`` by default. It accepts observation batches from the
customer's CV system, validates them locally, writes them to the spool, and
acknowledges quickly — it never blocks on, or fails because of, the cloud.

Endpoints:

* ``POST /v1/observations`` — validate + spool a batch; ``202`` ``{accepted,
  queued}`` on success, ``422`` on invalid input, ``401`` if the optional
  inbound token is wrong.
* ``GET /healthz`` — ``200`` ``{"ok": true}`` while the process is alive (stays
  200 even when cloud uploads are failing).
* ``GET /status`` — ``200`` with the secret-free :class:`CollectorStatus`.

Security: binds loopback by default; the optional ``inbound_token`` is compared
in constant time and never logged; request bodies and the token never reach the
logs.
"""

from __future__ import annotations

import hmac
import json
import logging
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from pydantic import ValidationError

from upstream_agent.api.schemas import ObservationBatchRequest
from upstream_agent.collector.spool import Spool
from upstream_agent.collector.status import CollectorStatus

logger = logging.getLogger(__name__)


def _validation_error(message: str, details: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"error": {"code": "validation_error", "message": message, "details": details or {}}}


def validate_and_spool(raw: bytes, spool: Spool, max_batch_size: int) -> tuple[int, dict[str, Any]]:
    """Validate a raw request body and, if valid, spool it.

    Pure (no HTTP) so the validation contract is unit-testable directly.
    Returns ``(status_code, json_body)``. Validation errors never echo the
    offending input, so a secret-bearing payload can't leak into the response.
    """

    try:
        data = json.loads(raw)
    except ValueError:
        return 422, _validation_error("Request body must be valid JSON.")
    if not isinstance(data, dict):
        return 422, _validation_error("Request body must be a JSON object.")

    try:
        req = ObservationBatchRequest.model_validate(data)
    except ValidationError as exc:
        errors = exc.errors(include_input=False, include_url=False)
        fields = [".".join(str(part) for part in err["loc"]) or "<root>" for err in errors]
        return 422, _validation_error("Observation batch failed validation.", {"fields": fields})

    if len(req.observations) > max_batch_size:
        return 422, _validation_error(
            f"Batch exceeds max_batch_size ({max_batch_size}).",
            {"observations": len(req.observations)},
        )

    result = spool.append(req.model_dump(mode="json"))
    return 202, {"accepted": True, "queued": result.queued_observations}


class _CollectorHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    # Set by ObservationReceiver before serving.
    spool: Spool
    inbound_token: str | None
    max_batch_size: int
    status_provider: Callable[[], CollectorStatus]


class _Handler(BaseHTTPRequestHandler):
    server: _CollectorHTTPServer  # type: ignore[assignment]

    # Keep the stdlib server's per-request access logging off the prompt/stderr
    # — and never let it format a path/body into the logs at info level.
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        logger.debug("collector receiver: " + format, *args)

    def _send_json(self, status: int, obj: dict[str, Any]) -> None:
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        expected = self.server.inbound_token
        if not expected:
            return True
        provided = self.headers.get("X-Upstream-Collector-Token")
        return provided is not None and hmac.compare_digest(provided, expected)

    def do_GET(self) -> None:  # noqa: N802 (stdlib handler contract)
        if self.path == "/healthz":
            self._send_json(200, {"ok": True})
            return
        if self.path == "/status":
            self._send_json(200, self.server.status_provider().to_dict())
            return
        self._send_json(404, {"error": {"code": "not_found", "message": "Unknown path."}})

    def do_POST(self) -> None:  # noqa: N802 (stdlib handler contract)
        if self.path != "/v1/observations":
            self._send_json(404, {"error": {"code": "not_found", "message": "Unknown path."}})
            return
        if not self._authorized():
            self._send_json(
                401,
                {"error": {"code": "unauthorized", "message": "Invalid collector token."}},
            )
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            self._send_json(422, _validation_error("Request body is required."))
            return
        raw = self.rfile.read(length)
        try:
            status, body = validate_and_spool(raw, self.server.spool, self.server.max_batch_size)
        except Exception:  # pragma: no cover - never let a handler 500 the app
            logger.exception("collector receiver failed to handle a request")
            self._send_json(
                500, {"error": {"code": "internal_error", "message": "Internal error."}}
            )
            return
        self._send_json(status, body)


class ObservationReceiver:
    """Owns the bound HTTP server. Bind happens in ``__init__`` so a port
    conflict surfaces immediately to the caller (the service)."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        spool: Spool,
        inbound_token: str | None,
        max_batch_size: int,
        status_provider: Callable[[], CollectorStatus],
    ) -> None:
        self._server = _CollectorHTTPServer((host, port), _Handler)
        self._server.spool = spool
        self._server.inbound_token = inbound_token
        self._server.max_batch_size = max_batch_size
        self._server.status_provider = status_provider

    @property
    def host(self) -> str:
        return str(self._server.server_address[0])

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    def serve_forever(self) -> None:
        self._server.serve_forever()

    def shutdown(self) -> None:
        self._server.shutdown()
        self._server.server_close()
