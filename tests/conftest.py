"""Shared pytest fixtures for upstream-agent tests.

The ``home_dir`` fixture monkeypatches :func:`pathlib.Path.home` to a
temporary directory so tests never touch the real ``~/.upstream/`` of the
person running them.
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def home_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect ``Path.home()`` to a temporary directory for the test."""

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    return tmp_path


@pytest.fixture
def upstream_dir(home_dir: Path) -> Path:
    """The ``~/.upstream/`` directory inside the redirected fake home."""

    return home_dir / ".upstream"
