# Milestone 7 — Reconnect & failure handling

A condensed summary suitable for pasting into a design doc. For the
full chronology and per-patch commit notes, see `docs/commit-log.md`.

## Goal

Before M7, every transient failure was terminal: a one-second network
blip, a camera read glitch, or an `ApiServerError` from the heartbeat
path would crash `upstream run` with a traceback. M7 makes the agent
survive normal field failures (Wi-Fi drops, cameras unplugged, laptops
sleep, MediaMTX restarts) and recover automatically while staying
honest in the logs about what it can and cannot prove.

## What the agent can prove

- **Local FFmpeg startup** — the subprocess launched and is consuming
  frames on stdin.
- **API-reported initial ingest** — after FFmpeg starts the supervisor
  polls `GET /v1/stream-sessions/{session_id}` until `state` flips to
  `"streaming"`. That value is set when MediaMTX's `runOnReady`
  webhook fires for *this specific session*, so it proves the
  streaming server accepted the initial SRT publish.
- **Heartbeat acceptance** — the `POST /v1/heartbeats` returned 200.

## What the agent cannot prove

- **Ongoing dashboard playback.** Nothing on the API surface lets the
  agent verify that a viewer is currently rendering frames. The
  `state` field is sticky once set — if the streaming server's
  `runOnNotReady` webhook is missed (sudden network drop, webhook
  failure, MediaMTX restart) the row can remain `"streaming"` after
  the publish has died. The dashboard reads the same field and sees
  the same staleness.
- **MediaMTX's continued re-broadcasting.** No push channel from the
  streaming server exists; the agent only finds out via downstream
  FFmpeg/SRT errors with a 5–30 s kernel-socket-timeout window.

Every operator-facing log message is worded around these limits.
`"Streaming started (frames flowing to streaming server)."` —
**not** "dashboard is rendering." `"API reports session streaming
(MediaMTX confirmed initial ingest). Dashboard playback is not
directly verified."` — **not** "Remote ingest confirmed."

## Architecture

```
  probe FFmpeg ─── fatal if missing / SRT / H264 unavailable
  open camera ─── fatal on initial open failure
  open API client
  ┌──── recovery loop ──────────────────────────────────────────┐
  │  _run_attempt:                                              │
  │     register (first attempt only) → start_stream_session    │
  │       → build FFmpeg command → start FFmpeg                 │
  │       → poll GET /v1/stream-sessions/{id} for "streaming"   │
  │       → start HeartbeatReporter → frame loop                │
  │       → ATTEMPT CLEANUP (heartbeat → ffmpeg → session)      │
  │  on normal exit / fatal_heartbeat: break                    │
  │  on fatal classification: re-raise                          │
  │  on recoverable_stream: backoff.sleep, retry                │
  │  on recoverable_camera: close camera, reopen + validate,    │
  │     retry                                                   │
  └─────────────────────────────────────────────────────────────┘
  OUTER CLEANUP (camera → API client)
```

End-to-end cleanup order: **heartbeat → FFmpeg → session → camera →
client**. The per-attempt cleanup stops the old session before the
next one is requested so the streaming server never sees two
concurrent streamids for the same camera.

## Failure classification

Classification is by **exception type**, never by message string.

| Category | Triggers | Action |
| --- | --- | --- |
| Fatal | `ApiTokenRevokedError`, `ApiTokenExpiredError`, `ApiAuthError`, `ApiValidationError`, `FfmpegNotFoundError`, `FfmpegSrtUnsupportedError`, `FfmpegH264UnsupportedError`, `FfmpegStartError`, `CommandBuildError` | Run attempt cleanup, outer cleanup, re-raise |
| Recoverable stream | `FfmpegExitError`, `FfmpegWriteError`, `ApiConnectionError`, `ApiServerError`, `ApiRateLimitError`, `ApiContractError`, `SrtUrlError`, generic `StreamingRuntimeError` | Run attempt cleanup, backoff sleep, retry with a fresh session |
| Recoverable camera | `CameraReadError`, mid-stream `CameraOpenError` | Close camera, reopen with backoff + settle + windowed validation, then retry with a fresh session |

## Recovery primitives

### Bounded backoff (`upstream_agent/runtime/backoff.py`)

`BackoffPolicy` with 1 s initial delay, ×2 multiplier, 30 s cap,
±20 % multiplicative jitter. `sleep()` is built on
`threading.Event.wait` so a shutdown signal wakes the wait
immediately. Backoff is shared across reopen cycles and is **not
reset** after a "successful" camera reopen — flaky devices don't
tight-loop.

### Camera reopen (`_reopen_camera_with_backoff`)

Three gates: open + settle + windowed validation.

1. **Open** — fresh `WebcamSource` + fresh `cv2.VideoCapture` per
   attempt. On macOS the capture is built with explicit
   `cv2.CAP_AVFOUNDATION` for backend determinism.
2. **Settle** — `_CAMERA_RECOVERY_SETTLE_S` (default 1.5 s) after
   `open()` returns success.
3. **Windowed validation** — `_validate_camera_reads` collects
   `_CAMERA_RECOVERY_VALIDATION_FRAMES` (default 3) consecutive good
   reads inside `_CAMERA_RECOVERY_VALIDATION_WINDOW_S` (default
   5 s). Transient failures inside the window reset the streak but
   don't fail the whole attempt.

After window-timeout the helper closes the camera (with a 0.5 s
release-settle) and loops. After **3 consecutive failed reopen
cycles** the helper emits a single `camera_unavailable_hint` event
telling the operator to restart the agent — see the macOS Continuity
Camera limitation below. Recovery keeps trying after the hint.

### Remote ingest confirmation (`_wait_for_remote_ingest`)

After FFmpeg starts, the agent writes frames into FFmpeg while
polling the API for `state == "streaming"`. Cadence
`_INGEST_POLL_INTERVAL_S` (default 0.5 s); bound
`_INGEST_CONFIRM_TIMEOUT_S` (default 15 s). On confirmation, the
heartbeat reporter is started and the main frame loop begins.

Transient API errors during the poll (`ApiConnectionError`, 5xx,
429, contract errors) emit `ingest_poll_transient_error` and keep
polling within the timeout. Fatal auth/token/validation errors
propagate. Terminal states (`ended` / `expired` / `failed`) raise a
recoverable error so a fresh session is requested.

### Safe shutdown

Every wait point is interruptible: frame loop, ingest poll,
inter-attempt backoff, camera reopen backoff, settle, validation
window, release settle. `KeyboardInterrupt` propagates through outer
cleanup; the CLI exits with code 0. Cleanup never reuses an SRT URL
or session token across attempts.

## Heartbeat status

The reporter is session-scoped — torn down between recovery
attempts — so the wire `HeartbeatStatus` is simply `"streaming" if
ffmpeg_running else "error"`. The schema literal `"reconnecting"`
exists for forward compatibility but the agent does not emit it
today (would require a session-independent reporter).

## Files

```
upstream_agent/runtime/backoff.py     (new)
upstream_agent/runtime/stats.py       (M7: ffmpeg_running flag for heartbeat)
upstream_agent/runtime/__init__.py
upstream_agent/heartbeat/reporter.py  (session-scoped, simple status mapping)
upstream_agent/encoder/ffmpeg.py      (read_stderr_snippet for debug)
upstream_agent/sources/webcam.py      (CAP_AVFOUNDATION on macOS)
upstream_agent/supervisor.py          (recovery loop, classification,
                                       _run_attempt, _reopen_camera_with_backoff,
                                       _wait_for_remote_ingest)
upstream_agent/api/client.py          (get_stream_session method)
upstream_agent/api/schemas.py         (no shape change; Session.state already there)
```

API contract: **no new endpoints**. M7 reuses the existing
`POST /v1/stream-sessions/start`, `POST /v1/stream-sessions/{id}/stop`,
`POST /v1/heartbeats`, and adopts the already-shipped
`GET /v1/stream-sessions/{session_id}`.

YAML config surface: **unchanged**. All recovery timings are
module-level constants. Tests inject smaller values via hidden
`run_agent` kwargs to keep the suite wall-clock-free.

## Known limitations

1. **Dashboard playback cannot be verified by the agent.** The API
   has no continuous remote-ingest signal. `state == "streaming"`
   proves only initial MediaMTX ingest; a missed `runOnNotReady`
   webhook can leave the row stuck while the publish is dead. The
   agent eventually detects the failure via FFmpeg errors with a
   5–30 s delay (kernel SRT socket timeout). The dashboard reads
   the same `state` field, so agent and dashboard see the same
   staleness. Closing this gap requires server-side work (push
   channel from the streaming server, or a TTL/staleness sweep).
2. **macOS Continuity Camera disconnect generally requires a
   process restart.** AVFoundation caches device handles per
   process. After an iPhone Continuity Camera leaves range (or any
   AVFoundation-cached device disappears),
   `cv2.VideoCapture(N)` inside the running Python process can
   still report `isOpened() == True`, but every `read()` returns
   `(False, None)` forever. The agent already builds a fresh
   `WebcamSource` + `cv2.VideoCapture` per attempt and uses the
   explicit AVFoundation backend, so there is nothing left to
   flush in-process. The `camera_unavailable_hint` event tells the
   operator after 3 cycles. Recovery keeps trying — real USB
   webcams often come back later.
3. **Sudden network loss has a 5–30 s detection delay.** The
   recovery loop is reactive (it fires when FFmpeg writes fail or
   the heartbeat fails). The detection window is dominated by
   kernel SRT socket timeouts. There is no faster local signal
   without a push channel from the API or streaming server.
4. **`HeartbeatStatus = "reconnecting"` is not emitted** even
   though the schema accepts it. The reporter is per-session and
   torn down inside attempt cleanup. Emitting reconnecting
   heartbeats during the recovery gap would require a
   session-independent reporter; out of scope for M7.
5. **`camera_already_streaming` (422) is fatal.** If a previous
   session's `stop_stream_session` call failed and the agent
   restarts before the server times the old session out, the new
   `start_stream_session` will be rejected. Documented in
   `docs/troubleshooting.md` and `docs/api-contracts.md`.

## Future work (out of M7 scope)

- Proactive session refresh from `expires_at` (today the agent
  reacts to failures only).
- Frame buffering during reconnect gaps (frames produced during
  the gap are currently dropped).
- Session-independent heartbeat reporter so the dashboard could
  see a `reconnecting` status during the gap.
- Out-of-process camera recovery (supervisor restarts the worker
  process on macOS Continuity disconnect) — or accept a process
  restart as the documented fix.
- A push channel from MediaMTX / API for continuous remote-ingest
  health (currently no such surface exists).

## Test surface

`pytest -q`: 367 tests (was 307 at M6).

New test files:
- `tests/test_runtime_backoff.py` — backoff progression, jitter
  bounds, interruptible sleep.
- `tests/test_supervisor_recovery.py` — end-to-end recovery
  scenarios: FFmpeg exit, broken pipe, transient API connection
  error, fatal API auth error, camera reopen with settle +
  validation, Ctrl+C during backoff, `max_attempts` cap,
  ingest-confirmation happy/timeout/transient/fatal paths,
  `camera_unavailable_hint`, leak-guards.

Lint: `ruff check upstream_agent tests` reports 5 baseline
warnings unchanged from prior milestones (4 × B008 typer.Option
in cli.py defaults, 1 × I001 in `tests/test_token_redaction.py`).

## Hidden `run_agent` test knobs

For driving tests without wall-clock waits:

- `max_attempts: int | None = None` — `None` is unbounded retry;
  `1` reproduces pre-M7 fail-fast (any failure re-raised).
- `backoff: BackoffPolicy | None = None` — tests pass a zero-delay
  policy.
- `ingest_timeout_s`, `ingest_poll_interval_s` — bounds for the
  ingest wait.
- `camera_settle_s`, `camera_validation_window_s`,
  `camera_validation_frames`, `camera_read_retry_s`,
  `camera_release_settle_s`, `camera_unavailable_hint_after` —
  bounds for the camera reopen path.

None of these are exposed in YAML; they exist for tests and for
future tuning if real-world telemetry suggests different defaults.
