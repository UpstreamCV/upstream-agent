"""Collector config: defaults, overrides, non-localhost token posture,
unknown-key rejection, and that a collector needs no extra config (camera
stays required for streaming, but the collector ignores it)."""

from __future__ import annotations

from pathlib import Path

import pytest

from upstream_agent.config import CollectorConfig, Config, load_config
from upstream_agent.errors import ConfigError

_REAL_TOKEN = "ups_test_ABCDEFGH_SECRETSECRETSECRET22"


def test_default_collector_config() -> None:
    c = CollectorConfig()
    assert c.enabled is False
    assert c.host == "127.0.0.1"
    assert c.port == 8756
    assert c.spool_dir == "~/.upstream/collector-spool"
    assert c.flush_interval_seconds == 5
    assert c.max_batch_size == 500
    assert c.max_spool_observations == 50_000
    assert c.max_spool_bytes == 104_857_600
    assert c.inbound_token is None


def test_custom_port_and_spool_dir() -> None:
    c = CollectorConfig(port=9000, spool_dir="/tmp/spool")
    assert c.port == 9000
    assert c.spool_dir == "/tmp/spool"


def test_non_localhost_host_requires_inbound_token() -> None:
    with pytest.raises(ValueError, match="inbound_token"):
        CollectorConfig(host="0.0.0.0")  # noqa: S104 - exercising the guard


def test_non_localhost_host_with_inbound_token_is_allowed() -> None:
    c = CollectorConfig(host="0.0.0.0", inbound_token="secret")  # noqa: S104
    assert c.host == "0.0.0.0"  # noqa: S104


def test_unknown_collector_key_rejected() -> None:
    with pytest.raises(Exception):  # noqa: B017 - pydantic ValidationError on extra
        CollectorConfig(bogus=True)  # type: ignore[call-arg]


def test_config_without_collector_section_uses_defaults() -> None:
    cfg = Config.model_validate(
        {
            "api": {"base_url": "http://api.test", "token": _REAL_TOKEN},
            "camera": {"name": "Front Door", "source_type": "webcam", "source": 0},
        }
    )
    # Collector is always present with safe defaults — no extra config needed.
    assert cfg.collector.enabled is False
    assert cfg.collector.port == 8756


def test_load_config_parses_collector_block(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        f"""
api:
  base_url: "http://api.test"
  token: "{_REAL_TOKEN}"
camera:
  name: "Front Door"
  source_type: "webcam"
  source: 0
collector:
  enabled: true
  port: 9100
  spool_dir: "{tmp_path}/spool"
""",
        encoding="utf-8",
    )
    cfg = load_config(config_path)
    assert cfg.collector.enabled is True
    assert cfg.collector.port == 9100
    assert cfg.collector.spool_dir == f"{tmp_path}/spool"


def test_unknown_collector_key_in_yaml_rejected(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        f"""
api:
  base_url: "http://api.test"
  token: "{_REAL_TOKEN}"
camera:
  name: "Front Door"
  source_type: "webcam"
  source: 0
collector:
  enabled: true
  nope: 1
""",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError):
        load_config(config_path)
