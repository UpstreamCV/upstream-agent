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

## What exists today (Milestones 1–6)

Milestone 1 delivered the CLI, config loader, local state, and
logging with token redaction. Milestone 2 added the camera-source
layer and `upstream check --camera`. Milestone 3 added the HTTP
client and `upstream check --api`. Milestone 4 added the local
FFmpeg/SRT/H264 preflight, the SRT URL parser, the FFmpeg command
builder, and `upstream check --streaming`. Milestone 5 adds the
`FfmpegEncoderProcess` subprocess wrapper, the supervisor
(`upstream_agent/supervisor.py`), and `upstream run` — the first
command that actually streams video. **Milestone 6** adds the
heartbeat loop: a background thread that POSTs runtime telemetry
(status, fps, bitrate, resolution, last error) to
`POST /v1/heartbeats` every 5 seconds while the stream is active.

| Layer | Status |
| --- | --- |
| CLI (`upstream init`, `upstream status`, `upstream check --camera` / `--api` / `--streaming`, `upstream run`, `--version`) | done |
| Config loader (YAML + Pydantic validation) | done |
| Local state (`agent_state.json` with stable `device_id`, `agent_id`, `last_camera_id`) | done |
| Logging (rotating file + stderr, token redaction) | done |
| Camera sources (`VideoSource` ABC + `WebcamSource`) | partial (webcam only) |
| API client (register-agent / register-camera / start/stop-stream-session / heartbeat) | M3–M6 contract confirmed; mocked tests green |
| Diagnostics (camera, API, streaming preflight) | done |
| FFmpeg probe (version / SRT protocol / H264 encoder) | done |
| SRT URL parsing/validation | done |
| FFmpeg command builder | done |
| Honest agent capabilities reporting via `register_agent` | done |
| FFmpeg subprocess wrapper (`FfmpegEncoderProcess`) | done (M5) |
| Supervisor / runtime (`run_agent`) | done (M5–M6) |
| Live SRT streaming end-to-end against real MediaMTX | done (M5) |
| Heartbeat loop + runtime health reporting | done (M6) |
| Frame buffer | **planned** |
| Reconnect/backoff, session refresh, token rotation, config fetch | **planned (M7+)** |
| RTSP / IP camera / file sources | **planned** |

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

## Local preflight layer (Milestone 4)

`upstream check --streaming` answers a single question — *is this
machine ready to stream?* — without actually streaming. The phases
chain together like this:

```
Config ── FFmpeg probe ── API approval ── SRT URL parse ── command build
                │             │              │                 │
            version,      approved      validate scheme,    list[str] —
            srt support,  config +      host, port, mode   construction
            H264 encoder  SRT URL                          only; not run
```

The orchestrator is :func:`upstream_agent.diagnostics.streaming.run_streaming_check`.
It composes three independent modules:

- :mod:`upstream_agent.diagnostics.ffmpeg_check` —
  :func:`probe_ffmpeg` runs ``ffmpeg -version`` / ``-protocols`` /
  ``-encoders`` via a safe ``subprocess.run`` (no shell, timeouts,
  captured stdout/stderr) and returns a :class:`FfmpegProbeResult`
  describing what the local binary supports.
- :mod:`upstream_agent.transport.srt` — :func:`parse_srt_url`
  validates the approved SRT URL and returns a :class:`ParsedSrtUrl`
  with only safe-to-display metadata (booleans, the port, the mode,
  and the latency). The raw URL, host, and streamid are never stored
  on the result.
- :mod:`upstream_agent.encoder.command_builder` —
  :func:`build_ffmpeg_command` returns the ``list[str]`` argv that
  Milestone 5 will execute. The list is constructed only to prove it
  builds; M4 discards it immediately. :func:`redact_command` exists
  for any future need to display the command — it replaces the SRT
  URL with the literal ``srt://<redacted>``.

The API approval phase shares logic with `upstream check --api` via
the private :func:`_obtain_stream_approval` helper in
:mod:`upstream_agent.diagnostics.api`. Both orchestrators build the
same `register_agent` / `register_camera` / `start_stream_session`
payloads and both stop the session before returning. The shared
helper also receives a `capabilities` dict so the streaming check
can pass its FFmpeg probe results straight into `register_agent` —
making the backend's view of "what can this agent do" honest.

**Capabilities reporting.** Before Milestone 4 the agent always sent
`{"ffmpeg": false, "srt": false}` to `register_agent`. After
Milestone 4, both `--api` and `--streaming` probe FFmpeg once and
send `{"ffmpeg": "<version>", "srt": <bool>}` based on what the
local binary actually supports. The probe is fast (~50 ms) and runs
only when the check needs to call the API.

## Runtime layer (Milestone 5 prep)

`upstream run` is the live streaming command. The runtime chains
the existing pieces:

```
Config ── FFmpeg probe ── Camera open ── API session approval ── Command build ── FFmpeg start ── Frame loop ── Cleanup
                                              │
                                          (session held open
                                           for the lifetime of
                                           the run, not stopped
                                           immediately)
```

The orchestrator is :func:`upstream_agent.supervisor.run_agent`. It
reuses every M3/M4 module verbatim:

- :func:`upstream_agent.diagnostics.ffmpeg_check.probe_ffmpeg` —
  probes the local binary; results feed both the capability payload
  sent to `register_agent` and the H264 encoder chosen for the
  output command.
- :func:`upstream_agent.diagnostics.api._obtain_stream_approval` —
  same private helper used by `upstream check --api` and
  `upstream check --streaming`. Returns the same outcome shape;
  `run_agent` holds onto the `StreamSessionResponse` only long
  enough to extract the SRT URL into a local variable.
- :func:`upstream_agent.transport.srt.parse_srt_url` — validates
  the API-issued URL.
- :func:`upstream_agent.encoder.command_builder.build_ffmpeg_command`
  and :func:`redact_command` — produce the raw FFmpeg argv (with
  the URL) and a redacted copy for logs/repr.
- :class:`upstream_agent.encoder.ffmpeg.FfmpegEncoderProcess` —
  owns the `subprocess.Popen`, the stdin pipe, and the lifecycle
  (`start`, `write_frame`, `poll`, `stop`).
- :class:`upstream_agent.sources.webcam.WebcamSource` — context-
  managed BGR frame source.

The frame loop is intentionally simple: read a frame, resize if
the camera shape doesn't match the server-approved shape, `tobytes`
it, write to FFmpeg's stdin. After every write the supervisor polls
FFmpeg; if FFmpeg exited mid-stream the supervisor raises
:class:`FfmpegExitError` and cleanup runs.

**Cleanup order** is the same regardless of how the loop exits
(normal completion, max_frames hit, Ctrl+C, FFmpeg start failure,
mid-stream FFmpeg exit, camera read failure, API approval failure):

1. `FfmpegEncoderProcess.stop()` — closes stdin so FFmpeg can drain
   the mpegts trailer, then SIGTERM with a timeout, then SIGKILL.
2. `WebcamSource.close()` — releases the OpenCV capture.
3. `client.stop_stream_session(session_id)` — best-effort; a
   failure here is logged as a warning, not propagated.

This ordering matches the user-visible shutdown block (`FFmpeg
stopped` → `Camera released` → `Stream session stopped`).

**Lifetime difference vs. preflight.** `upstream check --api` and
`upstream check --streaming` stop the API session almost
immediately after `start_stream_session` returns. `upstream run`
keeps the session open for the duration of the frame loop and
only stops it on shutdown. No new API endpoint is required — the
existing `start`/`stop` pair is sufficient.

**Sensitive-data discipline.**

- The raw SRT URL lives in a single local variable inside
  `run_agent` from the moment `_obtain_stream_approval` returns
  until the supervisor exits. It is passed once into
  `build_ffmpeg_command` and once into
  `FfmpegEncoderProcess(command, redacted_command)`. It is never
  stored on the result object or any persisted state.
- `FfmpegEncoderProcess.__repr__` returns only the redacted argv.
  Error messages from the wrapper (`FfmpegStartError`,
  `FfmpegWriteError`) never contain the URL; if FFmpeg echoes the
  URL in its own stderr, the wrapper scrubs any line containing
  `srt://` before quoting a short tail.

## Runtime health (Milestone 6)

`upstream run` now reports liveness back to the API on a 5-second
cadence so the dashboard can mark the camera/session online and
show basic telemetry. Two new modules implement this:

- :class:`upstream_agent.runtime.stats.RuntimeStats` — a small
  thread-safe counter object. The frame loop in
  :func:`run_agent` calls ``record_frame`` after every successful
  write to FFmpeg; the heartbeat thread calls ``snapshot`` once
  per interval. The snapshot is a frozen
  :class:`StatsSnapshot` dataclass with ``frames_written``,
  rolling ``fps`` (computed over the last 60 frames),
  ``camera_connected``, ``ffmpeg_running``, ``last_frame_at``,
  and ``last_error``.
- :class:`upstream_agent.heartbeat.reporter.HeartbeatReporter` —
  one daemon :class:`threading.Thread`. Its loop snapshots the
  stats, builds a :class:`Heartbeat` from the snapshot + the
  server-approved stream config + ``__version__``, and POSTs it
  via :meth:`UpstreamApiClient.send_heartbeat`. The reporter
  sleeps on a :class:`threading.Event` so calling :meth:`stop`
  wakes the thread immediately rather than waiting out the
  interval.

The heartbeat side-channel runs **alongside** the frame loop;
neither blocks the other:

```
   Camera ──► resize ──► FFmpeg.write_frame ──► stats.record_frame
                                                       │
                                                       ▼
                                              RuntimeStats (thread-safe)
                                                       ▲
                                                       │ snapshot()
                                          HeartbeatReporter thread
                                                       │
                                                       ▼
                                              POST /v1/heartbeats
                                                       │
                                                       ▼
                                                  UpStream API
```

**Failure handling** is intentionally minimal for M6:

- *Transient* failures (network blip, 5xx, 429, generic
  :class:`ApiError`) log a warning and try again on the next
  tick. There is no exponential backoff yet — that lands in M7.
- *Fatal* failures (:class:`ApiTokenRevokedError`,
  :class:`ApiTokenExpiredError`, :class:`ApiValidationError`)
  stop the reporter thread and store a reason string. The
  supervisor checks :meth:`HeartbeatReporter.fatal_reason` after
  every frame; if it becomes non-``None`` the loop exits cleanly
  with ``exit_reason="fatal_heartbeat"`` and the usual cleanup
  chain runs.

**Cleanup ordering** during shutdown is now
heartbeat → FFmpeg → camera → API session. Stopping the
heartbeat first means the dashboard sees no further "streaming"
pings after the agent has decided to shut down, even if FFmpeg
teardown takes a moment.

**Sensitive-data discipline for heartbeats.** The heartbeat
payload contains no SRT URL, streamid, or session token — only
the camera UUID, the session UUID, an enum status, and numeric
telemetry. The only sensitive value involved is the bearer token,
which is attached by :meth:`UpstreamApiClient._build_auth_header`
and never logged (the redaction filter scrubs it as a second
line of defense).

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
