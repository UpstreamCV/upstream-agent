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

## Installation

### `pip install -e .` fails on Python 3.9 or older

The package requires Python 3.10 or newer. Use `pyenv`, `uv`, or your
system package manager to install a supported version.
