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

## Installation

### `pip install -e .` fails on Python 3.9 or older

The package requires Python 3.10 or newer. Use `pyenv`, `uv`, or your
system package manager to install a supported version.
