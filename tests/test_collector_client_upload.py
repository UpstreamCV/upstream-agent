"""UpstreamApiClient.upload_observations: correct path + Bearer auth, response
parsing, and typed-error mapping (5xx / network / auth). Driven with
httpx.MockTransport — no real network."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import httpx
import pytest

from upstream_agent.api.client import UpstreamApiClient
from upstream_agent.api.schemas import ObservationBatchRequest, ObservationItem
from upstream_agent.errors import (
    ApiAuthError,
    ApiConnectionError,
    ApiServerError,
    ApiTokenRevokedError,
)

_TOKEN = "ups_test_ABCDEFGH_SECRETSECRETSECRET22"


def _req() -> ObservationBatchRequest:
    return ObservationBatchRequest(
        client_batch_id="batch_123",
        source="edge-box-4",
        observations=[
            ObservationItem(
                type="event_observed",
                observed_at=datetime(2026, 6, 22, 18, 5, 12, tzinfo=timezone.utc),
                payload={"event_name": "line_crossing"},
                external_camera_key="dock-12",
            )
        ],
    )


def _client(handler: Any) -> UpstreamApiClient:
    return UpstreamApiClient("http://api.test", _TOKEN, transport=httpx.MockTransport(handler))


def test_upload_uses_correct_path_auth_and_parses_response() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        captured["auth"] = request.headers.get("authorization")
        captured["body"] = request.content.decode()
        return httpx.Response(
            200,
            json={
                "accepted": 1,
                "batch_id": "11111111-1111-1111-1111-111111111111",
                "server_time": "2026-06-22T18:05:20Z",
            },
        )

    with _client(handler) as client:
        resp = client.upload_observations(_req())

    assert captured["method"] == "POST"
    assert captured["path"] == "/v1/reliability/observations"
    assert captured["auth"] == f"Bearer {_TOKEN}"
    assert "client_batch_id" in captured["body"]
    assert resp.accepted == 1
    assert resp.batch_id == "11111111-1111-1111-1111-111111111111"


def test_server_5xx_raises_server_error() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": {"code": "unavailable"}})

    with _client(handler) as client, pytest.raises(ApiServerError):
        client.upload_observations(_req())


def test_network_error_raises_connection_error() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    with _client(handler) as client, pytest.raises(ApiConnectionError):
        client.upload_observations(_req())


def test_revoked_token_raises_token_revoked() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"code": "token_revoked"}})

    with _client(handler) as client, pytest.raises(ApiTokenRevokedError):
        client.upload_observations(_req())


def test_forbidden_scope_raises_auth_error() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": {"code": "forbidden.scope"}})

    with _client(handler) as client, pytest.raises(ApiAuthError):
        client.upload_observations(_req())


def test_upload_does_not_log_token_or_body(caplog: pytest.LogCaptureFixture) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"accepted": 1, "batch_id": "x", "server_time": "2026-06-22T18:05:20Z"},
        )

    with caplog.at_level(logging.DEBUG), _client(handler) as client:
        client.upload_observations(_req())

    blob = "\n".join(r.getMessage() for r in caplog.records)
    assert _TOKEN not in blob
    assert "line_crossing" not in blob  # payload body never logged
