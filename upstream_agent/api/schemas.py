"""Pydantic request and response models for the UpStream API.

Design rules:

* **Requests use ``extra="forbid"``** so the client cannot accidentally
  send unknown fields. If a developer adds a field by mistake, Pydantic
  fails loud during request construction.
* **Responses use ``extra="ignore"``** so the backend can add new fields
  without breaking older clients in the field. The agent reads what it
  knows and discards the rest.
* All identifier and required-string response fields carry
  ``min_length=1`` so a malformed payload like ``{"id": ""}`` becomes a
  Pydantic ``ValidationError`` — which
  :class:`upstream_agent.api.client.UpstreamApiClient` translates to
  :class:`~upstream_agent.errors.ApiContractError`.

These shapes match the **confirmed** backend contract (M3.2). The
corresponding endpoint paths live in
:mod:`upstream_agent.api.client`. See ``docs/api-contracts.md`` for
the full request/response specification.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

# ---------------------------------------------------------------------------
# Agent register
# ---------------------------------------------------------------------------


class AgentRegisterRequest(BaseModel):
    """Body of ``POST /v1/agents/register``.

    ``hostname`` is intentionally optional and the agent leaves it
    unset by default — the real machine hostname can identify the
    customer. See ``docs/api-contracts.md`` for the privacy decision.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1)
    hostname: str | None = None
    version: str = Field(..., min_length=1)
    os: str = Field(..., min_length=1)
    python_version: str = Field(..., min_length=1)
    capabilities: dict[str, bool | str] = Field(default_factory=dict)


class Agent(BaseModel):
    """Server's view of an agent.

    Most fields are optional so future server-side additions do not
    break older clients. Only ``id`` is required — it is the value the
    agent persists locally as ``agent_id``.
    """

    model_config = ConfigDict(extra="ignore")

    id: str = Field(..., min_length=1)
    account_id: str | None = None
    name: str | None = None
    hostname: str | None = None
    version: str | None = None
    os: str | None = None
    python_version: str | None = None
    capabilities: dict[str, object] | None = None
    status: str | None = None
    last_seen_at: str | None = None
    created_at: str | None = None
    updated_at: str | None = None


class AgentRegisterResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    agent: Agent
    server_time: str | None = None


# ---------------------------------------------------------------------------
# Camera register
# ---------------------------------------------------------------------------


class CameraRegisterRequest(BaseModel):
    """Body of ``POST /v1/cameras/register``.

    The backend treats ``(agent_id, external_id)`` as the idempotency
    key when ``external_id`` is provided, so the agent always sends a
    stable identifier derived from the camera source — for a webcam at
    index 0, ``external_id="webcam-0"``.
    """

    model_config = ConfigDict(extra="forbid")

    agent_id: str = Field(..., min_length=1)
    name: str = Field(..., min_length=1)
    external_id: str = Field(..., min_length=1)
    device_path: str = Field(..., min_length=1)
    # Declared source kind ("webcam" | "rtsp"). Optional + omitted when
    # None (client serializes with exclude_none) so older servers see an
    # unchanged payload. The server validates it against the account's
    # allowed_stream_types and rejects with
    # limit_exceeded.stream_type_not_allowed.
    source_type: str | None = None
    default_width: int = Field(..., gt=0)
    default_height: int = Field(..., gt=0)
    default_fps: int = Field(..., gt=0)
    default_codec: str = Field(..., min_length=1)
    default_bitrate_kbps: int = Field(..., gt=0)
    default_latency_mode: Literal["low", "balanced", "stable"]
    metadata: dict[str, object] = Field(default_factory=dict)


class Camera(BaseModel):
    """Server's view of a camera. Only ``id`` is required for the
    agent's purposes."""

    model_config = ConfigDict(extra="ignore")

    id: str = Field(..., min_length=1)
    account_id: str | None = None
    agent_id: str | None = None
    name: str | None = None
    external_id: str | None = None
    device_path: str | None = None
    source_type: str | None = None
    status: str | None = None
    default_width: int | None = None
    default_height: int | None = None
    default_fps: int | None = None
    default_codec: str | None = None
    default_bitrate_kbps: int | None = None
    default_latency_mode: str | None = None


class CameraRegisterResponse(BaseModel):
    """Robust to both response shapes the backend might return.

    The published contract returns ``{"camera": {...}}``. Some quick-
    start examples in the upstream docs return a flat camera object
    (``{"id": "...", "name": "..."}``). The pre-validator below
    accepts either form by wrapping a flat body before model
    validation runs.
    """

    model_config = ConfigDict(extra="ignore")

    camera: Camera

    @model_validator(mode="before")
    @classmethod
    def _unwrap_flat(cls, data: object) -> object:
        if isinstance(data, dict) and "camera" not in data and "id" in data:
            return {"camera": data}
        return data


# ---------------------------------------------------------------------------
# Stream session
# ---------------------------------------------------------------------------


class RequestedStreamConfig(BaseModel):
    """Client-side preferences submitted with a session-start request.

    Used for both the request body (``StreamSessionRequest.requested``)
    and the echo on the response (``Session.requested``).
    """

    model_config = ConfigDict(extra="forbid")

    width: int = Field(..., gt=0)
    height: int = Field(..., gt=0)
    fps: int = Field(..., gt=0)
    codec: str = Field(..., min_length=1)
    bitrate_kbps: int = Field(..., gt=0)
    latency_mode: Literal["low", "balanced", "stable"]


class ApprovedStreamConfig(BaseModel):
    """Server-approved stream parameters.

    ``latency_mode`` is intentionally a plain ``str`` rather than a
    ``Literal`` because the server may return values that the agent
    has not enumerated; the agent only displays it.
    """

    model_config = ConfigDict(extra="ignore")

    width: int = Field(..., gt=0)
    height: int = Field(..., gt=0)
    fps: int = Field(..., gt=0)
    codec: str = Field(..., min_length=1)
    bitrate_kbps: int = Field(..., gt=0)
    latency_mode: str = Field(..., min_length=1)


class StreamSessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    camera_id: str = Field(..., min_length=1)
    requested: RequestedStreamConfig


class Session(BaseModel):
    """Server-returned session record.

    ``srt_url``, ``streamid``, and the top-level ``session_token`` on
    :class:`StreamSessionResponse` are sensitive — the agent must
    never log, print, or persist them. The orchestrator reads
    ``srt_url`` only to set a boolean and drops the response object
    immediately afterwards.
    """

    model_config = ConfigDict(extra="ignore")

    id: str = Field(..., min_length=1)
    account_id: str | None = None
    agent_id: str | None = None
    camera_id: str | None = None
    state: str | None = None
    srt_url: str | None = None
    streamid: str | None = None
    expires_at: str | None = None
    started_at: str | None = None
    ended_at: str | None = None
    requested: RequestedStreamConfig | None = None
    approved: ApprovedStreamConfig | None = None


class StreamSessionResponse(BaseModel):
    """The session-start response.

    The backend returns ``srt_url`` at the top level *and* echoes it
    inside ``session.srt_url``; either may be present. The orchestrator
    treats their union as the ``srt_url_present`` signal and discards
    both.
    """

    model_config = ConfigDict(extra="ignore")

    session: Session
    srt_url: str | None = None
    session_token: str | None = None
    expires_at: str | None = None


class StopStreamSessionRequest(BaseModel):
    """Body of ``POST /v1/stream-sessions/{session_id}/stop``.

    The endpoint requires no payload — the session id is on the URL.
    This empty request model exists so :meth:`UpstreamApiClient._request`
    can serialize a consistent ``{}`` body and benefit from the same
    ``extra="forbid"`` guard the other requests use.
    """

    model_config = ConfigDict(extra="forbid")


class StopStreamSessionResponse(BaseModel):
    """Stop-session response.

    The agent does not need to inspect anything specific on success;
    the HTTP 200 is the signal. The model is permissive
    (``extra="ignore"``, no required fields) so any shape the backend
    returns — empty, the updated session record, or something new —
    parses cleanly.
    """

    model_config = ConfigDict(extra="ignore")


# ---------------------------------------------------------------------------
# Heartbeat
# ---------------------------------------------------------------------------


HeartbeatStatus = Literal["online", "streaming", "error", "reconnecting"]
"""The four states the backend's ``heartbeats.status`` column accepts."""


class Heartbeat(BaseModel):
    """One heartbeat record.

    Field bounds mirror the server-side Pydantic model in
    ``upstream-api-server`` (``schemas/heartbeats.py``). Sending a
    value outside these bounds raises ``ValidationError`` locally so
    the request never goes on the wire.

    Sensitive fields are intentionally absent — no SRT URL, no
    streamid, no session token. Only IDs (UUIDs as strings), status,
    and numeric/string telemetry travel in a heartbeat.
    """

    model_config = ConfigDict(extra="forbid")

    camera_id: str | None = None
    session_id: str | None = None
    status: HeartbeatStatus
    fps: float | None = Field(default=None, ge=0, le=1000)
    bitrate_kbps: int | None = Field(default=None, ge=0, le=200_000)
    width: int | None = Field(default=None, ge=1, le=7680)
    height: int | None = Field(default=None, ge=1, le=4320)
    codec: str | None = Field(default=None, max_length=32)
    last_error: str | None = Field(default=None, max_length=512)
    agent_version: str | None = Field(default=None, max_length=64)
    reported_at: datetime


class HeartbeatBatchRequest(BaseModel):
    """Body of ``POST /v1/heartbeats?agent_id=<uuid>``.

    The endpoint accepts 1–100 heartbeats per call; M6 sends one at a
    time. Batching across cameras or queued offline heartbeats is
    deferred to the M7 reconnect work.
    """

    model_config = ConfigDict(extra="forbid")

    heartbeats: list[Heartbeat] = Field(min_length=1, max_length=100)


class HeartbeatBatchResponse(BaseModel):
    """Server response for a heartbeat batch.

    ``accepted`` may be lower than the number sent if individual
    items failed server-side validation. The endpoint does not return
    a ``next_interval_s`` or any stop/control directive — the agent
    picks its own cadence.
    """

    model_config = ConfigDict(extra="ignore")

    accepted: int
    server_time: str | None = None


# ---------------------------------------------------------------------------
# Bootstrap (GET /v1/agents/bootstrap)
# ---------------------------------------------------------------------------
#
# Everything below is deliberately tolerant: only the identifiers the agent
# truly needs are required; every other field is optional so the agent keeps
# working against older servers (where the endpoint may not exist at all —
# the client surfaces that as ApiValidationError and callers degrade
# gracefully) and newer servers that add fields.


class BootstrapAccount(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str = Field(..., min_length=1)
    name: str | None = None
    slug: str | None = None
    # Informational label only — the agent never derives privileges from it.
    plan: str | None = None
    status: str | None = None


class BootstrapTokenInfo(BaseModel):
    """Metadata about the API token in use. Never contains the secret."""

    model_config = ConfigDict(extra="ignore")

    name: str | None = None
    scopes: list[str] = Field(default_factory=list)
    last_four: str | None = None
    expires_at: str | None = None


class BootstrapLimits(BaseModel):
    """Account caps as the server reports them.

    All optional: a missing field means "the server didn't say", and the
    local pre-check (:mod:`upstream_agent.preflight`) skips that dimension —
    the server remains the enforcement point either way.
    """

    model_config = ConfigDict(extra="ignore")

    max_cameras: int | None = None
    max_concurrent_sessions: int | None = None
    max_fps: int | None = None
    max_width: int | None = None
    max_height: int | None = None
    max_bitrate_kbps: int | None = None
    allowed_codecs: list[str] | None = None
    allowed_stream_types: list[str] | None = None
    monthly_minutes_quota: int | None = None
    snapshot_storage_mb: int | None = None


class BootstrapUsage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    camera_count: int | None = None
    active_session_count: int | None = None
    monthly_minutes_used: int | None = None


class BootstrapSessionSummary(BaseModel):
    """An active session as reported by bootstrap.

    Carries no SRT URL, streamid, or token — the server's bootstrap
    contract excludes them by design.
    """

    model_config = ConfigDict(extra="ignore")

    id: str = Field(..., min_length=1)
    camera_id: str | None = None
    agent_id: str | None = None
    state: str | None = None
    created_at: str | None = None
    expires_at: str | None = None
    started_at: str | None = None
    approved: dict[str, object] | None = None


class AgentVersionPolicy(BaseModel):
    """Server-owned version-policy facts (update M2, notify-only).

    Optional everywhere: the server omits the whole object when no policy
    is configured, and individual fields may be absent. The agent computes
    the consequences locally (see ``upstream_agent.update_check``)."""

    model_config = ConfigDict(extra="ignore")

    latest_version: str | None = None
    minimum_supported_version: str | None = None
    severity_hint: str | None = None
    release_notes_url: str | None = None
    install_hint: str | None = None


class BootstrapResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    account: BootstrapAccount | None = None
    token: BootstrapTokenInfo | None = None
    agents: list[Agent] = Field(default_factory=list)
    limits: BootstrapLimits | None = None
    usage: BootstrapUsage | None = None
    cameras: list[Camera] = Field(default_factory=list)
    active_sessions: list[BootstrapSessionSummary] = Field(default_factory=list)
    server_time: str | None = None
    # Update M2: optional server version policy (None on older servers).
    agent_version_policy: AgentVersionPolicy | None = None


# ---------------------------------------------------------------------------
# Camera claim (POST /v1/cameras/{camera_id}/claim)
# ---------------------------------------------------------------------------


class CameraClaimRequest(BaseModel):
    """Body of ``POST /v1/cameras/{camera_id}/claim``.

    Assigns a dashboard-created (unassigned) camera to this agent so a
    stream session can be started for it. The server rejects claims on
    cameras held by a different agent with
    ``camera_claimed_by_other_agent`` (409).
    """

    model_config = ConfigDict(extra="forbid")

    agent_id: str = Field(..., min_length=1)


class CameraClaimResponse(BaseModel):
    """The claimed camera record. Same tolerant shape as ``Camera``."""

    model_config = ConfigDict(extra="ignore")

    id: str = Field(..., min_length=1)
    agent_id: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _unwrap_nested(cls, data: object) -> object:
        # Server returns the flat camera record; tolerate a wrapped
        # {"camera": {...}} shape too, mirroring CameraRegisterResponse.
        if isinstance(data, dict) and "camera" in data and "id" not in data:
            inner = data["camera"]
            if isinstance(inner, dict):
                return inner
        return data
