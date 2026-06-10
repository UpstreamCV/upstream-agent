# API contracts

The agent talks to the UpStream API server via HTTPS. Milestone 3.2
updated the client to match the **confirmed** backend contract.
Endpoint paths, request/response shapes, and error codes below
reflect what the API team has settled on.

> **Status:** CONFIRMED as of Milestone 3.2. Future server changes
> will land in the same two files (`upstream_agent/api/client.py` for
> paths, `upstream_agent/api/schemas.py` for shapes); the rest of
> the agent does not need to change.
>
> **Milestone 4 update:** no new endpoints. The streaming preflight
> reuses the existing `register_agent` / `register_camera` /
> `start_stream_session` / `stop_stream_session` flow. The only
> wire change is the `capabilities` payload on `register_agent`,
> which now reports honest probed values (`{"ffmpeg":
> "<version>", "srt": <bool>}`) instead of the hardcoded
> `{"ffmpeg": false, "srt": false}`.
>
> **Milestone 5 prep update:** still no new endpoints. The live
> streaming command (`upstream run`) reuses the same four endpoints.
> The only behavioural difference from `check --api` /
> `check --streaming` is that `upstream run` keeps the session open
> for the duration of the stream and calls `stop_stream_session`
> only on shutdown (or on any error path) — instead of stopping
> immediately after `start_stream_session` returns.
>
> **Blocker for M5 real smoke:** the SRT URL returned by
> `start_stream_session` must point at a host/port the agent can
> reach. Local-dev environments currently get a URL whose host or
> port doesn't reach the running MediaMTX; the agent's runtime is
> otherwise complete and gates the real end-to-end smoke on this
> being fixed server-side.
>
> **Milestone 6 update:** one new endpoint —
> `POST /v1/heartbeats?agent_id=<uuid>`. While `upstream run` is
> streaming, a background thread POSTs a heartbeat every 5 seconds
> so the API can mark the camera/session online and surface basic
> runtime telemetry. The heartbeat carries no SRT URL, streamid, or
> session token. See the new section below for the confirmed
> contract.
>
> **Milestone 7 update: no new endpoints.** The recovery loop reuses
> the existing `POST /v1/stream-sessions/start` and
> `POST /v1/stream-sessions/{session_id}/stop` calls. Every
> reconnect attempt asks the API for a fresh stream session; the
> previous session is stopped first via `stop_stream_session`. The
> agent never reuses an SRT URL or session token across attempts.
> The recoverable-vs-fatal HTTP-status mapping the supervisor uses
> is documented in the "Error responses" section near the bottom.
>
> **Milestone 7.2 update: one existing endpoint adopted.** The
> M7 reconnect loop trusted local FFmpeg startup as proof that
> streaming had resumed; live smoke showed that was wrong (MediaMTX
> may reject the SRT publish without FFmpeg knowing). The agent now
> calls the existing `GET /v1/stream-sessions/{session_id}`
> endpoint after FFmpeg starts and polls until the session's
> `state` flips to `"streaming"` (the value the API sets when
> MediaMTX's `runOnReady` webhook confirms remote ingest). No
> new server-side endpoint was needed. See the new
> `GET /v1/stream-sessions/{session_id}` section below.
>
> **Milestone 7.3 update: freshness note on the `state` field.**
> The `state == "streaming"` signal proves only **initial** ingest
> for the current session. Each `start_stream_session` creates a
> new DB row, so there is no stale-state leak between attempts.
> However, the field is *sticky once set*: if MediaMTX's
> `runOnNotReady` webhook is missed (network drop, webhook call
> fails, server restart), the row can remain `"streaming"` after
> the SRT publish has died. There is no server-side TTL job that
> resets stale `"streaming"` rows. The agent does not pretend to
> verify ongoing dashboard playback; the `ingest_confirmed`
> event explicitly says "Dashboard playback is not directly
> verified." Until the streaming server / API gains a push channel
> or a server-side staleness sweep, this is the strongest local
> signal available. M7.3 also drops the no-longer-emitted
> `reconnecting` heartbeat status from the agent's code paths —
> the literal stays in the schema for forward compatibility but
> the agent only sends `streaming` / `error`.

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
    "ffmpeg": "8.1.1",
    "srt": true
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
| `capabilities` | yes | Honest probed values from M4 onward: `{"ffmpeg": "<version>"\|false, "srt": <bool>}`. `ffmpeg` is the version string when FFmpeg is detected, else `false`; `srt` is `true` only if `ffmpeg -protocols` lists the `srt` protocol. |

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
    "capabilities": { "ffmpeg": "8.1.1", "srt": true },
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

**Received but not yet acted on — `expires_at`.** Both the top-level
`expires_at` and `session.expires_at` are received and validated by the
agent's Pydantic schema, but the supervisor does **not** currently
schedule a proactive refresh against them. The agent only notices
session expiry reactively, when an FFmpeg write fails because the
ingest server stopped accepting the publish (typically 5–30 s after
the underlying socket dies). Proactive refresh keyed off `expires_at`
is an M8 candidate; see `architecture.md` "Honest gaps and planned
work (M8 candidates)".

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

### `GET /v1/stream-sessions/{session_id}`

Read the current state of a stream session. Used by `upstream run`
in the Milestone 7.2 reconnect loop to confirm remote ingest has
actually started — i.e., MediaMTX accepted the SRT publish and
fired its `runOnReady` webhook, which flips the session's `state`
column from `"approved"` to `"streaming"` on the API side. Local
FFmpeg startup is **not** proof of remote ingest; the agent polls
this endpoint until the API confirms.

**Request**

```
GET /v1/stream-sessions/{session_id} HTTP/1.1
Authorization: Bearer <token>
Accept: application/json
```

No request body. The session UUID is on the URL.

**Response 200 — flat session record**

The response shape is the *flat* session object — not the nested
`{ "session": {...}, "srt_url": "...", "session_token": "..." }`
shape returned by `POST /v1/stream-sessions/start`. The agent
parses it via `upstream_agent.api.schemas.Session`.

```json
{
  "id": "0b91a1a8-7d3d-4c91-9c6e-ce5b9f0e6e3e",
  "account_id": "00000000-0000-0000-0000-000000000001",
  "agent_id": "...",
  "camera_id": "...",
  "state": "streaming",
  "srt_url": "srt://ingest.upstreamcv.com:18890?streamid=publish:sess_<hex>&latency=200",
  "streamid": "publish:sess_<hex>",
  "expires_at": "2026-05-14T13:00:00Z",
  "created_at": "2026-05-14T12:00:00Z",
  "started_at": "2026-05-14T12:00:01Z",
  "ended_at": null,
  "requested": { "width": 1280, "height": 720, "fps": 30, "codec": "h264", "bitrate_kbps": 3000, "latency_mode": "balanced" },
  "approved":  { "width": 1280, "height": 720, "fps": 30, "codec": "h264", "bitrate_kbps": 3000, "latency_mode": "balanced" }
}
```

**State values**

| `state` | Meaning |
| --- | --- |
| `approved` | Session created; awaiting MediaMTX ingest confirmation. |
| `streaming` | Remote ingest confirmed. **This is the value the agent waits for.** |
| `ended` | Session stopped (terminal — agent treats as recoverable; will request a new session). |
| `expired` | Session TTL exceeded (terminal — recoverable). |
| `failed` | Validation failure (terminal — recoverable). |

**Error responses**

Uses the same envelope and status-code mapping documented in
"Error envelope" below.

- `404` → `ApiValidationError` (fatal — agent stops).
- `401` with `code="token_revoked"` / `code="token_expired"` → typed
  fatal error.
- `5xx` / `429` / connection failure → recoverable; the agent's
  ingest poll catches these and keeps polling within the overall
  timeout.

**Sensitive-data discipline.** The response body still contains
`srt_url` and `streamid` — the agent reads only `state` and
discards the rest. The SRT URL and streamid never appear on
`Session` outside the orchestrator scope, never in logs, and never
in heartbeat payloads.

### `POST /v1/heartbeats`

Submit a batch of heartbeats for an agent. Called by `upstream run`
on a 5-second cadence from a background thread while the stream is
active. The agent sends one heartbeat per call in Milestone 6;
batching across multiple cameras or replaying offline-queued
heartbeats is deferred to the M7 reconnect work.

**Query parameter:** `agent_id` — the UUID of the agent (under the
calling account's token). Cameras and sessions referenced in the
batch must belong to that agent.

**Request body**

```json
{
  "heartbeats": [
    {
      "camera_id": "9c8c7e3e-c1a6-4d6c-bdda-b8c0f5a3d211",
      "session_id": "2b6f0ad4-e7c4-4d61-aa1e-a8d4c47e9b22",
      "status": "streaming",
      "fps": 29.97,
      "bitrate_kbps": 3000,
      "width": 1280,
      "height": 720,
      "codec": "h264",
      "last_error": null,
      "agent_version": "0.1.0",
      "reported_at": "2026-05-13T12:00:00Z"
    }
  ]
}
```

| Field | Required | Notes |
| --- | --- | --- |
| `heartbeats` | yes | 1–100 items per call. M6 sends one at a time. |
| `camera_id` | no | UUID; must belong to this agent. |
| `session_id` | no | UUID; must belong to this agent. |
| `status` | yes | Exactly one of `"online"`, `"streaming"`, `"error"`, `"reconnecting"`. The agent sends `"streaming"` while FFmpeg is running and frames are flowing, and `"error"` otherwise. `"reconnecting"` is reserved in the contract for forward compatibility but is **not currently emitted** — the reporter is session-scoped and torn down between recovery attempts, so the dashboard sees no in-between signal during backoff gaps. A session-independent reporter that could emit `"reconnecting"` is an M8 candidate; see `architecture.md` "Honest gaps and planned work (M8 candidates)". |
| `fps` | no | Float, 0..1000. Computed over a sliding 60-frame window. |
| `bitrate_kbps` | no | Int, 0..200000. The server-approved bitrate. |
| `width`, `height` | no | Int, 1..7680 / 1..4320. The server-approved resolution. |
| `codec` | no | String, max 32 chars. |
| `last_error` | no | String, max 512 chars. The most recent error the runtime saw, if any. |
| `agent_version` | no | String, max 64 chars. `upstream_agent.__version__`. |
| `reported_at` | yes | ISO 8601 with timezone. The agent emits UTC. |

**Response 200**

```json
{
  "accepted": 1,
  "server_time": "2026-05-13T12:00:01Z"
}
```

`accepted` is the number of items the server actually inserted (≤
the request count if individual items failed validation). The
endpoint does **not** return a `next_interval_s` directive or any
stop/control flag — the agent picks its own cadence.

**Server-side side effects.** On a successful batch the server:

1. Inserts one row per heartbeat into the `heartbeats` table.
2. Updates `agents.last_seen_at = now()` for the agent_id.
3. Updates `cameras.updated_at = now()` for each referenced camera.
4. Records a `usage_events` row (`event_type="heartbeat_batch"`).

**Error responses**

| HTTP status | Code | Notes |
| --- | --- | --- |
| 401 | `token_revoked` | Fatal. The reporter stops and the supervisor exits the run with `exit_reason="fatal_heartbeat"`. |
| 401 | `token_expired` | Fatal. Same handling as `token_revoked`. |
| 403 | `forbidden_resource` | `camera_id` or `session_id` in the batch is not owned by this agent. `details.missing_camera_ids` / `missing_session_ids` lists which. Mapped to `ApiAuthError`; treated as fatal. |
| 422 | `validation_error` | Body failed Pydantic validation. Mapped to `ApiValidationError`; treated as fatal. |
| 429, 5xx, network | various | Transient. The reporter logs a warning and tries again next tick. M7 will add backoff. |

**Related read endpoints** (used by the dashboard, not the agent):

- `GET /v1/agents/{agent_id}/heartbeats?limit=&before=` — agent
  heartbeat history, newest first.
- `GET /v1/cameras/{camera_id}/heartbeats?limit=&before=` — camera
  heartbeat history. Same shape.

The contract here matches the upstream-api-server implementation
verbatim — see `src/upstream_api/routers/heartbeats.py` and
`src/upstream_api/schemas/heartbeats.py` in that repo.

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

### Recoverable vs fatal (Milestone 7)

The supervisor's recovery loop classifies typed `ApiError`
subclasses to decide whether to retry the work or stop. The
mapping is purely by class — the agent never inspects message
strings.

| Typed error | Classification | Why |
| --- | --- | --- |
| `ApiConnectionError` | Recoverable | Transport blip — DNS, refused, timeout. |
| `ApiServerError` (5xx) | Recoverable | Server-side transient. |
| `ApiRateLimitError` (429) | Recoverable | Backoff handled by the supervisor, not by reading `Retry-After`. |
| `ApiContractError` | Recoverable | Often a server-side intermittent (partial JSON, transient gateway response). |
| `ApiTokenRevokedError` | Fatal | Requires human action — agent stops cleanly. |
| `ApiTokenExpiredError` | Fatal | Same — `exit_reason=fatal_heartbeat` from the heartbeat side-channel. |
| `ApiAuthError` (401/403 other) | Fatal | Credential / permission issue. |
| `ApiValidationError` (400/409/422/404) | Fatal | Bad request shape or unknown resource — retrying won't help. |

`camera_already_streaming` shows up as `ApiValidationError`
(422) and is classified as **fatal** — the supervisor stops
cleanly rather than retrying. The supervisor's normal
recovery path always stops the previous session before
requesting a new one, so this code only surfaces if the
previous `stop_stream_session` call also failed *and* the
server-side session hasn't timed out yet. If you hit this in
the field, wait for the server-side stream-session timeout
(typically tens of seconds) and re-run.

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
