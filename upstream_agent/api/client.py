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

Endpoint paths are module-level constants so the eventual swap to a
finalised backend contract is mechanical. They are documented as
provisional in ``docs/api-contracts.md``.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from upstream_agent.api.schemas import (
    AgentRegisterRequest,
    AgentRegisterResponse,
    CameraRegisterRequest,
    CameraRegisterResponse,
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
    ApiTokenRevokedError,
    ApiValidationError,
)
from upstream_agent.version import __version__

if TYPE_CHECKING:
    from upstream_agent.config import ApiConfig

logger = logging.getLogger(__name__)

# Endpoint paths — PROVISIONAL, pending backend confirmation.
# See docs/api-contracts.md for the assumed payload shapes.
AGENT_REGISTER_PATH: str = "/v1/agents/register"
CAMERA_REGISTER_PATH: str = "/v1/cameras/register"
STREAM_SESSION_START_PATH: str = "/v1/stream-sessions/start"

# Sentinel error code the backend is assumed to return when a token has
# been revoked. PROVISIONAL — confirm with backend team.
TOKEN_REVOKED_ERROR_CODE: str = "token_revoked"

T = TypeVar("T", bound=BaseModel)


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

    def register_camera(self, req: CameraRegisterRequest) -> CameraRegisterResponse:
        return self._request(
            "POST", CAMERA_REGISTER_PATH, req, CameraRegisterResponse
        )

    def start_stream_session(
        self, req: StreamSessionRequest
    ) -> StreamSessionResponse:
        return self._request(
            "POST", STREAM_SESSION_START_PATH, req, StreamSessionResponse
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
        body: BaseModel,
        response_model: type[T],
    ) -> T:
        headers = {
            **self._build_auth_header(),
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        payload = body.model_dump(mode="json", exclude_none=True)

        try:
            response = self._client.request(
                method, path, headers=headers, json=payload
            )
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
    def _parse_error_body(
        response: httpx.Response,
    ) -> tuple[str | None, str | None]:
        """Extract ``(error_code, error_message)`` from a JSON error body.

        Returns ``(None, None)`` if the body is missing, not JSON, not
        a mapping, or carries neither key. The shape is provisional:
        either ``error`` or ``message`` is accepted for the human
        string.
        """

        try:
            data = response.json()
        except ValueError:
            return None, None
        if not isinstance(data, dict):
            return None, None
        code = data.get("error_code") or data.get("code")
        message = data.get("error") or data.get("message")
        if code is not None and not isinstance(code, str):
            code = None
        if message is not None and not isinstance(message, str):
            message = None
        return code, message

    def _raise_for_status(
        self, response: httpx.Response, method: str, path: str
    ) -> None:
        status = response.status_code
        if status < 400:
            return

        error_code, parsed_msg = self._parse_error_body(response)
        reason = response.reason_phrase or ""
        suffix = f": {parsed_msg}" if parsed_msg else ""
        msg = f"{method} {path} -> {status} {reason}{suffix}".rstrip()

        if status in (401, 403):
            if error_code == TOKEN_REVOKED_ERROR_CODE:
                raise ApiTokenRevokedError(
                    f"API token was rejected as revoked. {msg}"
                )
            raise ApiAuthError(f"API token was rejected. {msg}")
        if status in (400, 422):
            raise ApiValidationError(f"API rejected the request. {msg}")
        if status == 429:
            raise ApiRateLimitError(f"API rate limit exceeded. {msg}")
        if status >= 500:
            raise ApiServerError(f"API server error. {msg}")
        raise ApiError(msg)
