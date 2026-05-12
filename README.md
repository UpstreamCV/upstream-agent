# upstream-agent

A public, pip-installable Python agent that runs on a customer or local
machine, reads a camera feed, asks the UpStream API for permission/config,
and streams video securely to the UpStream streaming server.

This README reflects the **current shape** of the project. The agent is
under active development. Today it can be configured locally and probe
a webcam (open, read one frame, report resolution and FPS, release).
API registration, encoding, and streaming will land in later milestones.

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

## Configuration

The agent reads a YAML config file. Default location:
`~/.upstream/config.yaml`. Override with `--config /path/to/file.yaml`
or the `UPSTREAM_CONFIG` environment variable.

Minimal config (example values are placeholders):

```yaml
api:
  base_url: "https://api.upstream.ai"
  token: "YOUR_API_TOKEN_HERE"

camera:
  name: "Front Door"
  source_type: "webcam"
  source: 0
  requested_width: 1280
  requested_height: 720
  requested_fps: 30

stream:
  latency_mode: "balanced"
  buffer_ms: 500

logging:
  level: "info"
```

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
- **Milestone 2 (current): camera check.** `VideoSource` ABC,
  `WebcamSource`, `upstream check --camera`.
- Milestone 3+: API registration, frame buffer, FFmpeg encoder, SRT
  transport, heartbeat, supervisor, additional source types and
  diagnostics.

See `docs/architecture.md` for the planned end-to-end flow.

## License

[MIT](LICENSE).
