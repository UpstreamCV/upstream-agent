"""UpStream API client package.

Two layers:

* :mod:`upstream_agent.api.schemas` — Pydantic request/response models.
* :mod:`upstream_agent.api.client` — :class:`UpstreamApiClient`, a
  synchronous httpx wrapper that translates transport errors and HTTP
  status codes into typed :class:`~upstream_agent.errors.ApiError`
  subclasses.

Endpoint paths are constants in :mod:`~upstream_agent.api.client` and
are documented as provisional in ``docs/api-contracts.md`` until the
backend contract is confirmed.
"""

from upstream_agent.api.client import (
    AGENT_REGISTER_PATH,
    CAMERA_REGISTER_PATH,
    STREAM_SESSION_START_PATH,
    TOKEN_REVOKED_ERROR_CODE,
    UpstreamApiClient,
)
from upstream_agent.api.schemas import (
    AgentRegisterRequest,
    AgentRegisterResponse,
    ApprovedStreamConfig,
    CameraRegisterRequest,
    CameraRegisterResponse,
    StreamSessionRequest,
    StreamSessionResponse,
)

__all__ = [
    "AGENT_REGISTER_PATH",
    "CAMERA_REGISTER_PATH",
    "STREAM_SESSION_START_PATH",
    "TOKEN_REVOKED_ERROR_CODE",
    "UpstreamApiClient",
    "AgentRegisterRequest",
    "AgentRegisterResponse",
    "ApprovedStreamConfig",
    "CameraRegisterRequest",
    "CameraRegisterResponse",
    "StreamSessionRequest",
    "StreamSessionResponse",
]
