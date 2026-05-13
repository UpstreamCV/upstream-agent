# Architecture

`upstream-agent` is a Python process that runs on a customer's local
machine. Its job is to read frames from a camera, authenticate and
configure itself against the UpStream API, and push the resulting video
to the UpStream streaming server.

## Target end-to-end flow

```
                       ┌──────────────────────────┐
                       │   ~/.upstream/config.yaml│
                       │   (user-edited)          │
                       └────────────┬─────────────┘
                                    │ load
                                    ▼
 ┌──────────┐   register   ┌─────────────────┐  config   ┌──────────┐
 │  camera  │ ───────────▶ │   UpStream API  │ ─────────▶│ supervisor│
 └────┬─────┘              └─────────────────┘            └─────┬────┘
      │ frames                                                  │
      ▼                                                         ▼
 ┌──────────┐   frames     ┌─────────────┐  encoded   ┌─────────────┐
 │  source  │ ───────────▶ │frame buffer │ ─────────▶│  ffmpeg    │
 │(webcam,  │              └─────────────┘            │  encoder   │
 │ rtsp)    │                                          └─────┬───────┘
 └──────────┘                                                │ packets
                                                             ▼
                                                  ┌───────────────────┐
                                                  │ SRT transport     │
                                                  │ → streaming srv   │
                                                  └───────────────────┘

           heartbeat (status, errors, metrics) ──▶ UpStream API
```

## What exists today (Milestones 1, 2, and 3)

The foundation, a local camera probe, and a three-step API
registration probe. Milestone 1 delivered the CLI, config loader,
local state, and logging with token redaction. Milestone 2 added the
camera-source layer and `upstream check --camera`. Milestone 3 added
the HTTP client foundation (`upstream_agent/api/`) and
`upstream check --api`, which proves the backend approves this agent
+ camera for streaming — stopping short of any actual video transport.

Nothing yet encodes video or streams.

| Layer | Status |
| --- | --- |
| CLI (`upstream init`, `upstream status`, `upstream check --camera` / `--api`, `--version`) | done |
| Config loader (YAML + Pydantic validation) | done |
| Local state (`agent_state.json` with stable `device_id`, `agent_id`, `last_camera_id`) | done |
| Logging (rotating file + stderr, token redaction) | done |
| Camera sources (`VideoSource` ABC + `WebcamSource`) | partial (webcam only) |
| API client (register-agent / register-camera / start-stream-session) | M3 contract confirmed; mocked tests green; real smoke pending |
| Diagnostics (`upstream check --camera`, `--api`) | partial (camera + API only) |
| Token rotation, config fetch, heartbeat, event endpoints | **planned** |
| RTSP / IP camera / file sources | **planned** |
| Frame buffer | **planned** |
| FFmpeg encoder | **planned** |
| SRT transport | **planned** |
| Supervisor (orchestrates the running agent) | **planned** |

## Camera source layer

Sources live under `upstream_agent/sources/`. The base ABC
`VideoSource` (`sources/base.py`) defines the small surface every
source must implement: `open()`, `read_frame()`, `close()`, plus
properties for `is_open`, current `info` (a `FrameInfo` with width,
height, fps), and `last_error`. The base class also implements
`__enter__`/`__exit__` so any source can be used as a context manager
with guaranteed `close()`.

`WebcamSource` (`sources/webcam.py`) is the only concrete source in
Milestone 2. It wraps `cv2.VideoCapture`, imports `cv2` lazily (so a
missing OpenCV install becomes a typed `CameraDependencyMissingError`
rather than an `ImportError`), writes the requested width/height/fps
properties before checking `isOpened()`, and reports the effective
values via `info`. Effective values may differ from requested values
because devices negotiate.

`upstream check --camera` is wired through
`upstream_agent.diagnostics.camera.run_camera_check`, which takes a
`CameraConfig` and returns a `CameraCheckResult`. The function never
raises; the CLI translates the result to stdout / stderr and exit
codes. This keeps the orchestrator unit-testable without `typer` in
the loop and gives future check kinds (`--api`, `--storage`,
`--ffmpeg`) a clear place to land.

The check does not call the API. A placeholder `api.token` is allowed
because the only thing being probed is the local camera.

## API client layer

`upstream_agent/api/` holds the synchronous httpx client. The
package's surface is intentionally small:

- `client.py` — `UpstreamApiClient` with three public methods
  (`register_agent`, `register_camera`, `start_stream_session`), an
  isolated `_build_auth_header()` so swapping the `Bearer` scheme is
  one place, one `_request` that handles JSON encode/decode and
  Pydantic-validated response parsing, and a status+code-driven
  `_raise_for_status` that maps the confirmed M3.2 error envelope
  (`{"error": {"code", "message", "details", "request_id"}}`) into
  typed `ApiError` subclasses. Endpoint paths are module-level
  constants (`AGENT_REGISTER_PATH`, `CAMERA_REGISTER_PATH`,
  `STREAM_SESSION_START_PATH`).
- `schemas.py` — Pydantic v2 request/response models matching the
  confirmed contract. Requests use `extra="forbid"` (cannot
  accidentally send unknown fields); responses use `extra="ignore"`
  (backend can add fields without breaking us). `CameraRegisterResponse`
  has a pre-validator that accepts both the wrapped (`{"camera":
  {...}}`) and flat (`{"id": ..., ...}`) shapes that have been
  observed in upstream docs.

`upstream_agent/diagnostics/api.py` is the orchestrator that walks
the three-step flow. It accepts an injectable `client_factory` (for
tests) and an optional `save_progress` callback that the CLI uses to
persist partial state immediately after each successful step — so a
later failure does not lose earlier IDs.

**Privacy decisions baked into the orchestrator:**

- The agent's `name` is generated as
  `upstream-agent-<first-8-hex-of-device_id>`. Stable across runs,
  non-sensitive, no hostname leakage.
- `hostname` is **never** sent. The schema permits it; the agent
  leaves it `None` and Pydantic's `exclude_none=True` keeps it off
  the wire.
- `external_id` and `device_path` are non-OS-specific (`webcam-0`,
  `webcam:0`) rather than Linux-style `/dev/video0`.

**`upstream check --api`** is the wiring:

1. Load config; require a non-placeholder `api.token` (the check
   would fail trivially on the placeholder).
2. Load local state; pass it through `run_api_check`.
3. Print a clean six-line success block on success, or
   `Error (<step>): <message>` on failure. The message carries the
   `code=` and `request_id=` segments from the error envelope.

**Never logged or printed:**

- The full API token. The redaction filter scrubs Bearer headers and
  the configured token even if a third-party library tries to log
  them.
- The SRT URL (top-level or nested), `streamid`, or `session_token`.
  The client receives them, the orchestrator extracts only
  `srt_url_present: bool`, and the response object is dropped.
- The `session_token` JWT is treated as opaque bytes — the agent
  does not parse it.
- `account_id` — it appears on the response but is not surfaced in
  CLI output. Reduces accidental leakage in support chats.

The check stops *before* any streaming. Future milestones will read
the SRT URL from a fresh `start_stream_session` call when they
actually need to connect to the streaming server.

## Design principles

1. **Public-repo-first.** No secrets, real URLs, or customer data in
   tracked files. Tokens are always redacted in any output the user
   could screenshot or paste.
2. **Single source of truth for configuration.** All knobs live in the
   YAML config file. The environment provides path overrides only, not
   secrets.
3. **State is recoverable.** Corrupt local state files are renamed,
   not silently overwritten, so a previously-registered `agent_id` can
   be recovered if needed.
4. **Errors are typed.** All package errors inherit from
   `UpstreamError`; the CLI catches that one type at the top level and
   exits non-zero with a clean stderr message.
