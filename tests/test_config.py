"""Tests for the YAML config loader."""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from upstream_agent.config import (
    CONFIG_ENV_VAR,
    PLACEHOLDER_TOKEN,
    Config,
    default_config_path,
    load_config,
    resolve_config_path,
    write_default_config,
)
from upstream_agent.errors import ConfigError

VALID_YAML = dedent(
    """\
    api:
      base_url: "https://api.upstream.ai"
      token: "real_secret_token_value"

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
    """
)


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------


def test_default_config_path_is_under_dot_upstream(home_dir: Path) -> None:
    assert default_config_path() == home_dir / ".upstream" / "config.yaml"


def test_resolve_config_path_explicit_wins(
    home_dir: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(CONFIG_ENV_VAR, str(tmp_path / "env.yaml"))
    explicit = tmp_path / "explicit.yaml"
    assert resolve_config_path(explicit) == explicit


def test_resolve_config_path_env_wins_over_default(
    home_dir: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    env_path = tmp_path / "from-env.yaml"
    monkeypatch.setenv(CONFIG_ENV_VAR, str(env_path))
    assert resolve_config_path(None) == env_path


def test_resolve_config_path_default(
    home_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(CONFIG_ENV_VAR, raising=False)
    assert resolve_config_path(None) == home_dir / ".upstream" / "config.yaml"


# ---------------------------------------------------------------------------
# load_config
# ---------------------------------------------------------------------------


def test_load_valid_config(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(VALID_YAML, encoding="utf-8")
    cfg = load_config(config_path)
    assert isinstance(cfg, Config)
    assert cfg.api.base_url == "https://api.upstream.ai"
    assert cfg.api.token == "real_secret_token_value"
    assert cfg.camera.name == "Front Door"
    assert cfg.camera.source_type == "webcam"
    assert cfg.camera.source == 0
    assert cfg.stream.latency_mode == "balanced"
    assert cfg.logging.level == "info"


def test_load_missing_file_raises_config_error(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist.yaml"
    with pytest.raises(ConfigError) as excinfo:
        load_config(missing)
    assert "not found" in str(excinfo.value).lower()
    assert "upstream init" in str(excinfo.value)


def test_load_invalid_yaml_raises_config_error(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("api: : not valid yaml: :\n  - oops", encoding="utf-8")
    with pytest.raises(ConfigError) as excinfo:
        load_config(bad)
    assert "invalid yaml" in str(excinfo.value).lower()


def test_load_missing_required_field_raises_config_error(tmp_path: Path) -> None:
    incomplete = tmp_path / "incomplete.yaml"
    incomplete.write_text(
        dedent(
            """\
            api:
              base_url: "https://api.upstream.ai"
              token: "real_token"
            # camera section missing
            """
        ),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError) as excinfo:
        load_config(incomplete)
    assert "camera" in str(excinfo.value).lower()


def test_load_rejects_placeholder_token(tmp_path: Path) -> None:
    cfg_path = tmp_path / "placeholder.yaml"
    cfg_path.write_text(
        VALID_YAML.replace("real_secret_token_value", PLACEHOLDER_TOKEN),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError) as excinfo:
        load_config(cfg_path)
    assert "placeholder" in str(excinfo.value).lower()


def test_load_empty_file_raises_config_error(tmp_path: Path) -> None:
    empty = tmp_path / "empty.yaml"
    empty.write_text("", encoding="utf-8")
    with pytest.raises(ConfigError) as excinfo:
        load_config(empty)
    assert "empty" in str(excinfo.value).lower()


def test_load_non_mapping_raises_config_error(tmp_path: Path) -> None:
    list_at_top = tmp_path / "list.yaml"
    list_at_top.write_text("- one\n- two\n", encoding="utf-8")
    with pytest.raises(ConfigError) as excinfo:
        load_config(list_at_top)
    assert "mapping" in str(excinfo.value).lower()


def test_config_error_message_never_contains_input_token(tmp_path: Path) -> None:
    """A wrong-type token field must not leak the offending value."""

    leaky = tmp_path / "leaky.yaml"
    # source_type set to an invalid literal — the input string is "secret-shaped"
    # to assert validation errors don't echo input back.
    leaky.write_text(
        dedent(
            """\
            api:
              base_url: "https://api.upstream.ai"
              token: "sk_live_real_token_DO_NOT_LEAK"
            camera:
              name: "Door"
              source_type: "sk_live_real_token_DO_NOT_LEAK"
              source: 0
            """
        ),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError) as excinfo:
        load_config(leaky)
    assert "sk_live_real_token_DO_NOT_LEAK" not in str(excinfo.value)


# ---------------------------------------------------------------------------
# write_default_config
# ---------------------------------------------------------------------------


def test_write_default_config_creates_file(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "config.yaml"
    write_default_config(target)
    assert target.exists()
    contents = target.read_text(encoding="utf-8")
    assert PLACEHOLDER_TOKEN in contents
    assert "DO NOT COMMIT" in contents


def test_write_default_config_refuses_overwrite_without_force(tmp_path: Path) -> None:
    target = tmp_path / "config.yaml"
    target.write_text("api:\n  base_url: existing\n", encoding="utf-8")
    with pytest.raises(ConfigError) as excinfo:
        write_default_config(target)
    assert "already exists" in str(excinfo.value)
    assert "--force" in str(excinfo.value)


def test_write_default_config_overwrites_with_force(tmp_path: Path) -> None:
    target = tmp_path / "config.yaml"
    target.write_text("# old content\n", encoding="utf-8")
    write_default_config(target, force=True)
    assert PLACEHOLDER_TOKEN in target.read_text(encoding="utf-8")
