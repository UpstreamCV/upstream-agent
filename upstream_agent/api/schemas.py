"""Pydantic request and response models for the UpStream API.

Design rules:

* **Requests use ``extra="forbid"``** so the client cannot accidentally
  send unknown fields. If a developer adds a field by mistake, Pydantic
  fails loud during request construction.
* **Responses use ``extra="ignore"``** so the backend can add new fields
  without breaking older clients in the field. The agent reads what it
  knows and discards the rest.
* All response fields carry explicit constraints (``min_length=1`` for
  identifiers, ``gt=0`` for positive integers) so a malformed payload
  like ``{"agent_id": ""}`` becomes a Pydantic ``ValidationError`` —
  which :class:`UpstreamApiClient` translates to
  :class:`~upstream_agent.errors.ApiContractError`.

The endpoint paths these payloads correspond to are constants in
:mod:`upstream_agent.api.client`. Both the paths and the field names
below are documented as provisional in ``docs/api-contracts.md`` until
the backend contract is confirmed.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# Agent register
# ---------------------------------------------------------------------------


class AgentRegisterRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    device_id: str = Field(..., min_length=1)
    agent_version: str = Field(..., min_length=1)
    platform: str | None = None


class AgentRegisterResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    agent_id: str = Field(..., min_length=1)


# ---------------------------------------------------------------------------
# Camera register
# ---------------------------------------------------------------------------


class CameraRegisterRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_id: str = Field(..., min_length=1)
    name: str = Field(..., min_length=1)
    source_type: Literal["webcam", "rtsp"]
    source: int | str
    requested_width: int = Field(..., gt=0)
    requested_height: int = Field(..., gt=0)
    requested_fps: int = Field(..., gt=0)


class CameraRegisterResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    camera_id: str = Field(..., min_length=1)


# ---------------------------------------------------------------------------
# Stream session
# ---------------------------------------------------------------------------


class StreamSessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_id: str = Field(..., min_length=1)
    camera_id: str = Field(..., min_length=1)
    source_type: Literal["webcam", "rtsp"]
    requested_width: int = Field(..., gt=0)
    requested_height: int = Field(..., gt=0)
    requested_fps: int = Field(..., gt=0)


class ApprovedStreamConfig(BaseModel):
    """Server-approved stream parameters.

    ``srt_url`` is sensitive — it may carry per-session stream tokens.
    Callers must never log, print, or persist it. The orchestrator
    drops the URL after extracting ``srt_url_present: bool``.
    """

    model_config = ConfigDict(extra="ignore")

    codec: str = Field(..., min_length=1)
    width: int = Field(..., gt=0)
    height: int = Field(..., gt=0)
    fps: int = Field(..., gt=0)
    bitrate: int | None = Field(default=None, gt=0)
    srt_url: str = Field(..., min_length=1)
    expires_at: str | None = None
    expires_in: int | None = Field(default=None, ge=0)


class StreamSessionResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    session_id: str = Field(..., min_length=1)
    approved_config: ApprovedStreamConfig
