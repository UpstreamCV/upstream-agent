"""Tests for the bootstrap + camera-claim client methods.

Same CapturingTransport strategy as ``test_api_client.py``: a mock
httpx transport records each request and returns scripted responses.
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from upstream_agent.api.client import (
    AGENT_BOOTSTRAP_PATH,
    CAMERA_CLAIM_PATH_TEMPLATE,
    UpstreamApiClient,
)
from upstream_agent.errors import ApiAuthError, ApiValidationError

BASE_URL = "http://api.test.local"
TOKEN = "ups_test_abcd1234_doNOT_leak_xxxxx"


def _make_client(
    handler: Callable[[httpx.Request], httpx.Response],
) -> tuple[UpstreamApiClient, list[httpx.Request]]:
    requests: list[httpx.Request] = []

    def _wrapper(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return handler(request)

    client = UpstreamApiClient(BASE_URL, TOKEN, transport=httpx.MockTransport(_wrapper))
    return client, requests


_FULL_BOOTSTRAP_BODY = {
    "account": {
        "id": "00000000-0000-0000-0000-000000000001",
        "name": "Josh Demo",
        "slug": "josh-demo-abc123",
        "plan": "free",
        "status": "active",
    },
    "token": {
        "name": "macbook token",
        "scopes": ["agent"],
        "last_four": "WXYZ",
        "expires_at": None,
    },
    "agents": [
        {
            "id": "0b91a1a8-7d3d-4c91-9c6e-ce5b9f0e6e3e",
            "name": "upstream-agent-12345678",
            "status": "active",
            "version": "0.1.0",
            "last_seen_at": "2026-06-10T12:00:00Z",
        }
    ],
    "limits": {
        "max_cameras": 5,
        "max_concurrent_sessions": 5,
        "max_fps": 30,
        "max_width": 1280,
        "max_height": 720,
        "max_bitrate_kbps": 3000,
        "allowed_codecs": ["h264"],
        "allowed_stream_types": ["webcam", "rtsp"],
        "monthly_minutes_quota": 600,
        "snapshot_storage_mb": 1024,
    },
    "usage": {
        "camera_count": 1,
        "active_session_count": 0,
        "monthly_minutes_used": 42,
    },
    "cameras": [
        {
            "id": "9c8c7e3e-0000-0000-0000-000000000002",
            "name": "Front Door",
            "external_id": "webcam-0",
            "device_path": "webcam:0",
            "source_type": "webcam",
            "agent_id": "0b91a1a8-7d3d-4c91-9c6e-ce5b9f0e6e3e",
            "default_width": 1280,
            "default_height": 720,
            "default_fps": 30,
        }
    ],
    "active_sessions": [
        {
            "id": "2b6f0ad4-0000-0000-0000-000000000003",
            "camera_id": "9c8c7e3e-0000-0000-0000-000000000002",
            "state": "streaming",
            "approved": {"width": 1280, "height": 720, "fps": 30},
        }
    ],
    "server_time": "2026-06-10T12:00:01Z",
}


def test_get_bootstrap_parses_full_payload() -> None:
    client, requests = _make_client(
        lambda _req: httpx.Response(200, json=_FULL_BOOTSTRAP_BODY)
    )
    with client:
        boot = client.get_bootstrap()

    assert requests[0].method == "GET"
    assert requests[0].url.path == AGENT_BOOTSTRAP_PATH
    assert requests[0].headers["Authorization"] == f"Bearer {TOKEN}"

    assert boot.account is not None and boot.account.plan == "free"
    assert boot.token is not None and boot.token.last_four == "WXYZ"
    assert boot.agents[0].id == "0b91a1a8-7d3d-4c91-9c6e-ce5b9f0e6e3e"
    assert boot.limits is not None
    assert boot.limits.allowed_stream_types == ["webcam", "rtsp"]
    assert boot.usage is not None and boot.usage.monthly_minutes_used == 42
    assert boot.cameras[0].source_type == "webcam"
    assert boot.active_sessions[0].state == "streaming"


def test_get_bootstrap_tolerates_unknown_and_missing_fields() -> None:
    sparse = {
        "account": {"id": "acct-1", "new_server_field": True},
        "limits": {"max_fps": 30, "brand_new_cap": 9},
        "some_future_block": {"x": 1},
    }
    client, _ = _make_client(lambda _req: httpx.Response(200, json=sparse))
    with client:
        boot = client.get_bootstrap()
    assert boot.account is not None and boot.account.id == "acct-1"
    assert boot.limits is not None and boot.limits.max_fps == 30
    assert boot.limits.max_width is None
    assert boot.agents == []
    assert boot.cameras == []


def test_get_bootstrap_maps_401_to_auth_error() -> None:
    body = {"error": {"code": "invalid_token", "message": "Token not recognized."}}
    client, _ = _make_client(lambda _req: httpx.Response(401, json=body))
    with client, pytest.raises(ApiAuthError):
        client.get_bootstrap()


def test_get_bootstrap_404_surfaces_as_validation_error() -> None:
    """Older servers without the endpoint -> callers degrade gracefully."""
    client, _ = _make_client(lambda _req: httpx.Response(404, json={}))
    with client, pytest.raises(ApiValidationError):
        client.get_bootstrap()


def test_claim_camera_sends_agent_id_and_parses_response() -> None:
    camera_id = "9c8c7e3e-0000-0000-0000-000000000002"
    agent_id = "0b91a1a8-7d3d-4c91-9c6e-ce5b9f0e6e3e"
    body = {"id": camera_id, "agent_id": agent_id}
    client, requests = _make_client(lambda _req: httpx.Response(200, json=body))
    with client:
        resp = client.claim_camera(camera_id=camera_id, agent_id=agent_id)

    assert requests[0].method == "POST"
    assert requests[0].url.path == CAMERA_CLAIM_PATH_TEMPLATE.format(camera_id=camera_id)
    import json

    assert json.loads(requests[0].content) == {"agent_id": agent_id}
    assert resp.id == camera_id
    assert resp.agent_id == agent_id


def test_claim_camera_conflict_maps_to_validation_error() -> None:
    body = {
        "error": {
            "code": "camera_claimed_by_other_agent",
            "message": "Camera is already claimed by another agent.",
        }
    }
    client, _ = _make_client(lambda _req: httpx.Response(409, json=body))
    with client, pytest.raises(ApiValidationError) as exc:
        client.claim_camera(camera_id="cam-1", agent_id="agent-1")
    assert "camera_claimed_by_other_agent" in str(exc.value)
