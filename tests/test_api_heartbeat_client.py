"""Tests for :meth:`UpstreamApiClient.send_heartbeat`.

Same MockTransport pattern as ``test_api_client.py``. These tests
exercise the heartbeat-specific contract:

* URL is ``/v1/heartbeats`` with the ``agent_id`` query parameter set.
* Request body is the batch envelope ``{"heartbeats": [...]}`` even
  when only one heartbeat is sent.
* Error envelope mapping reuses the typed-error machinery.
* Bearer token never appears in errors, caplog, or repr.
"""

from __future__ import annotations

import json as _json
import logging
from collections.abc import Callable
from datetime import datetime, timezone

import httpx
import pytest

from upstream_agent.api.client import HEARTBEAT_PATH, UpstreamApiClient
from upstream_agent.api.schemas import Heartbeat
from upstream_agent.errors import (
    ApiAuthError,
    ApiConnectionError,
    ApiRateLimitError,
    ApiServerError,
    ApiTokenExpiredError,
    ApiTokenRevokedError,
    ApiValidationError,
)

BASE_URL = "http://api.test.local"
TOKEN = "ups_test_abcd1234_doNOT_leak_xxxxx"
AGENT_ID = "0b91a1a8-7d3d-4c91-9c6e-ce5b9f0e6e3e"


def _make_client(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    token: str = TOKEN,
) -> tuple[UpstreamApiClient, list[httpx.Request]]:
    captured: list[httpx.Request] = []

    def wrapper(req: httpx.Request) -> httpx.Response:
        captured.append(req)
        return handler(req)

    transport = httpx.MockTransport(wrapper)
    client = UpstreamApiClient(BASE_URL, token, transport=transport)
    return client, captured


def _hb(**overrides: object) -> Heartbeat:
    defaults: dict[str, object] = {
        "camera_id": "cam-1",
        "session_id": "sess-1",
        "status": "streaming",
        "fps": 29.97,
        "bitrate_kbps": 3000,
        "width": 1280,
        "height": 720,
        "codec": "h264",
        "last_error": None,
        "agent_version": "0.1.0",
        "reported_at": datetime(2026, 5, 13, 12, 0, 0, tzinfo=timezone.utc),
    }
    defaults.update(overrides)
    return Heartbeat(**defaults)  # type: ignore[arg-type]


def _error_body(code: str, message: str = "msg", request_id: str = "rid_hb") -> dict:
    return {
        "error": {
            "code": code,
            "message": message,
            "details": {},
            "request_id": request_id,
        }
    }


# ---------------------------------------------------------------------------
# Happy path: URL, query string, body shape, response parsing
# ---------------------------------------------------------------------------


def test_send_heartbeat_uses_correct_path_and_query() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"accepted": 1, "server_time": "2026-05-13T12:00:01Z"}
        )

    client, captured = _make_client(handler)
    with client:
        resp = client.send_heartbeat(agent_id=AGENT_ID, heartbeat=_hb())

    assert resp.accepted == 1
    assert resp.server_time == "2026-05-13T12:00:01Z"

    assert len(captured) == 1
    req = captured[0]
    assert req.method == "POST"
    assert req.url.path == HEARTBEAT_PATH
    # httpx encodes query params under params=...
    assert req.url.params.get("agent_id") == AGENT_ID


def test_send_heartbeat_wraps_single_heartbeat_in_batch_envelope() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"accepted": 1})

    client, captured = _make_client(handler)
    with client:
        client.send_heartbeat(
            agent_id=AGENT_ID,
            heartbeat=_hb(status="streaming", fps=30.0, last_error=None),
        )

    body = _json.loads(captured[0].content)
    assert "heartbeats" in body
    assert isinstance(body["heartbeats"], list)
    assert len(body["heartbeats"]) == 1
    sent = body["heartbeats"][0]
    assert sent["status"] == "streaming"
    assert sent["fps"] == 30.0
    # None values are dropped on the wire (exclude_none).
    assert "last_error" not in sent
    # Required field present.
    assert "reported_at" in sent


def test_send_heartbeat_attaches_bearer_token() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"accepted": 1})

    client, captured = _make_client(handler)
    with client:
        client.send_heartbeat(agent_id=AGENT_ID, heartbeat=_hb())

    assert captured[0].headers.get("Authorization") == f"Bearer {TOKEN}"


def test_send_heartbeat_drops_none_fields_from_payload() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"accepted": 1})

    client, captured = _make_client(handler)
    with client:
        client.send_heartbeat(
            agent_id=AGENT_ID,
            heartbeat=Heartbeat(
                status="streaming",
                reported_at=datetime(2026, 5, 13, tzinfo=timezone.utc),
            ),
        )

    sent = _json.loads(captured[0].content)["heartbeats"][0]
    # Only the required fields plus reported_at should appear.
    assert sent.keys() == {"status", "reported_at"}


# ---------------------------------------------------------------------------
# Error envelope mapping
# ---------------------------------------------------------------------------


def test_401_token_revoked_maps_to_typed_error() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json=_error_body("token_revoked"))

    client, _ = _make_client(handler)
    with client, pytest.raises(ApiTokenRevokedError) as excinfo:
        client.send_heartbeat(agent_id=AGENT_ID, heartbeat=_hb())
    assert "code=token_revoked" in str(excinfo.value)


def test_401_token_expired_maps_to_typed_error() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json=_error_body("token_expired"))

    client, _ = _make_client(handler)
    with client, pytest.raises(ApiTokenExpiredError):
        client.send_heartbeat(agent_id=AGENT_ID, heartbeat=_hb())


def test_403_forbidden_resource_maps_to_auth_error() -> None:
    """The heartbeat endpoint returns 403 forbidden_resource when a
    camera or session in the batch is not owned by the agent. The
    existing 403 mapping returns ApiAuthError for non-limit codes —
    the reporter treats any auth error as fatal."""

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            json=_error_body(
                "forbidden_resource",
                message="Heartbeat references a camera not owned by this agent.",
            ),
        )

    client, _ = _make_client(handler)
    with client, pytest.raises(ApiAuthError) as excinfo:
        client.send_heartbeat(agent_id=AGENT_ID, heartbeat=_hb())
    assert "code=forbidden_resource" in str(excinfo.value)


def test_422_validation_error_maps_to_validation_error() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json=_error_body("validation_error"))

    client, _ = _make_client(handler)
    with client, pytest.raises(ApiValidationError):
        client.send_heartbeat(agent_id=AGENT_ID, heartbeat=_hb())


def test_429_maps_to_rate_limit_error() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json=_error_body("rate_limited"))

    client, _ = _make_client(handler)
    with client, pytest.raises(ApiRateLimitError):
        client.send_heartbeat(agent_id=AGENT_ID, heartbeat=_hb())


def test_503_maps_to_server_error() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json=_error_body("unavailable"))

    client, _ = _make_client(handler)
    with client, pytest.raises(ApiServerError):
        client.send_heartbeat(agent_id=AGENT_ID, heartbeat=_hb())


def test_connect_error_maps_to_connection_error() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=req)

    client, _ = _make_client(handler)
    with client, pytest.raises(ApiConnectionError):
        client.send_heartbeat(agent_id=AGENT_ID, heartbeat=_hb())


# ---------------------------------------------------------------------------
# Token-leak guards
# ---------------------------------------------------------------------------


def test_token_not_in_error_message() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json=_error_body("token_revoked"))

    client, _ = _make_client(handler)
    with client, pytest.raises(ApiTokenRevokedError) as excinfo:
        client.send_heartbeat(agent_id=AGENT_ID, heartbeat=_hb())
    assert TOKEN not in str(excinfo.value)


def test_token_not_in_caplog_on_failure(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json=_error_body("oops"))

    client, _ = _make_client(handler)
    with client, pytest.raises(ApiServerError):
        client.send_heartbeat(agent_id=AGENT_ID, heartbeat=_hb())

    for record in caplog.records:
        assert TOKEN not in record.getMessage()


# ---------------------------------------------------------------------------
# Schema validation (local)
# ---------------------------------------------------------------------------


def test_local_validation_rejects_out_of_range_fps() -> None:
    """Heartbeat schema validates fps in 0..1000; >1000 fails before send."""

    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Heartbeat(
            status="streaming",
            fps=99999.0,
            reported_at=datetime(2026, 5, 13, tzinfo=timezone.utc),
        )


def test_local_validation_rejects_unknown_status() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Heartbeat(
            status="not-a-real-status",  # type: ignore[arg-type]
            reported_at=datetime(2026, 5, 13, tzinfo=timezone.utc),
        )
