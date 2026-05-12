# upstream-agent

A public, pip-installable Python agent that runs on a customer or local
machine, reads a camera feed, asks the UpStream API for permission/config,
and streams video securely to the UpStream streaming server.

This README reflects the **current shape** of the project. The agent is
under active development; this milestone provides the package skeleton and
local config/state/logging machinery. Camera capture, API registration,
encoding, and streaming will land in later milestones.

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
```

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

- **Milestone 1 (current): package skeleton.** CLI, config, state, logging,
  token redaction, tests, docs.
- Milestone 2+: camera sources, frame buffer, FFmpeg encoder, SRT transport,
  API registration, heartbeat, supervisor, diagnostics.

See `docs/architecture.md` for the planned end-to-end flow.

## License

[MIT](LICENSE).
