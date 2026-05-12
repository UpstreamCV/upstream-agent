# Commit log

A running record of meaningful changes. **Append a new section at the
top after every meaningful commit** using the format below. Tests,
trivial typo fixes, and dependency bumps don't need their own entry —
group them with the next substantive change.

## Format

```
## YYYY-MM-DD - Short commit title

### Summary
Brief explanation of what changed.

### Files touched
- path/to/file.py - what changed here

### Functions/classes changed
- FunctionName - what it does and why it matters

### System interaction notes
Explain how this change affects the agent flow.

### Testing/checks
- What was tested
- What still needs testing

### Follow-up TODOs
- Any known next steps
```

---

## 2026-05-12 - feat: add camera source layer and upstream check --camera (Milestone 2)

### Summary
Milestone 2. Adds the camera-source abstraction (`VideoSource` ABC plus a concrete `WebcamSource` on top of OpenCV), a `run_camera_check` diagnostic, and the `upstream check --camera` CLI command. The agent can now prove that it can open the configured webcam, read a frame, and release the device cleanly. No API call, no streaming, no FFmpeg, no heartbeat — that all stays out of scope per the milestone boundary.

### Files touched
- pyproject.toml - added `opencv-python>=4.8,<5` to runtime dependencies.
- upstream_agent/errors.py - added `CameraError` plus `CameraOpenError`, `CameraReadError`, `UnsupportedCameraSourceError`, `CameraDependencyMissingError`.
- upstream_agent/sources/__init__.py - new package; re-exports `VideoSource`, `WebcamSource`, `FrameInfo`.
- upstream_agent/sources/base.py - new module; `VideoSource` ABC and `FrameInfo` dataclass.
- upstream_agent/sources/webcam.py - new module; `WebcamSource` wrapping `cv2.VideoCapture` with a lazy `cv2` import.
- upstream_agent/diagnostics/__init__.py - new package; documentation marker.
- upstream_agent/diagnostics/camera.py - new module; `CameraCheckResult` and `run_camera_check`.
- upstream_agent/cli.py - added `check_cmd` (the `upstream check --camera` command) and the `_format_fps` / `_print_camera_check_success` helpers; extended the module docstring.
- tests/test_errors.py - new file; 8 inheritance-shape tests.
- tests/test_sources_webcam.py - new file; 12 tests against a fake `cv2` module.
- tests/test_diagnostics_camera.py - new file; 4 orchestrator tests against a stub `VideoSource`.
- tests/test_cli.py - extended; 10 new `check` tests covering no-flag, no-config, success, renegotiated note, open failure, unsupported source, placeholder-token allowed, token-leak guard, invalid YAML, unknown FPS.
- docs/architecture.md - flipped the camera-source and diagnostics rows from planned to partial; added a "Camera source layer" section.
- docs/module-map.md - moved `sources/` and `diagnostics/` into the live table; documented the new error classes; added the new test files.
- docs/troubleshooting.md - added six camera entries (cannot open, in use, permission denied, wrong index, FPS unknown, frame read failed, OpenCV missing).
- README.md - documented `upstream check --camera` with the success-output sample.

### Functions/classes changed
- `VideoSource` - abstract base for every present and future video source. Defines the small contract (`open`, `read_frame`, `close`, `is_open`, `info`, `last_error`) and provides concrete `__enter__`/`__exit__` so subclasses get safe context-manager semantics for free.
- `FrameInfo` - frozen dataclass; carries the effective width / height / fps reported by an opened source. Distinct from the `requested_*` fields on `CameraConfig` because device negotiation can change them.
- `WebcamSource` - opens a local webcam via `cv2.VideoCapture(source)`. Writes width / height / fps properties before checking `isOpened()` (V4L2 drivers can ignore writes after the first read). Releases the cap immediately on failed open so we never leak the handle. `close()` is idempotent.
- `_import_cv2` - lazy importer that translates `ImportError` into `CameraDependencyMissingError`; keeps the rest of the package importable when OpenCV is missing.
- `CameraCheckResult` - frozen dataclass returned by `run_camera_check`; carries the requested values from `CameraConfig`, the effective `FrameInfo` on success, and a human-readable `error` string on failure.
- `run_camera_check` - probes the configured camera and returns a `CameraCheckResult`. Never raises. Dispatches on `source_type` (only `webcam` is supported today; anything else short-circuits with a typed-but-non-raising "not supported yet" message that doesn't construct the source). Uses `with WebcamSource(...)` so `release()` runs even when `read_frame()` raises.
- `check_cmd` - new Typer command. Validates `--camera` was passed (otherwise exit 2 with a usage hint), validates a config file exists (otherwise exit 1 with `Run 'upstream init'`), loads the config with `allow_placeholder=True` because no API call is made, registers any real token with the redaction filter, calls `run_camera_check`, and translates the result to stdout/stderr/exit code.
- `_format_fps` / `_print_camera_check_success` - rendering helpers. FPS `0.0` prints as `unknown`. When effective values differ from requested values, append a `Note: requested ..., device negotiated ...` line.
- Camera error classes (`CameraError`, `CameraOpenError`, `CameraReadError`, `UnsupportedCameraSourceError`, `CameraDependencyMissingError`) - all inherit from `UpstreamError`, so the CLI's existing `except UpstreamError → Error: <msg>, exit 1` contract picks them up with no change.

### System interaction notes
The new flow is: CLI `check_cmd` → `resolve_config_path` / `load_config(allow_placeholder=True)` → `setup_logging` (with any real token registered for redaction) → `run_camera_check(cfg.camera)` → `WebcamSource.open() / read_frame() / close()`. None of these layers calls the API or talks to the network. The orchestrator returns a structured result rather than raising; the CLI is the only layer that knows about `typer.Exit`.

### Testing/checks
- `pytest -q` passes (86 tests; 52 from M1 plus 34 new).
- New CLI tests use `monkeypatch.setitem(sys.modules, "cv2", fake)` to install a stand-in `cv2` module before `_import_cv2` runs, so CI never depends on a real webcam.
- Token-leak guard: the CLI test with a real-looking config token asserts the token appears in neither stdout, stderr, nor the rotating log file.
- Manual smoke against real hardware was deferred — the host machine has a Homebrew Python ↔ libexpat ABI mismatch that prevents normal `pip` from running without `DYLD_LIBRARY_PATH=/opt/homebrew/opt/expat/lib`, which is an environment issue unrelated to this milestone. The mocked test path verifies CLI wiring end-to-end; physical-camera smoke is a follow-up.

### Follow-up TODOs
- Smoke `upstream check --camera` against real hardware once the host expat issue is resolved.
- Add `sources/rtsp.py` in a later milestone; the `UnsupportedCameraSourceError` short-circuit in `run_camera_check` is the placeholder until then.
- Reserve `--api`, `--storage`, `--ffmpeg` flags on `check` for later diagnostics; the command's flag-based shape was chosen with those extensions in mind.

---

## 2026-05-12 - docs: seed architecture, module map, commit log, contracts, troubleshooting

### Summary
First pass of the five permanent doc pages: architecture overview, module map, commit log (this file), API contracts placeholder, and troubleshooting.

### Files touched
- docs/architecture.md - target end-to-end flow, M1 status table, design principles.
- docs/module-map.md - per-module purpose table and a deferred-submodules table for milestones still to come.
- docs/commit-log.md - format guidance plus one entry per M1 commit.
- docs/api-contracts.md - placeholder describing the planned register/config/heartbeat/event endpoints.
- docs/troubleshooting.md - M1-relevant issues (config not found, placeholder warning, redacted token, corrupt state recovery, log location).

### Functions/classes changed
None — documentation only.

### System interaction notes
Future commits should update `module-map.md` whenever a file is added/removed/renamed, `architecture.md` when a new layer becomes real, `commit-log.md` after every meaningful commit, `api-contracts.md` during the API milestone, and `troubleshooting.md` whenever a support pattern repeats.

### Testing/checks
- N/A; docs render as Markdown.

### Follow-up TODOs
- Fill in concrete request/response schemas in `api-contracts.md` once the server side is settled.

---

## 2026-05-12 - docs: add example webcam and rtsp configs

### Summary
Two copy-and-edit YAML templates under `examples/`, one for a local webcam and one for an RTSP network camera.

### Files touched
- examples/webcam.yaml - webcam template with placeholder token and `source: 0`.
- examples/rtsp.yaml - rtsp template with placeholder credentials in the URL.

### Functions/classes changed
None.

### System interaction notes
These are documentation, not code. They give users a starting point that mirrors what `upstream init` writes. The RTSP example deliberately uses a placeholder URL (USER:PASS@CAMERA_HOST) and warns against committing filled-in copies because RTSP URLs often embed credentials.

### Testing/checks
- Manually inspected — both parse with `yaml.safe_load`.

### Follow-up TODOs
- Add more examples (multi-camera, custom port, gstreamer source) as those source types come online.

---

## 2026-05-12 - feat: add upstream cli with init and status commands

### Summary
The Typer CLI for `upstream init` and `upstream status`, wired into the `upstream` entry point declared in `pyproject.toml`. Also refactored the config loader to gate placeholder rejection behind a flag so `status` can show users their config (with a friendly placeholder note) without erroring out.

### Files touched
- upstream_agent/cli.py - Typer app, `--version`, `init`, `status`.
- upstream_agent/config.py - moved placeholder rejection out of the Pydantic model and into `load_config(..., allow_placeholder=False)`.
- tests/test_cli.py - 12 tests covering help, version, init create/refuse/force/--config, status (no config, valid config, placeholder, corrupt state, invalid YAML).

### Functions/classes changed
- `app` (`upstream_agent.cli.app`) - Typer application; the entry point in pyproject.toml resolves to this object.
- `init_cmd` - resolves the config path, writes the template, refuses overwrite unless `--force`, prints the do-not-commit reminder.
- `status_cmd` - reads config (allow_placeholder=True so the user sees the placeholder note), redacts token via `redact_token`, loads or initialises state (creating a stable `device_id`), and prints both blocks. Errors raise `typer.Exit(1)`.
- `load_config(allow_placeholder=...)` - new flag. Default behavior (reject) is unchanged for any caller that doesn't opt in.

### System interaction notes
`status` is the first place that calls `setup_logging` with a non-empty `sensitive_values` set: the loaded `api.token` is registered with the redaction filter before any log line is emitted, so even unexpected log output during status execution will be scrubbed in `agent.log`.

### Testing/checks
- `pytest -q` passes (52 tests).
- Manual: `upstream --help`, `upstream --version` work; `upstream init` creates the config and refuses re-init without `--force`.

### Follow-up TODOs
- Add `upstream check` (diagnostics) and `upstream run` (supervisor) in later milestones.

---

## 2026-05-12 - feat: add local state manager with device id

### Summary
Persistent local state at `~/.upstream/agent_state.json`. The schema is deliberately small (`agent_id`, `device_id`, `last_camera_id`) and forbids extra fields so a stray token cannot be quietly written into state.

### Files touched
- upstream_agent/state.py - `State` model, `load_state`, `save_state`, `default_state_path`.
- tests/test_state.py - 10 tests covering missing-file creation, parent-dir creation, device-id reuse, corrupt JSON recovery, wrong-shape recovery, atomic-write strategy, schema field locking, extra-field rejection.

### Functions/classes changed
- `State` - Pydantic model with `extra="forbid"`; `device_id` required, `agent_id` and `last_camera_id` optional.
- `load_state` - creates parent directory, generates a fresh state with a uuid4 device_id if missing, recovers from corrupt JSON or wrong shape by renaming the file to `.corrupt-<unix-ts>` and writing fresh state.
- `save_state` - writes through a `.tmp` sibling and uses `os.replace` so an interrupted write cannot leave a half-written file behind.

### System interaction notes
`load_state` is what `upstream status` uses to surface the local device identity. Future milestones will populate `agent_id` after `POST /agent/register` and `last_camera_id` after camera selection — but never tokens.

### Testing/checks
- `pytest -q tests/test_state.py` passes.
- Manual: corrupt-state recovery exercised through `upstream status` (covered in the CLI test).

### Follow-up TODOs
- Token rotation will need state to coordinate with config rewrites (planned for the API milestone).

---

## 2026-05-12 - feat: add yaml config loader with pydantic validation

### Summary
Pydantic v2 models for the four config sections and a `load_config` that wraps the usual failure modes (missing file, invalid YAML, validation error) in `ConfigError` without leaking input values into error strings.

### Files touched
- upstream_agent/config.py - `ApiConfig`, `CameraConfig`, `StreamConfig`, `LoggingConfig`, `Config`; `resolve_config_path`, `load_config`, `write_default_config`; commented template string.
- tests/test_config.py - 15 tests for path resolution, valid load, missing file, invalid YAML, missing section, placeholder rejection, empty file, non-mapping root, no-input-leak guarantee, and template write/overwrite.

### Functions/classes changed
- `resolve_config_path` - explicit > `UPSTREAM_CONFIG` env > `~/.upstream/config.yaml`.
- `load_config` - reads the file, `yaml.safe_load`, validates via Pydantic, formats validation errors with `include_input=False` so a real-looking token cannot appear in a stderr-printed error.
- `write_default_config` - emits a commented template, refuses to overwrite unless `force=True`.

### System interaction notes
This is the only place where the YAML config is read. Other modules consume the parsed `Config` object, never the raw YAML.

### Testing/checks
- `pytest -q tests/test_config.py` passes.

### Follow-up TODOs
- After the API milestone, decide whether token rotation rewrites this file in place or stores rotated tokens elsewhere.

---

## 2026-05-12 - feat: add logging setup with token redaction filter

### Summary
`setup_logging` configures a rotating file handler under `~/.upstream/logs/agent.log` plus a stderr handler, with a `TokenRedactionFilter` attached to each that scrubs known sensitive substrings and common token-shaped patterns.

### Files touched
- upstream_agent/logging_setup.py - `setup_logging`, `TokenRedactionFilter`, `redact_token`.
- tests/test_token_redaction.py - filter and integration tests added on top of the helper tests from the previous commit.

### Functions/classes changed
- `setup_logging` - idempotent (clears existing handlers); creates `log_dir` if missing; 5 MB × 3 rotation.
- `TokenRedactionFilter` - two-layer scrubbing: (1) replace any configured sensitive substring with `****`, (2) regex-replace Bearer/Authorization/token=/api_key=/password=/secret= patterns and JWT-shaped strings.

### System interaction notes
The filter is attached to *handlers*, not the root logger, because Python's logging hierarchy consults a logger's filters only for records emitted at that logger — records propagated up from child loggers reach the root's handlers but skip the root's filter list. Sharing one filter instance between handlers is fine: the second invocation sees already-redacted text and is a no-op.

### Testing/checks
- `pytest -q tests/test_token_redaction.py` (15 tests) passes.
- Integration test asserts the real log file contains the redacted form, not the original secret.

### Follow-up TODOs
- Consider adding a "high-entropy random string" pattern once we have a feel for how third-party deps log credentials.

---

## 2026-05-12 - feat: add error hierarchy and token redaction helper

### Summary
Introduces `UpstreamError` (and its `ConfigError`/`StateError` children) so the CLI can catch one type at the top level, plus the pure `redact_token` helper used by `upstream status`.

### Files touched
- upstream_agent/errors.py - exception classes.
- upstream_agent/logging_setup.py - initial stub containing only `redact_token` (filter and setup added in the next commit).
- tests/__init__.py, tests/conftest.py - test package init and shared `home_dir` fixture.
- tests/test_token_redaction.py - six tests for `redact_token`.

### Functions/classes changed
- `UpstreamError`, `ConfigError`, `StateError` - the package-wide exception base.
- `redact_token(token)` - returns `<unset>` for empty, `****` for short, `****<last4>` for longer tokens.

### System interaction notes
The `home_dir` fixture monkey-patches `Path.home` so tests never write into the real `~/.upstream/` of whoever runs them.

### Testing/checks
- `pytest -q tests/test_token_redaction.py` (6 tests) passes.

### Follow-up TODOs
- Wire the filter and `setup_logging` (next commit).

---

## 2026-05-12 - chore: add pyproject.toml and package skeleton

### Summary
Makes the package installable. Hatchling backend, single-source version via `[tool.hatch.version]`, three runtime deps (typer, pyyaml, pydantic), dev extras (pytest, ruff, mypy), and the `upstream` script entry point pointing at `upstream_agent.cli:app`.

### Files touched
- pyproject.toml - PEP 621 metadata, deps, scripts entry, ruff/pytest config.
- upstream_agent/__init__.py - re-exports `__version__`.
- upstream_agent/version.py - `__version__ = "0.1.0"`.

### Functions/classes changed
None — packaging only.

### System interaction notes
`pip install -e ".[dev]"` brings up a working venv. The `upstream` entry point fails (`ModuleNotFoundError: upstream_agent.cli`) until the CLI is added later.

### Testing/checks
- `pip install -e ".[dev]"` succeeds.
- `python -c "import upstream_agent; print(upstream_agent.__version__)"` prints `0.1.0`.

### Follow-up TODOs
- Add CLI module in a follow-up commit so the entry point resolves.

---

## 2026-05-12 - chore: add license, gitignore, env example, expand readme

### Summary
Treats the repo as public from day one. MIT LICENSE, Python-aware `.gitignore` that also blocks local agent artifacts (`config.yaml`, `agent_state.json`, `*.log`, `.upstream/`) from accidental commits, a minimal `.env.example` that explicitly tells users tokens never go in `.env`, and a full README using placeholder values only.

### Files touched
- LICENSE - MIT, copyright 2026 UpstreamCV.
- .gitignore - Python build artifacts, venvs, env files, IDE/OS junk, caches, and the local agent artifact paths.
- .env.example - documents `UPSTREAM_CONFIG` and `UPSTREAM_LOG_LEVEL` only; warns against putting tokens here.
- README.md - install, quick-start, file layout, security notes, project status.

### Functions/classes changed
None.

### System interaction notes
This baseline must hold for every future commit: nothing in the repo may contain a real token, real URL beyond the public `api.upstream.ai`, or PII.

### Testing/checks
- `git status` confirms only the intended four files were added/modified.

### Follow-up TODOs
- None.
