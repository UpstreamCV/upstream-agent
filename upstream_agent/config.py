"""YAML config loading and validation for upstream-agent.

The config lives at ``~/.upstream/config.yaml`` by default. The caller may
override the path with an explicit argument, the ``UPSTREAM_CONFIG``
environment variable, or the CLI ``--config`` flag (which maps to the
explicit argument).

Validation is handled by Pydantic v2 models. Validation errors raised to
callers are formatted without including the offending input value, so a
real-looking API token cannot leak into stderr or logs via a Pydantic
``ValidationError`` traceback.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from upstream_agent.errors import ConfigError

PLACEHOLDER_TOKEN: str = "YOUR_API_TOKEN_HERE"
CONFIG_ENV_VAR: str = "UPSTREAM_CONFIG"


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class ApiConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_url: str = Field(..., min_length=1)
    token: str = Field(..., min_length=1)

    @property
    def is_placeholder_token(self) -> bool:
        return self.token.strip() == PLACEHOLDER_TOKEN


class CameraConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1)
    source_type: Literal["webcam", "rtsp"]
    source: int | str
    requested_width: int = Field(default=1280, gt=0)
    requested_height: int = Field(default=720, gt=0)
    requested_fps: int = Field(default=30, gt=0)


class StreamConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Backend accepts: "low" (lowest latency), "balanced" (default),
    # "stable" (steady throughput, slightly higher latency).
    latency_mode: Literal["low", "balanced", "stable"] = "balanced"
    buffer_ms: int = Field(default=500, ge=0)


class LoggingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    level: Literal["debug", "info", "warning", "error"] = "info"


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid")

    api: ApiConfig
    camera: CameraConfig
    stream: StreamConfig = Field(default_factory=StreamConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------


def default_config_path() -> Path:
    """Default user config path (``~/.upstream/config.yaml``)."""

    return Path.home() / ".upstream" / "config.yaml"


def resolve_config_path(explicit: Path | None = None) -> Path:
    """Resolve which config path to use.

    Order: ``explicit`` argument > ``UPSTREAM_CONFIG`` env var > default.
    """

    if explicit is not None:
        return Path(explicit)
    env_value = os.environ.get(CONFIG_ENV_VAR)
    if env_value:
        return Path(env_value)
    return default_config_path()


# ---------------------------------------------------------------------------
# Load and write
# ---------------------------------------------------------------------------


def load_config(path: Path | None = None, allow_placeholder: bool = False) -> Config:
    """Load and validate a YAML config from disk.

    Wraps :class:`FileNotFoundError`, :class:`yaml.YAMLError`, and
    :class:`pydantic.ValidationError` in :class:`ConfigError`. Validation
    errors are formatted without input values so secrets cannot leak.

    If ``allow_placeholder`` is False (default), a config still on the
    placeholder ``api.token`` is rejected with a clear error pointing the
    user at the file to edit. Pass ``allow_placeholder=True`` from
    informational commands (e.g. ``upstream status``) that want to show
    the user their config even when they have not set the token yet.
    """

    target = resolve_config_path(path)

    try:
        raw_text = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ConfigError(
            f"Config file not found: {target}. Run 'upstream init' to create one."
        ) from None
    except OSError as exc:
        raise ConfigError(f"Could not read config file {target}: {exc}") from None

    try:
        raw = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"Invalid YAML in {target}: {exc}") from None

    if raw is None:
        raise ConfigError(f"Config file {target} is empty.")
    if not isinstance(raw, dict):
        raise ConfigError(f"Config file {target} must be a YAML mapping at the top level.")

    try:
        cfg = Config.model_validate(raw)
    except ValidationError as exc:
        errors = exc.errors(include_input=False, include_url=False)
        lines = []
        for err in errors:
            loc = ".".join(str(part) for part in err["loc"]) or "<root>"
            lines.append(f"  - {loc}: {err['msg']}")
        raise ConfigError(
            f"Invalid config in {target}:\n" + "\n".join(lines)
        ) from None

    if cfg.api.is_placeholder_token and not allow_placeholder:
        raise ConfigError(
            f"api.token in {target} is still set to the placeholder. "
            f"Edit the file and replace {PLACEHOLDER_TOKEN} with your real token."
        )

    return cfg


_TEMPLATE = """\
# upstream-agent config
#
# DO NOT COMMIT a filled-in copy of this file. It holds your API token.
# Treat it like any other credential. The default location
# (~/.upstream/config.yaml) is gitignored by the upstream-agent repo,
# but other repos you copy it into may not be.

api:
  # Base URL of the UpStream API server.
  # Production / staging: "https://api.upstreamcv.com"
  # Local dev:            "http://localhost:4000"
  base_url: "https://api.upstreamcv.com"
  # Replace with your real API token before running `upstream` commands
  # that talk to the server. Tokens look like
  # ups_<env>_<8-char id>_<22-char secret>.
  token: "YOUR_API_TOKEN_HERE"

camera:
  # Human-readable name for this camera.
  name: "Front Door"
  # "webcam" (local device) or "rtsp" (network camera).
  source_type: "webcam"
  # For webcam: integer device index (0 = first camera).
  # For rtsp: the rtsp:// URL of the stream.
  source: 0
  requested_width: 1280
  requested_height: 720
  requested_fps: 30

stream:
  # "low" | "balanced" | "stable"
  latency_mode: "balanced"
  buffer_ms: 500

logging:
  # "debug" | "info" | "warning" | "error"
  level: "info"
"""


def write_default_config(path: Path, force: bool = False) -> None:
    """Write a template config to ``path``.

    Creates parent directories as needed. Raises :class:`ConfigError` if
    the file already exists and ``force`` is False.
    """

    if path.exists() and not force:
        raise ConfigError(
            f"{path} already exists. Use --force to overwrite."
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_TEMPLATE, encoding="utf-8")
