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
| API client (register-agent / register-camera / start-stream-session) | partial (M3 surface only) |
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
  one place, and one `_request` that handles JSON encode/decode,
  Pydantic-validated response parsing, and the status-to-typed-error
  mapping. Endpoint paths are module-level constants
  (`AGENT_REGISTER_PATH`, `CAMERA_REGISTER_PATH`,
  `STREAM_SESSION_START_PATH`) so the eventual backend swap is
  mechanical.
- `schemas.py` — Pydantic v2 request/response models. Requests use
  `extra="forbid"` (cannot accidentally send unknown fields);
  responses use `extra="ignore"` (backend can add fields without
  breaking us).

`upstream_agent/diagnostics/api.py` is the orchestrator that walks
the three-step flow. It accepts an injectable `client_factory` (for
tests) and an optional `save_progress` callback that the CLI uses to
persist partial state immediately after each successful step — so a
later failure does not lose earlier IDs.

**`upstream check --api`** is the wiring:

1. Load config; require a non-placeholder `api.token` (the check
   would fail trivially on the placeholder).
2. Load local state; pass it through `run_api_check`.
3. Print a clean six-line success block on success, or
   `Error (<step>): <message>` on failure.

**Never logged or printed:**

- The full API token. The redaction filter scrubs Bearer headers and
  the configured token even if a third-party library tries to log
  them.
- The SRT URL. The client receives it, the orchestrator extracts only
  `srt_url_present: bool`, and the URL is dropped along with the
  response object. The user sees `SRT URL received: yes`.

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
