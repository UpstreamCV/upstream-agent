# API contracts

The agent talks to the UpStream API server via HTTPS. None of the
endpoints below are implemented yet — this file documents the planned
contracts so future commits can fill them in without re-litigating shape
decisions.

> **Status:** TBD. Update with concrete request/response schemas during
> the API milestone, after the server side has confirmed the contract.

## Authentication

Every authenticated call carries the user's API token in an
`Authorization: Bearer <token>` header. The token lives in
`~/.upstream/config.yaml` under `api.token` and is **never** sent
anywhere else (no query parameters, no URL paths, no logs).

## Endpoints (planned)

### `POST /agent/register`

Registers a new device or returns the existing `agent_id` for a known
`device_id`.

* Request body: `device_id`, agent version, OS info, optional
  `last_camera_id`.
* Response body: `agent_id`, server-assigned camera/stream config.
* Failure modes: 401 (bad token), 409 (device already registered with a
  conflicting label), 5xx (transient).

### `GET /agent/{agent_id}/config`

Fetches the latest server-side config for this agent (camera settings,
stream destination, latency mode).

* TBD.

### `POST /agent/{agent_id}/heartbeat`

Sends a periodic liveness ping with a small status payload.

* Request body: timestamp, current camera id, frame rate, encoder
  status, last error (if any), stream connectivity.
* Response body: optional server-pushed instructions (e.g. "rotate
  token", "reload config", "stop streaming").
* Cadence: TBD, likely every 30–60 seconds with jitter.

### `POST /agent/{agent_id}/event`

Sends discrete events (errors, restarts, camera disconnects).

* TBD.

## Token rotation

When the server returns a "rotate token" instruction via heartbeat, the
agent will write the new token back into `~/.upstream/config.yaml` using
the same atomic write strategy the state file uses. The rotation flow
will be added in the API milestone.

## Local-only data

The agent never sends:

* The full content of `~/.upstream/config.yaml` (only specific fields).
* Raw camera frames over HTTP — frames go to the streaming server over
  SRT, not the API.
* Personally identifying information from the host machine beyond what
  is necessary to identify a device (`device_id`, OS, agent version).
