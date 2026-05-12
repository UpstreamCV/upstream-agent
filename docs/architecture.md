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

## What exists today (Milestones 1 and 2)

The foundation plus a local camera probe. Milestone 1 delivered the
CLI, config loader, local state, and logging with token redaction.
Milestone 2 adds the camera-source layer and the `upstream check
--camera` diagnostic so an operator can prove their webcam is usable
before any of the streaming machinery is built.

Nothing yet talks to the API, encodes video, or streams.

| Layer | Status |
| --- | --- |
| CLI (`upstream init`, `upstream status`, `upstream check`, `--version`) | done |
| Config loader (YAML + Pydantic validation) | done |
| Local state (`agent_state.json` with stable `device_id`) | done |
| Logging (rotating file + stderr, token redaction) | done |
| Camera sources (`VideoSource` ABC + `WebcamSource`) | partial (webcam only) |
| Diagnostics (`upstream check --camera`) | partial (camera only) |
| API client (registration, config fetch, heartbeat) | **planned** |
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
