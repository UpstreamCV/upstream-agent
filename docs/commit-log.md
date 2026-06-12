# Commit log

A running record of meaningful changes. **Append a new section at the
top after every meaningful commit** using the format below. Tests,
trivial typo fixes, and dependency bumps don't need their own entry —
group them with the next substantive change.

## 2026-06-12 - feat(update): notify-only version checking (update M2)

**Why:** stale agents miss reliability/security fixes and new API
contract expectations. First slice of the approved auto-update plan:
server owns the version facts, the agent owns the comparison, nothing
blocks and nothing self-updates.

**What:** `upstream_agent/update_check.py` — dependency-free strict
X.Y.Z parser, `evaluate_policy` (no_policy / up_to_date /
update_recommended / update_required / dev_build), notice text builders,
24h recommended-notice snooze helper. Bootstrap schema gains optional
`agent_version_policy` (extra="ignore" keeps old/new servers
compatible). The supervisor's existing best-effort bootstrap fetch
(renamed `_fetch_bootstrap`) now also emits at most ONE `update_notice`
event per run start — recommended tier throttled via the new
`State.last_update_notice_at` (state field-set lock consciously
widened), below-minimum always shown but STILL notify-only (streaming
proceeds; refusal is M3). Interactive shell prints the same notice once
per session after login/refresh. Dev builds (`X.Y.Z`-unparseable or
`UPSTREAM_AGENT_DEV=1`) and offline runs are completely silent — offline
behavior is byte-identical to before.

**Tests:** tests/test_update_check.py (10): parser/ordering truth
tables, dev detection, policy matrix, notice text, snooze window, and
supervisor integration (one notice + still streams; silent when
current/no-policy; snooze respected; offline unchanged). 431 passing.

**Server counterpart:** upstream-api-server ships the policy on
GET /v1/agents/bootstrap via five optional UPSTREAM_AGENT_* env settings.

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

## 2026-06-11 - docs: fix two stale 5 s cadence mentions + CI hardening

### Summary
Follow-up to the 5 s -> 15 s heartbeat change (157c46e). Review found
two doc paragraphs that still said "5-second cadence"; both are now
"15-second". The previous entry's test count was also wrong (claimed
419 passed; the suite passes 421) and is corrected. The CI workflow
gets standard hardening: a read-only token, a concurrency-cancel
group, and push triggers scoped to `main` so in-repo PR branches no
longer run every job twice (once for `push`, once for `pull_request`).

### Files touched
- `docs/api-contracts.md` — `POST /v1/heartbeats` section:
  "5-second cadence" -> "15-second cadence".
- `docs/architecture.md` — Milestone 6 "Runtime health" paragraph:
  "5-second cadence" -> "15-second cadence".
- `docs/commit-log.md` — 157c46e entry: test count corrected to
  421 passed; correction note appended about the two doc stragglers.
- `.github/workflows/ci.yml` — added `permissions: contents: read`;
  added `concurrency` group `${{ github.workflow }}-${{ github.ref }}`
  with `cancel-in-progress: true`; `push` now scoped to
  `branches: [main]`.

### Functions/classes changed
- None — docs and CI configuration only.

### System interaction notes
No runtime behavior changes. CI behavior changes: pushes to
non-`main` branches rely on the `pull_request` trigger (open a PR to
get CI); superseded runs on the same ref are cancelled; the
workflow's `GITHUB_TOKEN` can no longer write to the repo.

### Testing/checks
- `uv run pytest -q` — 421 passed.
- Re-grepped the whole repo for remaining 5-second heartbeat
  mentions; only unrelated constants (camera settle/validation
  windows, FFmpeg stop timeout) and historical commit-log entries
  remain.
- Workflow YAML changes are declarative; verified against GitHub
  Actions schema conventions, exercised on next push to `main`.

### Follow-up TODOs
- None beyond the existing CI matrix / lint TODOs in the previous
  entry.

---

## 2026-06-11 - ops: heartbeat interval 5 s -> 15 s + minimal CI workflow

### Summary
Two independent quick wins from the platform roadmap:

1. **Heartbeat cadence 5 s -> 15 s.** `DEFAULT_HEARTBEAT_INTERVAL_S`
   in `heartbeat/reporter.py` goes from `5.0` to `15.0`. At fleet
   scale (1000 agents) this cuts control-plane heartbeat writes from
   ~200/s to ~67/s. This is the constant-only change; the
   server-tunable interval (delivered via the `/start` response) is
   future work and is intentionally **not** built here — the backend
   still returns no next-interval directive, so the agent keeps a
   fixed value.
2. **CI bootstrap.** New `.github/workflows/ci.yml`: on every push
   and pull request, ubuntu-latest sets up Python 3.11, installs the
   package with `pip install -e ".[dev]"` (the README's documented
   dev install), and runs `pytest -q`. Minimal by design; lint/type
   gates can be layered on later. No system packages are needed:
   the only native dependency (`cv2`) is imported lazily inside
   `WebcamSource._import_cv2` and every test stubs it.

### Files touched
- `upstream_agent/heartbeat/reporter.py` —
  `DEFAULT_HEARTBEAT_INTERVAL_S: float = 15.0` (was `5.0`). No other
  logic changes; `interval_s` stays constructor-injectable.
- `tests/test_heartbeat_reporter.py` —
  `test_reporter_stop_joins_quickly` now uses `interval_s=15.0` as
  its "long sleep" interval (was `5.0`) and the comment matches; the
  test still asserts `stop()` returns in well under the interval.
- `.github/workflows/ci.yml` — new; push + pull_request, single
  ubuntu-latest job (checkout -> setup-python 3.11 ->
  `pip install -e ".[dev]"` -> `pytest -q`).
- `docs/api-contracts.md` — Milestone 6 note: heartbeat cadence now
  reads "every 15 seconds".
- `docs/architecture.md` — Milestone 6 paragraph: same 5 -> 15 fix.
- `docs/module-map.md` — `heartbeat/reporter.py` row: interval
  default now documented as `15.0`.
- `docs/troubleshooting.md` — heartbeat section: cadence, retry-tick,
  `reported_at` spacing, dashboard "online" threshold, and the
  network-drop warning rate all updated from 5 s to 15 s.

### Functions/classes changed
- `DEFAULT_HEARTBEAT_INTERVAL_S` (module constant) — the only code
  change. `HeartbeatReporter` itself is untouched; its loop already
  sleeps on `stop_event.wait(self._interval_s)`, so `stop()` still
  wakes the thread immediately regardless of interval length.

### System interaction notes
Dashboards or monitors that infer "online" from heartbeat recency
must tolerate ~15 s gaps (the server-side healthy window is already
wider than that). Transient-failure warnings in `agent.log` now
appear at most every 15 s instead of every 5 s. Nothing else in the
runtime keys off the heartbeat interval — supervisor fatal-reason
polling is per-frame, not per-heartbeat.

### Testing/checks
- `pytest -q` — 421 passed.
- Grepped code + tests + docs for remaining 5-second heartbeat
  assumptions; the only remaining "5 s" mentions are unrelated
  constants (camera validation window, FFmpeg stop timeout) and
  historical commit-log entries, which were left as written.
- Correction (follow-up commit): the grep above missed two doc
  stragglers — "5-second cadence" in `docs/api-contracts.md`
  (`POST /v1/heartbeats` section) and in `docs/architecture.md`
  (Milestone 6 "Runtime health" paragraph). Both fixed to 15-second
  in the follow-up commit.
- The workflow itself runs on the first push to GitHub; not
  executable locally.

### Follow-up TODOs
- Server-tunable heartbeat interval via the `/start` response
  (paired with the persistent supervisor-scoped reporter and the
  `"reconnecting"` status work).
- CI: add `ruff check` / `mypy` jobs and a Python-version matrix
  (3.10–3.12) once the test job has baked.

---

## 2026-06-10 - cleanup: webcam-only input + design-doc refresh

### Summary
A gap-analysis audit of the M7 v0.1.0 codebase surfaced one
user-visible trap and several deferred items the design docs did
not call out honestly:

1. **The RTSP trap.** `CameraConfig.source_type` accepted
   `Literal["webcam", "rtsp"]`, the default YAML template hinted at
   network-camera support, and `examples/rtsp.yaml` shipped as a
   ready-to-copy template — but no `RtspSource` ever existed and
   `diagnostics/camera.py` rejected the value at runtime with a
   "not supported yet" error. A user who followed the example
   crashed the agent rather than getting a clean config-load
   failure.
2. **Honest doc gaps.** Items the M7 summary flagged as future
   work (frame buffer, proactive session refresh from
   `expires_at`, token rotation, session-independent reporter for
   the `"reconnecting"` heartbeat status) were referenced
   inconsistently across `architecture.md`, `module-map.md`,
   `troubleshooting.md`, and `api-contracts.md`.

The agent is **webcam-input only by design** and SRT is the only
egress protocol. This commit tightens the schema to match,
removes the RTSP template and example, and rewrites the design
docs so the next coder/agent has an accurate map of what's done
vs. what's M8 candidate.

### Files touched
- `upstream_agent/config.py` — `source_type` is now
  `Literal["webcam"]`. Pydantic now rejects any other value at
  config load with a structured `ConfigError`. The default
  `_TEMPLATE` lost its RTSP comments.
- `upstream_agent/diagnostics/camera.py` — deleted the
  `if cfg.source_type != "webcam"` short-circuit. Unreachable
  after the schema change.
- `upstream_agent/sources/__init__.py`,
  `upstream_agent/sources/base.py` — module docstrings no longer
  name RTSP/IP-camera/file as planned sources. The `VideoSource`
  ABC stays in place for hypothetical future test sources.
- `examples/rtsp.yaml` — deleted.
- `README.md` — example list now mentions only `webcam.yaml`.
- `tests/test_sources_webcam.py` — the defensive-constructor test
  now uses `CameraConfig.model_construct(...)` to bypass
  Pydantic, so it can still exercise the
  `UnsupportedCameraSourceError` raise inside `WebcamSource`.
- `tests/test_diagnostics_camera.py` — deleted
  `test_unsupported_source_type_short_circuits_without_constructing_source`;
  the orchestrator no longer has a runtime guard to test.
- `tests/test_cli.py` — `test_check_camera_unsupported_source_type_exits_one`
  rewritten: writes a config with `source_type: "rtsp"` and now
  asserts the CLI surfaces the Pydantic-level error
  (`Error ... camera.source_type ... webcam`) rather than the
  old runtime "not supported yet" string.
- `docs/architecture.md`
  - ASCII flow diagram source box: `(webcam, rtsp)` → `(webcam)`,
    and labels the frame buffer as `(planned)`.
  - Layer-status table: split the old "RTSP / IP camera / file
    sources | planned" row into "Non-webcam input sources | not
    planned" plus three new M8-candidate rows (frame buffer,
    session refresh/token rotation/config fetch,
    session-independent reporter for `"reconnecting"`).
  - New section "Honest gaps and planned work (M8 candidates)"
    near the bottom listing each deferred item with the specific
    code references that prove it's not yet implemented.
- `docs/module-map.md`
  - `sources/base.py` row no longer names RTSP/file/IP-camera.
  - Deferred-submodules table now lists only the genuinely
    deferred paths (`buffer/frame_buffer.py`,
    `api/config_fetch.py`, `diagnostics/network.py`) and adds an
    explicit "Out of scope: non-webcam input sources" callout.
  - Removed the `rtsp.yaml` row from the `examples/` table.
- `docs/troubleshooting.md` — under
  `Error (register_camera)`, removed the now-obsolete
  `source_type: "rtsp"` bullet and added a parenthetical noting
  that `source_type` is locked at config-load time, so an
  invalid value can never reach `register_camera`.
- `docs/api-contracts.md`
  - Added a "Received but not yet acted on — `expires_at`"
    paragraph to the `start_stream_session` section explaining
    that the agent receives `expires_at` but does not yet
    schedule a proactive refresh (M8 candidate, link to
    `architecture.md`).
  - Extended the heartbeat `status` field description to make
    explicit that `"reconnecting"` is reserved in the contract
    but not currently emitted (reporter is session-scoped).

### Functions/classes changed
- `CameraConfig` (`upstream_agent/config.py`) —
  `source_type: Literal["webcam"]`. The class is otherwise
  unchanged; downstream callers that branched on `source_type`
  no longer need to (only the defensive raise inside
  `WebcamSource.__init__` survives, and it's only reachable via
  `CameraConfig.model_construct`).
- `run_camera_check` (`upstream_agent/diagnostics/camera.py`) —
  shorter; goes straight to the `WebcamSource` path. Behaviour
  unchanged for valid configs.

### System interaction notes
For a valid config the runtime is identical. For an invalid
`source_type` the failure point moves from
`diagnostics/camera.py` (a `CameraCheckResult(success=False, ...)`
returned by the orchestrator at runtime) to `config.load_config`
(a `ConfigError` raised before any camera, source, or API code
runs). The CLI's top-level `UpstreamError` handler surfaces it
as `Error: Invalid config in <path>:\n  - camera.source_type:
Input should be 'webcam'`. Users now see the problem at the
earliest possible moment, in a structured format that names the
field and the allowed value.

The design-doc changes do not affect runtime behaviour at all.
They make the existing M7 honesty stance (about `expires_at`,
`reconnecting`, the lack of a frame buffer, etc.) visible to
anyone reading the docs rather than only to anyone reading the
M7 summary or the code comments.

### Testing/checks
- `pytest` — the four updated test files
  (`test_sources_webcam.py`, `test_diagnostics_camera.py`,
  `test_cli.py`, plus the pre-existing
  `test_config.py:test_config_error_message_never_contains_input_token`)
  cover the schema-level rejection path end-to-end.
- `upstream init` — confirms the template no longer mentions
  rtsp.
- Manual smoke: writing `source_type: "rtsp"` into a config and
  running `upstream check --camera` produces the new Pydantic
  error on stderr and exits 1 without consulting OpenCV.
- `grep -rni rtsp upstream-agent/` — only commit-log history and
  intentional out-of-scope callouts in `architecture.md` /
  `module-map.md` remain (plus two tests that use `rtsp://` to
  prove the SRT URL parser rejects non-SRT schemes, which is
  about egress and unrelated to source types).

### Follow-up TODOs
The audit identified four M8 candidates. They are now documented
in detail in `architecture.md` "Honest gaps and planned work
(M8 candidates)":
- Frame buffer (`buffer/frame_buffer.py`) — consume
  `StreamConfig.buffer_ms`, drain on reconnect.
- Proactive session refresh keyed off `expires_at` — outer-loop
  timer that swaps in a fresh session before expiry without
  tearing down camera or encoder.
- Token rotation — heartbeat-response signal handling + atomic
  config rewrite.
- Session-independent heartbeat reporter — lift the reporter
  into the outer recovery loop so it can emit `"reconnecting"`
  during backoff gaps.

---

## 2026-06-10 - style: brand-themed interactive shell + logo on every launch

### Summary
The interactive shell now wears the brand palette top to bottom — green
`#344C2C` as the main color (logo, panel borders, headings, command
names, healthy states), blue `#4B816C` for informational text (labels,
descriptions, secondary values), gold `#81764B` for highlights and
in-between states (usage counts, unassigned cameras, starting/
reconnecting workers, hints). Errors stay red — semantic, not brand.
The existing ASCII logo (`branding.LOGO`) prints in brand green on
**every** shell launch; the one-time plain welcome moved from `main()`
into the subcommand path of the CLI callback so the first bare launch
doesn't show the logo twice and scriptable subcommands keep their
first-run welcome exactly as before.

### Files touched
- `upstream_agent/branding.py` — `BRAND_GREEN`/`BRAND_BLUE`/`BRAND_GOLD`
  constants + `logo_text()` (Rich Text, green mark, bold-green wordmark).
- `upstream_agent/interactive/shell.py` — palette applied everywhere:
  logo + banner, all five panels, key/value + camera/stream tables,
  worker-state colors via `_state_markup`, command-name accents.
- `upstream_agent/cli.py` — `maybe_print_welcome()` moved out of
  `main()` into the callback's subcommand branch.
- `tests/test_interactive_shell.py` — logo-on-launch test + a truecolor
  test asserting the exact RGB escape codes for all three brand colors.

### Functions/classes changed
- `logo_text()` — new; `_state_markup` / `_style_kv_table` — small
  styling helpers in shell.py.

### System interaction notes
Rich truecolor renders the hex values exactly in modern terminals and
degrades to nearest-color elsewhere; non-TTY output (pipes, tests with
`force_terminal=False`) is plain text, so nothing scripted changes.

### Testing/checks
- `pytest` — 421 passed (2 new). Visual smoke with COLORTERM=truecolor
  confirmed the exact RGB escape sequences.

### Follow-up TODOs
- None.

## 2026-06-10 - feat: interactive `upstream` shell + server-controlled stream config (smart flow)

### Summary
Two layers. **Smart flow:** the client gains `get_bootstrap()` /
`claim_camera()`; `run_agent` fetches account limits once per run (best
effort — older servers 404 and the pre-check is skipped) and
`preflight.plan_stream_config` clamps requested
width/height/fps/bitrate to those caps before the session request,
emitting `limits_precheck` and a `Requested/Approved/Action` decision
line. Disallowed codec/source-type raise `PreflightError` early; camera
registration now sends `source_type`. A `limit_exceeded.*` rejection
emits `limits_rejected` and one best-effort blocked-by-limits heartbeat
(`status="error"`, `last_error="blocked by limits: …"`). The server
stays the enforcement point throughout. **Interactive mode:** bare
`upstream` opens a REPL (`interactive/shell.py`) — `login` reads the
token hidden, verifies via bootstrap before `config.save_api_token`
persists it; status/account/limits/cameras/streams render from cached
bootstrap; `start` claims a dashboard-created camera, builds its
CameraConfig from the server record (`server_config.py`), and streams on
a background `StreamWorker` thread (`run_agent` gains an optional
external `shutdown` Event); logs go to a redaction-filtered
`RingBufferHandler` + file (`setup_logging(console=False,
extra_handlers=…)`) so nothing scrolls over the prompt.

### Files touched
- `api/schemas.py`, `api/client.py` — bootstrap/claim models + methods;
  `CameraRegisterRequest.source_type`.
- `preflight.py`, `server_config.py`, `interactive/` (worker.py,
  shell.py) — new.
- `supervisor.py` — bootstrap fetch, planned-config threading, decision
  line, blocked-by-limits heartbeat, external shutdown param.
- `logging_setup.py` — RingBufferHandler, console/extra_handlers opts.
- `config.py` — save_api_token (YAML-preserving; drops template
  comments). `errors.py` — PreflightError. `cli.py` — no-args callback
  launches the shell. `diagnostics/api.py` — source_type.
- Tests: 6 new files (preflight, api bootstrap, supervisor precheck,
  log buffer, worker, shell) + source_type/bootstrap-fake updates.
- README.md, docs/api-contracts.md, docs/architecture.md.

### Functions/classes changed
- `run_agent(..., shutdown: threading.Event | None = None)` — external
  Event lets a host thread stop the run; default behavior unchanged.
- `StreamWorker`/`WorkerRegistry` — name-keyed, N-ready for the
  multi-camera follow-up (upstream-agent#2).
- `InteractiveShell` — fully injectable (input_fn/password_fn/
  client_factory/run_fn/Console) for TTY-free tests.

### System interaction notes
Behavior change: bare `upstream` opens the shell instead of printing
help (with piped/closed stdin it EOFs and exits 0, so automation can't
hang). New agent vs old server degrades cleanly (bootstrap 404 →
pre-check skipped). Token never echoed/logged — redaction filter applies
to the ring buffer like every handler.

### Testing/checks
- `pytest` — 413 passed (46 new). Manual smoke in an isolated $HOME:
  shell opens, help/status render, quit exits 0.

### Follow-up TODOs
- RTSP capture is still a stub; shell surfaces an honest error for rtsp
  cameras until upstream-agent#8 lands.
- Shell-path agent registration sends empty capabilities (refreshed on
  the next supervisor registration).

## 2026-05-14 - cleanup: honest recovery logging + macOS camera limitation

### Summary
Fourth and final pass on the M7 live smoke. Goal was to audit
the recovery code, remove patches that only labelled things,
and make the remaining behaviour honest rather than
optimistic. No new features. No new API endpoints. No new YAML
knobs.

The user reported two persistent issues:

1. **Wi-Fi/network recovery logs claim the stream is back
   online, but the dashboard video does not always come back.**
   The previous patches had added a stack of cosmetic helpers
   that did not change behaviour — they only labelled
   failures. The real problem is that the agent's strongest
   remote signal (`GET /v1/stream-sessions/{id}` →
   `state="streaming"`) only proves *initial* MediaMTX ingest.
   The API has no signal for ongoing dashboard playback.
2. **Camera reopen never recovers on real hardware.** The
   in-process recovery loop is structurally correct (fresh
   `WebcamSource` + fresh `cv2.VideoCapture` per attempt, with
   settle + windowed validation), but macOS / AVFoundation
   caches device handles per-process. When an iPhone
   Continuity Camera leaves range, `cv2.VideoCapture(N)` still
   "opens" but every read returns no frame, *forever*. No
   in-process action can flush that cache. Process restart is
   the only honest fix.

This change is mostly **deletions** and **honest rewording**,
plus one small camera improvement: explicit
`cv2.CAP_AVFOUNDATION` on macOS for backend determinism and a
new `camera_unavailable_hint` event after N failed reopen
cycles that tells the operator to restart the agent.

### Files touched
- `upstream_agent/runtime/stats.py` — dropped `RuntimePhase`,
  `set_phase`, `_phase`, and `StatsSnapshot.phase`. Reverted
  the snapshot to the M6-era shape.
- `upstream_agent/runtime/__init__.py` — dropped `RuntimePhase`
  export.
- `upstream_agent/heartbeat/reporter.py` — replaced the
  `_phase_to_status(snapshot)` helper with the M6-era inline
  `status = "streaming" if snapshot.ffmpeg_running else "error"`.
  The schema literal `"reconnecting"` stays (harmless,
  forward-compatible) but the agent's code paths no longer
  emit it. Updated the module docstring to be honest about
  why: the reporter is session-scoped and torn down between
  recovery attempts, so a `"reconnecting"` heartbeat would
  require a session-independent reporter that is out of M7
  scope.
- `upstream_agent/supervisor.py`
  - Removed the entire `_NETWORK_FAILURE_PATTERNS` /
    `_NETWORK_FAILURE_EXIT_CODES` / `_looks_like_network_failure`
    / `_failure_tag` block. The classifier was logging-only;
    both kinds of recoverable stream failures retry
    identically.
  - Removed the `network_failure` event emission. The
    `reconnecting` event already carries the same
    information via `outcome.error_message` (which includes
    the scrubbed FFmpeg stderr snippet).
  - Removed the `stream_resumed` event and the
    `had_recoverable_failure` / `recovery_cycles` bookkeeping
    that fed it.
  - Removed every `stats.set_phase(...)` call.
  - Rewrote the `waiting_for_ingest` / `ingest_confirmed` /
    `streaming` / `reconnecting` event messages so they
    state what the agent can prove and nothing more.
    Specifically:
    - `waiting_for_ingest`: "FFmpeg started; waiting for API
      to report session streaming"
    - `ingest_confirmed`: "API reports session streaming
      (MediaMTX confirmed initial ingest). Dashboard playback
      is not directly verified."
    - `streaming`: "Streaming started (frames flowing to
      streaming server). Press Ctrl+C to stop."
    - `reconnecting`: "Recoverable failure; cleanup complete,
      will retry. {error_message}" — no `kind=...` tag.
  - Added `_CAMERA_UNAVAILABLE_HINT_AFTER = 3` constant and
    `camera_unavailable_hint_after` kwarg on
    `_reopen_camera_with_backoff` and `run_agent`.
  - After N consecutive failed reopen cycles inside
    `_reopen_camera_with_backoff`, emit a single
    `camera_unavailable_hint` event:
    `"Camera device has opened but failed to produce frames
    for {N} consecutive recovery cycles. On macOS / Continuity
    Camera this often requires restarting the agent. The
    agent will keep retrying."` Fires at most once per
    recovery cycle.
- `upstream_agent/sources/webcam.py` — on macOS pass the
  explicit `cv2.CAP_AVFOUNDATION` backend to `VideoCapture`
  for backend determinism. Defensive: `getattr(cv2,
  "CAP_AVFOUNDATION", None)` so the test fakes that lack the
  constant still work, and non-macOS platforms keep OpenCV's
  default backend selection.
- `tests/test_runtime_stats.py` — dropped
  `test_set_phase_round_trip`; removed the `phase == "idle"`
  line from `test_initial_snapshot_is_empty`.
- `tests/test_heartbeat_reporter.py` — dropped
  `test_reporter_status_is_reconnecting_when_phase_set` and
  `test_reporter_status_is_error_when_phase_stopping`. The
  shared `_make_reporter` helper no longer calls `set_phase`.
- `tests/test_supervisor_recovery.py`
  - Deleted the network-classification tests (5):
    `test_ffmpeg_io_error_is_classified_as_network_failure`,
    `test_ffmpeg_exit_code_251_classified_as_network`,
    `test_generic_ffmpeg_failure_is_not_classified_as_network`,
    `test_network_failure_triggers_fresh_session_restart`,
    `test_no_secret_leak_in_network_failure_events`.
  - Deleted the cosmetic `stream_resumed` tests (2):
    `test_stream_resumed_event_fires_after_recoverable_failure`,
    `test_stream_resumed_does_not_fire_on_clean_first_run`.
  - Replaced
    `test_phase_flips_to_reconnecting_between_attempts` with
    `test_reporter_is_torn_down_between_attempts` (now the
    honest assertion: one reporter per attempt, each started
    and stopped exactly once).
  - Added `test_ingest_confirmed_event_uses_honest_message` —
    asserts the new wording and the absence of "Remote ingest
    confirmed".
  - Added
    `test_streaming_event_message_does_not_claim_dashboard_visibility`
    — asserts the new "Streaming started (frames flowing to
    streaming server)" wording.
  - Added `test_camera_unavailable_hint_fires_after_N_failed_cycles`
    — five reopen cycles with a `fail_all_reads=True` camera
    fire the hint exactly once, then the recovery proceeds to
    a healthy camera and streaming resumes.
- `docs/architecture.md` — replaced the
  "Runtime recovery (Milestone 7)" section with a shorter,
  honest version: "What the agent CAN prove" / "What the
  agent CANNOT prove" / recovery loop / camera contract /
  macOS Continuity Camera limitation / Ctrl+C semantics /
  hidden test knobs / heartbeat status mapping.
- `docs/module-map.md` — dropped `RuntimePhase` from the
  `runtime/__init__.py` and `runtime/stats.py` rows. Updated
  `heartbeat/reporter.py` to describe the simpler status
  mapping. Updated `supervisor.py` to mention the new
  `camera_unavailable_hint_after` kwarg and drop references
  to deleted helpers. Updated `sources/webcam.py` to mention
  the explicit AVFoundation backend on macOS.
- `docs/troubleshooting.md` — rewrote the "Camera unplug" and
  "Remote ingest confirmation" sections with the new event
  wording. Added a "macOS Continuity Camera limitation"
  subsection that explicitly says restart-the-agent is the
  honest fix. Added a "Why dashboard playback may not match
  agent logs" subsection.
- `docs/api-contracts.md` — added an M7.3 note on the
  freshness of the `state` field: the per-session state is
  fresh-by-construction but sticky (no TTL), and the agent's
  `ingest_confirmed` event explicitly does not claim ongoing
  dashboard visibility.

### Functions/classes changed
- `RuntimePhase`, `RuntimeStats.set_phase`,
  `StatsSnapshot.phase` — removed.
- `_phase_to_status` (in `heartbeat/reporter.py`) — removed;
  replaced by inline status mapping.
- `_NETWORK_FAILURE_PATTERNS`,
  `_NETWORK_FAILURE_EXIT_CODES`,
  `_looks_like_network_failure`, `_failure_tag` — all removed
  from `supervisor.py`.
- `_CAMERA_UNAVAILABLE_HINT_AFTER` (new constant in
  `supervisor.py`).
- `_reopen_camera_with_backoff` — added
  `unavailable_hint_after` kwarg and the
  `camera_unavailable_hint` event emission.
- `run_agent` — added `camera_unavailable_hint_after` kwarg;
  removed the network-failure event emission and the
  `stream_resumed` / `had_recoverable_failure` /
  `recovery_cycles` bookkeeping.
- `WebcamSource.open` — passes `cv2.CAP_AVFOUNDATION` on
  macOS when the constant is available.

### System interaction notes
The `reconnecting` heartbeat status is no longer emitted by
the agent's code. The literal stays in the schema for forward
compatibility (a future, session-independent reporter could
emit it without an API change). Today every heartbeat is
either `streaming` (FFmpeg up) or `error` (FFmpeg down or
torn down).

The `camera_unavailable_hint` event fires at most once per
recovery cycle (the `hint_emitted` flag inside
`_reopen_camera_with_backoff` is local to each invocation).
A camera that briefly comes back and then disappears again
will re-enter the helper and the counter resets — that is the
intended behaviour: each cycle is its own diagnostic.

The macOS `CAP_AVFOUNDATION` change is a no-op on Linux/CI
because `getattr(cv2, "CAP_AVFOUNDATION", None)` returns
`None` outside macOS / the existing test fakes. Production
macOS gets deterministic backend selection.

### Testing/checks
- `pytest -q` — 367 tests pass (down from 374: removed 7
  cosmetic tests, added 3 honest-wording + camera-hint
  tests).
- `ruff check upstream_agent tests` — 5 baseline warnings
  unchanged (4 × B008 typer.Option, 1 × I001 in
  `tests/test_token_redaction.py`). No new warnings.
- Manual smoke (still to run on the macbook with live API +
  MediaMTX):
  1. Unplug webcam mid-stream. After 3 reopen cycles with no
     frames produced, expect exactly one
     `camera_unavailable_hint` line. The agent keeps
     retrying; replug a USB camera and confirm it eventually
     recovers; for Continuity Camera, expect the hint message
     to be the right advice.
  2. Drop Wi-Fi mid-stream. Expect the new event wording:
     `reconnecting` (no `kind=...`) → `reconnect_wait` →
     `api_approved` → `ffmpeg_started` →
     `waiting_for_ingest` (new message) → `ingest_confirmed`
     (new message — does not claim dashboard visibility) →
     `streaming` (new message — does not claim dashboard
     visibility).
  3. `kill -9 <ffmpeg-pid>` → expect the same recovery flow.
  4. Ctrl+C during a backoff → immediate clean exit.
  5. `grep -E 'srt://|streamid|session_token' ~/.upstream/logs/*`
     — must be empty.

### Follow-up TODOs
- The macOS Continuity Camera limitation is a known
  in-process recovery gap and is now documented honestly in
  `docs/architecture.md` and `docs/troubleshooting.md`. There
  is no smaller code fix; the only paths forward would be
  out-of-process recovery (subprocess + restart-on-failure)
  or a streaming-server-side push channel.
- The `reconnecting` heartbeat status is not emitted today.
  If product wants the dashboard to show a distinct
  "reconnecting" badge during recovery, the agent would need
  a session-independent reporter. Out of scope for M7.

---

## 2026-05-14 - fix: remote ingest confirmation + settle/windowed camera validation

### Summary
Third pass on the M7 live smoke. Two issues surfaced when
testing against a real backend + dashboard:

1. **Network/SRT recovery is not remotely verified.** After
   FFmpeg restart, the agent emitted "Streaming started" and
   marked the heartbeat as streaming — but the dashboard
   stream often did not come back. The root cause: local
   FFmpeg startup is not proof of remote ingest. MediaMTX
   must accept the SRT publish and fire its `runOnReady`
   webhook before the API session state transitions from
   `approved` to `streaming`, and only then can the dashboard
   serve the stream. The agent now polls
   `GET /v1/stream-sessions/{id}` after FFmpeg starts and
   waits for `state == "streaming"` before declaring success.
2. **Camera recovery still loops on real hardware.** The
   3-consecutive-reads validator added in the previous patch
   was still too strict: USB / Continuity / V4L2 cameras can
   take a couple of seconds to actually warm up and produce
   frames, and they sometimes drop a transient read in the
   middle of a settling stream. The validator now waits a
   settle period after open, then accepts up to a window of
   real time during which it can retry past transient
   failures, only giving up if the window expires.

No new API endpoint was needed for Issue 1 — `GET
/v1/stream-sessions/{session_id}` was already shipped by the
API server; the agent simply did not call it. The new client
method (`get_stream_session`) reuses the existing typed-error
mapping. No API contract changes; no new YAML config.

### Files touched
- `upstream_agent/api/client.py`
  - Added `STREAM_SESSION_GET_PATH_TEMPLATE =
    "/v1/stream-sessions/{session_id}"`.
  - Added `get_stream_session(session_id) -> Session`
    method. Returns the flat session record (the existing
    `Session` schema, which already had a `state` field).
  - Extended `_request` to accept `body: BaseModel | None`
    so GET-style requests omit `Content-Type` and the JSON
    body. Existing POST callers unchanged.
- `upstream_agent/api/__init__.py` — re-exports
  `STREAM_SESSION_GET_PATH_TEMPLATE`.
- `upstream_agent/supervisor.py`
  - New constants: `_INGEST_CONFIRM_TIMEOUT_S = 15.0`,
    `_INGEST_POLL_INTERVAL_S = 0.5`,
    `_CAMERA_RECOVERY_SETTLE_S = 1.5`,
    `_CAMERA_RECOVERY_VALIDATION_WINDOW_S = 5.0`,
    `_CAMERA_RECOVERY_READ_RETRY_S = 0.1`,
    `_CAMERA_RELEASE_SETTLE_S = 0.5`.
  - New internal `_IngestSkip(BaseException)` sentinel for
    "shutdown during ingest wait" so the supervisor's blanket
    `except Exception` does not classify shutdown as a
    recoverable failure.
  - New `_wait_for_remote_ingest(...)` helper. Pumps frames
    into FFmpeg while polling `client.get_stream_session` at
    the configured interval until `state == "streaming"` or
    the timeout fires. Transient API failures
    (`ApiConnectionError` / `ApiServerError` /
    `ApiRateLimitError` / `ApiContractError`) emit an
    `ingest_poll_transient_error` event and keep polling.
    Fatal API errors propagate. Terminal session states
    (`ended` / `expired` / `failed`) raise a
    `StreamingRuntimeError` flagged for recoverable retry.
    FFmpeg or camera failures during pre-warm propagate via
    their existing typed-error paths.
  - `_run_attempt` now calls `_wait_for_remote_ingest`
    between `ffmpeg.start()` and the heartbeat reporter
    start. The reporter only starts after `ingest_confirmed`;
    `streaming` only fires after the reporter starts.
    Pre-warm frames count toward the attempt's
    `frames_written` so `max_frames` still works.
  - Replaced the consecutive-read validator with a windowed
    `_validate_camera_reads(target, window_s, retry_s, ...)`
    that allows transient failures inside the window and
    only gives up on window-expiry. Emits
    `camera_validation_read_failed` per transient failure
    and `camera_validation_timeout` on window-expiry.
  - `_reopen_camera_with_backoff` now waits
    `_CAMERA_RECOVERY_SETTLE_S` after every successful
    `open()` and calls the windowed validator. Between
    reopen attempts it also waits `_CAMERA_RELEASE_SETTLE_S`
    so the driver fully releases the device. All wait
    points are interruptible via the shared shutdown event.
    A `try/finally` inside the helper ensures the camera is
    closed even if `KeyboardInterrupt` raises from the
    validator's read.
  - `run_agent` exposes `ingest_timeout_s`,
    `ingest_poll_interval_s`, `camera_settle_s`,
    `camera_validation_window_s`, `camera_validation_frames`,
    `camera_read_retry_s`, `camera_release_settle_s` as
    optional kwargs so tests can dial these down to zero or
    sub-second values without changing module-level defaults.
- `tests/test_api_client.py` — 6 new tests for
  `get_stream_session`: happy path returns flat Session
  with `state`, sends GET without Content-Type/body, sends
  Bearer + Accept JSON, 404 → `ApiValidationError`,
  401+`token_revoked` → `ApiTokenRevokedError`, 5xx →
  `ApiServerError`, network → `ApiConnectionError`, and a
  token-leak guard on the error path.
- `tests/test_supervisor.py` — `FakeApiClient.get_stream_session`
  added (default `state="streaming"`, FIFO-list overrideable
  via the `get_session_states` constructor kwarg).
- `tests/test_supervisor_recovery.py`
  - `FlakyApiClient.get_stream_session` added — supports
    `get_session_states` FIFO, `get_session_raises` FIFO, and
    `timeout_first_session` for clean two-attempt tests.
  - `RecoveringCamera` gained `fail_all_reads=True` and
    `keyboard_interrupt_after=N` knobs.
  - `_FAST_INGEST` and `_FAST_CAMERA_TIMINGS` helpers so
    recovery tests don't wall-sleep.
  - 8 new ingest-wait tests:
    - `test_ingest_state_streaming_starts_main_loop_and_heartbeat`
    - `test_streaming_event_is_not_emitted_before_ingest_confirmed`
    - `test_ingest_timeout_triggers_recoverable_retry`
    - `test_ingest_transient_api_error_keeps_polling`
    - `test_ingest_fatal_api_error_is_fatal`
    - `test_ingest_terminal_state_is_recoverable`
    - `test_keyboard_interrupt_during_ingest_wait_propagates`
    - `test_ingest_wait_polls_multiple_times_before_confirm`
    - `test_no_secrets_in_ingest_events`
  - 3 new camera-validation tests (replacing the older
    "abort on first failed read" ones, which described the
    pre-windowed behaviour):
    - `test_camera_open_succeeds_but_validation_fails_does_not_start_session`
    - `test_camera_validation_tolerates_transient_failures_within_window`
    - `test_repeated_validation_failures_do_not_tight_loop`
  - 2 new camera-shutdown tests:
    - `test_initial_camera_open_failure_is_fatal`
    - `test_keyboard_interrupt_during_validation_window_propagates`
- `docs/api-contracts.md` — new
  "`GET /v1/stream-sessions/{session_id}`" section
  documenting the request shape (no body, Bearer auth, no
  query params required), the flat response schema, the
  state-enum table (`approved` / `streaming` / `ended` /
  `expired` / `failed`), and the error mapping. M7.2 note
  added up top.
- `docs/architecture.md` — "Camera recovery requires open +
  multi-frame validation" subsection rewritten to describe
  the settle + windowed validation contract. New
  "Remote ingest must be confirmed before declaring
  streaming" subsection with the failure-mode table.
- `docs/troubleshooting.md` — "Camera unplug / reconnect"
  section rewritten with the new event flow including
  `camera_settle`, `camera_validation_read_failed`,
  `camera_validation_timeout`, `camera_validated`. New
  "Remote ingest confirmation" subsection covering the
  `waiting_for_ingest` / `ingest_confirmed` lifecycle and
  the `remote_ingest_timeout` recoverable path.
- `docs/module-map.md` — `api/client.py` row updated for
  `get_stream_session` and the new path constant.
  `supervisor.py` row updated for the new kwargs and the
  multi-phase recovery contract.

### Functions/classes changed
- `UpstreamApiClient.get_stream_session(session_id) -> Session`
  (new) — GET-style call to the existing session-state
  endpoint.
- `UpstreamApiClient._request` — `body` is now
  `BaseModel | None`; a `None` body omits Content-Type and
  the JSON payload so the request goes out as a clean GET.
- `_wait_for_remote_ingest(...)` (new) — pre-warm + poll
  helper. Bounded, interruptible, leak-free.
- `_validate_camera_reads(target, window_s, retry_s, ...)`
  (replaced) — windowed validator tolerant of transient
  read failures.
- `_reopen_camera_with_backoff` — added settle + windowed
  validation + release-settle. `try/finally` around the
  camera so `KeyboardInterrupt` from the validator does
  not leak the open device.
- `_IngestSkip(BaseException)` (new) — control-flow sentinel
  for shutdown during ingest wait.

### System interaction notes
The ingest wait writes frames into FFmpeg during the pre-warm
phase — MediaMTX needs an active SRT publish before its
`runOnReady` webhook fires. The pre-warm frame rate matches
whatever the camera's `read_frame` produces (≈30 fps in
production). The wait typically completes in 1–3 polls (≤1.5 s)
on a healthy network; the 15 s timeout exists to bound the
worst case where MediaMTX is rejecting the publish.

If the timeout fires, the supervisor tears down the attempt
and requests a brand-new stream session. The previous
session's `streamid` is stopped via `stop_stream_session` so
the streaming server doesn't see two concurrent publishes for
the same camera.

The heartbeat reporter is started only after
`ingest_confirmed`. During the pre-warm window no heartbeats
are sent. This is intentional: the dashboard reads
`heartbeats.status == "streaming"` and the API's session
`state == "streaming"` as the two signals that an agent is
live; both should agree. Sending "streaming" heartbeats before
ingest confirms would lie to the dashboard.

The camera recovery now spends real time inside settle and
validation windows. In production those add up to roughly
6–7 s of wall clock per reopen attempt (1.5 s settle + ≤5 s
validation window). Tests inject zero-or-tiny values via the
new `camera_*_s` kwargs to keep the suite under 15 s total.

### Testing/checks
- `pytest -q` — 374 tests pass (340 from the M7 baseline +
  prior camera-verification patches and the previous M7.1
  patch; 9 new for ingest-wait + camera shutdown paths;
  6 new for `get_stream_session`).
- `ruff check upstream_agent tests` — 5 baseline warnings
  unchanged (4 B008 typer-Option-in-default in `cli.py`,
  1 I001 in `tests/test_token_redaction.py`). No new
  categories.
- Manual re-smoke plan (still to run on the macbook with
  live API + MediaMTX):
  1. Drop Wi-Fi mid-stream. Confirm the event sequence
     includes `network_failure` → `reconnecting` →
     `waiting_for_ingest` → `ingest_confirmed` →
     `Streaming started` on the next attempt. Verify the
     dashboard recovers within a few seconds of
     `ingest_confirmed`.
  2. Unplug the webcam mid-stream. Confirm `camera_settle`
     and `camera_validation_read_failed` appear during
     warm-up cycles, followed by `camera_validated`,
     `waiting_for_ingest`, `ingest_confirmed`, and
     `Streaming started`. No "camera reopen attempt #1"
     repeating with no progress.
  3. Start the agent with the webcam unplugged from the
     start — confirm immediate `CameraOpenError` exit
     (initial open is still fatal).
  4. `grep -E 'srt://|streamid|session_token' ~/.upstream/logs/*`
     after each — must be empty.

### Follow-up TODOs
- The 15 s ingest-wait timeout is hard-coded. If MediaMTX's
  warm-up regularly takes longer than that on the customer
  fleet, lift it to a YAML knob (`stream.ingest_timeout_s`
  or similar). Not needed today.
- The validator's settle / window constants are similarly
  hard-coded. Operators reporting a slow-warming USB camera
  could justify a YAML knob; not needed today.
- Future API milestone: if the server adds a webhook /
  push-notification path so the agent doesn't have to poll
  `get_stream_session`, drop the polling in favour of a
  blocking SSE stream or similar. The current contract has
  no such surface.

---

## 2026-05-14 - fix: 3-frame camera recovery validation + SRT/network failure classification

### Summary
Second pass on the M7 smoke. Two issues surfaced live:

1. The single-read camera verification added in the previous
   patch was not strict enough. OpenCV's V4L2 backend would
   sometimes return one stale buffered frame after `open()`, the
   verifier would accept it, the supervisor would start a fresh
   session + FFmpeg, and the **next** `read_frame` call (the
   first one inside the live frame loop) would still raise
   `CameraReadError`. The agent ended up in a tight
   "camera reopened -> session start -> FFmpeg start -> read
   failure -> session stop" cycle showing "camera reopen attempt
   #1" repeatedly. Fixed by requiring 3 consecutive successful
   frame reads before declaring recovery.
2. Network / SRT drops were not clearly labelled in the event
   stream. The supervisor classified them as
   `recoverable_stream` and reconnected correctly, but operators
   could not tell from the logs whether a particular failure
   was "FFmpeg crashed" vs "SRT publish died." Added a small
   classifier that inspects FFmpeg's exit code and stderr
   snippet for SRT/network signatures and emits a dedicated
   `network_failure` event. The supervisor also now emits
   `stream_resumed` on the first attempt after a recoverable
   cycle so it is easy to grep the log for "did the agent come
   back."

No API contract changes. No new YAML config knobs. No new API
endpoints (a session-status GET would be nice; the API has
none today).

### Files touched
- `upstream_agent/supervisor.py`
  - Added `_CAMERA_RECOVERY_VALIDATION_FRAMES = 3` and a
    new `_validate_camera_reads(cam, n, attempt, on_event)`
    helper that reads up to N frames and returns False on the
    first failure. The helper emits
    `camera_validation_failed` events with the precise frame
    index (`frame X/Y`) so the operator can tell which read in
    the sequence failed.
  - `_reopen_camera_with_backoff` now calls
    `_validate_camera_reads` after every successful `open()`.
    Validation failures stay inside the helper's backoff loop —
    they don't escape, don't reset the shared `BackoffPolicy`,
    and don't start a new session. On success the helper emits
    `camera_validated` and returns the camera.
  - Added a `validation_frames` keyword on
    `_reopen_camera_with_backoff` so tests can dial the count
    down. Production defaults to the module constant.
  - Added the `_NETWORK_FAILURE_PATTERNS` /
    `_NETWORK_FAILURE_EXIT_CODES` tables and the
    `_looks_like_network_failure(message, *, exit_code=None)`
    classifier. Patterns include `broken pipe`, `i/o error`,
    `input/output error`, `connection refused`,
    `connection reset`, `connection timed out`,
    `no route to host`, `network is unreachable`,
    `muxer error`, `av_interleaved_write_frame`, `srt`,
    `rendezvous`. Exit codes include 251 (EIO), 255 (generic),
    234 (EINVAL).
  - Added `_failure_tag(outcome)` that uses the classifier to
    return `"network"` or `"stream"` for a recoverable stream
    outcome.
  - The frame-loop FFmpeg-exit detection now calls
    `ffmpeg.read_stderr_snippet()` (when available) and
    attaches the scrubbed snippet to the `FfmpegExitError`
    message so the classifier has stderr to inspect.
  - The recovery loop now emits a dedicated
    `network_failure` event when the kind is `network`, enriches
    the `reconnecting` event message with the failure kind
    (`Recoverable failure (kind=network|stream|camera); cleanup
    complete, will retry. …`), and emits `stream_resumed`
    before the next attempt's `streaming` event whenever a
    recoverable cycle preceded it. Tracking is done via a local
    `had_recoverable_failure` flag and a `recovery_cycles`
    counter; both are scoped to the current `run_agent` call.
- `upstream_agent/encoder/ffmpeg.py` — new public method
  `FfmpegEncoderProcess.read_stderr_snippet()` that reads
  whatever has been captured on the subprocess's stderr pipe,
  scrubs SRT URLs, and caps at the existing 200-char limit.
  Used by the supervisor's mid-stream-exit path to feed
  classification.
- `tests/test_encoder_ffmpeg.py` — 3 new tests:
  empty-before-start, SRT-URL scrubbed and length-capped
  snippet, and the missing-stderr-pipe no-op case.
- `tests/test_supervisor_recovery.py` — 8 new tests:
  - `test_camera_validation_aborts_if_any_read_in_sequence_fails`
    — a camera whose read #2 raises after read #1 succeeds
    must not pass validation; only 2 reads happen on that fake
    before the validator gives up.
  - `test_ffmpeg_io_error_is_classified_as_network_failure` —
    a `FfmpegWriteError` whose stderr says "Input/output error"
    fires the `network_failure` event.
  - `test_ffmpeg_exit_code_251_classified_as_network` — an
    `FfmpegExitError` with code 251 fires `network_failure`.
  - `test_generic_ffmpeg_failure_is_not_classified_as_network`
    — a plain failure ("encoder ran out of memory") does **not**
    fire `network_failure` and reports `kind=stream`.
  - `test_network_failure_triggers_fresh_session_restart` —
    confirms the actual recovery actions (old session stop,
    new session start, fresh FFmpeg) happen.
  - `test_stream_resumed_event_fires_after_recoverable_failure`
    — the event fires on the first attempt after a recovery
    and ordering is "before the next `streaming` event."
  - `test_stream_resumed_does_not_fire_on_clean_first_run` —
    a happy-path run with no recoverable failure never emits
    the event.
  - `test_no_secret_leak_in_network_failure_events` — none of
    the new events embed the SRT URL, streamid, session
    token, or API token.
  - The existing
    `test_camera_open_succeeds_and_first_read_succeeds_proceeds_to_new_session`
    test now asserts the recovered camera saw at least 4
    reads (3 validation + ≥1 streaming) instead of the
    previous "≥2".
- `docs/architecture.md` — "Runtime recovery" section now
  documents the multi-frame validation contract, the
  `_failure_tag` classifier, and the local-vs-remote
  visibility caveat.
- `docs/troubleshooting.md` — "Camera unplug / reconnect"
  section rewritten with the new event stream and validation
  contract; new "Local encoder/SRT publish failed" subsection
  with the classifier's pattern table; new "Local vs remote
  stream visibility" subsection.

### Functions/classes changed
- `_validate_camera_reads(cam, n, attempt, on_event)` (new,
  private) — N-frame read sequence with per-frame failure
  events.
- `_reopen_camera_with_backoff(..., validation_frames=3)` —
  added the validation call after open. New `validation_frames`
  test knob.
- `_looks_like_network_failure(message, *, exit_code=None)`
  (new) — text + exit-code SRT/network classifier.
- `_failure_tag(outcome)` (new, private) — returns the
  `network` / `stream` tag for event messages.
- `FfmpegEncoderProcess.read_stderr_snippet()` (new) —
  leak-free post-mortem stderr peek.
- `run_agent` — added the `had_recoverable_failure` /
  `recovery_cycles` bookkeeping, the enriched `reconnecting`
  message, and the `network_failure` / `stream_resumed`
  emissions.

### System interaction notes
The classifier is intentionally **conservative** — short list
of patterns chosen to minimise false-positives. Broad terms
like "error" or "failed" are not on the list because they
appear in plenty of non-network FFmpeg output. The exit-code
list mirrors what FFmpeg actually emits when libsrt's
`srt_sendmsg` errors out: 251 (EIO) is the common one, 255 is
the generic fallback, and 234 (EINVAL) sometimes surfaces on
truncated streams. The classifier is informational only — it
does not change the recovery action; both `network` and
`stream` follow the same per-attempt stop-and-restart path.

The 3-frame validation discards 3 frames per recovery cycle.
Cameras that recover cleanly only pay this cost once; cameras
that keep failing the validation never reach the streaming
loop so the discarded frames are inconsequential. There is no
new wall-clock cost beyond the existing backoff sleeps.

The `stream_resumed` event tracking is per-`run_agent`-call,
not persistent. A normal happy-path run never emits it. After
a recovery cycle the event fires once per attempt until the
agent exits.

### Testing/checks
- `pytest -q` — 356 tests pass (340 from the M7 baseline + 5
  from the previous camera-verification patch + 8 new
  recovery + 3 new encoder).
- `ruff check upstream_agent tests` — 5 baseline warnings,
  same as before (4 B008 typer-Option-in-default in `cli.py`,
  1 I001 in `tests/test_token_redaction.py`). No new
  categories introduced.
- Manual re-smoke plan (still to run on the macbook with
  live API + MediaMTX):
  1. Unplug the webcam mid-stream. Confirm the event log
     shows `camera_validation_failed` lines (with `frame X/Y`
     indices) for any warm-up cycles, followed by
     `camera_validated` and `stream_resumed`. Verify the
     supervisor does **not** show repeated "camera reopen
     attempt #1" with no progress.
  2. Turn Wi-Fi off mid-stream. Confirm the event log shows a
     `network_failure` line when FFmpeg/SRT eventually
     errors, then the usual cycle of stop → backoff → new
     session → `stream_resumed`. Confirm the detection
     latency is 5–30 s on a typical macOS box (matches the
     kernel's default SRT socket timeout window).
  3. `grep -E 'srt://|streamid|session_token' ~/.upstream/logs/*`
     after each — must be empty.

### Follow-up TODOs
- Consider exposing the `_CAMERA_RECOVERY_VALIDATION_FRAMES`
  constant as a YAML knob if operators report it being too
  strict (e.g., on a flaky USB camera that takes a few extra
  seconds to settle). M7 ships with the constant fixed at 3.
- Future API milestone: if the server adds a "get current
  session state" GET, the supervisor could poll it during
  suspected network gaps to detect remote disconnects faster
  than waiting for FFmpeg's socket timeout. No such endpoint
  exists today; do not invent one.

---

## 2026-05-14 - fix: verify camera recovery with a frame read before declaring success

### Summary
Live-smoke testing the M7 camera-unplug path exposed a tight loop:
`VideoCapture.open()` on OpenCV's V4L2 backend can return success
even when the device hasn't started producing frames yet. The
supervisor treated that as recovered, started a fresh stream
session, launched FFmpeg, hit `CameraReadError` on the very first
read, tore it all down, and immediately re-entered the camera
recovery loop. The recovery helper now performs a verification
`read_frame()` after every successful `open()` and only returns
the camera if both succeed. Open-but-no-frame failures stay
inside `_reopen_camera_with_backoff`, sharing the same
`BackoffPolicy` so the delay keeps growing. The supervisor also
stops resetting the backoff after a "successful" camera recovery
— consecutive recovery cycles now accumulate, capped at the
configured `max_delay` (30 s).

### Files touched
- `upstream_agent/supervisor.py` — `_reopen_camera_with_backoff`
  now reads one frame after `open()` and discards it; failures
  (typed `CameraReadError`, unexpected `Exception`, or
  `(False, None)`) close the camera and continue the backoff
  loop. New `_safe_close(cam)` helper centralises the
  swallow-all close pattern used in both the helper and the
  outer recovery path. The outer recovery loop no longer calls
  `backoff_policy.reset()` after a successful camera recovery.
  Event messages distinguish `failed (open)` vs `failed (no
  frame)` so the operator can tell which step actually failed.
- `tests/test_supervisor_recovery.py` — five new tests:
  `test_camera_open_succeeds_but_first_read_fails_does_not_start_session`
  (no second `start_stream_session` call, no extra FFmpeg, both
  bad-recovery cameras closed after exactly one read);
  `test_camera_open_succeeds_and_first_read_succeeds_proceeds_to_new_session`
  (happy path of the verification — one extra read on the
  recovered camera, second session + second FFmpeg started);
  `test_repeated_open_then_read_fails_does_not_tight_loop`
  (records `BackoffPolicy.sleep` delays and asserts they
  progress 1 → 2 → 4 → 8 s across consecutive open+read-fail
  cycles, never restarting at the initial value);
  `test_shutdown_during_camera_recovery_wait_exits_cleanly`
  (fake sleep that sets the shutdown event returns
  `keyboard_interrupt` cleanly);
  `test_keyboard_interrupt_during_camera_recovery_wait_propagates`
  (fake sleep that raises `KeyboardInterrupt` propagates through
  outer cleanup to the caller).
- `docs/troubleshooting.md` — "Camera unplug / reconnect"
  section now documents the verified-recovery contract, the
  two event-message reason strings (`failed (open)` vs
  `failed (no frame)`), and the fact that backoff does not
  reset between recovery cycles.

### Functions/classes changed
- `_reopen_camera_with_backoff` — added the verification read.
- `_safe_close(cam)` (new, private) — best-effort
  `cam.close()` that never raises; replaces three inline copies.

### System interaction notes
The verification read consumes one frame from the camera before
the supervisor declares recovery. Stale frames at the start of
capture are not useful, so discarding the first one is harmless;
OpenCV's V4L2 backend in particular tends to deliver a stale
buffered frame as the first read after `open()` anyway.

Because backoff is no longer reset between camera recovery
cycles, a chronically-flaky camera that briefly returns frames
and then disconnects again will see growing delays between
recovery attempts (1 s → 2 s → 4 s → … up to 30 s). For a
camera that recovers cleanly, the agent still streams normally
until the next failure — the cap just means a long pause if
the device keeps failing.

### Testing/checks
- `pytest -q` — 345 tests pass (340 from M7 + 5 new in
  `test_supervisor_recovery.py`).
- `ruff check upstream_agent tests` — 5 baseline warnings only.
- Manual live smoke (to be re-run on the macbook):
  unplug the webcam mid-stream and confirm the event stream
  shows `failed (open)` / `failed (no frame)` lines for any
  warm-up cycles before `Camera reopened`. Re-plug and confirm
  streaming resumes within one verification read.

### Follow-up TODOs
- None. The "going-offline" final heartbeat and
  proactive-session-refresh items from the M7 entry are still
  the next things to consider.

---

## 2026-05-14 - feat: reconnect, camera reopen, session restart (Milestone 7)

### Summary
Milestone 7 makes `upstream run` survive normal field failures
without crashing. The supervisor now wraps the per-session work in a
recovery loop with bounded exponential backoff (1 s → 30 s, ×2,
±20 % jitter, interruptible by Ctrl+C). FFmpeg mid-stream exits,
broken pipes, transient API connection / 5xx / 429 / contract
errors, and mid-stream `CameraReadError` are all classified as
recoverable: the attempt is torn down, a fresh stream session is
requested from the API, and streaming resumes — or, for camera
loss, the camera is closed and reopened with backoff before the
next attempt. Auth / token / validation errors and unrecoverable
FFmpeg failures (`FfmpegStartError`, `FfmpegNotFoundError`, etc.)
stay fatal. The agent never reuses an SRT URL or session token
across attempts; every retry asks the API for a new one. Ctrl+C is
interrupt-able at every wait point (frame loop, inter-attempt
backoff, camera-reopen backoff). No new API endpoints — recovery
reuses the existing `POST /v1/stream-sessions/start` and
`POST /v1/stream-sessions/{id}/stop`.

### Files touched
- `upstream_agent/runtime/backoff.py` (new) — `BackoffPolicy(*,
  initial=1.0, multiplier=2.0, max_delay=30.0, jitter_factor=0.2,
  rng=None)`. `next_delay()` advances the internal geometric step
  capped at `max_delay`, with multiplicative jitter applied per
  call; `reset()` returns to initial; `sleep(delay, shutdown)`
  builds on `threading.Event.wait` so a shutdown event (or Ctrl+C
  on the main thread) wakes the wait immediately. Defaults match
  the M7 spec exactly. `rng` injection point lets tests assert
  progression deterministically. Module-level constants
  (`DEFAULT_INITIAL_DELAY_S`, etc.) are re-exported from
  `runtime/__init__.py`.
- `upstream_agent/runtime/__init__.py` — re-exports `BackoffPolicy`,
  `RuntimePhase`, and the four backoff defaults.
- `upstream_agent/runtime/stats.py` — added the
  `RuntimePhase = Literal["idle", "streaming", "reconnecting",
  "stopping"]` alias plus a `set_phase` method and a `phase` field
  on `StatsSnapshot`. The heartbeat builder reads `phase` to pick
  the wire `HeartbeatStatus`; the supervisor flips it as the
  recovery loop transitions between attempts.
- `upstream_agent/heartbeat/reporter.py` — `_build_heartbeat` now
  calls a new private `_phase_to_status(snapshot)` helper: phase
  `streaming` + `ffmpeg_running` → `streaming`; phase `reconnecting`
  → `reconnecting`; everything else → `error`. Module docstring
  updated to mention M7 phase wiring and the fact that the reporter
  itself still has no backoff (transient `ApiError` keeps logging
  warnings every 5 s).
- `upstream_agent/supervisor.py` — split the body. The outer
  `run_agent` opens the camera (initial open failure stays fatal),
  opens the API client (shared across attempts), then runs a
  recovery loop around a new private `_run_attempt` helper. Each
  attempt owns one session / FFmpeg / heartbeat reporter and cleans
  them up in its own `finally` (heartbeat → FFmpeg → session). The
  outer loop classifies the per-attempt outcome via a new
  `_classify(exc)` helper into fatal / recoverable_stream /
  recoverable_camera; recoverable_stream sleeps via `BackoffPolicy`
  before retrying, recoverable_camera calls a new
  `_reopen_camera_with_backoff` helper. Two new private
  registration helpers — `_register_if_needed` (idempotent agent +
  camera registration) and `_start_session` — replace the M5/M6
  call into `_obtain_stream_approval`; these propagate typed
  `ApiError` subclasses so the classifier can branch correctly. The
  `_obtain_stream_approval` orchestrator in `diagnostics/api.py` is
  unchanged for `upstream check --api`. Two new hidden test knobs
  on `run_agent`: `max_attempts: int | None = None` (default
  unbounded; `1` reproduces pre-M7 fail-fast) and
  `backoff: BackoffPolicy | None = None` (zero-delay policy for
  fast tests). End-to-end shutdown order is now heartbeat → FFmpeg
  → session (per-attempt) → camera → client (outer). The raw SRT
  URL lives in a single local inside `_run_attempt` and is `del`'d
  before attempt cleanup runs.
- `tests/test_runtime_backoff.py` (new) — 15 tests covering default
  progression (1 → 2 → 4 → 8 → 16 → 30 s with a zero-jitter rng),
  `max_delay` clamp, `reset()`, jitter swing within
  ±`jitter_factor`, validation rejection for bad
  `initial` / `multiplier` / `max_delay` / `jitter_factor`, and the
  interruptible-sleep contract: full wait returns `False`,
  concurrent `event.set()` returns `True` quickly, already-set
  event returns immediately, zero delay is a no-op that still
  honors a set event.
- `tests/test_supervisor_recovery.py` (new) — 13 supervisor
  recovery-loop tests using `StepwiseFfmpeg`,
  `RecoveringCamera`, `FlakyApiClient`, a `CapturingReporter`, and
  a zero-delay backoff. Covers FFmpeg mid-stream exit triggering a
  second attempt; broken-pipe write triggering the same path;
  per-attempt session stop before the next start; transient
  `ApiConnectionError` + `ApiServerError` retrying then succeeding;
  `ApiTokenRevokedError` being fatal with no retry; mid-stream
  `CameraReadError` closing and reopening the camera; reopen with
  backoff after two `CameraOpenError`s; phase tracking; shutdown
  during the backoff sleep returning early; `KeyboardInterrupt`
  during the sleep propagating with full cleanup; `max_attempts=3`
  re-raising the underlying exception; `stop_stream_session`
  failure being logged but not fatal; and a leak guard that asserts
  no SRT URL / streamid / session token / API token appears in any
  event or caplog record during a multi-attempt cycle.
- `tests/test_runtime_stats.py` — added `set_phase` round-trip test
  and updated the empty-snapshot assertion for the new `phase`
  field (default `"idle"`).
- `tests/test_heartbeat_reporter.py` — the shared `_make_reporter`
  helper now sets `phase="streaming"` when it sets
  `ffmpeg_running=True` (matches the new production contract).
  Added two new tests: `test_reporter_status_is_reconnecting_when_phase_set`
  asserts the `reconnecting` mapping, and
  `test_reporter_status_is_error_when_phase_stopping` asserts the
  fallback to `error`.
- `tests/test_supervisor.py` — the cleanup-ordering assertion now
  expects ffmpeg_stopped → session_stopped → camera_released (M7
  moved session-stop into the per-attempt cleanup so the old
  session is released before the next attempt starts). The four
  M5/M6 failure tests
  (`test_frame_write_failure_cleans_up_everything`,
  `test_camera_read_failure_stops_ffmpeg_and_session`,
  `test_ffmpeg_exit_mid_stream_raises_ffmpeg_exit_error_and_cleans_up`,
  `test_api_failure_does_not_start_ffmpeg`) now pass `max_attempts=1`
  and a fast backoff so they reproduce the pre-M7 fail-fast
  behaviour (one attempt, recoverable failures re-raise the typed
  exception). `test_api_failure_does_not_start_ffmpeg` asserts the
  raw `ApiAuthError` propagates — M7 dropped the M5/M6
  `StreamingRuntimeError` wrapper so callers can branch on the
  original class.
- `tests/test_supervisor_heartbeat.py` — the
  `test_missing_agent_id_skips_reporter_and_warns` test now
  monkeypatches the supervisor's new `_register_if_needed` helper
  (the M5/M6 `_obtain_stream_approval` is no longer imported by
  the supervisor).
- `docs/architecture.md` — bumped "What exists today" to
  Milestones 1–7. New "Runtime recovery (Milestone 7)"
  subsection with the recovery-loop diagram, the
  recoverable-vs-fatal classification table, the phase-to-status
  mapping, the end-to-end cleanup ordering, the Ctrl+C contract,
  and the two hidden `run_agent` test knobs. Status table
  refreshed: reconnect/backoff/camera-reopen/session-restart
  moves to "done (M7)"; proactive session refresh + token
  rotation move to "planned (M8+)".
- `docs/module-map.md` — added `runtime/backoff.py`. Updated
  `runtime/__init__.py`, `runtime/stats.py`,
  `heartbeat/reporter.py`, and `supervisor.py` for the new
  symbols and behaviour. Added `tests/test_runtime_backoff.py`
  and `tests/test_supervisor_recovery.py` to the test table.
  Removed the M7 entry from the deferred-submodules table.
- `docs/troubleshooting.md` — new "Reconnect / reliability
  (Milestone 7)" section covering camera unplug, recoverable
  FFmpeg failures, network drops, indefinite heartbeat warnings,
  fatal heartbeat exit, session restart between attempts, and
  Ctrl+C during reconnect.
- `docs/api-contracts.md` — added the M7 note up top stating no
  new endpoints. Added a "Recoverable vs fatal (Milestone 7)"
  subsection with the typed-error classification table.

### Functions/classes changed
- `BackoffPolicy` (new) — bounded exponential backoff with
  jitter and interruptible sleep.
- `RuntimePhase` (new) and `RuntimeStats.set_phase`,
  `StatsSnapshot.phase` (new fields) — let the heartbeat
  reporter report `RECONNECTING` while the supervisor is
  between attempts.
- `_phase_to_status(snapshot)` (new, private) in
  `heartbeat/reporter.py` — maps phase + ffmpeg flag to wire
  `HeartbeatStatus`.
- `_run_attempt(...)` (new, private) in `supervisor.py` — one
  streaming attempt with its own session / FFmpeg / reporter
  lifecycle. Never raises; returns an `_AttemptOutcome` the
  outer loop reads.
- `_classify(exc)` (new, private) — maps an exception type to
  fatal / recoverable_stream / recoverable_camera. The agent
  branches on type, never on message.
- `_register_if_needed` and `_start_session` (new, private) —
  typed-error-propagating registration + start-session calls
  shared across recovery attempts.
- `_reopen_camera_with_backoff` (new, private) — closes the
  current camera and reopens it with backoff; honors shutdown.
- `run_agent` — added `max_attempts` and `backoff` kwargs.
  Refactored into outer recovery loop + outer cleanup. The
  existing `client_factory` / `camera_factory` / `ffmpeg_factory`
  / `reporter_factory` / `ffmpeg_probe` / `save_progress` /
  `max_frames` / `on_event` kwargs are unchanged.

### System interaction notes
The recovery loop never invents an SRT URL: every attempt calls
`POST /v1/stream-sessions/start` to get a new session and
`POST /v1/stream-sessions/{id}/stop` to release the previous
one. Cleanup ordering across the lifetime of `run_agent` is
heartbeat → FFmpeg → session (per attempt) → camera → client
(outer), so the old session is released to the server promptly
before the next attempt begins. The heartbeat reporter is
per-session and torn down inside attempt cleanup, so during the
actual reconnect gap there are no heartbeats; the dashboard
sees one `reconnecting` heartbeat at most (the brief window
before the reporter stops) and then the agent goes silent on
the heartbeat channel until the next attempt's reporter starts.

Ctrl+C is honored at every wait point. The backoff sleep is
built on `threading.Event.wait`, which both raises
`KeyboardInterrupt` for SIGINT on the main thread *and* returns
immediately if a separate `shutdown.set()` is called. The
`_reopen_camera_with_backoff` helper uses the same primitive.
After Ctrl+C the per-attempt and outer cleanup both run, then
`KeyboardInterrupt` propagates out of `run_agent` and the CLI
exits 0.

Pre-M7 callers that expected a typed exception to propagate
from `run_agent` can opt back into the old behaviour by passing
`max_attempts=1`. Production callers (the `upstream run` CLI)
leave it unset for unbounded retry.

### Testing/checks
- `pytest -q` — 340 tests pass (327 from M1–M6 + 13 new in
  `test_supervisor_recovery.py`; plus `test_runtime_backoff.py`
  with 15 tests, two new heartbeat-status tests, one new stats
  test, and minor updates to four existing supervisor tests).
- `ruff check upstream_agent tests` — 5 warnings, all
  pre-existing baseline (4 B008 typer-Option-in-default in
  `cli.py`, 1 I001 in `tests/test_token_redaction.py`).
- Manual smoke verification plan (with a live
  `upstream-api-server` and MediaMTX):
  1. `upstream run`; once streaming, unplug the webcam — confirm
     `camera_lost` / `Waiting … before camera reopen` / `Camera
     reopened` event sequence and that frames resume.
  2. `upstream run`; once streaming, `kill -9` the FFmpeg PID —
     confirm recoverable_stream classification, fresh
     `start_stream_session` call, streaming resumes.
  3. `upstream run`; once streaming, drop Wi-Fi for ~10 s —
     confirm heartbeat warnings and that the stream resumes
     after the network returns.
  4. Ctrl+C during a backoff sleep — confirm immediate exit, no
     traceback, no zombie FFmpeg (`pgrep ffmpeg`), no locked
     camera (`lsof | grep video0`).
  5. Revoke the API token mid-stream — confirm
     `exit_reason=fatal_heartbeat`, no reconnect loop.
  6. `grep -E 'srt://|streamid|session_token' ~/.upstream/logs/*`
     after each scenario — must be empty.

### Follow-up TODOs
- Milestone 8: proactive session refresh based on
  `expires_at` (today we react to failures only); token rotation
  / refresh; reporter-side backoff for very long network
  outages so the warning rate slows from 5 s to ~30 s after the
  first few failures; optional "going offline" final heartbeat
  on shutdown; frame buffering across reconnect gaps so the
  dashboard doesn't see a hard cut.
- Optional later: surface the attempt counter on `RunResult`
  for downstream observability; expose `reconnect_attempts`
  via the heartbeat schema once the server supports it.
- Optional later: classify `camera_already_streaming` (422)
  specially so the supervisor waits out the server-side
  session timeout and then retries. Today it stays fatal —
  the user has to wait and re-run.

---

## 2026-05-14 - encoder: pin GOP to fps for snappy first-frame on join

### Summary
The H264 encoders the agent targets (libx264, h264_videotoolbox, etc.)
default to long GOPs — libx264 is 250 frames. At 60 fps that's a ~4 s
keyframe interval, which directly bounds how long a WebRTC reader on
the dashboard waits for first video after the session goes live.
Add `-g <fps>`, `-keyint_min <fps>`, and `-sc_threshold 0` to the
ffmpeg command so the GOP is a fixed 1 s cadence regardless of encoder
or scene-cut heuristics. First-frame latency on the dashboard drops
from 3–4 s to ~1 s. Bitrate penalty is ~10–15% on typical
webcam/talking-head content (more keyframes = larger frames).

### Files touched
- upstream_agent/encoder/command_builder.py — added the three flags in
  `encode_args`. Inline comment explains why all three are needed
  together for predictable cadence.
- tests/test_encoder_command_builder.py — two new tests
  (`test_keyframe_cadence_is_one_second`,
  `test_scene_cut_keyframes_disabled`) that exercise the GOP-equals-fps
  invariant across 15/30/60 fps and the scene-cut-disabled flag.

### Testing/checks
- `uv run pytest tests/test_encoder_command_builder.py -q` — 15 passing
  (was 13; +2 new).
- Live verified separately on the macbook: after rebuilding the agent
  and restarting `upstream run`, the dashboard's first-frame time drops
  to ~1 s with no visible quality regression.

### Follow-up TODOs
- None. If we ever target very low-bitrate scenarios (< 500 kbps)
  where the GOP penalty starts mattering, expose a config knob like
  `encoder.gop_seconds` so operators can trade snappy joins for lower
  bitrate.

---

## 2026-05-14 - docs: refresh srt_url example to current ingest host and streamid

### Summary
`docs/api-contracts.md` still showed `srt://ingest.upstream.tv:8890?streamid=publish:...` — a stale placeholder hostname and the pre-fix JWT-bearing streamid shape. Updated both example responses to match what the live API returns today: `srt://ingest.upstreamcv.com:18890?streamid=publish:sess_<hex>&latency=200`. Doc-only; no code change.

### Files touched
- docs/api-contracts.md — two `srt_url` strings and the `streamid` example value.

### Testing/checks
- Doc-only; nothing exercises the changed strings at runtime.

### Follow-up TODOs
- None.

---

## 2026-05-13 - feat: heartbeat loop + runtime health reporting (Milestone 6)

### Summary
Milestone 6 adds the heartbeat side-channel. While `upstream run` is streaming, a background daemon thread POSTs to `POST /v1/heartbeats?agent_id=<uuid>` every 5 seconds with the runtime's current status, fps, bitrate, resolution, codec, and last error. The dashboard uses those heartbeats to mark the camera/session online and surface basic telemetry; the API additionally bumps `agents.last_seen_at` and `cameras.updated_at` server-side. No reconnect/backoff and no session refresh — both are deferred to M7. Fatal API responses (token revoked/expired, body validation) stop the agent cleanly with a new `exit_reason="fatal_heartbeat"`.

The contract used here was read verbatim from `../upstream-api-server` (`routers/heartbeats.py`, `schemas/heartbeats.py`, the 20260513120000 migration). No API-side changes were made.

### Files touched
- `upstream_agent/api/schemas.py` - added `HeartbeatStatus = Literal["online", "streaming", "error", "reconnecting"]`, `Heartbeat` (request body item, `extra="forbid"`, with the same numeric bounds the server enforces — fps 0..1000, bitrate_kbps 0..200000, width 1..7680, height 1..4320, codec ≤32, last_error ≤512, agent_version ≤64), `HeartbeatBatchRequest` (`heartbeats` list of 1..100), and `HeartbeatBatchResponse` (`accepted` + `server_time`, `extra="ignore"`). Added `datetime` import.
- `upstream_agent/api/client.py` - added `HEARTBEAT_PATH = "/v1/heartbeats"`. Extended `_request` with an optional `params: dict[str, str] | None = None` argument passed through to `httpx.Client.request`. Added `send_heartbeat(*, agent_id, heartbeat)` that wraps a single-item batch and passes `agent_id` as a query parameter. Reuses the same auth, redaction, and `_raise_for_status` machinery as every other endpoint — token never appears in logs/errors.
- `upstream_agent/api/__init__.py` - re-exports `HEARTBEAT_PATH`, `Heartbeat`, `HeartbeatBatchRequest`, `HeartbeatBatchResponse`, `HeartbeatStatus`.
- `upstream_agent/runtime/__init__.py` (new) - re-exports `RuntimeStats`, `StatsSnapshot`.
- `upstream_agent/runtime/stats.py` (new) - `RuntimeStats` (thread-safe via `threading.Lock`) and `StatsSnapshot` (frozen dataclass). Tracks `frames_written`, a sliding 60-frame monotonic FPS window, `camera_connected`, `ffmpeg_running`, `last_frame_at` (UTC datetime), and `last_error` (auto-truncated to 512 chars to match the heartbeat schema). FPS = `(window-1) / (window[-1] - window[0])` once at least two frames are recorded; `None` for zero or negative span (no `ZeroDivisionError`). `record_frame(monotonic=None, wall_clock=None)` takes optional injection points so tests can drive the FPS window deterministically.
- `upstream_agent/heartbeat/__init__.py` (new) - re-exports `HeartbeatReporter`, `DEFAULT_HEARTBEAT_INTERVAL_S = 5.0`.
- `upstream_agent/heartbeat/reporter.py` (new) - `HeartbeatReporter` owns one daemon `threading.Thread`. Loop: snapshot `RuntimeStats` → build `Heartbeat` (`status="streaming"` while `ffmpeg_running`, else `"error"`) → `client.send_heartbeat(...)` → sleep on `stop_event` for `interval_s`. Transient `ApiError` failures log a warning and continue; `ApiTokenRevokedError` / `ApiTokenExpiredError` / `ApiValidationError` set a stored fatal-reason string the supervisor polls via `fatal_reason()`. `start()` is idempotent; `stop(timeout=2.0)` sets the event and joins, logging a warning if the join times out. The `clock` kwarg is an injection point for tests.
- `upstream_agent/supervisor.py` - added `"fatal_heartbeat"` to the `ExitReason` literal. Added imports for `ApprovedStreamConfig`, `HeartbeatReporter`, `RuntimeStats`, `__version__`. Added `ReporterFactory` type alias and a `_default_reporter_factory` matching the camera/ffmpeg/client factory pattern. `run_agent` now constructs a `RuntimeStats` at the top of the try block, flips `camera_connected` after `cam.open()`, flips `ffmpeg_running` after `ffmpeg.start()`, then constructs and starts the reporter (skipping with a logged warning if `state.agent_id` is `None`). Inside the frame loop: `stats.record_frame()` after every successful write, and a `reporter.fatal_reason()` check that breaks the loop with `exit_reason="fatal_heartbeat"`. All exception handlers now flip `stats.set_ffmpeg_running(False)` and (for `FfmpegExitError` / generic `Exception`) call `stats.record_error(...)`. The `_cleanup` helper takes the reporter as its first argument and stops it before FFmpeg — so the dashboard sees no further "streaming" pings after shutdown begins. Module and function docstrings updated to mention heartbeat in the cleanup order.
- `tests/test_supervisor.py` - the shared `FakeApiClient.start_stream_session` now echoes `camera_id=req.camera_id` on the returned `Session` so the heartbeat tests can assert the supervisor reads it correctly. Existing tests pass unchanged.
- `tests/test_runtime_stats.py` (new) - 10 tests covering empty snapshot, frame counter + timestamp, FPS-is-None-until-two-frames, sliding window saturation, zero-span guard, camera/ffmpeg flags, `record_error` round-trip and truncation, eight-thread concurrent `record_frame` smoke, and frozen-dataclass snapshot.
- `tests/test_api_heartbeat_client.py` (new) - 15 `httpx.MockTransport` tests covering path / query / body shape, Bearer header, None-field omission, every status-code mapping (401 token_revoked, 401 token_expired, 403 forbidden_resource → `ApiAuthError`, 422, 429, 503, connect error), token-leak guards on errors and caplog, and local Pydantic validation (out-of-range fps, unknown status).
- `tests/test_heartbeat_reporter.py` (new) - 13 reporter tests using a `FakeClient` and a `threading.Event` to wait deterministically for the Nth call. Covers steady-state cadence, `status="streaming"` vs `"error"` based on stats, snapshot data carried into the payload, idempotent `start()`, safe `stop()` before start, fast `stop()` from inside a long interval, transient-failure-then-continue with caplog warning, `ApiTokenRevokedError` / `ApiValidationError` setting `fatal_reason`, unexpected exception logged but loop continues, and leak guards on payload + caplog.
- `tests/test_supervisor_heartbeat.py` (new) - 5 supervisor-lifecycle tests with a `FakeReporter` injected via `reporter_factory`. Covers start-after-FFmpeg / stop-before-FFmpeg ordering on success, `stats.record_frame` runs once per frame, fatal-reason exit (`exit_reason="fatal_heartbeat"`), missing-`agent_id` skip path (warning logged, stream still runs), `KeyboardInterrupt` stop ordering, and a leak guard on what is passed to the reporter.
- `docs/api-contracts.md` - added the M6 update note up top and a new "POST /v1/heartbeats" section documenting query parameter, request body (every field, bound, and meaning), 200 response shape, server-side side effects, and the full error-response table including the fatal vs transient mapping. Cites the upstream-api-server files the contract was confirmed against.
- `docs/architecture.md` - "What exists today" header bumped to Milestones 1–6, status table refreshed (heartbeat done, M5 smoke done, reconnect/refresh moved to M7+). New "Runtime health (Milestone 6)" subsection with the runtime-stats + reporter description, the side-channel diagram, the failure-handling rules, and the updated cleanup order.
- `docs/module-map.md` - documented `runtime/__init__.py`, `runtime/stats.py`, `heartbeat/__init__.py`, `heartbeat/reporter.py`. Updated `api/__init__.py`, `api/client.py`, `api/schemas.py`, and `supervisor.py` rows for the new symbols / behaviour. Updated `tests/` table with the three new test files and the supervisor-heartbeat additions. Trimmed the deferred-submodules table (heartbeat reporter is no longer deferred; `api/heartbeat.py` is removed because the heartbeat lives under `heartbeat/`, not `api/`).
- `docs/troubleshooting.md` - new "Heartbeat / dashboard offline (upstream run)" section covering transient-failure warnings, fatal `exit_reason=fatal_heartbeat` causes (revoked / expired / validation), dashboard-still-offline-but-heartbeats-arrived, and the `agent_id missing` defensive log line.

### Functions/classes changed
- `RuntimeStats` / `StatsSnapshot` (new) - thread-safe runtime telemetry shared between the frame loop and the heartbeat thread.
- `HeartbeatReporter` (new) - daemon-thread heartbeat sender with stop / fatal-reason semantics.
- `Heartbeat` / `HeartbeatBatchRequest` / `HeartbeatBatchResponse` / `HeartbeatStatus` (new) - Pydantic request/response models matching the confirmed server contract verbatim.
- `UpstreamApiClient.send_heartbeat(*, agent_id, heartbeat)` (new) - POST `/v1/heartbeats?agent_id=<uuid>` with a one-item batch.
- `UpstreamApiClient._request(..., *, params=None)` - extended with an optional query-parameter kwarg. Existing callers continue to work unchanged.
- `run_agent` - new `reporter_factory` kwarg, internal `RuntimeStats` instance, heartbeat lifecycle wiring (start after FFmpeg, stop before FFmpeg), and a `"fatal_heartbeat"` exit reason. `_cleanup` extended to accept and stop the reporter first.

### System interaction notes
The heartbeat side-channel runs alongside the frame loop on its own daemon thread; neither blocks the other. The supervisor checks `reporter.fatal_reason()` after each frame, so a token-revoked response causes the loop to exit within at most one frame interval (≈33 ms at 30 fps). Cleanup order is heartbeat → FFmpeg → camera → API session: stopping the heartbeat first means the dashboard sees no further "streaming" pings after the agent has decided to shut down.

The heartbeat payload contains no SRT URL, streamid, or session token — only the camera UUID, session UUID, status enum, and numeric telemetry. The bearer token is attached by the existing `_build_auth_header` and remains the only sensitive value in flight; the redaction filter is still the second line of defense.

`state.agent_id` is now load-bearing for the heartbeat — `_obtain_stream_approval` already sets it during registration, but the supervisor defensively logs a warning and continues without a heartbeat if it ever finds the field unset (rather than crashing the stream).

### Testing/checks
- `pytest -q` passes (307 tests; 265 from M5 + 42 new across the four M6 test files; plus the one-line update to the shared `FakeApiClient.start_stream_session` to echo `camera_id`).
- `ruff check upstream_agent tests` — 5 total warnings, all pre-existing baseline (4 B008 typer-Option-in-default in `cli.py`, 1 I001 in `tests/test_token_redaction.py`). No new lint categories.
- Manual smoke verification plan (with a live `upstream-api-server` and MediaMTX): run `upstream run`, watch `GET /v1/agents/{agent_id}/heartbeats` on the API and confirm rows arrive every ~5s; confirm `agents.last_seen_at` advances; Ctrl+C and confirm the heartbeat thread joins within 2s before FFmpeg/camera/session teardown; force-revoke the token mid-stream and confirm `exit_reason=fatal_heartbeat` with no traceback and no SRT URL in any log output.

### Follow-up TODOs
- Milestone 7: reconnect/backoff on transient heartbeat / FFmpeg / API failures, session-refresh when a session expires mid-stream, camera recovery on read failures, optional "going offline" final heartbeat on shutdown, and batching of missed heartbeats during a network blip.
- Optional later: surface `last_heartbeat_accepted` + `server_time` clock-skew on the CLI for operator visibility; for M6 the agent simply discards the response after the typed-error mapping runs.
- Optional later: per-heartbeat `last_error` lifecycle (currently we only set it from FFmpeg-exited and camera-read-failure paths; transient ApiError in the reporter doesn't update it because the reporter logs the error directly).

---

## Milestone 5 cleanup — removed temporary SRT URL override

After real API-issued SRT streaming was verified end-to-end, the temporary `UPSTREAM_DEV_SRT_URL_OVERRIDE` path was removed. `upstream run` now uses only the SRT URL returned by the API stream session response.

---

## 2026-05-13 - feat: `upstream run` live streaming runtime (Milestone 5 prep)

### Summary
Milestone 5 prep adds the runtime that ties everything together: `FfmpegEncoderProcess` (a managed `subprocess.Popen` wrapper around ffmpeg), the supervisor (`upstream_agent/supervisor.py`, with `run_agent()`), and `upstream run` — the first command that actually streams video. The runtime chains FFmpeg probe → camera open → API session approval → FFmpeg command build → FFmpeg start → frame loop → cleanup. Every cleanup path (normal completion, `max_frames` hit, FFmpeg start failure, mid-stream FFmpeg exit, camera read failure, API failure, `KeyboardInterrupt`) tears down FFmpeg → camera → API session in that fixed order.

This is **prep**, not full M5: the real end-to-end smoke against MediaMTX is gated on `upstream-api-server` returning an SRT URL whose host/port reaches the running MediaMTX. The runtime is otherwise complete and the mocked tests exercise every cleanup path.

### Files touched
- `upstream_agent/errors.py` - added `StreamingRuntimeError` (umbrella), `FfmpegRuntimeError`, `FfmpegStartError`, `FfmpegWriteError`, `FfmpegExitError`. All inherit from `UpstreamError` so the top-level CLI catch keeps working.
- `upstream_agent/encoder/ffmpeg.py` (new) - `FfmpegEncoderProcess(command, redacted_command=None, *, popen_factory=None)`. Wraps `subprocess.Popen` with `shell=False`, `stdin=PIPE`, `stdout=DEVNULL`, `stderr=PIPE`. Public methods: `start()` (idempotent guard; raises `FfmpegStartError` on `FileNotFoundError`/`OSError`), `write_frame(bytes)` (raises `FfmpegWriteError` on `BrokenPipeError`/`OSError` — error messages scrub any `srt://` line out of FFmpeg's own stderr), `poll() -> int | None`, `stop(timeout_seconds=5.0)` (close stdin → terminate → wait → kill on timeout; never raises; idempotent). `__repr__` returns only the redacted argv. The `popen_factory` kwarg lets tests drive a fake `Popen` without any real subprocess.
- `upstream_agent/encoder/__init__.py` - re-exports `FfmpegEncoderProcess`.
- `upstream_agent/supervisor.py` (new) - `RunResult` dataclass and `run_agent(cfg, state, *, client_factory=None, camera_factory=None, ffmpeg_factory=None, ffmpeg_probe=None, save_progress=None, max_frames=None, on_event=None)`. Reuses `probe_ffmpeg`, `_obtain_stream_approval` from `diagnostics/api.py`, `build_ffmpeg_command` / `redact_command`, `parse_srt_url`, `WebcamSource`, and the existing `State` / `save_state` flow. The raw SRT URL lives in a single local variable inside this function and is dropped before returning — only safe booleans/IDs cross out. `on_event(stage, message)` is the CLI's hook for printing progress lines. `max_frames` lets tests run a deterministic short loop.
- `upstream_agent/cli.py` - added `@app.command("run")`, wired through to `run_agent` with a `save_progress` callback and an `on_event` lambda that forwards to `typer.echo`. Catches `KeyboardInterrupt` (exit 0 — clean shutdown) and `UpstreamError` (exit 1). Updated the module docstring to mention `upstream run`.
- `tests/test_encoder_ffmpeg.py` (new) - 21 tests against an injected fake `Popen`. Real ffmpeg is never invoked. Covers start/idempotency/`FileNotFoundError`/`OSError`, write happy path / `BrokenPipeError` / stderr scrub / other `OSError`, `poll` proxying, `is_running` after exit, stop ordering, kill-after-timeout, stop-before-start no-op, stop idempotency, stop-never-raises, `__repr__` redaction, and `redacted_command` property defensive copy.
- `tests/test_supervisor.py` (new) - tests against `FakeCamera`/`FakeApiClient`/`FakeFfmpeg`. Covers happy path with `max_frames`, cleanup ordering (FFmpeg → camera → session), capabilities reflecting the probe, `save_progress` called twice, session-started-before-FFmpeg ordering, server-approved-config overriding local-config, FFmpeg start failure with full cleanup, frame write failure with cleanup, camera read failure stopping FFmpeg and session, FFmpeg exit mid-stream raising `FfmpegExitError`, API failure short-circuiting before FFmpeg, `KeyboardInterrupt` cleanup-then-reraise, frame resize via injected fake cv2, no-resize fast path, and leak guards on events / `caplog` / result.
- `tests/test_cli.py` - 8 new tests for `upstream run`: help message, no-config, placeholder-token, happy path with mocked supervisor, `KeyboardInterrupt` shutdown (exit 0), FFmpeg start failure (exit 1, clean error), API failure (exit 1), and a leak guard.
- `README.md` - added "Streaming video (preview)" section with the safe output block, flag for the M5-prep blocker, and a quick-start step `upstream run`. Updated the status section: M4 done, M5 in preview, M6+ deferred.
- `docs/architecture.md` - status table refreshed (M5-prep done; live SRT smoke gated on API fix). New "Runtime layer (Milestone 5 prep)" subsection documenting the supervisor flow, reuse of M3/M4 helpers, cleanup ordering, sensitive-data discipline, and the M5 blocker.
- `docs/module-map.md` - documented `supervisor.py` and `encoder/ffmpeg.py`; updated `cli.py` row for the `run` command; expanded `errors.py` row to list the new `StreamingRuntimeError` family; trimmed the "deferred submodules" table (encoder/supervisor moved out of deferred).
- `docs/api-contracts.md` - added an M5-prep note up top: no new endpoints; `upstream run` keeps the session open for the lifetime of the stream and only stops it on shutdown. Documented the M5-real-smoke blocker.
- `docs/troubleshooting.md` - new "Live streaming (`upstream run`)" section with entries for FFmpeg start failure, pipe-closed mid-stream, camera read failure during run, session-won't-end-after-Ctrl-C, and the early-exit symptom that points at an unreachable SRT host/port.

### Functions/classes changed
- `FfmpegEncoderProcess` (new) - managed FFmpeg subprocess wrapper. Only ever exposes the redacted command via `__repr__` / errors.
- `run_agent` / `RunResult` (new) - live streaming orchestrator. Never raises `ApiError` directly; wraps API approval failures in `StreamingRuntimeError`.
- `StreamingRuntimeError` and the `FfmpegRuntimeError` family - new typed errors. The supervisor uses `StreamingRuntimeError` for non-FFmpeg failures it surfaces (e.g. API approval). FFmpeg-specific failures use the more specific subclasses.
- `check_cmd` (CLI) - unchanged; `run_cmd` is a sibling command, not a flag.

### System interaction notes
`upstream run` keeps the API session open for the entire stream lifetime — a real change from M3/M4 checks, which stopped the session immediately after `start_stream_session` returned. The state file is updated after each API step (agent registered, camera registered) but not after `start_stream_session` (session info is never persisted). On shutdown, the supervisor calls `stop_stream_session(session_id)` exactly once in the `_cleanup` helper. No new API endpoint is required.

The redaction filter in `setup_logging` is unchanged; the supervisor's own discipline (raw SRT URL stays in one local variable, FFmpeg command is held by `FfmpegEncoderProcess` which only exposes the redacted form, all `on_event` messages contain no URL/token/streamid) does the heavy lifting.

### Testing/checks
- `pytest -q` passes (265 tests; 219 from M4 + 46 new across `test_encoder_ffmpeg.py`, `test_supervisor.py`, and 8 new CLI tests).
- `ruff check upstream_agent tests` — 5 total warnings: the 4 baseline `B008` typer-Option-in-default false-positives (1 new for the `run` command, same pattern as the 3 existing) plus the 1 pre-existing `I001` in `tests/test_token_redaction.py`. No new lint categories.
- `upstream --help` and `upstream run --help` render the new command cleanly.
- **Real-backend smoke: BLOCKED.** Cannot run end-to-end against MediaMTX until `upstream-api-server` returns an SRT URL whose host/port reaches the running MediaMTX. The runtime is otherwise complete; the mocked tests cover every cleanup path. Once the API blocker is fixed, the smoke test is: configure a real token, run `upstream run`, confirm frames land in MediaMTX (visible in its own log), press Ctrl+C, confirm the agent prints the full shutdown block and exits 0.

### Follow-up TODOs
- **Real end-to-end smoke** once `upstream-api-server` returns a correct local/dev ingest host/port. This is the only remaining piece for M5 proper.
- Milestone 6: heartbeat loop (`heartbeat/reporter.py`).
- Milestone 7: reconnect/backoff on transient FFmpeg or API failures.
- Possible later: `signal.signal(SIGTERM, ...)` handler. Ctrl+C → `KeyboardInterrupt` is sufficient for M5 prep; SIGTERM from systemd matters once the agent is run as a service.

---

## 2026-05-13 - feat: FFmpeg/SRT streaming preflight (Milestone 4)

### Summary
Milestone 4 adds `upstream check --streaming`, a local preflight that answers "is this machine ready to stream?" without actually streaming. It probes the local FFmpeg install (version, SRT protocol support, H264 encoder), reuses the existing M3 API approval flow to obtain an approved SRT URL, validates that URL, builds the FFmpeg command Milestone 5 will execute, then stops the temporary session. No frames are pushed, no long-running subprocess is started.

The same milestone makes the `capabilities` payload the agent sends to `register_agent` honest. Pre-M4 it was always `{"ffmpeg": false, "srt": false}`. Post-M4, both `--api` and `--streaming` probe FFmpeg once and report `{"ffmpeg": "<version>", "srt": <bool>}` based on what the local binary actually supports.

### Files touched
- `upstream_agent/errors.py` - added `FfmpegError` family (`FfmpegNotFoundError`, `FfmpegProbeError`, `FfmpegSrtUnsupportedError`, `FfmpegH264UnsupportedError`), `SrtUrlError`, `CommandBuildError`. All inherit from `UpstreamError` so the top-level CLI catch keeps working.
- `upstream_agent/diagnostics/ffmpeg_check.py` (new) - `FfmpegProbeResult` dataclass and `probe_ffmpeg(*, timeout_seconds=5.0, runner=None)`. Runs `ffmpeg -version` / `-protocols` / `-encoders` via one safe subprocess helper. Detects SRT with a line-boundary regex (no false match on `srtp`). Picks an H264 encoder by priority: libx264 → h264_videotoolbox → h264_nvenc → h264_qsv → h264_amf. Never raises. The `runner` kwarg lets tests stub subprocess entirely.
- `upstream_agent/transport/__init__.py` (new), `upstream_agent/transport/srt.py` (new) - `ParsedSrtUrl` dataclass and `parse_srt_url(url)`. Validates scheme (`srt`), host, and port; raises `SrtUrlError` otherwise. Returns only safe metadata (booleans, port, mode, latency). The raw URL, hostname, and streamid never reach the dataclass.
- `upstream_agent/encoder/__init__.py` (new), `upstream_agent/encoder/command_builder.py` (new) - `build_ffmpeg_command(...)` returns a `list[str]` ready for Milestone 5 to execute. `redact_command(cmd, srt_url)` returns a fresh list with the SRT URL replaced by `srt://<redacted>`, never mutating the input. Input handling is provisional (`-f rawvideo -pix_fmt bgr24 ... -i pipe:0`) — M5 will finalise it.
- `upstream_agent/diagnostics/api.py` - removed the static `_AGENT_CAPABILITIES = {"ffmpeg": False, "srt": False}` constant. Added `_default_capabilities()` which probes FFmpeg lazily. Extracted `_obtain_stream_approval(cfg, state, client, capabilities, save_progress)` private helper (steps 1–3) reused by both `run_api_check` and the new streaming orchestrator. `run_api_check` now accepts an optional `capabilities` kwarg; defaults to the probed value. Public signature is backwards-compatible.
- `upstream_agent/diagnostics/streaming.py` (new) - `StreamingCheckResult` dataclass and `run_streaming_check(cfg, state, *, client_factory=None, save_progress=None, ffmpeg_probe=None)`. Eight phases (FFmpeg → SRT support → H264 encoder → register agent → register camera → start session → SRT URL parse → command build) plus a best-effort cleanup stop. The raw SRT URL lives in a single local variable inside the function and is dropped before returning — only safe booleans cross out.
- `upstream_agent/cli.py` - added `--streaming` flag to `check`, `_print_streaming_check_success` helper, and wiring after the `--api` block. Updated the placeholder-token gate to fire on `api OR streaming`. Updated the "no flag" hint to mention `--streaming`.
- `tests/test_diagnostics_ffmpeg.py` (new) - 12 tests against an injected `runner`. Real `ffmpeg` is never invoked.
- `tests/test_transport_srt.py` (new) - 14 tests. Uses placeholder URLs only; asserts `repr()` does not leak streamid or hostname.
- `tests/test_encoder_command_builder.py` (new) - 13 tests covering shape, encoder choice, redaction, edge cases.
- `tests/test_diagnostics_streaming.py` (new) - 14 tests against a fake API client and injected FFmpeg probe. Covers happy path, three local-failure phases (no API call fires), three API-failure phases (correct stop-or-not behaviour), SRT URL failures, command-build failure, stop-warning best-effort, leak guards on both the result and `caplog`, and capability-payload reporting from the probe.
- `tests/test_diagnostics_api.py` - updated the capabilities test to inject a fixed dict (deterministic, no real FFmpeg dependency) and added a new test that the default capabilities come from a probed value when no kwarg is given.
- `tests/test_cli.py` - added 9 tests for `--streaming` (no config, placeholder token, happy path, FFmpeg missing, SRT unsupported, no H264, API failure, leak guard, capabilities flow), plus a `--camera --api --streaming` ordering test.
- `README.md` - new "Verifying streaming readiness" section with success/error examples and FFmpeg install hints for macOS / Linux / Windows. Updated the status section: M3 → done, M4 → current, M5 → live streaming.
- `docs/architecture.md` - status table refreshed (M4 done; live streaming and frame buffer deferred to M5). New "Local preflight layer (Milestone 4)" subsection documenting the three new modules and the capabilities-reporting change.
- `docs/module-map.md` - documented `diagnostics/ffmpeg_check.py`, `diagnostics/streaming.py`, `encoder/`, `transport/`. Updated the `diagnostics/api.py` row for the new `capabilities` kwarg and `_obtain_stream_approval` helper. Updated `errors.py` to list the new exception classes. Trimmed the "deferred submodules" table to what's still unimplemented (M5 wiring of `encoder/ffmpeg.py` and the live SRT transport).
- `docs/api-contracts.md` - added an M4 note up top: no new endpoints; only the `capabilities` payload on `register_agent` changed. Updated the `register_agent` example payload to show `{"ffmpeg": "8.1.1", "srt": true}`.
- `docs/troubleshooting.md` - new section with entries for FFmpeg not found, no SRT support, no H264 encoder, API failure during streaming check, invalid SRT URL, command build failure, stop warning, and the "passes but cannot stream yet" expectation.

### Functions/classes changed
- `FfmpegProbeResult` / `probe_ffmpeg()` (new) - structured outcome of the local probe; safe to log, no environment leak.
- `ParsedSrtUrl` / `parse_srt_url()` (new) - validate and extract safe display metadata from an SRT URL.
- `build_ffmpeg_command()` / `redact_command()` (new) - argv builder + display redactor; H264-only for M4.
- `StreamingCheckResult` / `run_streaming_check()` (new) - eight-phase orchestrator; never raises.
- `_default_capabilities()` (new) - lazy FFmpeg probe driving the `register_agent` capabilities payload.
- `_obtain_stream_approval()` (new private helper) - extracted from `run_api_check`; now shared by both orchestrators. Carries the raw `StreamSessionResponse` so callers can use the SRT URL locally; callers are responsible for the cleanup stop and for dropping the response.
- `run_api_check()` - gained an optional `capabilities` kwarg. Internal flow simplified by delegating steps 1–3 to the shared helper.
- `check_cmd` (CLI) - gained the `--streaming` flag, the placeholder gate now covers both `api` and `streaming`.

### System interaction notes
The agent now probes FFmpeg before any API call when an API check is requested. The probe adds ~50 ms in the warm-cache case, runs three short subprocess calls, never opens a window, and reports honest capabilities to the backend. The streaming check creates and stops its own temporary session — separate from any session `--api` might have created. Running `upstream check --camera --api --streaming` will create and stop two temporary sessions back-to-back.

Local state (`agent_state.json`) is unchanged: still only `agent_id`, `device_id`, `last_camera_id`. Session info, SRT URLs, and session tokens are never persisted.

### Testing/checks
- `pytest -q` passes (219 tests; 155 from M3.3 + 64 new across the four M4 test files and two updated M3 tests).
- `ruff check upstream_agent tests` — same 4 pre-existing warnings (3 B008 typer Option defaults + 1 I001 in `tests/test_token_redaction.py`); no new lint categories.
- Real `upstream check --streaming` smoke against the live backend, run **twice**:
  - First run on a default Homebrew `ffmpeg` build (no libsrt): the check fails at the `srt_support` phase with the expected install hint. Useful proof that the failure path renders correctly.
  - Second run on Homebrew `ffmpeg-full` (libsrt enabled): the check passes all eight phases — FFmpeg detected, SRT supported, libx264 picked, API approval received, SRT URL parsed, command built. The temporary session is stopped before exit. Output is the spec example block verbatim with no tokens, SRT URLs, stream IDs, or session tokens.

### Follow-up TODOs
- macOS users: document the `brew install ffmpeg-full` requirement in `README.md` and `docs/troubleshooting.md` (done — added an SRT-support entry pointing operators at `ffmpeg-full`).
- M5 will wire the actual FFmpeg subprocess: take the `list[str]` from `build_ffmpeg_command`, launch it with a managed `subprocess.Popen`, pipe BGR frames in from `WebcamSource`, monitor exit codes, and reconnect on transient failure.
- Once M5 lands, `encoder/__init__.py` will gain `FfmpegStreamer` (or similar) and the provisional `pipe:0` input flow may be revisited if a different source-to-FFmpeg path proves better.

---

## 2026-05-12 - feat: cleanup stop session after upstream check --api (Milestone 3.3)

### Summary
Real-backend smoke during M3.2 surfaced a UX bug: `upstream check --api` started an approved stream session and never released it, so a second back-to-back run hit `409 camera_already_streaming`. M3.3 adds a cleanup-stop step: after a successful `start_stream_session`, the orchestrator calls `POST /v1/stream-sessions/{session_id}/stop`. The check is now safely re-runnable.

Stop failures are **non-fatal** — registration and session approval already prove the path works. A failing stop becomes a `Warning:` line on stderr; the command still exits 0.

### Files touched
- upstream_agent/api/schemas.py - added `StopStreamSessionRequest` (empty, `extra="forbid"`) and `StopStreamSessionResponse` (permissive, no required fields).
- upstream_agent/api/client.py - added `STREAM_SESSION_STOP_PATH_TEMPLATE = "/v1/stream-sessions/{session_id}/stop"` and `stop_stream_session(session_id)`. The method interpolates the path and sends an empty body. Re-uses `_request` so it benefits from the same status / code / contract error mapping.
- upstream_agent/api/__init__.py - re-exports the new path template and the two new schemas.
- upstream_agent/diagnostics/api.py - added a step 4 cleanup-stop inside the `with factory(...) as client` block. The call is wrapped in `try/except ApiError`; on failure, the exception string lands on `result.stop_warning` and `result.success` stays True. `ApiCheckResult` gained a `stop_warning: str | None` field; the `_failure` helper sets it to None for steps 1–3 failures.
- upstream_agent/cli.py - after `_print_api_check_success`, if `result.stop_warning is not None`, print `Warning: failed to stop stream session after check: <msg>` to stderr. Exit stays 0.
- tests/test_api_client.py - 4 new tests: stop uses the interpolated path with `POST` and an empty body; response model accepts any shape (empty / wrapped / arbitrary); 404 → `ApiValidationError`; 5xx → `ApiServerError`.
- tests/test_diagnostics_api.py - `FakeUpstreamApiClient` gained `stop_response` / `stop_raises` and a `stop_session_calls` list. Updated assertions on the happy path and on the session-start-failure path. Two new tests: stop failure surfaces as `result.stop_warning` with `result.success=True`; the orchestrator stops the *correct* session id when the server returns one different from the default.
- tests/test_cli.py - `_install_fake_api_client` gained `stop_raises` and a `stop_calls` capture list. Happy-path test asserts the cleanup stop was called with `sess_789` and that no warning appears on stderr. New test for the stop-failure path (exit 0, success block printed, warning on stderr, no token / SRT URL / session_token leakage). New back-to-back test that asserts both runs exit 0 and stop is called once per run.
- docs/api-contracts.md - added a `POST /v1/stream-sessions/{session_id}/stop` section documenting the path-interpolated endpoint, the empty-body request, the permissive-response policy, and the non-fatal warning behavior.
- docs/troubleshooting.md - added "Warning: failed to stop stream session after check" entry, plus a "Running back-to-back" entry explaining the expected behavior.
- README.md - extended the "Verifying API registration" section: lists step 4 (cleanup stop), notes that twice-in-a-row should succeed, and explains the warning-without-failure behavior.

### Functions/classes changed
- `StopStreamSessionRequest` / `StopStreamSessionResponse` - new schemas. Request is empty (`{}`); response accepts any shape so future server-side changes don't break the agent.
- `UpstreamApiClient.stop_stream_session(session_id)` - new public method. Interpolates `STREAM_SESSION_STOP_PATH_TEMPLATE`, sends an empty body, returns a parsed response (mostly unused by callers).
- `ApiCheckResult.stop_warning` - new field. Only ever set on otherwise-successful results; `None` on failures (which have their own `error` / `failed_step`).
- `run_api_check` - now performs the stop call inside the same context manager as the start call. The stop sits *outside* the three numbered failure points; an exception there cannot flip `success` to False.
- `check_cmd` (CLI) - prints the success block, then conditionally prints `Warning: failed to stop stream session after check: ...` on stderr. Exit code is unchanged from the success path.

### System interaction notes
The agent's API-check flow now ends with a `POST /v1/stream-sessions/{session_id}/stop` on the happy path. The session lifecycle on the server is:

1. `start_stream_session` → server creates `state=approved` session, hands back `srt_url` + `session_token`.
2. **Without the agent ever opening an SRT connection**, the cleanup stop call sets the session to a stopped/expired state on the server.
3. Subsequent `start_stream_session` calls for the same camera find no active session and succeed.

The agent's local state (`agent_state.json`) is unchanged: still only `agent_id`, `device_id`, `last_camera_id`. Session info is never persisted.

### Testing/checks
- `pytest -q` passes (155 tests; 147 from M3.2 + 8 new for stop coverage).
- `ruff check upstream_agent tests` — same 4 pre-existing warnings; no new lint categories.
- Real-backend smoke: needs a real token to actually verify the back-to-back run on the live server. The mocked back-to-back test (`test_check_api_back_to_back_runs_both_succeed`) verifies the wiring; the live run is the operator's confirmation that the camera slot is genuinely released server-side.

### Follow-up TODOs
- Real-backend smoke (twice-in-a-row) once a token is plugged into `~/.upstream/config.yaml`.
- If stop reliably fails in real deployments, escalate to a retry / out-of-band cleanup. Today's "best-effort with warning" is fine because session expiry server-side eventually clears the slot.

---

## 2026-05-12 - feat: align API client with confirmed backend contract (Milestone 3.2)

### Summary
M3.1 shipped a provisional API client. M3.2 updates every shape to match the backend contract the API teammate just confirmed: new agent-register payload (`name`, `version`, `os`, `python_version`, `capabilities` — NOT `device_id`), wrapped agent response (`{agent: {id, ...}, server_time}`), new camera-register payload (`external_id`, `device_path`, `default_*`, `metadata`), robust camera response (accepts both wrapped and flat shapes), nested stream-session request (`{camera_id, requested: {codec, bitrate_kbps, latency_mode, ...}}`), nested stream-session response (`{session: {id, approved, srt_url, streamid, ...}, srt_url, session_token, expires_at}`), and the new error envelope (`{error: {code, message, details, request_id}}`).

Also tightens privacy: the agent now generates a non-sensitive `name` (`upstream-agent-<8-hex-of-device-id>`) and never sends `platform.node()` as hostname. Camera `external_id` and `device_path` are derived as `webcam-0` / `webcam:0` for stability across OSes.

The CLI surface, state file shape, orchestrator signature, and Typer flags are unchanged — only payloads and error parsing moved.

### Files touched
- upstream_agent/config.py - `StreamConfig.latency_mode` Literal → `low | balanced | stable`; default `api.base_url` in the template → `https://api.upstreamcv.com`; template comments mention local-dev URL and token format.
- upstream_agent/errors.py - added `ApiTokenExpiredError(ApiAuthError)`.
- upstream_agent/api/schemas.py - rewritten. New models: `Agent`, `Camera`, `Session`, `RequestedStreamConfig`, plus refreshed request/response wrappers. `CameraRegisterResponse` carries a `model_validator(mode="before")` that unwraps a flat camera body into `{"camera": {...}}`.
- upstream_agent/api/client.py - `_parse_error_body` rewritten to read the nested envelope and return a `ParsedError` dataclass with `code`, `message`, `request_id`. `_raise_for_status` rewritten to switch on `(status, code)` — `token_revoked` / `token_expired` codes hit their specific subclasses; `limit_exceeded.*` on 403 maps to `ApiValidationError`; 404 maps to validation. Added `_format_error` helper that produces `"{method} {path} -> {status} {reason}, code=..., request_id=...: {message}"` without leaking bodies. Added `TOKEN_EXPIRED_ERROR_CODE` constant.
- upstream_agent/api/__init__.py - re-exports updated (`Agent`, `Camera`, `Session`, `RequestedStreamConfig`, `ParsedError`, `TOKEN_EXPIRED_ERROR_CODE`).
- upstream_agent/diagnostics/api.py - rewritten request builders. Helpers `_agent_name_from(device_id)`, `_camera_external_id(camera_cfg)`, `_camera_device_path(camera_cfg)`. Constants `_DEFAULT_CODEC="h264"`, `_DEFAULT_BITRATE_KBPS=3000`, `_AGENT_CAPABILITIES={"ffmpeg": False, "srt": False}`. `ApiCheckResult` expanded with `approved_bitrate_kbps` and `approved_latency_mode`. Reads agent/camera/session IDs through the new nested response shapes (`agent_resp.agent.id`, `camera_resp.camera.id`, `session_resp.session.id`).
- upstream_agent/cli.py - `_print_api_check_success` updated to the verbose 6-line block: `Approved config: h264, 1280x720 @ 30 FPS, 3000 kbps, balanced`. `account_id` is intentionally not displayed.
- tests/test_api_client.py - rewritten for the confirmed contract. Adds happy-path payload-shape assertions, wrapped vs flat camera response handling, all error-code mappings (`invalid_token`, `token_revoked`, `token_expired`, `limit_exceeded.*`, `camera_not_found`, `camera_already_streaming`, 400/422/429/5xx, unmapped 4xx, missing-envelope tolerance), and token-leak guards.
- tests/test_diagnostics_api.py - rewritten with the new `FakeUpstreamApiClient` that returns nested response objects. New assertions: generated agent name format and stability, no hostname sent, lowercased OS, `webcam-0` external_id, `webcam:0` device_path, hard-coded codec/bitrate, latency_mode echoed from config. Leak guards now cover both `srt_url` and `session_token`.
- tests/test_cli.py - `_install_fake_api_client` helper rewritten to return the new wrapper shapes; added `_API_SECRET_SESSION_TOKEN`. Happy path now asserts the verbose approved-config line and that `account_id` is absent from output. New test: `test_check_api_succeeds_with_flat_camera_response` covers the pre-validator. Token-leak guard extended to streamid and session_token.
- examples/webcam.yaml, examples/rtsp.yaml - `api.base_url` updated to `https://api.upstreamcv.com`; comments mention local-dev URL and token format; `latency_mode` comment lists `low | balanced | stable`.
- docs/api-contracts.md - replaced PROVISIONAL with CONFIRMED. Documents both base URLs, token format, confirmed payloads for all three endpoints, the new error envelope, full status/code → typed-error mapping table, and the agent-name privacy decision.
- docs/architecture.md - "API client" row flipped to "M3 contract confirmed; mocked tests green; real smoke pending". Rewrote the "API client layer" section to reference the confirmed shapes, the privacy decisions, and the `account_id`-not-displayed rule.
- docs/module-map.md - `errors.py` row now lists `ApiTokenExpiredError`. `api/__init__.py`, `api/client.py`, `api/schemas.py` rows describe the M3.2 surface.
- docs/troubleshooting.md - added nine confirmed-contract entries (invalid_token, token_revoked, token_expired, limit_exceeded.max_cameras, agent_not_found, agent_account_mismatch, camera_not_found, camera_already_streaming, limit_exceeded.max_concurrent_sessions) plus a "where do I find the request_id" entry.
- README.md - example `api.base_url` updated to `https://api.upstreamcv.com`; added a local-dev snippet; updated success-output sample to the verbose form; noted the agent-name privacy decision; updated failure-output samples to show the new `code=` / `request_id=` segments.

### Functions/classes changed
- `Agent`, `Camera`, `Session`, `RequestedStreamConfig`, `ApprovedStreamConfig` - new Pydantic models matching the backend's nested response shapes. `ApprovedStreamConfig.latency_mode` is a plain `str` (not Literal) since the server may evolve.
- `AgentRegisterRequest` - shape moved from `{device_id, agent_version, platform}` to `{name, hostname, version, os, python_version, capabilities}`. Hostname is optional and intentionally None.
- `CameraRegisterRequest` - shape moved from `{source_type, source, requested_*}` to `{name, external_id, device_path, default_*, metadata}`.
- `CameraRegisterResponse` - carries a `model_validator(mode="before")` that unwraps a top-level `{"id": ..., ...}` body into `{"camera": {...}}` so the agent works against either backend shape.
- `StreamSessionRequest` / `StreamSessionResponse` - flat structure replaced with nested `requested` / `session` blocks.
- `UpstreamApiClient._parse_error_body` - now returns a `ParsedError(code, message, request_id)`. Switches on the confirmed envelope; tolerates absent or malformed bodies.
- `UpstreamApiClient._raise_for_status` - new dispatch on `(status, code)`. 401 with `token_revoked` / `token_expired` hits the specific subclasses; 403 with `limit_exceeded.*` becomes `ApiValidationError`; 404 (`agent_not_found`, `camera_not_found`) becomes `ApiValidationError` so the CLI reads naturally.
- `UpstreamApiClient._format_error` - new helper. Produces messages of the form `"POST /v1/cameras/register -> 403 Forbidden, code=limit_exceeded.max_cameras, request_id=abc: ..."`. Never includes request or response bodies.
- `run_api_check` - same signature, rewritten internals. Builds the new payloads, reads through nested responses, returns the expanded `ApiCheckResult`.
- `_agent_name_from(device_id)` / `_camera_external_id(cfg)` / `_camera_device_path(cfg)` - private helpers exposed for unit testing. Stable across runs.
- `ApiTokenExpiredError(ApiAuthError)` - new error class for the `token_expired` code.
- `_print_api_check_success` - verbose approved-config line.

### System interaction notes
The agent flow on `upstream check --api` is unchanged at the CLI level. Internally, every request body and every parsed response now uses the confirmed shapes. State persistence is identical (`agent_id`, `device_id`, `last_camera_id`); `account_id` and session info are never written to state.

The privacy posture is meaningfully better: the customer's real hostname never leaves the machine, the agent's name is reproducible from `device_id`, and the camera's `device_path` is OS-neutral.

The redaction filter remains the second line of defense for accidental token logging by third-party libraries. The first line is the client itself, which emits no request or response bodies. New: `streamid` and `session_token` are also covered by the CLI's leak-guard tests.

### Testing/checks
- `pytest -q` passes (147 tests; 135 from M1/M2/M3.1 + 12 net new for M3.2 — covering wrapped vs flat camera response, all confirmed error codes, expanded leak guards, and request-payload sanity).
- `ruff check upstream_agent tests` — same pre-existing 4 warnings (1 I001 in test_token_redaction.py from M1, 3 B008 from Typer's option-default idiom). No new lint categories.
- All M3.2 tests are mocked (`httpx.MockTransport` for the client; a `FakeUpstreamApiClient` for the orchestrator; a `_default_factory` monkeypatch for CLI). No real backend, no internet, no real token needed for CI.
- Real-backend smoke against `https://api.upstreamcv.com` or `http://localhost:4000` is the conditional verification step — only runs if a real token is already present in the local config.

### Follow-up TODOs
- Run real smoke once a dev token is provisioned; record which `CameraRegisterResponse` shape (wrapped vs flat) the backend actually returns.
- Implement token rotation handling once the backend exposes the rotation signal.
- Add `--api` flag variants (`--api-config-only`, `--api-no-session`) if operators want a lighter check; the orchestrator can support short-circuiting after any step.

---

## 2026-05-12 - feat: add API client and upstream check --api (Milestone 3)

### Summary
Milestone 3. Adds the HTTP client foundation (`upstream_agent/api/` with a synchronous httpx-based `UpstreamApiClient`, Pydantic schemas, and isolated endpoint-path constants), a `run_api_check` orchestrator that walks the three-step backend flow (register agent → register camera → start stream session), and the `upstream check --api` CLI command. The agent can now prove the backend approves it for a given camera — stopping before any streaming. No FFmpeg, no SRT transport, no heartbeat, no token rotation; those stay out of scope.

Backend contract is still being finalised by another teammate. Everything that's likely to change (endpoint paths, request fields, response fields, error codes) is isolated to two files so the eventual swap is mechanical.

### Files touched
- pyproject.toml - added `httpx>=0.27` to runtime dependencies.
- upstream_agent/errors.py - added `ApiError` plus `ApiConnectionError`, `ApiAuthError`, `ApiTokenRevokedError`, `ApiValidationError`, `ApiRateLimitError`, `ApiServerError`, `ApiContractError`.
- upstream_agent/api/__init__.py - new package; re-exports the client, path constants, and schemas.
- upstream_agent/api/schemas.py - new module; Pydantic v2 request/response models for the three endpoints. Requests use `extra="forbid"`; responses use `extra="ignore"`.
- upstream_agent/api/client.py - new module; `UpstreamApiClient` synchronous wrapper around httpx. Endpoint constants (`AGENT_REGISTER_PATH`, `CAMERA_REGISTER_PATH`, `STREAM_SESSION_START_PATH`), isolated `_build_auth_header()`, one `_request()` that handles JSON encoding, response validation, transport-error mapping, and status-code mapping.
- upstream_agent/diagnostics/api.py - new module; `ApiCheckResult` frozen dataclass and `run_api_check(cfg, state, *, client_factory=None, save_progress=None)` orchestrator. Never raises; folds `ApiError` into the result. Persists partial progress via the optional `save_progress` callback so a later failure preserves earlier IDs.
- upstream_agent/cli.py - extended `check_cmd` with the `--api` flag; added `_print_api_check_success` helper. Both flags compose (`--camera --api` runs both sequentially).
- tests/test_errors.py - extended; 9 new inheritance assertions for the API error hierarchy.
- tests/test_api_client.py - new file; 21 tests against `httpx.MockTransport` covering happy paths, header construction, base-URL normalisation, every status-code mapping, transport errors, contract errors, and token-leak guards.
- tests/test_diagnostics_api.py - new file; 8 orchestrator tests against a `FakeUpstreamApiClient` covering happy path, each step-failure scenario, SRT-URL leak guard via `caplog`, and request-payload sanity.
- tests/test_cli.py - extended; 8 new `--api` tests covering no-config, placeholder rejection, happy path with state persistence, each step-failure with correct partial state, token + SRT URL leak guard, and both-flags-compose.
- docs/architecture.md - flipped API client and `--api` rows from planned to partial; added an "API client layer" section.
- docs/module-map.md - moved `api/` from the deferred table to the live table; documented `diagnostics/api.py`, the new error classes, and the new test files.
- docs/api-contracts.md - replaced the placeholder with concrete (provisional) request/response shapes for the three endpoints, the assumed error-body shape, status-code mapping, and a checklist of pending backend confirmations.
- docs/troubleshooting.md - added nine API-check entries covering missing config, placeholder token, unreachable, invalid token, revoked token, camera rejected, session rejected, contract mismatch, and rate limit.
- README.md - to be updated.

### Functions/classes changed
- `UpstreamApiClient` - synchronous HTTP client. Constructor accepts `base_url`, `token`, and an injectable `transport` for tests. Class method `from_config(cfg.api)` for production wiring. Context-manager support so the underlying httpx connection is closed on failure. Never logs request or response bodies.
- `_build_auth_header` - isolated helper. Returns `{"Authorization": f"Bearer {token}"}`. Swapping `Bearer` for `X-API-Key` or anything else is one method change.
- `_request` - handles every shared concern: JSON encode of the typed request body (`exclude_none=True`), `httpx.TransportError → ApiConnectionError`, `_raise_for_status` for non-2xx, JSON decode of the response body, and Pydantic validation against the typed response model with `ValidationError → ApiContractError` and no input values in the error string.
- `_raise_for_status` - status-to-typed-error mapping: 400/422 → `ApiValidationError`, 401/403 → `ApiAuthError` (or `ApiTokenRevokedError` if backend `error_code == "token_revoked"`), 429 → `ApiRateLimitError`, 5xx → `ApiServerError`, other 4xx → bare `ApiError`. Error messages carry `method`, `path`, `status`, and `reason` but never request or response bodies.
- `AgentRegisterRequest` / `AgentRegisterResponse` - device-register payload. Carries `device_id`, `agent_version`, and optional `platform` (`platform.system()` value, e.g. "Darwin"). Hostname is intentionally NOT sent — it identifies the customer machine.
- `CameraRegisterRequest` / `CameraRegisterResponse` - camera-register payload. Carries `agent_id`, `name`, `source_type`, `source`, and the three `requested_*` fields. M3 only exercises the `webcam` source type.
- `StreamSessionRequest` / `StreamSessionResponse` / `ApprovedStreamConfig` - session approval payload. `ApprovedStreamConfig.srt_url` is documented as sensitive; the orchestrator extracts `srt_url_present: bool` and drops the URL.
- `ApiCheckResult` - structured outcome of the three-step probe. Carries IDs, the approved-config summary fields (codec/width/height/fps), `srt_url_present: bool`, and a `failed_step: Literal["register_agent" | "register_camera" | "start_stream_session"] | None`. **The SRT URL itself is never on this object.**
- `run_api_check` - the orchestrator. Builds the request from `state.device_id` + `__version__` + `platform.system()`, then walks the three calls. After each successful step, immutably updates state via `state.model_copy(update={...})` and invokes the optional `save_progress` callback. Catches `ApiError`; lets other exceptions propagate.
- `check_cmd` - extended to accept `--api`. Validates exactly-one-flag-or-both-flags; rejects placeholder token before any HTTP request; sets up logging with the real token in `sensitive_values`; runs camera check then API check (if both flags set); exits 1 if any failed, 2 if no flag, 0 on full success.
- API error classes - all inherit from `UpstreamError`, so the CLI's existing top-level catch picks them up with no change. `ApiTokenRevokedError` is also a subclass of `ApiAuthError`.

### System interaction notes
The new flow on `upstream check --api`:
`resolve_config_path` → `load_config(allow_placeholder=True)` → reject placeholder if `--api` was passed → `setup_logging(sensitive_values={cfg.api.token})` → `load_state(default_state_path())` → `run_api_check(cfg, state, save_progress=save_state)`.

The orchestrator:
1. `client.register_agent(req)` → `state.agent_id = resp.agent_id`; calls `save_progress(state)` so `agent_state.json` records the new ID immediately.
2. `client.register_camera(req)` → `state.last_camera_id = resp.camera_id`; calls `save_progress(state)`.
3. `client.start_stream_session(req)` → no state update; the session ID and SRT URL are not persisted.

If any step fails, the orchestrator returns `(failure_result, working_state)` immediately. The CLI does a final `save_state(updated_state)` (idempotent) before exiting.

**Secrets handling:** the token redaction filter is on the logger handlers, so even if httpx debug-logs an `Authorization` header it gets scrubbed. The SRT URL is held in memory only inside the response object and dropped after the orchestrator extracts the boolean. The result, the CLI output, and the log file all stay clean.

### Testing/checks
- `pytest -q` passes (135 tests; 86 from M1/M2 + 49 new).
- All M3 tests are mocked (`httpx.MockTransport` for the client; a `FakeUpstreamApiClient` for the orchestrator; a `_default_factory` monkeypatch for CLI). No real backend, no internet, no real token.
- Token-leak guards: real-looking config tokens never appear in stdout, stderr, the log file, or any captured `ApiError` message.
- SRT-URL leak guard: a sentinel URL planted in the fake response never appears in `dataclasses.asdict(result)`, `caplog` records, stdout, stderr, or the log file.
- `upstream check` (no flag) now mentions both `--camera` and `--api` in the usage hint.
- `upstream check --camera --api` runs both probes; camera block prints before API block.
- Real-backend smoke against `http://api.upstreamcv.com` is the next manual step once a real token is provisioned.

### Follow-up TODOs
- Smoke against the real dev backend once a token is available; confirm the provisional paths and field names with the API teammate.
- Add token-rotation handling (server-pushed refresh).
- Add `upstream check --network` / `--ffmpeg` / `--storage` flags as later milestones bring those layers online; the flag-composition design already supports them.

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
