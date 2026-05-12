# Module map

Update this file whenever a module is added, removed, or renamed.

## `upstream_agent/`

| Module | Purpose |
| --- | --- |
| `__init__.py` | Package root. Re-exports `__version__`. |
| `version.py` | Single-source `__version__` string. Read by `pyproject.toml` via `[tool.hatch.version]`. |
| `cli.py` | Typer CLI app. Exposes `init` and `status` subcommands and a `--version` flag. Entry point is `upstream_agent.cli:app`. |
| `config.py` | YAML config loading (`load_config`), path resolution (`resolve_config_path`), and the default-config template writer (`write_default_config`). Pydantic v2 models for `api`, `camera`, `stream`, `logging`. |
| `state.py` | Local agent state: `State` model and `load_state` / `save_state`. Generates a stable `device_id` on first run. Handles corrupt state files by renaming to `.corrupt-<ts>`. |
| `logging_setup.py` | `setup_logging`, `TokenRedactionFilter`, and the pure `redact_token` helper. **Named `logging_setup` (not `logging`) to avoid shadowing the stdlib `logging` module.** |
| `errors.py` | Exception hierarchy: `UpstreamError` → `ConfigError`, `StateError`. The CLI catches `UpstreamError` at the top level. |

## Deferred submodules (added in later milestones)

These directories are intentionally **not present** yet. They will be
added in the milestone that needs them so their shape is informed by
real requirements, not premature placeholders.

| Path | Milestone | Purpose |
| --- | --- | --- |
| `api/` (`client.py`, `schemas.py`) | API milestone | HTTP client for register/config/heartbeat. |
| `sources/` (`base.py`, `webcam.py`, `rtsp.py`) | Camera milestone | Frame source abstraction over OpenCV, etc. |
| `buffer/frame_buffer.py` | Buffering milestone | Bounded ring buffer between source and encoder. |
| `encoder/` (`ffmpeg.py`, `command_builder.py`) | Encoder milestone | Wraps ffmpeg as a subprocess. |
| `transport/srt.py` | Transport milestone | SRT push to streaming server. |
| `heartbeat/reporter.py` | Heartbeat milestone | Periodic status reporting to the API. |
| `supervisor.py` | Supervisor milestone | Top-level orchestration of source → buffer → encoder → transport. |
| `diagnostics/` (`ffmpeg_check.py`, `camera_check.py`, `network_check.py`) | Diagnostics milestone | Backs `upstream check`. |

## `tests/`

| File | Covers |
| --- | --- |
| `conftest.py` | `home_dir` and `upstream_dir` fixtures (monkeypatch `Path.home` so tests cannot touch the real `~/.upstream/`). |
| `test_token_redaction.py` | `redact_token` helper, `TokenRedactionFilter` patterns, and end-to-end log-file scrubbing. |
| `test_config.py` | Path resolution, valid load, missing file, invalid YAML, missing section, placeholder rejection, no-input-leak, and template write/overwrite. |
| `test_state.py` | Fresh creation, reuse of `device_id`, corrupt JSON recovery, atomic write, schema field locking. |
| `test_cli.py` | `--help`, `--version`, `init` create/refuse/force/`--config`, `status` (no config, valid config redacted token, placeholder warning, corrupt state recovery, invalid YAML error). |

## `examples/`

| File | Purpose |
| --- | --- |
| `webcam.yaml` | Ready-to-copy config for a local USB or built-in camera. |
| `rtsp.yaml` | Ready-to-copy config for an RTSP network camera. |
