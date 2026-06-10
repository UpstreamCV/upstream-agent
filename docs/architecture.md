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
 │ (webcam) │              │ (planned)   │            │  encoder   │
 │          │              └─────────────┘            └─────┬───────┘
 └──────────┘                                                │ packets
                                                             ▼
                                                  ┌───────────────────┐
                                                  │ SRT transport     │
                                                  │ → streaming srv   │
                                                  └───────────────────┘

           heartbeat (status, errors, metrics) ──▶ UpStream API
```

## What exists today (Milestones 1–7)

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
**Milestone 7** makes the runtime resilient: transient FFmpeg,
SRT, network, and camera failures no longer kill the agent.
The supervisor now runs an interruptible recovery loop with
bounded exponential backoff, requests a fresh stream session per
attempt, and reopens the camera if it disconnects mid-stream.

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
| Reconnect/backoff, camera reopen, session restart | done (M7) |
| Frame buffer | **planned (M8 candidate)** |
| Proactive session refresh from `expires_at`, token rotation, config fetch | **planned (M8 candidate)** |
| Session-independent heartbeat reporter (so `"reconnecting"` status can be emitted) | **planned (M8 candidate)** |
| Non-webcam input sources (RTSP, IP camera) | **not planned** — agent is webcam-only by design; SRT is the only egress protocol |

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

## Runtime recovery (Milestone 7)

`upstream run` survives normal field failures by retrying the
work that can be retried, without reusing any session token or
SRT URL across attempts. This section is **deliberately honest**
about what the agent can and cannot prove from inside its own
process — the live-smoke tests showed that earlier wordings
over-claimed visibility into the dashboard.

### What the agent CAN prove (per attempt)

- **Local FFmpeg startup.** The subprocess launched and is
  consuming frames on stdin.
- **API-reported session state.** After FFmpeg starts the
  supervisor polls `GET /v1/stream-sessions/{session_id}` and
  waits for `state == "streaming"`. That value flips when
  MediaMTX's `runOnReady` webhook fires for *this specific
  session*, which proves the streaming server accepted at
  least the initial SRT publish + ffprobe handshake. Each new
  `start_stream_session` call creates a new database row, so
  the read is fresh-by-construction — no stale state from
  prior attempts can leak in.
- **Heartbeat acceptance.** The POST to `/v1/heartbeats`
  returned 200.

### What the agent CANNOT prove

- **Ongoing dashboard playback.** Nothing on the API surface
  lets the agent verify that a viewer is currently rendering
  frames. The `state` field is sticky once set to
  `"streaming"`; if the streaming server's `runOnNotReady`
  webhook is missed (e.g. on a sudden network drop) the value
  can lag reality. The agent will eventually see this as an
  FFmpeg write failure or session-stop error, but there can
  be a 5–30 s window where the agent's logs and the
  dashboard's view disagree.
- **MediaMTX's continued re-broadcasting.** Same story —
  the agent has no push channel from the streaming server.

Log messages are worded to reflect this: `ingest_confirmed`
says "API reports session streaming (MediaMTX confirmed
initial ingest). Dashboard playback is not directly
verified." `streaming` says "Streaming started (frames
flowing to streaming server)." Neither claims viewer-side
playback.

### Recovery loop

```
   probe FFmpeg ─── fatal if missing/SRT/H264 unavailable
   open camera ─── fatal on initial open failure
   open API client
   ┌──── recovery loop ──────────────────────────────────────────┐
   │  _run_attempt:                                              │
   │     register (first attempt only) → start_stream_session    │
   │       → build FFmpeg command → start FFmpeg                 │
   │       → poll GET /v1/stream-sessions/{id} for streaming     │
   │       → start HeartbeatReporter → frame loop                │
   │       → ATTEMPT CLEANUP (heartbeat → ffmpeg → session)      │
   │  on normal exit / fatal_heartbeat: break                    │
   │  on fatal classification: re-raise                          │
   │  on recoverable_stream: backoff.sleep, retry                │
   │  on recoverable_camera: close camera, reopen with backoff,  │
   │     retry                                                   │
   └─────────────────────────────────────────────────────────────┘
   OUTER CLEANUP (camera → API client)
```

**Backoff.** `BackoffPolicy` implements bounded exponential
backoff with ±20 % multiplicative jitter: 1 s initial, ×2,
capped at 30 s. Sleep is built on `threading.Event.wait` so a
shutdown signal wakes the wait immediately.

**Recoverable vs fatal classification.** `_classify` maps the
exception type — never the message string — to one of three
categories.

| Category | Triggers | Action |
| --- | --- | --- |
| Fatal | `ApiTokenRevokedError`, `ApiTokenExpiredError`, `ApiAuthError`, `ApiValidationError`, `FfmpegNotFoundError`, `FfmpegSrtUnsupportedError`, `FfmpegH264UnsupportedError`, `FfmpegStartError`, `CommandBuildError`, any non-`Exception` BaseException | Run attempt cleanup, outer cleanup, re-raise |
| Recoverable stream | `FfmpegExitError`, `FfmpegWriteError`, `ApiConnectionError`, `ApiServerError`, `ApiRateLimitError`, `ApiContractError`, `SrtUrlError`, generic `StreamingRuntimeError` | Run attempt cleanup, backoff sleep, retry with a fresh session |
| Recoverable camera | `CameraReadError`, mid-stream `CameraOpenError` | Close camera, reopen with backoff + settle + windowed validation, then retry with a fresh session |

**Camera recovery: open + settle + windowed validation.**
`VideoCapture.open()` success alone is not proof the device is
producing frames — V4L2 and AVFoundation will both return
`isOpened() == True` for a dead handle. The reopen helper:

1. Sleeps `_CAMERA_RECOVERY_SETTLE_S` (default **1.5 s**) after
   `open()`.
2. Reads frames inside a `_CAMERA_RECOVERY_VALIDATION_WINDOW_S`
   (default **5 s**) window; recovery succeeds only when
   `_CAMERA_RECOVERY_VALIDATION_FRAMES` (default **3**)
   consecutive reads return frames. Transient failures inside
   the window reset the streak but do not fail the whole
   attempt.
3. On window timeout, closes the camera (with a
   `_CAMERA_RELEASE_SETTLE_S` ≈ **0.5 s** release-settle) and
   loops. The `BackoffPolicy` is shared across reopen attempts
   so the delay grows.

**macOS / Continuity Camera limitation.** When an iPhone
Continuity Camera leaves range, or any AVFoundation-backed
device disappears, the kernel can keep a stale handle alive in
the Python process. `cv2.VideoCapture(N)` still opens, but
every read returns `(False, None)` forever. The agent does
everything a process can do — fresh `VideoCapture` per
attempt, explicit `CAP_AVFOUNDATION` backend on macOS, settle
window, multi-frame validation — but **in-process recovery
from a disappeared AVFoundation device is not reliable.** After
`_CAMERA_UNAVAILABLE_HINT_AFTER` (default **3**) consecutive
failed reopen cycles, the supervisor emits a single
`camera_unavailable_hint` event suggesting the operator
restart the agent. Recovery keeps trying after that — some
USB cameras come back later.

**Initial camera open failure stays fatal.** Only mid-stream
camera loss triggers the reopen loop. A camera that is never
present at startup almost always indicates a config / hardware
problem rather than a transient field failure.

**End-to-end shutdown order** is heartbeat → FFmpeg → session
(inside the per-attempt `finally`) → camera → client (outer
cleanup). The per-attempt cleanup stops the old session before
the next attempt starts so the streaming server never sees two
concurrent streamids for the same camera.

**Ctrl+C semantics.** Every wait point — frame loop, ingest
poll, inter-attempt backoff, camera-reopen backoff, camera
settle, validation window, release settle — is interruptible.
`KeyboardInterrupt` propagates through outer cleanup; the CLI
exits with code 0.

**Hidden test knobs on `run_agent`.**

- `max_attempts: int | None = None` — default unbounded retry.
  Tests use `max_attempts=1` to reproduce pre-M7 behavior (one
  attempt, any failure re-raised) or small values to exercise
  the "give up after N" path.
- `backoff: BackoffPolicy | None = None` — tests pass a
  zero-delay policy so recovery never sleeps on wall clock.
- `ingest_timeout_s`, `ingest_poll_interval_s` — tunable
  bounds for the ingest wait. Tests use tiny values; production
  defaults to 15 s / 0.5 s.
- `camera_settle_s`, `camera_validation_window_s`,
  `camera_validation_frames`, `camera_read_retry_s`,
  `camera_release_settle_s`, `camera_unavailable_hint_after` —
  tunable bounds for the camera reopen path.

### Heartbeat status mapping

The heartbeat reporter is **per-session**. Each `_run_attempt`
builds its own reporter and stops it in the attempt cleanup.
The reporter is not kept alive across the reconnect gap; no
heartbeats are sent while the supervisor is between attempts.
The wire `HeartbeatStatus` is simply `streaming if
stats.snapshot().ffmpeg_running else error` — the
`reconnecting` literal exists in the API schema but the agent
does not have a code path that emits it (would require a
session-independent reporter, which is out of scope for M7).

## Honest gaps and planned work (M8 candidates)

A May 2026 audit cross-checked the design docs against the live code
and surfaced four items that are referenced in the contract or config
but **not yet implemented**. They are gathered here so the next
milestone has a single source of truth for what's actually left to
build:

1. **Frame buffer.** `StreamConfig.buffer_ms` is parsed and validated
   by Pydantic (default `500`, range `>=0`) and surfaced in CLI status
   output, but no code path consumes it. There is no `buffer/`
   subpackage. Frames produced during recovery backoff are dropped
   (the M7 summary calls this out). The planned shape is a bounded
   ring buffer between `source.read_frame()` and
   `ffmpeg.write_frame()` with a drain-on-reconnect policy.

2. **Proactive session refresh from `expires_at`.** Both the top-level
   and nested `expires_at` fields on `StreamSessionResponse` are
   received and Pydantic-validated, but the supervisor never reads
   them. Today the agent only notices session expiry reactively, when
   FFmpeg's stdin write fails — a 5–30 s detection delay. The planned
   shape is an outer-loop timer that requests a fresh session ahead
   of `expires_at` and swaps it in without tearing down camera or
   encoder.

3. **Token rotation.** `docs/api-contracts.md` describes a heartbeat
   response that can signal a token refresh, but
   `upstream_agent/api/client.py` and `heartbeat/reporter.py` do not
   parse such a signal, and there is no atomic `~/.upstream/config.yaml`
   rewrite path. The planned shape is: (a) handle a rotation flag on
   the heartbeat response, (b) validate the new token via a
   round-trip, then (c) atomic-rename the config file in place.

4. **Session-independent heartbeat reporter (for `"reconnecting"`).**
   `HeartbeatStatus` includes the literal `"reconnecting"` in the
   schema, but the reporter is built and torn down inside each
   `_run_attempt`, so during the backoff sleep no heartbeat is sent
   at all. The dashboard sees `streaming` → `error` → `streaming`
   with no in-between signal. The planned shape is to lift the
   reporter into the outer recovery loop so it can stay alive
   between attempts and emit `"reconnecting"` during the gap.

None of these are required for the current v0.1.0 functionality
(M1–M7 are complete and tested), but they are the natural next set
of improvements to streaming continuity and contract honesty.

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

## Interactive mode + server-controlled config (smart flow)

Bare `upstream` opens `upstream_agent/interactive/shell.py::InteractiveShell`,
a line-oriented REPL. Module map:

- `interactive/shell.py` — REPL: `login` (hidden token prompt, verified
  against `GET /v1/agents/bootstrap` before `config.save_api_token`
  persists it), `status`/`account`/`limits`/`cameras`/`streams` rendered
  from the cached `BootstrapResponse`, `start`/`stop`, `logs`,
  `refresh`. Disconnects flip a "disconnected" flag; `refresh` retries;
  workers keep their own recovery loops.
- `interactive/worker.py` — `StreamWorker`: one `run_agent` call on a
  daemon thread, stopped via the supervisor's external `shutdown` Event
  (new optional `run_agent(..., shutdown=…)` parameter; standalone
  `upstream run` is unchanged). Supervisor `on_event` messages are
  logged — not printed — so they land in the ring buffer and the file.
  `WorkerRegistry` is name-keyed and N-ready (multi-camera is
  upstream-agent#2).
- `logging_setup.py::RingBufferHandler` + `setup_logging(console=False,
  extra_handlers=…)` — interactive mode disables the stderr handler so
  logs never scroll over the prompt; the redaction filter applies to the
  ring buffer like every other handler.
- `preflight.py` — local pre-check after bootstrap: clamps requested
  width/height/fps/bitrate to account limits (`limits_precheck` event),
  raises `PreflightError` for disallowed codec/source-type. Convenience
  only; the server re-validates everything.
- `server_config.py` — maps a server camera record (`device_path`
  convention `webcam:<index>` / `rtsp:<url>` + `default_*`) onto the
  local `CameraConfig`, so dashboard-configured cameras stream without a
  local YAML camera section. The local `camera:` section remains as a
  fallback when the account has no server cameras, and `upstream run` /
  `upstream check` still use it directly.

Start flow for a dashboard-created camera: ensure agent registered →
`POST /v1/cameras/{id}/claim` → build `CameraConfig` from the server
record → spawn `StreamWorker` → prompt returns immediately. Server
rejections (`limit_exceeded.*`) render in the shell, mark the worker
`error`, and fire one blocked-by-limits heartbeat.
