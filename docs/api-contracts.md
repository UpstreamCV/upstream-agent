# API contracts

The agent talks to the UpStream API server via HTTPS. Milestone 3
shipped the first concrete client and three endpoints — but the
**backend contract is still being finalised by another teammate**, so
every endpoint path, request field, response field, and error code on
this page is **provisional** until they confirm.

> **Status:** PROVISIONAL — confirm with the API server team before
> relying on any specific path or field name. The agent isolates these
> in two files (`upstream_agent/api/client.py` for paths,
> `upstream_agent/api/schemas.py` for shapes), so adjusting after
> confirmation is mechanical.

The current dev base URL is `http://api.upstreamcv.com`. The agent
takes it from `api.base_url` in `~/.upstream/config.yaml` and **never**
hardcodes it.

## Authentication

Every request carries:

```
Authorization: Bearer <token>
Content-Type: application/json
Accept: application/json
User-Agent: upstream-agent/<version>
```

The token lives in `~/.upstream/config.yaml` under `api.token` and is
**never** sent anywhere else (no query parameters, no URL paths, no
logs). The CLI rejects the literal placeholder
`YOUR_API_TOKEN_HERE` before any HTTP request is made.

The auth-header construction is isolated to
`UpstreamApiClient._build_auth_header()` so swapping `Bearer` for
another scheme (e.g. `X-API-Key`) is a one-method change.

## Endpoints

Three endpoints back the `upstream check --api` flow. All are flat
`POST`s with IDs carried in the JSON body (not path-interpolated).

### `POST /v1/agents/register`

Registers a new device or returns the existing `agent_id` for a known
`device_id`. Idempotency semantics are **assumed** for M3 — confirm
with backend.

**Request body**

```json
{
  "device_id": "uuid-string-from-local-state",
  "agent_version": "0.1.0",
  "platform": "Darwin"
}
```

| Field | Required | Notes |
| --- | --- | --- |
| `device_id` | yes | Stable UUID generated locally on first run. |
| `agent_version` | yes | Read from `upstream_agent.version.__version__`. |
| `platform` | no | `platform.system()` value, e.g. `"Darwin"`, `"Linux"`, `"Windows"`. Hostname is intentionally **not** sent. |

**Response body**

```json
{
  "agent_id": "agent_123"
}
```

| Field | Required | Notes |
| --- | --- | --- |
| `agent_id` | yes | Stored in `~/.upstream/agent_state.json`. |

### `POST /v1/cameras/register`

Registers (or looks up) a camera for the agent.

**Request body**

```json
{
  "agent_id": "agent_123",
  "name": "Front Door",
  "source_type": "webcam",
  "source": 0,
  "requested_width": 1280,
  "requested_height": 720,
  "requested_fps": 30
}
```

| Field | Required | Notes |
| --- | --- | --- |
| `agent_id` | yes | Returned by the agent-register call. |
| `name` | yes | Human-readable, from `camera.name`. |
| `source_type` | yes | `"webcam"` or `"rtsp"`. M3 only supports webcam. |
| `source` | yes | Integer index for webcam, RTSP URL string for rtsp (RTSP is unimplemented in M3). |
| `requested_width` / `_height` / `_fps` | yes | Client requests — the server decides what is allowed. |

**Response body**

```json
{
  "camera_id": "cam_456"
}
```

`camera_id` is stored as `last_camera_id` in local state.

### `POST /v1/stream-sessions/start`

Requests an approved stream session for the given agent + camera.

**Request body**

```json
{
  "agent_id": "agent_123",
  "camera_id": "cam_456",
  "source_type": "webcam",
  "requested_width": 1280,
  "requested_height": 720,
  "requested_fps": 30
}
```

**Response body**

```json
{
  "session_id": "sess_789",
  "approved_config": {
    "codec": "h264",
    "width": 1280,
    "height": 720,
    "fps": 30,
    "bitrate": 2500000,
    "srt_url": "srt://...",
    "expires_in": 3600
  }
}
```

| Field | Required | Notes |
| --- | --- | --- |
| `session_id` | yes | Echoed in CLI output. Not persisted in state. |
| `approved_config.codec` | yes | e.g. `"h264"`. |
| `approved_config.width` / `height` / `fps` | yes | Server-approved values. May differ from requested. |
| `approved_config.bitrate` | no | bits/sec. |
| `approved_config.srt_url` | yes | **Sensitive** — may contain per-session stream tokens. The agent reads it once, exposes only `srt_url_present: bool` on the result, and never logs/prints/persists the URL itself. |
| `approved_config.expires_at` / `expires_in` | no | ISO timestamp or seconds-until-expiry. Either form is accepted. |

## Sensitive-data rules

- **`srt_url` is sensitive** even though the path looks public. It is
  the only artifact in any response that may carry session tokens.
  Future code that actually streams will read it from a fresh
  `start_stream_session` call rather than persisting it.
- **The full API token is never** sent in any field except the
  `Authorization` header.
- **Local state never stores tokens** (API token, session token, or
  any URL containing one).

## Error responses

All non-2xx responses are translated to typed
`upstream_agent.errors.ApiError` subclasses. The assumed JSON body
shape (provisional) is:

```json
{
  "error_code": "machine_readable_code",
  "error": "Human-readable message"
}
```

The parser accepts `error` or `message` for the human string and
`error_code` or `code` for the machine code. All fields are optional —
a body that is absent, non-JSON, or missing both keys still produces a
typed error with a generic message.

| HTTP status | Mapped to | Trigger |
| --- | --- | --- |
| 400, 422 | `ApiValidationError` | Server rejected the request body. |
| 401, 403 | `ApiAuthError` | Generic auth failure. |
| 401, 403 with `error_code == "token_revoked"` | `ApiTokenRevokedError` | Provisional code — confirm with backend. |
| 429 | `ApiRateLimitError` | Too many requests. |
| 5xx | `ApiServerError` | Backend transient/internal failure. |
| Other 4xx (404, 409, …) | bare `ApiError` | Unmapped — caller can inspect. |
| Network failure / timeout | `ApiConnectionError` | No HTTP status to report. |
| Missing/malformed response field | `ApiContractError` | Body parsed but didn't match expected shape. |

Error messages carry `{method} {path} -> {status} {reason}: {parsed_msg}`
but **never** include request or response bodies. Tokens cannot leak
through error strings.

## Token rotation

Not implemented in M3. When the backend later signals "rotate token"
(e.g. via heartbeat), the agent will write the new token back to
`~/.upstream/config.yaml` using the same atomic write strategy as the
state file. Tracked under the heartbeat milestone.

## Local-only data

The agent never sends:

- The full content of `~/.upstream/config.yaml`.
- Raw camera frames over HTTP — frames will go over SRT, not the API.
- Hostname or other identifying machine info beyond the optional
  `platform` string.

## Pending backend confirmation

These are the load-bearing assumptions to confirm with the API team:

1. **Endpoint paths**: `/v1/agents/register`, `/v1/cameras/register`,
   `/v1/stream-sessions/start`.
2. **Flat request bodies with IDs in JSON** (vs nested
   `/v1/agents/{id}/cameras` paths).
3. **Error body shape**: `{"error_code": "...", "error": "..."}`.
4. **`token_revoked` error-code string** for the revoked-token signal.
5. **Agent-register idempotency**: same `device_id` → same `agent_id`,
   200 not 409.
6. **`approved_config.srt_url`** is the field name carrying the SRT
   endpoint.
