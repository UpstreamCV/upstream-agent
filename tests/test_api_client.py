"""Tests for :class:`upstream_agent.api.client.UpstreamApiClient`.

Strategy: build a small ``httpx.MockTransport`` that:

* captures each request (method, path, headers, body) on a list, and
* returns a scripted response per call.

This lets the test assert exactly what got sent without standing up a
real server or pulling in a mocking library.
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from upstream_agent.api.client import (
    AGENT_REGISTER_PATH,
    CAMERA_REGISTER_PATH,
    STREAM_SESSION_START_PATH,
    UpstreamApiClient,
)
from upstream_agent.api.schemas import (
    AgentRegisterRequest,
    CameraRegisterRequest,
    StreamSessionRequest,
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

BASE_URL = "http://api.test.local"
TOKEN = "sk_live_abcd1234_doNOT_leak"


class CapturingTransport:
    """httpx transport that records requests and returns scripted responses."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self._handler: Callable[[httpx.Request], httpx.Response] | None = None

    def respond_with(
        self, handler: Callable[[httpx.Request], httpx.Response]
    ) -> httpx.MockTransport:
        self._handler = handler

        def _wrapper(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            assert self._handler is not None
            return self._handler(request)

        return httpx.MockTransport(_wrapper)


def _make_client(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    token: str = TOKEN,
) -> tuple[UpstreamApiClient, CapturingTransport]:
    cap = CapturingTransport()
    transport = cap.respond_with(handler)
    client = UpstreamApiClient(BASE_URL, token, transport=transport)
    return client, cap


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


def test_register_agent_happy_path_returns_parsed_response() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"agent_id": "agent_123"})

    client, _ = _make_client(handler)
    with client:
        resp = client.register_agent(
            AgentRegisterRequest(
                device_id="dev_1", agent_version="0.1.0", platform="Darwin"
            )
        )
    assert resp.agent_id == "agent_123"


def test_register_agent_uses_post_and_correct_path() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"agent_id": "agent_1"})

    client, cap = _make_client(handler)
    with client:
        client.register_agent(
            AgentRegisterRequest(device_id="dev_1", agent_version="0.1.0")
        )
    assert len(cap.requests) == 1
    assert cap.requests[0].method == "POST"
    assert cap.requests[0].url.path == AGENT_REGISTER_PATH


def test_register_agent_sends_bearer_authorization_header() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"agent_id": "agent_1"})

    client, cap = _make_client(handler)
    with client:
        client.register_agent(
            AgentRegisterRequest(device_id="dev_1", agent_version="0.1.0")
        )
    auth = cap.requests[0].headers.get("Authorization")
    assert auth == f"Bearer {TOKEN}"


def test_request_omits_none_fields_in_body() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        import json as _json

        body = _json.loads(req.content)
        assert "platform" not in body  # platform was None, must be omitted
        return httpx.Response(200, json={"agent_id": "agent_1"})

    client, _ = _make_client(handler)
    with client:
        client.register_agent(
            AgentRegisterRequest(device_id="dev_1", agent_version="0.1.0")
        )


def test_register_camera_happy_path_uses_correct_path() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"camera_id": "cam_42"})

    client, cap = _make_client(handler)
    with client:
        resp = client.register_camera(
            CameraRegisterRequest(
                agent_id="agent_1",
                name="Front Door",
                source_type="webcam",
                source=0,
                requested_width=1280,
                requested_height=720,
                requested_fps=30,
            )
        )
    assert resp.camera_id == "cam_42"
    assert cap.requests[0].url.path == CAMERA_REGISTER_PATH


def test_start_stream_session_happy_path() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "session_id": "sess_99",
                "approved_config": {
                    "codec": "h264",
                    "width": 1280,
                    "height": 720,
                    "fps": 30,
                    "srt_url": "srt://stream.test.local:1935/live/secret",
                },
            },
        )

    client, cap = _make_client(handler)
    with client:
        resp = client.start_stream_session(
            StreamSessionRequest(
                agent_id="agent_1",
                camera_id="cam_1",
                source_type="webcam",
                requested_width=1280,
                requested_height=720,
                requested_fps=30,
            )
        )
    assert resp.session_id == "sess_99"
    assert resp.approved_config.codec == "h264"
    assert cap.requests[0].url.path == STREAM_SESSION_START_PATH


# ---------------------------------------------------------------------------
# Error status mapping
# ---------------------------------------------------------------------------


def _agent_req() -> AgentRegisterRequest:
    return AgentRegisterRequest(device_id="dev_1", agent_version="0.1.0")


def test_401_without_token_revoked_code_raises_auth_error() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "bad token"})

    client, _ = _make_client(handler)
    with client, pytest.raises(ApiAuthError) as excinfo:
        client.register_agent(_agent_req())
    # And not the more specific subclass:
    assert not isinstance(excinfo.value, ApiTokenRevokedError)


def test_401_with_token_revoked_code_raises_token_revoked_error() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401,
            json={"error_code": "token_revoked", "error": "token revoked"},
        )

    client, _ = _make_client(handler)
    with client, pytest.raises(ApiTokenRevokedError) as excinfo:
        client.register_agent(_agent_req())
    # Subclass relationship preserved:
    assert isinstance(excinfo.value, ApiAuthError)


def test_403_maps_to_auth_error() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": "forbidden"})

    client, _ = _make_client(handler)
    with client, pytest.raises(ApiAuthError):
        client.register_agent(_agent_req())


@pytest.mark.parametrize("status", [400, 422])
def test_4xx_validation_codes_map_to_validation_error(status: int) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"error": "bad request"})

    client, _ = _make_client(handler)
    with client, pytest.raises(ApiValidationError):
        client.register_agent(_agent_req())


def test_429_maps_to_rate_limit_error() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": "slow down"})

    client, _ = _make_client(handler)
    with client, pytest.raises(ApiRateLimitError):
        client.register_agent(_agent_req())


def test_503_maps_to_server_error() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "unavailable"})

    client, _ = _make_client(handler)
    with client, pytest.raises(ApiServerError):
        client.register_agent(_agent_req())


def test_404_maps_to_bare_api_error() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": "not found"})

    client, _ = _make_client(handler)
    with client, pytest.raises(ApiError) as excinfo:
        client.register_agent(_agent_req())
    # Not one of the specific subclasses:
    assert type(excinfo.value) is ApiError


# ---------------------------------------------------------------------------
# Transport-layer errors
# ---------------------------------------------------------------------------


def test_connect_error_maps_to_api_connection_error() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=req)

    client, _ = _make_client(handler)
    with client, pytest.raises(ApiConnectionError) as excinfo:
        client.register_agent(_agent_req())
    assert BASE_URL in str(excinfo.value)


def test_read_timeout_maps_to_api_connection_error() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("read timeout", request=req)

    client, _ = _make_client(handler)
    with client, pytest.raises(ApiConnectionError) as excinfo:
        client.register_agent(_agent_req())
    assert "imed out" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Contract errors
# ---------------------------------------------------------------------------


def test_response_missing_required_field_raises_contract_error() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"unrelated": "value"})

    client, _ = _make_client(handler)
    with client, pytest.raises(ApiContractError) as excinfo:
        client.register_agent(_agent_req())
    assert "agent_id" in str(excinfo.value)


def test_response_with_blank_required_field_raises_contract_error() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"agent_id": ""})

    client, _ = _make_client(handler)
    with client, pytest.raises(ApiContractError):
        client.register_agent(_agent_req())


def test_non_json_response_body_raises_contract_error() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>nope</html>")

    client, _ = _make_client(handler)
    with client, pytest.raises(ApiContractError):
        client.register_agent(_agent_req())


# ---------------------------------------------------------------------------
# Token-leak guard
# ---------------------------------------------------------------------------


def test_token_does_not_appear_in_any_error_message() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "denied"})

    client, _ = _make_client(handler)
    with client:
        with pytest.raises(ApiError) as excinfo:
            client.register_agent(_agent_req())
        assert TOKEN not in str(excinfo.value)


def test_token_does_not_appear_in_connection_error_message() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=req)

    client, _ = _make_client(handler)
    with client:
        with pytest.raises(ApiConnectionError) as excinfo:
            client.register_agent(_agent_req())
        assert TOKEN not in str(excinfo.value)


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


def test_client_close_is_safe_to_call() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"agent_id": "x"})

    client, _ = _make_client(handler)
    client.close()
    # Calling close again should not raise.
    client.close()


def test_from_config_builds_client() -> None:
    from upstream_agent.config import ApiConfig

    cfg = ApiConfig(base_url="https://example.test", token="sk_test_abcd")
    client = UpstreamApiClient.from_config(cfg)
    try:
        # Trust normalisation happens during __init__:
        # base_url stored without trailing slash.
        assert client._base_url == "https://example.test"
    finally:
        client.close()


def test_base_url_trailing_slash_is_normalised() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"agent_id": "agent_1"})

    cap = CapturingTransport()
    transport = cap.respond_with(handler)
    client = UpstreamApiClient(
        "http://api.test.local/", TOKEN, transport=transport
    )
    try:
        client.register_agent(_agent_req())
    finally:
        client.close()
    # The path observed by the transport must not start with a doubled
    # slash even though the base_url ended with one.
    assert cap.requests[0].url.path == AGENT_REGISTER_PATH
