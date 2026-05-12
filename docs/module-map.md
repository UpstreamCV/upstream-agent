# Module map

Update this file whenever a module is added, removed, or renamed.

## `upstream_agent/`

| Module | Purpose |
| --- | --- |
| `__init__.py` | Package root. Re-exports `__version__`. |
| `version.py` | Single-source `__version__` string. Read by `pyproject.toml` via `[tool.hatch.version]`. |
| `cli.py` | Typer CLI app. Exposes `init`, `status`, and `check` subcommands and a `--version` flag. Entry point is `upstream_agent.cli:app`. |
| `config.py` | YAML config loading (`load_config`), path resolution (`resolve_config_path`), and the default-config template writer (`write_default_config`). Pydantic v2 models for `api`, `camera`, `stream`, `logging`. |
| `state.py` | Local agent state: `State` model and `load_state` / `save_state`. Generates a stable `device_id` on first run. Handles corrupt state files by renaming to `.corrupt-<ts>`. |
| `logging_setup.py` | `setup_logging`, `TokenRedactionFilter`, and the pure `redact_token` helper. **Named `logging_setup` (not `logging`) to avoid shadowing the stdlib `logging` module.** |
| `errors.py` | Exception hierarchy: `UpstreamError` → `ConfigError`, `StateError`, `CameraError` (`CameraOpenError`, `CameraReadError`, `UnsupportedCameraSourceError`, `CameraDependencyMissingError`). The CLI catches `UpstreamError` at the top level. |
| `sources/__init__.py` | Re-exports `VideoSource`, `WebcamSource`, `FrameInfo`. |
| `sources/base.py` | `VideoSource` ABC (`open`, `read_frame`, `close`, `is_open`, `info`, `last_error`) plus `FrameInfo` dataclass and concrete `__enter__`/`__exit__`. Every future source (RTSP, file, IP camera) implements this interface. |
| `sources/webcam.py` | `WebcamSource(VideoSource)` — wraps `cv2.VideoCapture`. Lazy `cv2` import via `_import_cv2()` so a missing OpenCV install becomes `CameraDependencyMissingError`. Writes width/height/fps before checking `isOpened()`; reports the effective values via `info`. `close()` is idempotent. |
| `diagnostics/__init__.py` | Documentation-only package marker for local diagnostic routines. |
| `diagnostics/camera.py` | `CameraCheckResult` dataclass and `run_camera_check(cfg)`. Never raises; the CLI translates the result to stdout / stderr / exit code. |

## Deferred submodules (added in later milestones)

These directories are intentionally **not present** yet. They will be
added in the milestone that needs them so their shape is informed by
real requirements, not premature placeholders.

| Path | Milestone | Purpose |
| --- | --- | --- |
| `api/` (`client.py`, `schemas.py`) | API milestone | HTTP client for register/config/heartbeat. |
| `sources/rtsp.py`, `sources/file.py`, `sources/ip_camera.py` | Later source milestones | Additional `VideoSource` implementations. |
| `buffer/frame_buffer.py` | Buffering milestone | Bounded ring buffer between source and encoder. |
| `encoder/` (`ffmpeg.py`, `command_builder.py`) | Encoder milestone | Wraps ffmpeg as a subprocess. |
| `transport/srt.py` | Transport milestone | SRT push to streaming server. |
| `heartbeat/reporter.py` | Heartbeat milestone | Periodic status reporting to the API. |
| `supervisor.py` | Supervisor milestone | Top-level orchestration of source → buffer → encoder → transport. |
| `diagnostics/ffmpeg.py`, `diagnostics/network.py`, `diagnostics/api.py` | Later diagnostics milestones | Additional `upstream check --…` probes. |

## `tests/`

| File | Covers |
| --- | --- |
| `conftest.py` | `home_dir` and `upstream_dir` fixtures (monkeypatch `Path.home` so tests cannot touch the real `~/.upstream/`). |
| `test_token_redaction.py` | `redact_token` helper, `TokenRedactionFilter` patterns, and end-to-end log-file scrubbing. |
| `test_config.py` | Path resolution, valid load, missing file, invalid YAML, missing section, placeholder rejection, no-input-leak, and template write/overwrite. |
| `test_state.py` | Fresh creation, reuse of `device_id`, corrupt JSON recovery, atomic write, schema field locking. |
| `test_cli.py` | `--help`, `--version`, `init` create/refuse/force/`--config`, `status` (no config, valid config redacted token, placeholder warning, corrupt state recovery, invalid YAML error), `check` (no flag, no config, success, renegotiated note, open failure, unsupported source, placeholder token allowed, no token leak, invalid YAML, unknown FPS). |
| `test_errors.py` | Inheritance shape of the exception hierarchy (every package error subclasses `UpstreamError`). |
| `test_sources_webcam.py` | `WebcamSource` against a fake `cv2` installed via `sys.modules`. Covers property writes before `isOpened()`, `CameraOpenError` with release on failed open, effective `FrameInfo`, `CameraReadError`, idempotent `close()`, missing-cv2 `CameraDependencyMissingError`, and rejection of non-webcam source types. |
| `test_diagnostics_camera.py` | `run_camera_check` orchestrator behavior against a stub `VideoSource`: success, open failure, read failure, and unsupported `source_type` short-circuit. |

## `examples/`

| File | Purpose |
| --- | --- |
| `webcam.yaml` | Ready-to-copy config for a local USB or built-in camera. |
| `rtsp.yaml` | Ready-to-copy config for an RTSP network camera. |
