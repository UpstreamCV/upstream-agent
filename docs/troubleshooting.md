# Troubleshooting

Common problems and what to check first. This page grows over time; add
new entries as soon as the same support question comes up twice.

## CLI

### `upstream init` says the file already exists

The default config path (`~/.upstream/config.yaml`) is created on first
run and is not overwritten by subsequent `init` calls. To rewrite the
template — discarding any local edits — pass `--force`:

```
upstream init --force
```

### `upstream status` reports the token as `****`

That is expected. The CLI never prints API tokens in full. The last
four characters are shown as a non-secret hint (`****abcd`) so you can
distinguish multiple tokens without exposing them.

### `upstream status` says "placeholder — set your real token"

You have run `init` but have not edited the file yet. Open
`~/.upstream/config.yaml` and replace `YOUR_API_TOKEN_HERE` with your
real token. Other commands that talk to the API will refuse to run
while the placeholder is still in place.

### `upstream` exits with `Error: Config file not found`

The config has not been created yet. Run `upstream init` to create one
at the default path, or pass an explicit `--config /path/to/file.yaml`.

## State

### `agent_state.json` was renamed to `agent_state.json.corrupt-<ts>`

The state file was unparseable JSON or did not match the expected
schema. The agent renamed it (preserving the original contents for
inspection) and generated a fresh state with a new `device_id`. If you
need to recover a prior `agent_id`, look inside the `.corrupt-<ts>`
file. After recovering what you need, you can delete it.

## Logs

### Where are the logs?

`~/.upstream/logs/agent.log`, with up to three rotated backups
(`.1`, `.2`, `.3`) of 5 MB each.

### My API token appears in the log

It should not. If you see a full token in the log file, treat this as a
bug — open an issue with the redacted log snippet (replace the token
with `****` before pasting). Common ways tokens leak that we want to
catch in the filter:

* Bearer headers in HTTP error traces.
* Pydantic validation errors that echo the input value.
* Third-party libraries that log request payloads at debug level.

## Camera

### `upstream check --camera` says `Could not open webcam at index 0`

`cv2.VideoCapture.isOpened()` returned False. Walk through these
checks in order:

1. **Is the camera connected?** For an external USB webcam, unplug
   and replug it, then re-run the check.
2. **Is another application using it?** macOS Photo Booth, Zoom,
   Teams, OBS, browser tabs with `getUserMedia`, and most video chat
   apps hold an exclusive lock. Close those and try again.
3. **Is the source index right?** `source: 0` is the first camera the
   OS enumerates. Built-in laptop webcams are usually `0`; an external
   plugged in afterwards may be `1` or higher. Edit
   `~/.upstream/config.yaml` and try `source: 1`, then `source: 2`.
4. **macOS only — has the terminal been granted Camera permission?**
   System Settings → Privacy & Security → Camera, then enable Terminal
   (or iTerm, VS Code, etc., whatever you launched `upstream` from).
   The first time you run the check, macOS may prompt; if you
   dismissed it, the toggle is the only way to restore access.
5. **Linux only — is the user in the `video` group?** Run
   `groups` and look for `video`. If missing,
   `sudo usermod -aG video $USER` and log out / back in.

### Camera in use by another app

Same fix list as above, step 2. The agent does not force a takeover —
if another process owns the device, the check fails immediately rather
than racing for it. Quit the other app and re-run.

### Permission denied

* **macOS**: System Settings → Privacy & Security → Camera. Toggle on
  for the terminal app you used to launch `upstream`. If you launched
  via a custom shell or IDE, that specific app needs the permission —
  permission is per-process-bundle, not user-wide.
* **Linux (V4L2)**: `ls -l /dev/video0` and confirm group read/write
  bits and group ownership of `video`. Add yourself to that group with
  `sudo usermod -aG video $USER`, then log out and back in.
* **Windows**: Settings → Privacy & security → Camera → "Let apps
  access your camera." Then look further down for desktop-app access.

### Wrong camera index

OpenCV's `VideoCapture(N)` takes a zero-based index into the
OS-enumerated cameras. The mapping is not stable across reboots on
some Linux setups. If `source: 0` opens the wrong device, try `1`,
`2`, etc. There is no built-in enumeration command in this milestone
— a quick way to scan is to try indices 0 through 3, one at a time.

### `upstream check --camera` prints `FPS: unknown`

OpenCV reports `0.0` for FPS when the driver does not provide a frame
rate. The CLI renders that as `unknown`. This is usually fine — the
camera will still produce frames; the driver just refuses to declare a
nominal rate. Streaming-side code will measure the actual rate when we
get to that milestone.

### Frame read failed

The camera was opened but `cv2.VideoCapture.read()` returned `(False,
None)`. Common causes: the device was disconnected after `open()`, the
USB hub went into power-save, or the camera produced a malformed
frame the driver dropped. Re-run the check; if it fails repeatedly,
try a different USB port (avoiding hubs) and a different cable.

### `Error: OpenCV (opencv-python) is required for webcam capture`

OpenCV is part of the agent's runtime dependency set, so this should
not happen after a normal `pip install upstream-agent`. If you see it,
re-run the install:

```
pip install --upgrade --force-reinstall opencv-python
```

If pip refuses, check that you have a working compiler/wheel index for
your platform; on Apple Silicon you generally want Python 3.10+ from
Homebrew or `python.org`, not the system Python.

## API check

### `upstream check --api` says `Config file not found`

Same as the camera check — run `upstream init` first, edit the file
to point at your token, then re-run.

### `upstream check --api` says `API token is still a placeholder`

The check refuses to run with the default `YOUR_API_TOKEN_HERE`
value. Open `~/.upstream/config.yaml` and replace `api.token` with
the real token your team gave you. `upstream check --camera` works
under the placeholder; `--api` does not (it would just hit a 401).

### `Error (register_agent): Could not connect to API server at ...`

The agent could not reach `api.base_url`. Walk these in order:

1. Is the URL right? Double-check `api.base_url` in
   `~/.upstream/config.yaml`. The dev server is
   `http://api.upstreamcv.com`.
2. Are you online? `curl <base_url>/healthz` or whatever the team's
   liveness endpoint is.
3. Are you behind a proxy? Set `HTTPS_PROXY` / `HTTP_PROXY` in your
   shell and re-run. httpx respects the standard env vars.
4. Is DNS working? `nslookup api.upstreamcv.com`.

### `Error (register_agent): ... code=invalid_token`

The server returned `401` with `error.code = invalid_token`. The
token in `~/.upstream/config.yaml` is wrong — typo, trailing
newline, or the placeholder is still in place. Re-paste the token
and try again. Tokens look like `ups_<env>_<8-char id>_<22-char
secret>`.

### `Error (register_agent): ... code=token_revoked`

The token was valid but has been revoked server-side. Generate a new
one from the dashboard and rewrite `api.token` in
`~/.upstream/config.yaml`. The agent does not (yet) auto-rotate
tokens.

### `Error (register_agent): ... code=token_expired`

The token's expiry has passed. Same fix as `token_revoked`:
generate a fresh token and edit `api.token`. The agent raises
`ApiTokenExpiredError` for this case; it's still surfaced as an
auth failure in the CLI.

### `Error (register_camera): ... code=limit_exceeded.max_cameras`

Your account has hit its camera-slot limit. The
`details` field on the server response contains `in_use` and `max`
counts; the CLI shows the `request_id` so you can share it with
support. Either delete an unused camera in the dashboard or
upgrade the plan.

### `Error (register_camera): ... code=agent_not_found`

Unusual — the agent registered moments earlier. Most likely the
account_id changed or the agent was deleted server-side between
calls. Delete `~/.upstream/agent_state.json` and re-run; the agent
will register fresh.

### `Error (register_camera): ... code=agent_account_mismatch`

The `agent_id` in `~/.upstream/agent_state.json` belongs to a
different account than the current `api.token`. This happens when
the token is swapped without clearing local state. Delete
`agent_state.json` and re-run, or switch back to the original
token.

### `Error (start_stream_session): ... code=camera_not_found`

The `last_camera_id` in local state points at a camera the server
no longer knows about (deleted from the dashboard, or
account-swapped). Delete the `last_camera_id` field from
`~/.upstream/agent_state.json` (leave `agent_id` and `device_id`
intact) and re-run.

### `Error (start_stream_session): ... code=camera_already_streaming`

Another session for this camera is currently active. Wait for it
to finish or end it from the dashboard. The agent does not yet
attempt to take over an existing session.

### `Error (start_stream_session): ... code=limit_exceeded.max_concurrent_sessions`

Account-wide concurrency cap. End an existing session in the
dashboard or upgrade the plan.

### `Error (...): API response from ... did not match expected shape`

The agent reached the server but the response was missing or
malformed for a required field. With the contract now confirmed
this should be rare; if it happens, compare the response against
`docs/api-contracts.md` and either:

- Update `upstream_agent/api/schemas.py` to match what the backend
  actually returns, **or**
- File an issue with the API team if the response shape regressed.

The full response body is intentionally **not** included in this
error message to avoid leaking session tokens / SRT URLs.

### Where do I find the `request_id`?

Every `ApiError` message now includes `request_id=<id>` when the
server provided one. Share that string with support — it's the
fastest way to find the matching server-side log entry.

### `Warning: failed to stop stream session after check: ...`

`upstream check --api` opens a stream session, confirms approval,
and then calls `stop_stream_session` to release the camera slot.
If that final cleanup-stop fails, the check **still exits 0** —
registration and session approval already proved the path works —
but you'll see this warning on stderr.

What it means in practice: a session may stay active on the server
until it expires. A second back-to-back `upstream check --api` may
hit `camera_already_streaming` or
`limit_exceeded.max_concurrent_sessions` until the orphan session
clears. Two ways out:

1. Wait for the server's session expiry (the `expires_at` field on
   the start response — usually minutes-to-hours depending on plan).
2. End the session manually from the dashboard.

If you see this warning *repeatedly* on every run, the stop endpoint
is failing for a real reason — check the `code=` and `request_id=`
in the warning message and share them with support.

### Running `upstream check --api` back-to-back

With M3.3's cleanup-stop in place, running `upstream check --api`
twice in a row should succeed both times. The first run registers
the agent + camera, opens a session, prints "SRT URL received: yes",
and then stops the session. The second run uses the same (idempotent)
agent + camera and opens a fresh session. If the second run fails
with `camera_already_streaming`, check for the stop-warning entry
above.

### `Error (register_camera): ...`

The agent registered successfully but the camera was rejected.
Common causes:

- **`name` not unique on this agent** — change `camera.name` in
  config.
- **`source_type: "rtsp"`** — not supported in M3. Use `webcam`.
- **`requested_*` values out of range** — the server may cap
  resolution or FPS per plan.

The `agent_id` was still saved to `~/.upstream/agent_state.json` so
the next attempt won't re-register the agent.

### `Error (start_stream_session): ...`

The agent and camera are registered, but the server refused to
approve a session. This usually means a plan/quota limit, server
maintenance, or an internal error. Wait a minute and retry; if it
persists, check the dashboard.

### `Error (...): API response from POST ... did not match expected shape`

The agent reached the server but the response was missing a field
the client expected. The backend contract is still being finalised,
so this is the most likely error during early integration. Compare
the response against `docs/api-contracts.md` and either:

- Update `upstream_agent/api/schemas.py` to match what the backend
  actually returns, **or**
- File an issue with the API team if the response shape is wrong.

The full response body is intentionally **not** included in this
error message to avoid leaking session tokens / SRT URLs. Inspect
the raw response with a tool like `curl` or `httpie` during
development, never paste it into a bug report verbatim.

### `Error (...): API rate limit exceeded`

The server returned 429. Wait a bit. The agent does not retry on its
own in M3; reconnect/backoff lands in a later milestone.

### My API token appears somewhere

It should not. If you see the full token in stdout, stderr, the log
file, or any error message, that is a bug — please report it. The
token redaction filter is supposed to scrub it everywhere. Common
near-misses we already test for: error messages from the API client,
log records emitted by httpx when debug logging is on, and config
display in `upstream status`.

## Streaming preflight (`upstream check --streaming`)

### `Error (ffmpeg): FFmpeg is not installed or not on PATH.`

The agent looks for `ffmpeg` on `PATH`. Install it (the agent does
not install it for you):

- **macOS**: `brew install ffmpeg` (verify that the build includes
  `libsrt` and `libx264` — see the next entry).
- **Linux**: `apt install ffmpeg` (Debian/Ubuntu) or your distro's
  equivalent. SRT support depends on how the distro built the
  package.
- **Windows**: a static FFmpeg build from a trusted source. Make
  sure `ffmpeg.exe` is on `PATH`.

Verify by running `ffmpeg -version` in the same shell where you'll
run `upstream`.

### `Error (srt_support): FFmpeg is installed, but this build does not support SRT.`

Your `ffmpeg` binary was compiled without `libsrt`. The agent can
detect this — `ffmpeg -protocols | grep -w srt` returns nothing
(only `srtp`, which is unrelated). Reinstall with libsrt enabled:

- **macOS (Homebrew)**: the default `brew install ffmpeg` formula
  does **not** ship with libsrt on most bottles. Install the
  `ffmpeg-full` formula instead:

  ```bash
  brew uninstall ffmpeg
  brew install ffmpeg-full
  ```

  After reinstall, confirm with `ffmpeg -protocols | grep -w srt` —
  you should see a bare `srt` line (not just `srtp`). Then re-run
  `upstream check --streaming`.
- **Linux**: install the version that includes `libsrt`, or compile
  from source with `--enable-libsrt`.
- **Windows**: pick a static build advertised as including libsrt.

### `Error (h264_encoder): FFmpeg is installed but no usable H264 encoder is available.`

The build does not include `libx264` and no platform H264 encoder
(`h264_videotoolbox` on macOS, `h264_nvenc` on NVIDIA, etc.) is
available either. Install an FFmpeg build with libx264.

### `Error (start_stream_session): ...` during `--streaming`

The local FFmpeg/SRT/H264 phases passed but the API rejected the
session-start. This is the same error you'd see from
`upstream check --api`; the troubleshooting under "API" applies.

### `Error (srt_url_parse): Approved SRT URL is invalid: ...`

The server returned an SRT URL that fails parsing (wrong scheme,
missing host, missing port). The agent does **not** echo the URL in
the error message. If this fires, the backend probably regressed —
share the `request_id` from the matching API error (if any) with the
backend team. The agent still stops the temporary session before
exiting, so the camera slot is released.

### `Error (command_build): ...`

The approved config the server returned is something the M4 command
builder cannot encode (e.g. a codec other than H264). The most
common cause is a server-side update that returned a new codec
without bumping the client. The agent stops the temporary session
before exiting.

### `Warning: failed to stop stream session after check`

Mirrors the same warning under `--api`. The check itself succeeded;
the server-side session will expire on its own. If this fires
consistently, look at the server logs for the `stop_stream_session`
endpoint.

### Streaming check passes but `upstream` cannot actually stream yet

That is expected as of Milestone 4. `upstream check --streaming`
verifies every prerequisite for streaming but does not start a
long-running FFmpeg process or push any frames. Live streaming
lands in Milestone 5.

## Live streaming (`upstream run`)

`upstream run` is the first command that actually streams video. It
opens the camera, requests a stream session, launches FFmpeg, pipes
BGR frames over SRT, and stops everything cleanly on `Ctrl+C` (or
on any error).

### `Error: FFmpeg binary not found.` or `FFmpeg is not installed or not on PATH.`

The `upstream check --streaming` troubleshooting entries above apply
verbatim. The runtime probes FFmpeg before opening the camera or
contacting the API, so a missing/unsupported binary fails fast
without side effects.

### `Error (register_agent|register_camera|start_stream_session): ...`

The API rejected the run before any FFmpeg subprocess was started.
Same diagnostics as `upstream check --api`: confirm your token,
verify the camera config is valid, and check `request_id` in the
error message with the backend team.

### `Error: FFmpeg pipe closed; exit_code=...`

FFmpeg exited while the agent was still piping frames. This is the
runtime's `FfmpegWriteError`. Common causes:

- The SRT server (MediaMTX) refused or dropped the connection.
- A network blip closed the underlying TCP/UDT link.
- FFmpeg's stderr (if anything was emitted that didn't contain
  `srt://`) is included as a short scrubbed snippet — that's the
  best hint.

The error message contains the FFmpeg exit code but never echoes
the SRT URL.

### `Error: FFmpeg exited mid-stream with code ...`

Same root cause as the previous entry, detected by the supervisor's
post-write `poll()`. The supervisor stops the camera and the API
session before propagating.

### `Error: Camera was opened but failed to read a frame.`

The webcam was disconnected or stopped producing data mid-stream.
The supervisor stops FFmpeg, releases the camera, and stops the
API session before propagating. Reconnecting the camera and re-
running is the fix.

### Stream session won't end after Ctrl+C

The supervisor's cleanup tries to stop the session best-effort. If
the server is unreachable at shutdown, the agent prints
`Warning: failed to stop stream session: ...` and exits 0 anyway.
The server-side session will expire on its own.

### `upstream run` ends immediately with "Stream session stopped" and no `Streaming started`

The SRT URL returned by `/v1/stream-sessions/start` points at a
host or port the agent cannot reach — the underlying SRT push
fails before any frame is written, and FFmpeg exits quickly. Check
that the streaming server is reachable from this machine and that
`upstream-api-server` is configured with the correct ingest
host/port for this environment. The agent now uses only the
API-issued SRT URL; the previous temporary local-dev SRT URL
override was removed after real API-issued streaming was verified
end-to-end.

## Heartbeat / dashboard offline (`upstream run`)

While `upstream run` is active, a background thread POSTs a
heartbeat to `POST /v1/heartbeats` every 5 seconds. The dashboard
uses those to mark the camera/session online and surface basic
runtime telemetry (fps, bitrate, resolution, last error).

### Dashboard shows the camera offline but `upstream run` is still streaming

Check `~/.upstream/logs/agent.log` for `Heartbeat send failed
(transient): ...` lines. Common causes:

- **API unreachable from the agent host** — network blip, DNS,
  proxy. The agent will keep retrying on the next 5-second tick.
  Run `upstream check --api` in a second terminal to confirm
  baseline connectivity.
- **API server slow / 5xx** — the warning carries the `code=` and
  `request_id=` from the server response; share those with
  support.
- **Clock skew** — heartbeat `reported_at` is in UTC and the
  server response includes its own `server_time`; large skew can
  confuse the dashboard's "online" window.

The stream itself is not affected — heartbeat failures are
non-fatal by default.

### `upstream run` exits with `exit_reason=fatal_heartbeat`

The heartbeat endpoint returned a clearly-fatal error and the
runtime shut down cleanly. The CLI log line above the exit will
identify which:

- `ApiTokenRevokedError` — token was revoked server-side.
  Generate a new token from the dashboard and rewrite
  `api.token` in `~/.upstream/config.yaml`.
- `ApiTokenExpiredError` — token expired. Same fix.
- `ApiValidationError` — usually `403 forbidden_resource` (the
  `camera_id` or `session_id` in the heartbeat is not owned by
  this agent, e.g. the camera was deleted from the dashboard
  mid-stream) or `422 validation_error` (agent bug — please
  report). Re-running after the token / camera issue is fixed
  picks up cleanly.

### Heartbeat send succeeds but the dashboard still shows offline

Confirm the `agent_id` query parameter matches the registered
agent by inspecting recent heartbeats via the API directly:

```
curl -H "Authorization: Bearer <token>" \
  https://api.upstreamcv.com/v1/agents/<agent_id>/heartbeats?limit=5
```

A non-empty list with `reported_at` advancing every ~5 seconds
means the agent side is working — the dashboard's stale-time
window may simply not have refreshed yet, or its threshold for
"online" is longer than 5 seconds.

### `agent_id missing from state; heartbeat reporter not started.`

Defensive log line emitted when `state.agent_id` is `None` at the
point the supervisor is about to start the heartbeat thread. In
normal operation the supervisor's `_register_if_needed` helper
sets `agent_id` during registration; if you see this, your local
`~/.upstream/agent_state.json` may have been deleted between
registration and the streaming phase. Delete the state file
entirely and re-run — the agent will register fresh.

## Reconnect / reliability (Milestone 7)

The supervisor recovers from transient failures automatically.
The agent never invents an SRT URL — every retry asks the API
for a fresh stream session, and the previous session's `streamid`
is stopped before the next one is requested.

### Camera unplug / reconnect

Pull the USB cable mid-stream (or `lsof | grep video0` and force
the device away). The agent emits, in order:

```
Recoverable failure; cleanup complete, will retry. CameraReadError: ...
Camera disconnected
Waiting 1.0s before camera reopen attempt #1
Reopening camera (attempt #1)
Camera reopen attempt #1 failed (open): CameraOpenError: ...
Waiting 2.0s before camera reopen attempt #2
Reopening camera (attempt #2)
Camera opened; waiting 1.5s for device to settle
Camera validation read failed (attempt #2, read #1): ... retrying within window
Camera validation timed out after 5.0s (attempt #2, 0/3 good, ...); will retry reopen
... (more attempts; backoff grows; the helper does not reset between cycles)
Camera opened; waiting 1.5s for device to settle
Camera recovery validated after 3 good frame(s)
API session approved
FFmpeg started
FFmpeg started; waiting for API to report session streaming
API reports session streaming (MediaMTX confirmed initial ingest). Dashboard playback is not directly verified.
Heartbeat reporting started
Streaming started (frames flowing to streaming server).
```

**Camera recovery has three gates:** open + settle + windowed
validation.

1. **Open** — `VideoCapture.open()` must succeed. Open failures
   surface as `Camera reopen attempt #N failed (open): ...`.
2. **Settle** — `_CAMERA_RECOVERY_SETTLE_S` (default **1.5 s**)
   after open. Some USB / Continuity / V4L2 cameras need this
   to start producing frames.
3. **Windowed validation** — collect
   `_CAMERA_RECOVERY_VALIDATION_FRAMES` (default **3**)
   consecutive successful reads inside
   `_CAMERA_RECOVERY_VALIDATION_WINDOW_S` (default **5 s**).
   Failed reads inside the window reset the streak but are
   logged as `camera_validation_read_failed` rather than
   failing the whole attempt. Window timeout surfaces as
   `camera_validation_timeout`.

Only after all three gates pass does the supervisor proceed
(`camera_validated`). Backoff is shared across reopen attempts
and **does not reset** between cycles. Every wait point is
interruptible by Ctrl+C.

### `camera_unavailable_hint` — macOS Continuity Camera limitation

After three consecutive failed reopen cycles, the supervisor
emits a single `camera_unavailable_hint` event:

```
camera_unavailable_hint: Camera device has opened but failed to
produce frames for 3 consecutive recovery cycles. On macOS /
Continuity Camera this often requires restarting the agent. The
agent will keep retrying.
```

**Why.** macOS / AVFoundation caches device handles per process.
When an iPhone Continuity Camera leaves range, or any
AVFoundation-backed device disappears, `cv2.VideoCapture(N)`
inside the running Python process can still report
`isOpened() == True`, but every `read()` returns `(False, None)`
forever. The agent already builds a fresh `WebcamSource` and
fresh `cv2.VideoCapture` (with explicit `CAP_AVFOUNDATION` on
macOS) per attempt — there is nothing left to flush from
inside the process. **The honest fix is `Ctrl+C` and re-run.**

The agent does keep retrying after the hint — some USB cameras
come back later — so if you can replug a real USB webcam, you
do not need to restart the agent.

### Remote ingest confirmation (`waiting_for_ingest`)

After FFmpeg starts, the agent polls
`GET /v1/stream-sessions/{id}` and waits for `state ==
"streaming"`. The event sequence:

```
FFmpeg started
FFmpeg started; waiting for API to report session streaming
... (the agent pumps frames into FFmpeg while polling every 0.5 s)
API reports session streaming (MediaMTX confirmed initial ingest).
Dashboard playback is not directly verified.
Heartbeat reporting started
Streaming started (frames flowing to streaming server).
```

Timeout is `_INGEST_CONFIRM_TIMEOUT_S` (default **15 s**); poll
cadence is `_INGEST_POLL_INTERVAL_S` (default **0.5 s**). The
polling helper writes frames into FFmpeg while it waits because
MediaMTX needs an active SRT publish before its `runOnReady`
webhook fires.

If the wait times out, the supervisor classifies it as a
recoverable stream failure (`remote_ingest_timeout`), tears the
attempt down, and asks for a fresh session. A persistent
timeout typically means MediaMTX is rejecting the SRT publish;
check the streaming server's logs for the failing `streamid`.

### Why dashboard playback may not match agent logs

The agent's strongest local signal is "API reports session
streaming," which fires when MediaMTX accepts the *initial* SRT
publish (its `runOnReady` webhook posts to the API). After
that, **the agent has no continuous remote-playback signal:**

- The API's `state` field is sticky. If the streaming server's
  `runOnNotReady` webhook is missed (sudden network drop,
  webhook call itself fails, MediaMTX restart), the state can
  remain `"streaming"` even though the SRT publish is dead.
  The dashboard reads the same field, so it sees the same
  staleness.
- The agent eventually notices via an FFmpeg write/exit error,
  which can take 5–30 s depending on the kernel's SRT-socket
  timeout. Until then, agent logs and dashboard state will
  disagree with reality.
- No API surface today lets the agent verify continuous remote
  playback. The supervisor's job is to (1) start cleanly, (2)
  confirm initial ingest, (3) detect failures reactively and
  retry. The dashboard is a separate trust boundary.

In short: an "API reports session streaming" log line proves
initial MediaMTX ingest, not ongoing dashboard visibility.
That's the most the local process can know.

### `Recoverable recoverable_stream during streaming attempt: FfmpegExitError`

FFmpeg exited unexpectedly while frames were flowing — usually
the streaming server dropped the SRT connection or the local
FFmpeg ran into a transcode error. The supervisor stops the old
session, requests a new one, and restarts FFmpeg. If the same
failure keeps firing (visible in the logs as repeated
`recoverable_stream` lines), check the streaming server's logs
for the matching `streamid` rejection reason — the agent can't
diagnose server-side rules.

### `Recoverable recoverable_stream during streaming attempt: FfmpegWriteError`

The local stdin pipe to FFmpeg broke — almost always because
FFmpeg itself exited a moment before the write. Same recovery
path as the exit case above; the supervisor scrubs the snippet
of FFmpeg stderr included in the message, so no SRT URL ever
appears in the event or log.

### Network drop mid-stream

A short Wi-Fi blip surfaces as transient warnings from the
heartbeat thread (`Heartbeat send failed (transient): ...`) and
eventually as a `FfmpegExitError` once FFmpeg's SRT socket
times out. The recovery loop backs off and retries. The
heartbeat thread doesn't have its own backoff — repeated
transient failures just keep logging warnings every 5 s. If the
network is down for hours the agent keeps retrying forever; hit
Ctrl+C to stop.

### `Heartbeat send failed (transient): ...` repeats indefinitely

The heartbeat reporter is intentionally noisy on transient
failures. M7 did not add backoff to the reporter — it stays
loud so operators can see the issue. If FFmpeg is still
healthy, the stream itself is unaffected; the dashboard simply
goes offline until the heartbeat path recovers. If you see
this for hours, check the API server / DNS, then restart the
agent.

### `Heartbeat reported fatal error: ApiTokenRevokedError`

The API revoked the agent's token mid-stream. The recovery
loop **does not** retry — the agent stops cleanly with
`exit_reason=fatal_heartbeat`. Rotate the token in
`~/.upstream/config.yaml` and re-run.

### Session restart between attempts

Each recovery attempt requests a brand-new stream session. The
agent caches `agent_id` and `last_camera_id` in
`~/.upstream/agent_state.json` so it only registers once;
subsequent attempts call `POST /v1/stream-sessions/start`
directly. The previous attempt's `session_id` is stopped via
`POST /v1/stream-sessions/{id}/stop` before the new one is
requested. If the stop call fails, you'll see a
`session_stop_warning` event; the next start attempt may then
hit `camera_already_streaming` (422) which the supervisor
classifies as fatal (validation errors don't retry — see
`docs/api-contracts.md`). Wait out the server-side session
timeout and re-run.

### Ctrl+C during reconnect / backoff sleep

Ctrl+C is interrupt-able at every wait point:

- During the frame loop: the in-flight syscall is interrupted,
  attempt cleanup runs, outer cleanup runs, `KeyboardInterrupt`
  propagates to the CLI which exits 0.
- During the inter-attempt backoff sleep: `BackoffPolicy.sleep`
  uses `threading.Event.wait` which raises `KeyboardInterrupt`
  in the main thread. Same cleanup chain.
- During the camera reopen backoff: identical path.

You should never see the agent hang in a 30 s sleep after
Ctrl+C. If you do, capture a stack trace
(`py-spy dump --pid <pid>`) and file an issue — that would be
a bug.

## Installation

### `pip install -e .` fails on Python 3.9 or older

The package requires Python 3.10 or newer. Use `pyenv`, `uv`, or your
system package manager to install a supported version.
