"""Tests for :class:`upstream_agent.api.client.UpstreamApiClient`.

Strategy: build a small ``httpx.MockTransport`` that captures each
request (method, path, headers, body) on a list and returns a
scripted response per call. Lets us assert exactly what got sent
without standing up a real server or pulling in a mocking library.

These tests cover the **confirmed** backend contract (M3.2):
- Agent register payload uses ``name``, ``version``, ``os``,
  ``python_version``, ``capabilities`` (NOT ``device_id``).
- Camera register uses ``external_id``, ``device_path``, ``default_*``
  fields.
- Stream session uses nested ``requested``.
- Error envelope is ``{"error": {"code", "message", "details",
  "request_id"}}``.
"""

from __future__ import annotations

import json as _json
from collections.abc import Callable

import httpx
import pytest

from upstream_agent.api.client import (
    AGENT_REGISTER_PATH,
    CAMERA_REGISTER_PATH,
    STREAM_SESSION_GET_PATH_TEMPLATE,
    STREAM_SESSION_START_PATH,
    STREAM_SESSION_STOP_PATH_TEMPLATE,
    UpstreamApiClient,
)
from upstream_agent.api.schemas import (
    AgentRegisterRequest,
    CameraRegisterRequest,
    RequestedStreamConfig,
    StreamSessionRequest,
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

BASE_URL = "http://api.test.local"
TOKEN = "ups_test_abcd1234_doNOT_leak_xxxxx"


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


# Sample happy-path response bodies matching the confirmed contract.
_AGENT_RESPONSE_BODY = {
    "agent": {
        "id": "0b91a1a8-7d3d-4c91-9c6e-ce5b9f0e6e3e",
        "account_id": "00000000-0000-0000-0000-000000000001",
        "name": "upstream-agent-12345678",
        "version": "0.1.0",
        "os": "darwin",
        "python_version": "3.12.13",
        "capabilities": {"ffmpeg": False, "srt": False},
        "status": "active",
    },
    "server_time": "2026-05-13T02:00:00Z",
}


def _agent_req() -> AgentRegisterRequest:
    return AgentRegisterRequest(
        name="upstream-agent-12345678",
        hostname=None,
        version="0.1.0",
        os="darwin",
        python_version="3.12.13",
        capabilities={"ffmpeg": False, "srt": False},
    )


def _camera_req() -> CameraRegisterRequest:
    return CameraRegisterRequest(
        agent_id="agent-1",
        name="Front Door",
        external_id="webcam-0",
        device_path="webcam:0",
        default_width=1280,
        default_height=720,
        default_fps=30,
        default_codec="h264",
        default_bitrate_kbps=3000,
        default_latency_mode="balanced",
        metadata={},
    )


def _session_req() -> StreamSessionRequest:
    return StreamSessionRequest(
        camera_id="cam-1",
        requested=RequestedStreamConfig(
            width=1280,
            height=720,
            fps=30,
            codec="h264",
            bitrate_kbps=3000,
            latency_mode="balanced",
        ),
    )


# ---------------------------------------------------------------------------
# Agent register happy path
# ---------------------------------------------------------------------------


def test_register_agent_happy_path_returns_parsed_response() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_AGENT_RESPONSE_BODY)

    client, _ = _make_client(handler)
    with client:
        resp = client.register_agent(_agent_req())
    assert resp.agent.id == "0b91a1a8-7d3d-4c91-9c6e-ce5b9f0e6e3e"
    assert resp.agent.account_id == "00000000-0000-0000-0000-000000000001"
    assert resp.server_time == "2026-05-13T02:00:00Z"


def test_register_agent_uses_post_and_correct_path() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_AGENT_RESPONSE_BODY)

    client, cap = _make_client(handler)
    with client:
        client.register_agent(_agent_req())
    assert len(cap.requests) == 1
    assert cap.requests[0].method == "POST"
    assert cap.requests[0].url.path == AGENT_REGISTER_PATH


def test_register_agent_sends_bearer_authorization_header() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_AGENT_RESPONSE_BODY)

    client, cap = _make_client(handler)
    with client:
        client.register_agent(_agent_req())
    auth = cap.requests[0].headers.get("Authorization")
    assert auth == f"Bearer {TOKEN}"


def test_register_agent_payload_matches_confirmed_shape() -> None:
    captured: dict[str, object] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured.update(_json.loads(req.content))
        return httpx.Response(200, json=_AGENT_RESPONSE_BODY)

    client, _ = _make_client(handler)
    with client:
        client.register_agent(_agent_req())

    assert captured["name"] == "upstream-agent-12345678"
    assert captured["version"] == "0.1.0"
    assert captured["os"] == "darwin"
    assert captured["python_version"] == "3.12.13"
    assert captured["capabilities"] == {"ffmpeg": False, "srt": False}
    # Hostname is None on the model; exclude_none drops it from the wire.
    assert "hostname" not in captured


# ---------------------------------------------------------------------------
# Camera register happy path
# ---------------------------------------------------------------------------


def test_register_camera_happy_path_wrapped_response() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "camera": {
                    "id": "cam_42",
                    "external_id": "webcam-0",
                    "status": "active",
                }
            },
        )

    client, cap = _make_client(handler)
    with client:
        resp = client.register_camera(_camera_req())
    assert resp.camera.id == "cam_42"
    assert resp.camera.external_id == "webcam-0"
    assert cap.requests[0].url.path == CAMERA_REGISTER_PATH


def test_register_camera_happy_path_flat_response() -> None:
    """Backend may also return camera fields at the top level — the
    pre-validator unwraps the flat case into ``{"camera": {...}}``."""

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"id": "cam_flat", "external_id": "webcam-0"}
        )

    client, _ = _make_client(handler)
    with client:
        resp = client.register_camera(_camera_req())
    assert resp.camera.id == "cam_flat"


def test_register_camera_payload_matches_confirmed_shape() -> None:
    captured: dict[str, object] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured.update(_json.loads(req.content))
        return httpx.Response(200, json={"camera": {"id": "cam_1"}})

    client, _ = _make_client(handler)
    with client:
        client.register_camera(_camera_req())

    assert captured["agent_id"] == "agent-1"
    assert captured["external_id"] == "webcam-0"
    assert captured["device_path"] == "webcam:0"
    assert captured["default_width"] == 1280
    assert captured["default_codec"] == "h264"
    assert captured["default_bitrate_kbps"] == 3000
    assert captured["default_latency_mode"] == "balanced"
    assert captured["metadata"] == {}


# ---------------------------------------------------------------------------
# Stream session happy path
# ---------------------------------------------------------------------------


_SESSION_RESPONSE_BODY = {
    "session": {
        "id": "sess_99",
        "account_id": "00000000-0000-0000-0000-000000000001",
        "agent_id": "agent-1",
        "camera_id": "cam-1",
        "state": "approved",
        "srt_url": "srt://ingest.test.local:8890?streamid=publish:abc",
        "streamid": "publish:abc",
        "expires_at": "2026-05-13T06:00:00Z",
        "requested": {
            "width": 1280,
            "height": 720,
            "fps": 30,
            "codec": "h264",
            "bitrate_kbps": 3000,
            "latency_mode": "balanced",
        },
        "approved": {
            "width": 1280,
            "height": 720,
            "fps": 30,
            "codec": "h264",
            "bitrate_kbps": 3000,
            "latency_mode": "balanced",
        },
    },
    "srt_url": "srt://ingest.test.local:8890?streamid=publish:abc",
    "session_token": "eyJhbGciOiJIUzI1NiI.fakejwt.payload",
    "expires_at": "2026-05-13T06:00:00Z",
}


def test_start_stream_session_happy_path_parses_nested_session() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_SESSION_RESPONSE_BODY)

    client, cap = _make_client(handler)
    with client:
        resp = client.start_stream_session(_session_req())

    assert resp.session.id == "sess_99"
    assert resp.session.state == "approved"
    assert resp.session.approved is not None
    assert resp.session.approved.bitrate_kbps == 3000
    assert resp.session.approved.latency_mode == "balanced"
    assert resp.srt_url is not None  # carried, never logged
    assert resp.session_token is not None
    assert cap.requests[0].url.path == STREAM_SESSION_START_PATH


def test_start_stream_session_payload_nests_requested() -> None:
    captured: dict[str, object] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured.update(_json.loads(req.content))
        return httpx.Response(200, json=_SESSION_RESPONSE_BODY)

    client, _ = _make_client(handler)
    with client:
        client.start_stream_session(_session_req())

    assert captured["camera_id"] == "cam-1"
    assert isinstance(captured["requested"], dict)
    requested = captured["requested"]
    assert requested["codec"] == "h264"
    assert requested["bitrate_kbps"] == 3000
    assert requested["latency_mode"] == "balanced"


# ---------------------------------------------------------------------------
# Stop stream session
# ---------------------------------------------------------------------------


def test_stop_stream_session_uses_interpolated_path_and_empty_body() -> None:
    captured_path: dict[str, str] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured_path["path"] = req.url.path
        captured_path["method"] = req.method
        captured_path["body"] = req.content.decode("utf-8") if req.content else ""
        return httpx.Response(200, json={})

    client, cap = _make_client(handler)
    with client:
        client.stop_stream_session("sess_42")

    expected = STREAM_SESSION_STOP_PATH_TEMPLATE.format(session_id="sess_42")
    assert captured_path["path"] == expected
    assert captured_path["method"] == "POST"
    # Empty model serialises to {}.
    assert captured_path["body"] == "{}"
    # Bearer header still attached.
    assert cap.requests[0].headers.get("Authorization") == f"Bearer {TOKEN}"


def test_stop_stream_session_returns_parsed_response_for_any_body() -> None:
    """The response model is permissive — empty / wrapped / arbitrary
    shapes must all parse."""

    bodies = [{}, {"session": {"id": "sess_1", "state": "stopped"}}, {"foo": "bar"}]
    for body in bodies:
        def handler(req: httpx.Request, _b: dict = body) -> httpx.Response:
            return httpx.Response(200, json=_b)

        client, _ = _make_client(handler)
        with client:
            # Should not raise — the model accepts anything.
            client.stop_stream_session("sess_1")


def test_stop_stream_session_404_maps_to_validation_error() -> None:
    """If the session has already expired server-side, the stop call
    may return 404. Same mapping as other 404s."""

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json=_error_body("session_not_found"))

    client, _ = _make_client(handler)
    with client, pytest.raises(ApiValidationError):
        client.stop_stream_session("sess_missing")


def test_stop_stream_session_5xx_maps_to_server_error() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json=_error_body("unavailable"))

    client, _ = _make_client(handler)
    with client, pytest.raises(ApiServerError):
        client.stop_stream_session("sess_x")


# ---------------------------------------------------------------------------
# Error envelope mapping (confirmed shape)
# ---------------------------------------------------------------------------


def _error_body(code: str, message: str = "msg", request_id: str = "rid_001") -> dict:
    return {
        "error": {
            "code": code,
            "message": message,
            "details": {},
            "request_id": request_id,
        }
    }


def test_401_invalid_token_maps_to_auth_error() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json=_error_body("invalid_token"))

    client, _ = _make_client(handler)
    with client, pytest.raises(ApiAuthError) as excinfo:
        client.register_agent(_agent_req())
    assert "code=invalid_token" in str(excinfo.value)
    assert "request_id=rid_001" in str(excinfo.value)
    # Not the more specific subclasses:
    assert not isinstance(excinfo.value, ApiTokenRevokedError)
    assert not isinstance(excinfo.value, ApiTokenExpiredError)


def test_401_token_revoked_maps_to_token_revoked_error() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json=_error_body("token_revoked"))

    client, _ = _make_client(handler)
    with client, pytest.raises(ApiTokenRevokedError) as excinfo:
        client.register_agent(_agent_req())
    assert isinstance(excinfo.value, ApiAuthError)
    assert "code=token_revoked" in str(excinfo.value)


def test_401_token_expired_maps_to_token_expired_error() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json=_error_body("token_expired"))

    client, _ = _make_client(handler)
    with client, pytest.raises(ApiTokenExpiredError) as excinfo:
        client.register_agent(_agent_req())
    assert isinstance(excinfo.value, ApiAuthError)
    assert "code=token_expired" in str(excinfo.value)


def test_403_limit_exceeded_maps_to_validation_error() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        body = _error_body(
            "limit_exceeded.max_cameras",
            message="Camera slot limit reached: 3 of 3.",
        )
        # Test data details — included to confirm we don't leak them.
        body["error"]["details"] = {"in_use": 3, "max": 3}
        return httpx.Response(403, json=body)

    client, _ = _make_client(handler)
    with client, pytest.raises(ApiValidationError) as excinfo:
        client.register_camera(_camera_req())
    assert "code=limit_exceeded.max_cameras" in str(excinfo.value)
    assert "request_id=" in str(excinfo.value)


def test_403_without_limit_code_maps_to_auth_error() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json=_error_body("forbidden"))

    client, _ = _make_client(handler)
    with client, pytest.raises(ApiAuthError):
        client.register_agent(_agent_req())


def test_404_camera_not_found_maps_to_validation_error() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json=_error_body("camera_not_found"))

    client, _ = _make_client(handler)
    with client, pytest.raises(ApiValidationError):
        client.start_stream_session(_session_req())


def test_409_camera_already_streaming_maps_to_validation_error() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json=_error_body("camera_already_streaming"))

    client, _ = _make_client(handler)
    with client, pytest.raises(ApiValidationError):
        client.start_stream_session(_session_req())


@pytest.mark.parametrize("status", [400, 422])
def test_400_422_validation_codes_map_to_validation_error(status: int) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=_error_body("validation"))

    client, _ = _make_client(handler)
    with client, pytest.raises(ApiValidationError):
        client.register_agent(_agent_req())


def test_429_maps_to_rate_limit_error() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json=_error_body("rate_limited"))

    client, _ = _make_client(handler)
    with client, pytest.raises(ApiRateLimitError):
        client.register_agent(_agent_req())


def test_503_maps_to_server_error() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json=_error_body("unavailable"))

    client, _ = _make_client(handler)
    with client, pytest.raises(ApiServerError):
        client.register_agent(_agent_req())


def test_unmapped_4xx_maps_to_bare_api_error() -> None:
    """A 418 with no recognised code should fall through to bare ApiError."""

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(418, json=_error_body("teapot"))

    client, _ = _make_client(handler)
    with client, pytest.raises(ApiError) as excinfo:
        client.register_agent(_agent_req())
    assert type(excinfo.value) is ApiError


def test_error_envelope_missing_fields_still_raises_typed_error() -> None:
    """Bodies without ``error.code`` / ``error.message`` should still
    produce a typed error; the formatted message gracefully omits the
    missing pieces."""

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={})

    client, _ = _make_client(handler)
    with client, pytest.raises(ApiRateLimitError) as excinfo:
        client.register_agent(_agent_req())
    # No "code=" segment because no code was present.
    assert "code=" not in str(excinfo.value)


def test_error_envelope_non_dict_still_raises_typed_error() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(500, content=b"<html>boom</html>")

    client, _ = _make_client(handler)
    with client, pytest.raises(ApiServerError) as excinfo:
        client.register_agent(_agent_req())
    assert "500" in str(excinfo.value)


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


def test_agent_response_missing_id_raises_contract_error() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"agent": {"name": "x"}})

    client, _ = _make_client(handler)
    with client, pytest.raises(ApiContractError) as excinfo:
        client.register_agent(_agent_req())
    assert "id" in str(excinfo.value).lower()


def test_agent_response_blank_id_raises_contract_error() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"agent": {"id": ""}})

    client, _ = _make_client(handler)
    with client, pytest.raises(ApiContractError):
        client.register_agent(_agent_req())


def test_session_response_missing_session_id_raises_contract_error() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        body = dict(_SESSION_RESPONSE_BODY)
        body["session"] = {k: v for k, v in body["session"].items() if k != "id"}
        return httpx.Response(200, json=body)

    client, _ = _make_client(handler)
    with client, pytest.raises(ApiContractError):
        client.start_stream_session(_session_req())


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
        return httpx.Response(401, json=_error_body("invalid_token"))

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
# Lifecycle / base URL
# ---------------------------------------------------------------------------


def test_client_close_is_safe_to_call() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_AGENT_RESPONSE_BODY)

    client, _ = _make_client(handler)
    client.close()
    client.close()


def test_from_config_builds_client() -> None:
    from upstream_agent.config import ApiConfig

    cfg = ApiConfig(base_url="https://example.test", token="ups_test_abcd")
    client = UpstreamApiClient.from_config(cfg)
    try:
        assert client._base_url == "https://example.test"
    finally:
        client.close()


def test_base_url_trailing_slash_is_normalised() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_AGENT_RESPONSE_BODY)

    cap = CapturingTransport()
    transport = cap.respond_with(handler)
    client = UpstreamApiClient(
        "http://api.test.local/", TOKEN, transport=transport
    )
    try:
        client.register_agent(_agent_req())
    finally:
        client.close()
    assert cap.requests[0].url.path == AGENT_REGISTER_PATH


# ---------------------------------------------------------------------------
# get_stream_session — M7 remote-ingest confirmation
# ---------------------------------------------------------------------------


_FLAT_SESSION_BODY = {
    "id": "0b91a1a8-7d3d-4c91-9c6e-ce5b9f0e6e3e",
    "account_id": "00000000-0000-0000-0000-000000000001",
    "agent_id": "agent_1",
    "camera_id": "cam_1",
    "state": "approved",
    "srt_url": "srt://ingest.test.local:8890?streamid=publish:sess_secret",
    "streamid": "publish:sess_secret",
    "expires_at": "2026-05-14T13:00:00Z",
    "created_at": "2026-05-14T12:00:00Z",
    "started_at": "2026-05-14T12:00:01Z",
    "ended_at": None,
    "requested": {
        "width": 1280, "height": 720, "fps": 30, "codec": "h264",
        "bitrate_kbps": 3000, "latency_mode": "balanced",
    },
    "approved": {
        "width": 1280, "height": 720, "fps": 30, "codec": "h264",
        "bitrate_kbps": 3000, "latency_mode": "balanced",
    },
}


def test_get_stream_session_returns_flat_session_with_state() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        body = dict(_FLAT_SESSION_BODY)
        body["state"] = "streaming"
        return httpx.Response(200, json=body)

    client, cap = _make_client(handler)
    with client:
        session = client.get_stream_session("sess_42")

    assert session.id == _FLAT_SESSION_BODY["id"]
    assert session.state == "streaming"
    assert cap.requests[0].method == "GET"
    assert cap.requests[0].url.path == STREAM_SESSION_GET_PATH_TEMPLATE.format(
        session_id="sess_42"
    )
    # GET requests must not include a JSON Content-Type or a request body.
    body_bytes = cap.requests[0].content
    assert body_bytes == b""
    assert "content-type" not in {k.lower() for k in cap.requests[0].headers}


def test_get_stream_session_sends_bearer_auth_and_accept_json() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_FLAT_SESSION_BODY)

    client, cap = _make_client(handler)
    with client:
        client.get_stream_session("sess_42")
    headers = {k.lower(): v for k, v in cap.requests[0].headers.items()}
    assert headers["authorization"] == f"Bearer {TOKEN}"
    assert headers["accept"] == "application/json"


def test_get_stream_session_404_maps_to_validation_error() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            404, json={"error": {"code": "session_not_found", "message": "no"}}
        )

    client, _ = _make_client(handler)
    with client, pytest.raises(ApiValidationError):
        client.get_stream_session("sess_missing")


def test_get_stream_session_401_token_revoked_maps_to_token_revoked() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401, json={"error": {"code": "token_revoked", "message": "no"}}
        )

    client, _ = _make_client(handler)
    with client, pytest.raises(ApiTokenRevokedError):
        client.get_stream_session("sess_42")


def test_get_stream_session_5xx_maps_to_server_error() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": {"code": "x", "message": "y"}})

    client, _ = _make_client(handler)
    with client, pytest.raises(ApiServerError):
        client.get_stream_session("sess_42")


def test_get_stream_session_connection_error_maps_to_connection_error() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("network down")

    client, _ = _make_client(handler)
    with client, pytest.raises(ApiConnectionError):
        client.get_stream_session("sess_42")


def test_get_stream_session_error_response_carries_no_token_leak() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            503,
            json={"error": {"code": "x", "message": "y", "request_id": "rid"}},
        )

    client, _ = _make_client(handler)
    with client:
        try:
            client.get_stream_session("sess_42")
        except ApiError as exc:
            assert TOKEN not in str(exc)
