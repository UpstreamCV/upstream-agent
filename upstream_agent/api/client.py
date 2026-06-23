"""HTTP client for the UpStream API.

:class:`UpstreamApiClient` is a thin synchronous wrapper around
:mod:`httpx`. It owns three responsibilities:

1. **Build authenticated requests.** The token is attached via the
   isolated :meth:`_build_auth_header` so swapping ``Bearer`` for an
   API-key header is a one-place edit.
2. **Translate transport errors and HTTP status codes into typed
   :class:`~upstream_agent.errors.ApiError` subclasses.** Callers
   never see raw httpx exceptions.
3. **Parse JSON responses into Pydantic models** declared in
   :mod:`upstream_agent.api.schemas`. Missing or malformed fields
   surface as :class:`~upstream_agent.errors.ApiContractError`.

Logging policy: the client emits **no** request or response bodies
to logs. Anything that might contain tokens or session URLs stays in
memory and is discarded after the response is parsed.

Endpoint paths are module-level constants so any future backend path
change is a single-line edit. The error-body envelope and code names
are documented in ``docs/api-contracts.md`` and match the confirmed
backend contract (M3.2).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from upstream_agent.api.schemas import (
    AgentRegisterRequest,
    AgentRegisterResponse,
    BootstrapResponse,
    CameraClaimRequest,
    CameraClaimResponse,
    CameraRegisterRequest,
    CameraRegisterResponse,
    Heartbeat,
    HeartbeatBatchRequest,
    HeartbeatBatchResponse,
    ObservationBatchRequest,
    ObservationBatchResponse,
    Session,
    StopStreamSessionRequest,
    StopStreamSessionResponse,
    StreamSessionRequest,
    StreamSessionResponse,
)
from upstream_agent.errors import (
    ApiAuthError,
    ApiConnectionError,
    ApiContractError,
    ApiError,
    ApiRateLimitError,
    ApiServerError,
    ApiTokenExpiredError,
    ApiTokenRevokedError,
    ApiValidationError,
)
from upstream_agent.version import __version__

if TYPE_CHECKING:
    from upstream_agent.config import ApiConfig

logger = logging.getLogger(__name__)

# Endpoint paths — CONFIRMED contract. See docs/api-contracts.md.
AGENT_REGISTER_PATH: str = "/v1/agents/register"
# Server-side context for this token's account: account/limits/usage/
# cameras/active sessions. The response never carries secrets.
AGENT_BOOTSTRAP_PATH: str = "/v1/agents/bootstrap"
CAMERA_REGISTER_PATH: str = "/v1/cameras/register"
# Path-interpolated. Assigns a dashboard-created camera to this agent.
CAMERA_CLAIM_PATH_TEMPLATE: str = "/v1/cameras/{camera_id}/claim"
STREAM_SESSION_START_PATH: str = "/v1/stream-sessions/start"
# Path-interpolated. Build with .format(session_id=...).
STREAM_SESSION_STOP_PATH_TEMPLATE: str = "/v1/stream-sessions/{session_id}/stop"
# Read a single session's current state. Returns the flat session
# record (Session in :mod:`upstream_agent.api.schemas`) — the M7
# recovery path polls this after FFmpeg starts to confirm remote
# ingest is live before declaring the stream up.
STREAM_SESSION_GET_PATH_TEMPLATE: str = "/v1/stream-sessions/{session_id}"
# Heartbeat endpoint takes ``agent_id`` as a query parameter, not a path part.
HEARTBEAT_PATH: str = "/v1/heartbeats"
# Upstream Reliability M1a ingest: the collector uploads buffered observation
# batches here. Account is derived from the bearer token, never the body.
RELIABILITY_OBSERVATIONS_PATH: str = "/v1/reliability/observations"

# Stable error codes the backend returns inside the error envelope.
TOKEN_REVOKED_ERROR_CODE: str = "token_revoked"
TOKEN_EXPIRED_ERROR_CODE: str = "token_expired"

T = TypeVar("T", bound=BaseModel)


@dataclass(frozen=True)
class ParsedError:
    """Fields the client extracts from the backend's error envelope.

    Any field may be ``None`` when the body is missing, not JSON, or
    doesn't match the expected shape — the client still raises a typed
    error in that case and the formatted message degrades gracefully.
    """

    code: str | None
    message: str | None
    request_id: str | None


class UpstreamApiClient:
    """Synchronous HTTP client for the UpStream API.

    Use as a context manager so the underlying httpx connection is
    released even on failure::

        with UpstreamApiClient.from_config(cfg.api) as client:
            resp = client.register_agent(req)
    """

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout: float = 10.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._timeout = timeout
        client_kwargs: dict[str, Any] = {
            "base_url": self._base_url,
            "timeout": timeout,
            "headers": {"User-Agent": f"upstream-agent/{__version__}"},
        }
        if transport is not None:
            client_kwargs["transport"] = transport
        self._client = httpx.Client(**client_kwargs)

    @classmethod
    def from_config(cls, cfg: ApiConfig) -> UpstreamApiClient:
        """Build a client from an :class:`ApiConfig` slice."""

        return cls(base_url=cfg.base_url, token=cfg.token)

    # ------------------------------------------------------------------
    # Context manager / lifecycle
    # ------------------------------------------------------------------

    def __enter__(self) -> UpstreamApiClient:
        return self

    def __exit__(
        self, exc_type: object, exc: object, tb: object
    ) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    # ------------------------------------------------------------------
    # Public endpoint methods
    # ------------------------------------------------------------------

    def register_agent(self, req: AgentRegisterRequest) -> AgentRegisterResponse:
        return self._request(
            "POST", AGENT_REGISTER_PATH, req, AgentRegisterResponse
        )

    def get_bootstrap(self) -> BootstrapResponse:
        """Fetch the server-side context for this token's account.

        One call answers: which account this token belongs to, what
        limits apply, which cameras exist (with their server-controlled
        config), what sessions are active, and current usage. The
        response carries no secrets. Servers that predate the endpoint
        return 404, which surfaces as :class:`ApiValidationError` —
        callers treat that as "bootstrap unavailable" and degrade
        gracefully.
        """

        return self._request("GET", AGENT_BOOTSTRAP_PATH, None, BootstrapResponse)

    def register_camera(self, req: CameraRegisterRequest) -> CameraRegisterResponse:
        return self._request(
            "POST", CAMERA_REGISTER_PATH, req, CameraRegisterResponse
        )

    def claim_camera(self, *, camera_id: str, agent_id: str) -> CameraClaimResponse:
        """Claim a dashboard-created (unassigned) camera for this agent.

        Idempotent when the camera is already held by ``agent_id``. A
        camera held by a different agent surfaces as
        :class:`ApiValidationError` (409 ``camera_claimed_by_other_agent``).
        """

        path = CAMERA_CLAIM_PATH_TEMPLATE.format(camera_id=camera_id)
        return self._request(
            "POST", path, CameraClaimRequest(agent_id=agent_id), CameraClaimResponse
        )

    def start_stream_session(
        self, req: StreamSessionRequest
    ) -> StreamSessionResponse:
        return self._request(
            "POST", STREAM_SESSION_START_PATH, req, StreamSessionResponse
        )

    def stop_stream_session(self, session_id: str) -> StopStreamSessionResponse:
        """Stop the named stream session.

        Called by the API-check diagnostic after a successful
        ``start_stream_session`` to release the camera so a subsequent
        check (or any other start) doesn't hit
        ``camera_already_streaming``.

        The endpoint takes no payload — the session id is on the URL.
        The agent does not need to inspect the response body; the
        HTTP 200 is the signal.
        """

        path = STREAM_SESSION_STOP_PATH_TEMPLATE.format(session_id=session_id)
        return self._request(
            "POST", path, StopStreamSessionRequest(), StopStreamSessionResponse
        )

    def get_stream_session(self, session_id: str) -> Session:
        """Read the current state of a stream session.

        Used by the M7 recovery path: after a fresh
        :meth:`start_stream_session` and FFmpeg startup, the supervisor
        polls this endpoint until the server-side ``state`` flips to
        ``"streaming"`` — i.e. MediaMTX's ingest webhook has confirmed
        the stream is live. Local FFmpeg startup alone is **not** proof
        that the dashboard can see frames; only the API can confirm
        remote ingest.

        Errors are translated via the same typed
        :class:`~upstream_agent.errors.ApiError` mapping as every other
        endpoint. A 404 surfaces as :class:`ApiValidationError`
        (fatal — unknown session id).
        """

        path = STREAM_SESSION_GET_PATH_TEMPLATE.format(session_id=session_id)
        return self._request("GET", path, None, Session)

    def send_heartbeat(
        self, *, agent_id: str, heartbeat: Heartbeat
    ) -> HeartbeatBatchResponse:
        """Submit a single-heartbeat batch for ``agent_id``.

        ``agent_id`` is sent as a query parameter; the heartbeat
        itself is wrapped in a one-item list to match the batch
        endpoint shape. Errors are translated via the same typed
        :class:`~upstream_agent.errors.ApiError` mapping as every
        other endpoint.
        """

        body = HeartbeatBatchRequest(heartbeats=[heartbeat])
        return self._request(
            "POST",
            HEARTBEAT_PATH,
            body,
            HeartbeatBatchResponse,
            params={"agent_id": agent_id},
        )

    def upload_observations(
        self, req: ObservationBatchRequest
    ) -> ObservationBatchResponse:
        """Upload one buffered observation batch to Upstream Cloud (M1a).

        Errors are translated via the same typed
        :class:`~upstream_agent.errors.ApiError` mapping as every other
        endpoint; the collector's flusher decides whether to retry (transient)
        or pause (fatal auth). The request body is never logged.
        """

        return self._request(
            "POST",
            RELIABILITY_OBSERVATIONS_PATH,
            req,
            ObservationBatchResponse,
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _build_auth_header(self) -> dict[str, str]:
        """Construct the auth header.

        Isolated so swapping ``Bearer`` for ``X-API-Key`` or any other
        scheme is a single-method change.
        """

        return {"Authorization": f"Bearer {self._token}"}

    def _request(
        self,
        method: str,
        path: str,
        body: BaseModel | None,
        response_model: type[T],
        *,
        params: dict[str, str] | None = None,
    ) -> T:
        headers = {
            **self._build_auth_header(),
            "Accept": "application/json",
        }
        request_kwargs: dict[str, Any] = {"headers": headers}
        if body is not None:
            headers["Content-Type"] = "application/json"
            request_kwargs["json"] = body.model_dump(mode="json", exclude_none=True)
        if params is not None:
            request_kwargs["params"] = params

        try:
            response = self._client.request(method, path, **request_kwargs)
        except httpx.TimeoutException as exc:
            raise ApiConnectionError(
                f"Timed out talking to API server at {self._base_url}."
            ) from exc
        except httpx.TransportError as exc:
            raise ApiConnectionError(
                f"Could not connect to API server at {self._base_url}."
            ) from exc

        self._raise_for_status(response, method, path)

        try:
            data = response.json()
        except ValueError as exc:
            raise ApiContractError(
                f"API response from {method} {path} was not valid JSON."
            ) from exc

        try:
            return response_model.model_validate(data)
        except ValidationError as exc:
            errors = exc.errors(include_input=False, include_url=False)
            lines = []
            for err in errors:
                loc = ".".join(str(part) for part in err["loc"]) or "<root>"
                lines.append(f"  - {loc}: {err['msg']}")
            raise ApiContractError(
                f"API response from {method} {path} did not match expected shape:\n"
                + "\n".join(lines)
            ) from None

    @staticmethod
    def _parse_error_body(response: httpx.Response) -> ParsedError:
        """Extract ``(code, message, request_id)`` from the confirmed
        error envelope shape: ``{"error": {"code", "message",
        "details", "request_id"}}``.

        Tolerates a body that is absent, not valid JSON, not a JSON
        object, or missing the ``"error"`` key. The client always
        raises a typed error regardless; this helper just provides
        whatever context is available.
        """

        try:
            data = response.json()
        except ValueError:
            return ParsedError(None, None, None)
        if not isinstance(data, dict):
            return ParsedError(None, None, None)
        err = data.get("error")
        if not isinstance(err, dict):
            return ParsedError(None, None, None)

        code = err.get("code") if isinstance(err.get("code"), str) else None
        message = err.get("message") if isinstance(err.get("message"), str) else None
        request_id = (
            err.get("request_id")
            if isinstance(err.get("request_id"), str)
            else None
        )
        return ParsedError(code, message, request_id)

    @staticmethod
    def _format_error(
        method: str, path: str, status: int, reason: str, parsed: ParsedError
    ) -> str:
        """Build the human-readable error string.

        Format: ``"{method} {path} -> {status} {reason}[, code={c}]
        [, request_id={rid}][: {message}]"``. **Never** includes the
        request body or the raw response body — only the parsed,
        non-sensitive fields.
        """

        parts = [f"{method} {path} -> {status} {reason}".rstrip()]
        meta: list[str] = []
        if parsed.code:
            meta.append(f"code={parsed.code}")
        if parsed.request_id:
            meta.append(f"request_id={parsed.request_id}")
        if meta:
            parts.append(", ".join(meta))
        suffix = ", ".join(parts)
        if parsed.message:
            return f"{suffix}: {parsed.message}"
        return suffix

    def _raise_for_status(
        self, response: httpx.Response, method: str, path: str
    ) -> None:
        status = response.status_code
        if status < 400:
            return

        parsed = self._parse_error_body(response)
        reason = response.reason_phrase or ""
        msg = self._format_error(method, path, status, reason, parsed)

        if status == 401:
            if parsed.code == TOKEN_REVOKED_ERROR_CODE:
                raise ApiTokenRevokedError(msg)
            if parsed.code == TOKEN_EXPIRED_ERROR_CODE:
                raise ApiTokenExpiredError(msg)
            raise ApiAuthError(msg)
        if status == 403:
            # 403 with a `limit_exceeded.*` code is a quota/limit
            # rejection, which the CLI surfaces as a register/session
            # validation error rather than an auth error.
            if parsed.code and parsed.code.startswith("limit_exceeded."):
                raise ApiValidationError(msg)
            raise ApiAuthError(msg)
        if status == 404:
            # agent_not_found / camera_not_found / etc — treated as
            # validation so the CLI's `Error (register_camera): ...`
            # line reads naturally.
            raise ApiValidationError(msg)
        if status in (400, 409, 422):
            raise ApiValidationError(msg)
        if status == 429:
            raise ApiRateLimitError(msg)
        if status >= 500:
            raise ApiServerError(msg)
        raise ApiError(msg)
