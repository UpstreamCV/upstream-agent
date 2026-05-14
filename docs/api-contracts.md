# API contracts

The agent talks to the UpStream API server via HTTPS. Milestone 3.2
updated the client to match the **confirmed** backend contract.
Endpoint paths, request/response shapes, and error codes below
reflect what the API team has settled on.

> **Status:** CONFIRMED as of Milestone 3.2. Future server changes
> will land in the same two files (`upstream_agent/api/client.py` for
> paths, `upstream_agent/api/schemas.py` for shapes); the rest of
> the agent does not need to change.

## Base URLs

| Environment | URL |
| --- | --- |
| Production / staging | `https://api.upstreamcv.com` |
| Local dev | `http://localhost:4000` |

The agent reads `api.base_url` from `~/.upstream/config.yaml` and
never hardcodes a value.

## Authentication

Every request carries:

```
Authorization: Bearer <token>
Content-Type: application/json
Accept: application/json
User-Agent: upstream-agent/<version>
```

API tokens look like:

```
ups_<env>_<8-char id>_<22-char secret>
```

The token is scoped to one account, so the agent does **not** send
`account_id` in any request. The token lives in
`~/.upstream/config.yaml` under `api.token` and is never sent
anywhere else (no query parameters, no URL paths, no logs). The CLI
rejects the literal placeholder `YOUR_API_TOKEN_HERE` before any HTTP
request is made.

Auth-header construction is isolated to
`UpstreamApiClient._build_auth_header()` so swapping `Bearer` for
another scheme is a one-method change.

## Endpoints

Three endpoints back the `upstream check --api` flow. All are flat
`POST`s with IDs carried in the JSON body.

### `POST /v1/agents/register`

Registers a new agent or returns the existing record. The backend
is **idempotent on `(account_id, name)`**: re-running with the same
`name` updates `last_seen_at` and `capabilities` rather than
creating a duplicate.

**Privacy note.** The schema permits `hostname`, but the real
machine hostname can identify the customer. The agent omits
`hostname` and instead sends a stable, non-sensitive generated
`name` derived from the local `device_id`:
`upstream-agent-<first-8-hex-chars-of-device-id>`. This is documented
in the agent code (`upstream_agent.diagnostics.api._agent_name_from`)
and stable across runs.

**Request body**

```json
{
  "name": "upstream-agent-12345678",
  "version": "0.1.0",
  "os": "darwin",
  "python_version": "3.12.13",
  "capabilities": {
    "ffmpeg": false,
    "srt": false
  }
}
```

| Field | Required | Notes |
| --- | --- | --- |
| `name` | yes | Stable, non-sensitive. Derived from `device_id`. |
| `hostname` | no | **Intentionally omitted** by the agent. Schema accepts a string if a future deployment needs it. |
| `version` | yes | `upstream_agent.__version__`. |
| `os` | yes | `platform.system().lower()` — `"darwin"`, `"linux"`, `"windows"`. |
| `python_version` | yes | `platform.python_version()` — e.g. `"3.12.13"`. |
| `capabilities` | yes | Honest static `{"ffmpeg": false, "srt": false}` until those features land in M4+. |

**Response 200**

```json
{
  "agent": {
    "id": "0b91a1a8-7d3d-4c91-9c6e-ce5b9f0e6e3e",
    "account_id": "00000000-0000-0000-0000-000000000001",
    "name": "upstream-agent-12345678",
    "version": "0.1.0",
    "os": "darwin",
    "python_version": "3.12.13",
    "capabilities": { "ffmpeg": false, "srt": false },
    "status": "active",
    "last_seen_at": "2026-05-13T02:00:00Z",
    "created_at": "2026-05-12T20:00:00Z",
    "updated_at": "2026-05-13T02:00:00Z"
  },
  "server_time": "2026-05-13T02:00:00Z"
}
```

The agent stores `agent.id` locally as `agent_id`. Everything else
is informational.

### `POST /v1/cameras/register`

Registers (or updates) a camera for the agent. The backend is
**idempotent on `(agent_id, external_id)`** when `external_id` is
provided, so the agent always sends a stable one (`webcam-0`).

**Request body**

```json
{
  "agent_id": "0b91a1a8-7d3d-4c91-9c6e-ce5b9f0e6e3e",
  "name": "Front Door",
  "external_id": "webcam-0",
  "device_path": "webcam:0",
  "default_width": 1280,
  "default_height": 720,
  "default_fps": 30,
  "default_codec": "h264",
  "default_bitrate_kbps": 3000,
  "default_latency_mode": "balanced",
  "metadata": {}
}
```

| Field | Required | Notes |
| --- | --- | --- |
| `agent_id` | yes | Returned by agent-register. |
| `name` | yes | From `camera.name` in config. |
| `external_id` | yes | Stable across runs. For a webcam: `<source_type>-<source>`, e.g. `webcam-0`. |
| `device_path` | yes | Non-OS-specific. For a webcam: `<source_type>:<source>`, e.g. `webcam:0`. (Linux `/dev/video0` is possible but not used by default.) |
| `default_width` / `_height` / `_fps` | yes | From `camera.requested_*` in config. |
| `default_codec` | yes | Hard-coded `"h264"` in M3.2. |
| `default_bitrate_kbps` | yes | Hard-coded `3000` in M3.2. |
| `default_latency_mode` | yes | `"low"`, `"balanced"`, or `"stable"`. Mapped from `stream.latency_mode`. |
| `metadata` | yes | Currently `{}`; reserved for future structured fields. |

**Response 200**

The published contract is `{"camera": {...full CameraResponse...}}`.
Some upstream quick-start examples have shown a flat camera object
at the top level. The agent's `CameraRegisterResponse` model
accepts **both** via a pre-validator (`_unwrap_flat`). Whichever
shape the backend serves, `camera.id` is the field the agent
persists as `last_camera_id`.

```json
{
  "camera": {
    "id": "9c8c7e3e-c1a6-4d6c-bdda-b8c0f5a3d211",
    "agent_id": "0b91a1a8-...",
    "name": "Front Door",
    "external_id": "webcam-0",
    "device_path": "webcam:0",
    "status": "active",
    "default_width": 1280,
    "default_height": 720,
    "default_fps": 30,
    "default_codec": "h264",
    "default_bitrate_kbps": 3000,
    "default_latency_mode": "balanced"
  }
}
```

### `POST /v1/stream-sessions/start`

Requests an approved stream session for an agent + camera.

**Request body**

```json
{
  "camera_id": "9c8c7e3e-c1a6-4d6c-bdda-b8c0f5a3d211",
  "requested": {
    "width": 1280,
    "height": 720,
    "fps": 30,
    "codec": "h264",
    "bitrate_kbps": 3000,
    "latency_mode": "balanced"
  }
}
```

**Response 200**

```json
{
  "session": {
    "id": "2b6f0ad4-e7c4-4d61-aa1e-a8d4c47e9b22",
    "account_id": "00000000-...",
    "agent_id": "0b91a1a8-...",
    "camera_id": "9c8c7e3e-...",
    "state": "approved",
    "srt_url": "srt://ingest.upstreamcv.com:18890?streamid=publish:sess_<hex>&latency=200",
    "streamid": "publish:sess_<hex>",
    "expires_at": "2026-05-13T06:00:00Z",
    "started_at": null,
    "ended_at": null,
    "requested": { "width": 1280, "height": 720, "fps": 30, "codec": "h264", "bitrate_kbps": 3000, "latency_mode": "balanced" },
    "approved":  { "width": 1280, "height": 720, "fps": 30, "codec": "h264", "bitrate_kbps": 3000, "latency_mode": "balanced" }
  },
  "srt_url": "srt://ingest.upstreamcv.com:18890?streamid=publish:sess_<hex>&latency=200",
  "session_token": "eyJhbGciOiJIUzI1NiI...",
  "expires_at": "2026-05-13T06:00:00Z"
}
```

The agent reads:

- `session.id` → printed as the session identifier.
- `session.approved.*` → printed as the approved-config block.
- `srt_url` (top-level or `session.srt_url`) → only its **presence**
  is exposed via `result.srt_url_present: bool`. The URL itself is
  dropped immediately.

The agent **never**:

- Stores `srt_url`, `streamid`, `session_token`, or `account_id` in
  local state.
- Logs, prints, or otherwise exposes `srt_url`, `streamid`, or
  `session_token`.
- Parses the `session_token` JWT. It is treated as opaque bytes.

### `POST /v1/stream-sessions/{session_id}/stop`

Stops the named session. Called by `upstream check --api` as a
**cleanup** after a successful `start_stream_session` so the camera
slot is released before the next run — otherwise a second
back-to-back check would hit
`limit_exceeded.max_concurrent_sessions` or
`camera_already_streaming`.

**Path parameter:** `session_id` — the `id` returned from
`start_stream_session`.

**Request body:** empty (`{}`). The endpoint takes no payload; the
session id is on the URL.

**Response 200:** the agent does not inspect the body. The HTTP 200
is the signal. The backend may return an empty body, the updated
session record, or any other shape; the agent's
`StopStreamSessionResponse` model is permissive and accepts all of
them.

**Non-fatal failure handling.** The agent treats stop failures as
*warnings*, not check failures. Once `start_stream_session` returns
200, the diagnostic has already proved registration + session
approval work. If the subsequent stop call fails, the operator sees
a warning line on stderr but `upstream check --api` still exits 0
and the success block prints normally. The session will eventually
expire server-side regardless.

## Error envelope

All non-2xx responses use this shape (any field may be absent):

```json
{
  "error": {
    "code": "limit_exceeded.max_cameras",
    "message": "Camera slot limit reached: 3 of 3.",
    "details": { "in_use": 3, "max": 3 },
    "request_id": "abc123def456"
  }
}
```

The client switches on `error.code`, never on the human `message`.
Raised `ApiError` messages carry `code=` and `request_id=` segments
when present; that's the information operators should share with
support.

### Status / code → typed error mapping

| HTTP status | Code (if any) | Mapped to |
| --- | --- | --- |
| 400, 409, 422 | any | `ApiValidationError` |
| 401 | (none / generic) | `ApiAuthError` |
| 401 | `token_revoked` | `ApiTokenRevokedError` |
| 401 | `token_expired` | `ApiTokenExpiredError` |
| 403 | `limit_exceeded.*` | `ApiValidationError` |
| 403 | other | `ApiAuthError` |
| 404 | `agent_not_found`, `camera_not_found`, … | `ApiValidationError` |
| 429 | any | `ApiRateLimitError` |
| 5xx | any | `ApiServerError` |
| Other 4xx | any | bare `ApiError` |
| Network failure / timeout | n/a | `ApiConnectionError` |
| Missing / malformed JSON | n/a | `ApiContractError` |

### Auth error codes

- `missing_credentials`
- `invalid_auth_scheme`
- `invalid_token`
- `token_revoked` → `ApiTokenRevokedError`
- `token_expired` → `ApiTokenExpiredError`

### Camera-register error codes

- `agent_not_found`
- `agent_account_mismatch`
- `limit_exceeded.max_cameras`
- `limit_exceeded.codec_not_allowed`
- `limit_exceeded.fps_too_high`
- `limit_exceeded.resolution_too_high`
- `limit_exceeded.bitrate_too_high`

### Stream-session error codes

- `camera_not_found`
- `camera_already_streaming`
- `limit_exceeded.max_concurrent_sessions`
- `limit_exceeded.fps_too_high`
- `limit_exceeded.resolution_too_high`
- `limit_exceeded.bitrate_too_high`
- `limit_exceeded.codec_not_allowed`

## Sensitive-data rules

| Item | Status | Reason |
| --- | --- | --- |
| `api.token` | Never logged, printed, or sent except in the `Authorization` header. | Account credential. |
| `srt_url`, `session.srt_url` | Never logged, printed, or persisted. | May contain per-session stream tokens. |
| `streamid` | Never logged or printed. | Embedded in `srt_url`; same exposure surface. |
| `session_token` (JWT) | Never logged, printed, persisted, or parsed. | Streaming-side credential. |
| `error.details` | Not embedded in `ApiError` strings. | May echo request fields. |
| `account_id` | Not printed in CLI output. | Not actionable for the operator; reduces support-chat copy-paste leaks. |

## Local-only data

The agent never sends:

- The full content of `~/.upstream/config.yaml`.
- Raw camera frames over HTTP — frames will go over SRT, not the API.
- The real machine hostname (`platform.node()`) or other identifying
  host info beyond the `platform.system()` value and the Python
  version.

## Token rotation

Not implemented in M3.2. When the backend signals "rotate token" via
a future heartbeat endpoint, the agent will write the new token back
to `~/.upstream/config.yaml` using the same atomic write strategy as
the state file. Tracked under the heartbeat milestone.
