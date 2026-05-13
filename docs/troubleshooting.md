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

## Installation

### `pip install -e .` fails on Python 3.9 or older

The package requires Python 3.10 or newer. Use `pyenv`, `uv`, or your
system package manager to install a supported version.
