"""UpStream API client package.

Two layers:

* :mod:`upstream_agent.api.schemas` — Pydantic request/response models.
* :mod:`upstream_agent.api.client` — :class:`UpstreamApiClient`, a
  synchronous httpx wrapper that translates transport errors and HTTP
  status codes into typed :class:`~upstream_agent.errors.ApiError`
  subclasses.

Endpoint paths and error codes are confirmed against the backend
contract in M3.2 (see ``docs/api-contracts.md``).
"""

from upstream_agent.api.client import (
    AGENT_REGISTER_PATH,
    CAMERA_REGISTER_PATH,
    HEARTBEAT_PATH,
    STREAM_SESSION_START_PATH,
    STREAM_SESSION_STOP_PATH_TEMPLATE,
    TOKEN_EXPIRED_ERROR_CODE,
    TOKEN_REVOKED_ERROR_CODE,
    ParsedError,
    UpstreamApiClient,
)
from upstream_agent.api.schemas import (
    Agent,
    AgentRegisterRequest,
    AgentRegisterResponse,
    ApprovedStreamConfig,
    Camera,
    CameraRegisterRequest,
    CameraRegisterResponse,
    Heartbeat,
    HeartbeatBatchRequest,
    HeartbeatBatchResponse,
    HeartbeatStatus,
    RequestedStreamConfig,
    Session,
    StopStreamSessionRequest,
    StopStreamSessionResponse,
    StreamSessionRequest,
    StreamSessionResponse,
)

__all__ = [
    "AGENT_REGISTER_PATH",
    "CAMERA_REGISTER_PATH",
    "HEARTBEAT_PATH",
    "STREAM_SESSION_START_PATH",
    "STREAM_SESSION_STOP_PATH_TEMPLATE",
    "TOKEN_EXPIRED_ERROR_CODE",
    "TOKEN_REVOKED_ERROR_CODE",
    "ParsedError",
    "UpstreamApiClient",
    "Agent",
    "AgentRegisterRequest",
    "AgentRegisterResponse",
    "ApprovedStreamConfig",
    "Camera",
    "CameraRegisterRequest",
    "CameraRegisterResponse",
    "Heartbeat",
    "HeartbeatBatchRequest",
    "HeartbeatBatchResponse",
    "HeartbeatStatus",
    "RequestedStreamConfig",
    "Session",
    "StopStreamSessionRequest",
    "StopStreamSessionResponse",
    "StreamSessionRequest",
    "StreamSessionResponse",
]
