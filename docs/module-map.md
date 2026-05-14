# Module map

Update this file whenever a module is added, removed, or renamed.

## `upstream_agent/`

| Module | Purpose |
| --- | --- |
| `__init__.py` | Package root. Re-exports `__version__`. |
| `version.py` | Single-source `__version__` string. Read by `pyproject.toml` via `[tool.hatch.version]`. |
| `cli.py` | Typer CLI app. Exposes `init`, `status`, `check` (with `--camera` / `--api` / `--streaming`), and `run` subcommands plus a `--version` flag. Entry point is `upstream_agent.cli:app`. |
| `supervisor.py` | `RunResult` dataclass and `run_agent(cfg, state, *, client_factory=None, camera_factory=None, ffmpeg_factory=None, ffmpeg_probe=None, save_progress=None, max_frames=None, on_event=None)`. Live streaming orchestrator powering `upstream run`. Chains FFmpeg probe → camera open → API session approval (reuses `_obtain_stream_approval`) → command build → FFmpeg start → frame loop → cleanup. Cleanup order is FFmpeg → camera → session, regardless of how the loop exits (normal, `max_frames`, `KeyboardInterrupt`, FFmpeg start failure, mid-stream FFmpeg exit, camera read failure, API failure). The raw SRT URL lives in a single local variable inside this function; it never appears on the result, in events, or in logs. Uses only the SRT URL returned by `/v1/stream-sessions/start`. |
| `config.py` | YAML config loading (`load_config`), path resolution (`resolve_config_path`), and the default-config template writer (`write_default_config`). Pydantic v2 models for `api`, `camera`, `stream`, `logging`. |
| `state.py` | Local agent state: `State` model and `load_state` / `save_state`. Generates a stable `device_id` on first run. Handles corrupt state files by renaming to `.corrupt-<ts>`. |
| `logging_setup.py` | `setup_logging`, `TokenRedactionFilter`, and the pure `redact_token` helper. **Named `logging_setup` (not `logging`) to avoid shadowing the stdlib `logging` module.** |
| `errors.py` | Exception hierarchy: `UpstreamError` → `ConfigError`, `StateError`, `CameraError` (`CameraOpenError`, `CameraReadError`, `UnsupportedCameraSourceError`, `CameraDependencyMissingError`), `ApiError` (`ApiConnectionError`, `ApiAuthError` → `ApiTokenRevokedError`, `ApiTokenExpiredError`, `ApiValidationError`, `ApiRateLimitError`, `ApiServerError`, `ApiContractError`), `FfmpegError` (`FfmpegNotFoundError`, `FfmpegProbeError`, `FfmpegSrtUnsupportedError`, `FfmpegH264UnsupportedError`), `SrtUrlError`, `CommandBuildError`, `StreamingRuntimeError` (`FfmpegRuntimeError` → `FfmpegStartError`, `FfmpegWriteError`, `FfmpegExitError`). The CLI catches `UpstreamError` at the top level. |
| `sources/__init__.py` | Re-exports `VideoSource`, `WebcamSource`, `FrameInfo`. |
| `sources/base.py` | `VideoSource` ABC (`open`, `read_frame`, `close`, `is_open`, `info`, `last_error`) plus `FrameInfo` dataclass and concrete `__enter__`/`__exit__`. Every future source (RTSP, file, IP camera) implements this interface. |
| `sources/webcam.py` | `WebcamSource(VideoSource)` — wraps `cv2.VideoCapture`. Lazy `cv2` import via `_import_cv2()` so a missing OpenCV install becomes `CameraDependencyMissingError`. Writes width/height/fps before checking `isOpened()`; reports the effective values via `info`. `close()` is idempotent. |
| `diagnostics/__init__.py` | Documentation-only package marker for local diagnostic routines. |
| `diagnostics/camera.py` | `CameraCheckResult` dataclass and `run_camera_check(cfg)`. Never raises; the CLI translates the result to stdout / stderr / exit code. |
| `diagnostics/api.py` | `ApiCheckResult` dataclass and `run_api_check(cfg, state, *, client_factory=None, save_progress=None, capabilities=None)`. Walks the four-step API flow (register agent → register camera → start stream session → cleanup stop). Folds `ApiError` into the result; persists partial progress via the optional `save_progress` callback so a later step's failure does not lose earlier IDs. Defaults `capabilities` to a live FFmpeg probe (`{"ffmpeg": "<version>", "srt": <bool>}`) so the backend gets honest values. Exposes the private `_obtain_stream_approval` helper shared with the streaming orchestrator. The full SRT URL never appears on the result — only `srt_url_present: bool`. |
| `diagnostics/ffmpeg_check.py` | `FfmpegProbeResult` dataclass and `probe_ffmpeg(*, timeout_seconds=5.0, runner=None)`. Runs `ffmpeg -version` / `-protocols` / `-encoders` via a safe `subprocess.run` (no shell, captured stdout/stderr, timeouts, `FileNotFoundError`/`TimeoutExpired`/nonzero-exit handling). Detects SRT via a strict line-boundary regex (not `srtp`); picks an H264 encoder by priority (libx264 → h264_videotoolbox → h264_nvenc → h264_qsv → h264_amf). Never raises. The `runner` injection point lets tests stub subprocess. |
| `diagnostics/streaming.py` | `StreamingCheckResult` dataclass and `run_streaming_check(cfg, state, *, client_factory=None, save_progress=None, ffmpeg_probe=None)`. Eight-phase orchestrator: FFmpeg → SRT support → H264 encoder → API approval → SRT URL parse → command build → cleanup stop. Reuses `_obtain_stream_approval` from `diagnostics/api.py` so capabilities flow honestly through `register_agent`. The raw SRT URL stays in a local variable inside this function and is dropped before returning — only safe booleans and the approved config cross out. |
| `api/__init__.py` | Re-exports `UpstreamApiClient`, `ParsedError`, the path constants, the token-code sentinels, and every schema (`Agent`, `Camera`, `Session`, `RequestedStreamConfig`, `ApprovedStreamConfig`, plus request/response wrappers). |
| `api/client.py` | `UpstreamApiClient` — synchronous httpx wrapper. Three public methods (`register_agent`, `register_camera`, `start_stream_session`), one isolated `_build_auth_header` so swapping `Bearer` is one place, one `_request` that handles JSON encode/decode and Pydantic-validated response parsing, and a status+code `_raise_for_status` that maps the confirmed error envelope (`error.code` / `error.message` / `error.request_id`) to typed `ApiError` subclasses. Endpoint path constants: `AGENT_REGISTER_PATH`, `CAMERA_REGISTER_PATH`, `STREAM_SESSION_START_PATH`. Token-code sentinels: `TOKEN_REVOKED_ERROR_CODE`, `TOKEN_EXPIRED_ERROR_CODE`. Never logs request or response bodies. |
| `api/schemas.py` | Pydantic v2 request/response models matching the confirmed contract. Request wrappers (`AgentRegisterRequest`, `CameraRegisterRequest`, `StreamSessionRequest`) use `extra="forbid"`; response wrappers and nested objects (`Agent`, `Camera`, `Session`, `ApprovedStreamConfig`) use `extra="ignore"`. `CameraRegisterResponse` has a pre-validator that accepts both wrapped (`{"camera": {...}}`) and flat camera bodies. Sensitive fields (`Session.srt_url`, `Session.streamid`, `StreamSessionResponse.session_token`) are accepted in the model but never exposed past the orchestrator. |
| `encoder/__init__.py` | Re-exports `build_ffmpeg_command`, `redact_command`, and `FfmpegEncoderProcess`. |
| `encoder/command_builder.py` | `build_ffmpeg_command(*, width, height, fps, bitrate_kbps, codec, encoder, latency_mode, srt_url) -> list[str]` builds the argv FFmpeg runs in `upstream run`; raises `CommandBuildError` for non-H264 codecs or unknown encoders. Input shape is `-f rawvideo -pix_fmt bgr24 -s WxH -r FPS -i pipe:0` (OpenCV BGR frames piped to stdin). Output is H264/MPEG-TS over SRT. `redact_command(cmd, srt_url) -> list[str]` returns a fresh list with any element starting with `srt://` (or equal to the URL) replaced by `srt://<redacted>`; never mutates the input. |
| `encoder/ffmpeg.py` | `FfmpegEncoderProcess(command, redacted_command=None, *, popen_factory=None)`. Owns one `subprocess.Popen` running ffmpeg with `shell=False`, `stdin=PIPE`, `stdout=DEVNULL`, `stderr=PIPE`. Methods: `start()` (raises `FfmpegStartError` on `FileNotFoundError`/`OSError`; idempotent guard), `write_frame(bytes)` (raises `FfmpegWriteError` on `BrokenPipeError`/`OSError` — error messages scrub any `srt://` line out of FFmpeg's own stderr), `poll() -> int | None`, `stop(timeout_seconds=5.0)` (closes stdin, terminates, kills after timeout; never raises; idempotent). The `redacted_command` is the only argv shape exposed via `__repr__` and error strings. `popen_factory` injection point lets tests drive a fake `Popen` without launching any subprocess. |
| `transport/__init__.py` | Re-exports `ParsedSrtUrl` and `parse_srt_url`. |
| `transport/srt.py` | `parse_srt_url(url) -> ParsedSrtUrl`. Validates scheme (`srt`), host, and port; raises `SrtUrlError` otherwise. Returns only safe-to-display metadata (`has_host`, `has_port`, `port`, `has_streamid` as a bool, `mode`, `latency_ms`). The raw URL, hostname, and streamid are never stored on the dataclass — they stay in the caller's local scope. |

## Deferred submodules (added in later milestones)

These directories are intentionally **not present** yet. They will be
added in the milestone that needs them so their shape is informed by
real requirements, not premature placeholders.

| Path | Milestone | Purpose |
| --- | --- | --- |
| `api/config_fetch.py`, `api/heartbeat.py` | Later API milestones | Additional HTTP calls (get-config, heartbeat, event). |
| `sources/rtsp.py`, `sources/file.py`, `sources/ip_camera.py` | Later source milestones | Additional `VideoSource` implementations. |
| `buffer/frame_buffer.py` | Buffering milestone | Bounded ring buffer between source and encoder. |
| `heartbeat/reporter.py` | Milestone 6 | Periodic status reporting to the API. |
| Reconnect/backoff in `supervisor.py` | Milestone 7 | Automatic retry on transient FFmpeg or API failures. |
| `diagnostics/network.py` | Later diagnostics milestones | Additional `upstream check --…` probes. |

## `tests/`

| File | Covers |
| --- | --- |
| `conftest.py` | `home_dir` and `upstream_dir` fixtures (monkeypatch `Path.home` so tests cannot touch the real `~/.upstream/`). |
| `test_token_redaction.py` | `redact_token` helper, `TokenRedactionFilter` patterns, and end-to-end log-file scrubbing. |
| `test_config.py` | Path resolution, valid load, missing file, invalid YAML, missing section, placeholder rejection, no-input-leak, and template write/overwrite. |
| `test_state.py` | Fresh creation, reuse of `device_id`, corrupt JSON recovery, atomic write, schema field locking. |
| `test_cli.py` | `--help`, `--version`, `init` create/refuse/force/`--config`, `status` (no config, valid config redacted token, placeholder warning, corrupt state recovery, invalid YAML error), `check --camera` (no flag, no config, success, renegotiated note, open failure, unsupported source, placeholder token allowed, no token leak, invalid YAML, unknown FPS), `check --api` (no config, placeholder rejection, happy path with state persistence, register-agent failure, register-camera failure, session failure, token + SRT URL leak guard, both flags compose). |
| `test_errors.py` | Inheritance shape of the exception hierarchy (every package error subclasses `UpstreamError`; `ApiTokenRevokedError` is a subclass of `ApiAuthError`). |
| `test_sources_webcam.py` | `WebcamSource` against a fake `cv2` installed via `sys.modules`. Covers property writes before `isOpened()`, `CameraOpenError` with release on failed open, effective `FrameInfo`, `CameraReadError`, idempotent `close()`, missing-cv2 `CameraDependencyMissingError`, and rejection of non-webcam source types. |
| `test_diagnostics_camera.py` | `run_camera_check` orchestrator behavior against a stub `VideoSource`: success, open failure, read failure, and unsupported `source_type` short-circuit. |
| `test_api_client.py` | `UpstreamApiClient` against an `httpx.MockTransport`. Covers happy paths, Bearer-header injection, base-URL normalisation, 401/403/422/429/5xx/404 mapping, `token_revoked` → `ApiTokenRevokedError`, connection/timeout → `ApiConnectionError`, missing/blank/non-JSON response body → `ApiContractError`, and token-leak guards on every error path. |
| `test_diagnostics_api.py` | `run_api_check` orchestrator behavior against a `FakeUpstreamApiClient`. Covers happy path with `save_progress` called exactly twice, each of the three step-failure scenarios with correct state persistence, SRT-URL leak guard via `caplog`, request-payload sanity, and capabilities reporting (injected fixed dict + probed defaults). |
| `test_diagnostics_ffmpeg.py` | `probe_ffmpeg` against an injected `runner` stub. Covers happy path, missing FFmpeg (`FileNotFoundError`), timeout, nonzero exit, missing SRT, missing H264, unknown version, partial failure, leak-free errors, and the `videotoolbox` fallback when libx264 is absent. |
| `test_transport_srt.py` | `parse_srt_url` validation. Covers valid URLs, non-SRT schemes, missing host/port/scheme, empty/whitespace input, invalid port, missing streamid (allowed, sets `has_streamid=False`), mixed-case scheme, and `repr()` privacy (no host/streamid leak). |
| `test_encoder_command_builder.py` | `build_ffmpeg_command` returns a `list[str]` with the approved resolution/FPS/bitrate/codec, output container `mpegts`, the SRT URL as the final arg, and the chosen encoder. `redact_command` replaces the URL with `srt://<redacted>`, preserves other args, and never mutates the input list. Edge cases: bad codec, unknown encoder, non-positive dimensions/FPS, empty URL. |
| `test_diagnostics_streaming.py` | `run_streaming_check` orchestrator against the same fake API client. Covers happy path, three local-failure phases (no API call fires), three API-failure phases (correct stop-or-not behaviour), SRT URL parse failure, command build failure, stop-warning best-effort, leak guards on the result object and `caplog`, and capability-payload reporting from the probe. |
| `test_encoder_ffmpeg.py` | `FfmpegEncoderProcess` against an injected fake `Popen`. Covers `start` invocation/idempotency, `FileNotFoundError`/`OSError` mapping to `FfmpegStartError`, `write_frame` happy path and `BrokenPipeError`/`OSError` mapping to `FfmpegWriteError` with scrubbed stderr, `poll` proxying, `stop` ordering (close stdin → terminate → wait → kill on timeout), `stop` no-op semantics, idempotency, and `__repr__` redaction. |
| `test_supervisor.py` | `run_agent` orchestrator against `FakeCamera`/`FakeApiClient`/`FakeFfmpeg`. Covers happy path with `max_frames`, cleanup ordering (FFmpeg → camera → session), capabilities payload reflecting the probe, `save_progress` called twice, session-before-FFmpeg ordering, server-approved-config over local-config, FFmpeg start failure, frame write failure, camera read failure, FFmpeg mid-stream exit, API failure (no FFmpeg started), `KeyboardInterrupt` (cleanup runs then re-raises), frame resize via injected fake cv2, no-resize fast path, leak guards on events/`caplog`/result, and that the API-issued SRT URL is the URL passed into the FFmpeg command. |

## `examples/`

| File | Purpose |
| --- | --- |
| `webcam.yaml` | Ready-to-copy config for a local USB or built-in camera. |
| `rtsp.yaml` | Ready-to-copy config for an RTSP network camera. |
