# upstream-agent

A public, pip-installable Python agent that runs on a customer or local
machine, reads a camera feed, asks the UpStream API for permission/config,
and streams video securely to the UpStream streaming server.

This README reflects the **current shape** of the project. The agent is
under active development. Today it can be configured locally, probe
a webcam (open, read one frame, report resolution and FPS, release),
probe the API (register the agent, register the camera, request
a stream session), run a streaming preflight (verify FFmpeg, SRT
support, H264, build the FFmpeg command Milestone 5 will execute),
and run the live streaming loop end-to-end (`upstream run`).
Milestone 5 is in **preview**: the runtime works under mocks/fakes
and against a hand-pointed MediaMTX via a temporary dev override,
but the real backend smoke is still gated on `upstream-api-server`
returning an SRT URL whose host/port reaches MediaMTX.

## Requirements

- Python 3.10 or newer.

## Install

For local development:

```bash
git clone https://github.com/UpstreamCV/upstream-agent.git
cd upstream-agent
pip install -e ".[dev]"
```

Once published to PyPI:

```bash
pip install upstream-agent
```

## Quick start

```bash
# 1. Create a default config at ~/.upstream/config.yaml
upstream init

# 2. Edit the file and replace YOUR_API_TOKEN_HERE with your real token.
#    DO NOT commit this file to any git repo.

# 3. View local agent and config status (token is always redacted).
upstream status

# 4. Verify the configured webcam is accessible.
upstream check --camera

# 5. Verify the API accepts this agent + camera and approves a stream.
upstream check --api

# 6. Verify this machine is ready to stream (FFmpeg, SRT, H264, command build).
upstream check --streaming

# 7. Stream video. Press Ctrl+C to stop.
upstream run
```

### Verifying camera access

`upstream check --camera` opens the configured camera, reads one
frame, prints the actual resolution and FPS, and releases the device.
It runs entirely locally — no API call, no video upload — so it works
even before you have a real API token. A placeholder token is fine.

Expected output on success:

```
Camera opened successfully
Resolution: 1280x720
FPS: 30
```

If the device negotiates different parameters than you asked for in
the config, a note is appended:

```
Note: requested 1920x1080 @ 60, device negotiated 1280x720 @ 30.
```

On failure the command exits non-zero with an actionable message,
e.g.:

```
Error: Could not open webcam at index 0. Check that the camera is
connected, not already in use, and that the source index in your
config is correct.
```

See `docs/troubleshooting.md` for the full list of camera diagnostics.

### Verifying API registration

`upstream check --api` walks the backend flow:

1. Register the agent (using your local `device_id` and the package
   version).
2. Register the camera (using the fields under `camera:` in your
   config).
3. Request a stream session and confirm the server approves it.
4. **Stop the session** — cleanup so a back-to-back check doesn't
   hit `camera_already_streaming`. The check is a diagnostic, not a
   streaming launch.

It stops there — no video is captured or transported. Requires a real
API token in `~/.upstream/config.yaml`; the placeholder is rejected
with a clear message before any HTTP request is made.

Running `upstream check --api` twice in a row should succeed both
times. If the cleanup-stop fails for some reason, you'll see a
`Warning: failed to stop stream session…` line on stderr but the
command still exits 0 — registration and session approval already
proved the path works.

Expected output on success:

```
API check successful
Agent registered: agent_123
Camera registered: cam_456
Stream session approved: sess_789
Approved config: h264, 1280x720 @ 30 FPS, 3000 kbps, balanced
SRT URL received: yes
```

The returned `agent_id` and `camera_id` are persisted in
`~/.upstream/agent_state.json` (partial progress is saved after each
successful step, so a retry doesn't re-register what's already
registered). The full SRT URL, the `streamid`, and the
`session_token` are **never** printed, logged, or persisted — only
the boolean "received: yes" shows up. The agent also does not send
your real machine hostname; it identifies itself with a stable
generated name like `upstream-agent-12345678` derived from the local
`device_id`.

On failure the command exits non-zero with a step-tagged error
that carries the server's `code` and `request_id`:

```
Error (register_agent): POST /v1/agents/register -> 401 Unauthorized, code=invalid_token, request_id=abc123: bad token
```

```
Error (register_camera): POST /v1/cameras/register -> 403 Forbidden, code=limit_exceeded.max_cameras, request_id=def456: Camera slot limit reached: 3 of 3.
```

Share the `request_id` with support — it's the quickest way to find
the matching server-side log entry.

You can run any combination of probes in one invocation:
`upstream check --camera --api --streaming`. They run in that fixed
order; each is independent.

### Verifying streaming readiness

`upstream check --streaming` is the local preflight for actually
streaming. It checks:

1. **FFmpeg** is installed and on `PATH`, reports its version.
2. The installed FFmpeg build **supports the SRT protocol** (`ffmpeg
   -protocols` lists `srt`, not just `srtp`).
3. FFmpeg has a usable **H264 encoder** — `libx264`,
   `h264_videotoolbox`, or another platform encoder.
4. The API approves a stream session and returns an SRT URL
   (re-uses the `upstream check --api` flow, then **stops** the
   session so the check is repeatable).
5. The SRT URL is syntactically valid (scheme, host, port).
6. The FFmpeg command that Milestone 5 will execute **builds**
   from the approved config without error.

It does **not** stream video. Milestone 5 will wire the actual
FFmpeg subprocess and SRT transport.

Expected output on success:

```
Streaming preflight successful
FFmpeg: found, version 8.1.1
SRT support: yes
H264 encoder: yes (libx264)
API stream approval: yes
Approved config: h264, 1280x720 @ 30 FPS, 3000 kbps, balanced
SRT URL received: yes
FFmpeg command build: yes
```

The full SRT URL, the `streamid`, the `session_token`, and the API
token are **never** printed or logged. Only the booleans appear.

#### Installing FFmpeg

The agent does not install FFmpeg automatically. Make sure your build
has both **libsrt** (for SRT transport) and **libx264** (or a
platform H264 encoder).

* **macOS** (Homebrew):

  ```bash
  brew install ffmpeg
  ```

  Note: not every Homebrew FFmpeg bottle ships with `libsrt`. If
  `upstream check --streaming` reports `srt_support` as missing,
  rebuild with `brew install ffmpeg --with-srt` or pull a static
  build that bundles libsrt.

* **Linux**: install through your package manager (e.g.
  `apt install ffmpeg`). SRT support depends on how your distro
  built the package; verify with `ffmpeg -protocols | grep -w srt`.

* **Windows**: install a binary build that includes libsrt and
  libx264 (the [official static builds](https://ffmpeg.org/download.html)
  are one option). Make sure `ffmpeg.exe` is on `PATH`.

On failure the command exits non-zero with a step-tagged error.
The most common ones:

```
Error (ffmpeg): FFmpeg is not installed or not on PATH.
Error (srt_support): FFmpeg is installed, but this build does not support SRT. Install an FFmpeg build with libsrt enabled.
Error (h264_encoder): FFmpeg is installed but no usable H264 encoder is available.
```

The agent also updates the `capabilities` it reports to the backend
on `register_agent`: ``{"ffmpeg": "<version>", "srt": <bool>}`` based
on the local probe. This applies to both `--api` and `--streaming`
runs.

### Streaming video (preview)

`upstream run` is the live streaming command. It opens the camera,
requests a stream session, starts FFmpeg, and pipes BGR frames over
SRT until you press `Ctrl+C`.

```bash
upstream run
```

Expected output:

```
Starting UpStream agent...
Camera opened: webcam 0
API session approved
Approved config: h264, 1280x720 @ 30 FPS, 3000 kbps, balanced
FFmpeg started
Streaming started
Press Ctrl+C to stop.
```

On shutdown (Ctrl+C or any error):

```
Stopping stream...
FFmpeg stopped
Camera released
Stream session stopped
```

The agent uses the **server-approved** resolution, FPS, bitrate, and
codec — not the local config values, which are only sent as a
preference. If the camera negotiates a different resolution than the
server approves, the agent resizes frames before piping them to
FFmpeg.

The full SRT URL, the `streamid`, the `session_token`, and the API
token are **never** printed or logged. The FFmpeg command, which
contains the URL, is held in a single local variable and never
appears in `__repr__`, error messages, or log records.

**Milestone 5 is in preview.** The end-to-end smoke against a real
backend is blocked until `upstream-api-server` returns an SRT URL
whose host/port reaches a running MediaMTX. The mocked tests cover
every cleanup path; the live smoke is the remaining acceptance
criterion. See `docs/troubleshooting.md` for the temporary dev
override.

Heartbeat, reconnect/backoff, and token rotation are deferred to
Milestone 6+.

## Configuration

The agent reads a YAML config file. Default location:
`~/.upstream/config.yaml`. Override with `--config /path/to/file.yaml`
or the `UPSTREAM_CONFIG` environment variable.

Minimal config (example values are placeholders):

```yaml
api:
  # Production / staging
  base_url: "https://api.upstreamcv.com"
  token: "YOUR_API_TOKEN_HERE"

camera:
  name: "Front Door"
  source_type: "webcam"
  source: 0
  requested_width: 1280
  requested_height: 720
  requested_fps: 30

stream:
  # "low" | "balanced" | "stable"
  latency_mode: "balanced"
  buffer_ms: 500

logging:
  level: "info"
```

For local development against a dev server, swap `base_url`:

```yaml
api:
  base_url: "http://localhost:4000"
  token: "ups_test_YOUR_DEV_TOKEN"
```

API tokens look like `ups_<env>_<8-char id>_<22-char secret>`.

See `examples/webcam.yaml` and `examples/rtsp.yaml` for fuller templates.

## Local file layout

The agent stores everything under `~/.upstream/`:

| Path | Purpose |
| --- | --- |
| `~/.upstream/config.yaml` | Your config (you write this; gitignored). |
| `~/.upstream/agent_state.json` | Generated. Stores `device_id` and last known IDs. No tokens. |
| `~/.upstream/logs/agent.log` | Rotating log. Tokens are redacted. |

## Security notes

- API tokens are **never** printed in full. The CLI shows only the last
  four characters (e.g. `****abcd`).
- Log records are filtered through a redaction filter that masks known
  secrets and common token patterns (`Bearer ...`, JWT-shaped strings, etc.).
- The state file never stores session tokens — only stable identifiers.

## Project status

- **Milestone 1: package skeleton.** CLI, config, state, logging,
  token redaction, tests, docs.
- **Milestone 2: camera check.** `VideoSource` ABC, `WebcamSource`,
  `upstream check --camera`.
- **Milestone 3: API registration.** `UpstreamApiClient`,
  three POST endpoints (register agent / register camera / start
  stream session), `upstream check --api`.
- **Milestone 4: streaming preflight.** FFmpeg probe, SRT URL parser,
  FFmpeg command builder, `upstream check --streaming`.
- **Milestone 5 (preview, current): live streaming runtime.**
  `FfmpegEncoderProcess`, supervisor (`upstream_agent/supervisor.py`),
  and `upstream run`. Pipes BGR webcam frames through FFmpeg to SRT.
  Full real-backend smoke is blocked until the API returns an SRT
  URL whose host/port reaches a running MediaMTX — see
  `docs/troubleshooting.md` for the dev override.
- Milestone 6+: heartbeat, reconnect/backoff, token rotation,
  additional source types and diagnostics.

See `docs/architecture.md` for the planned end-to-end flow.

## License

[MIT](LICENSE).
