"""Shared test isolation for machine-local Atlas settings."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from atlas.theme import configure_visuals


@pytest.fixture(autouse=True)
def isolate_atlas_environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Keep host configuration, visual state, and Atlas settings out of every test."""

    for name in tuple(os.environ):
        if name.startswith("ATLAS_"):
            monkeypatch.delenv(name, raising=False)
    home = tmp_path / "profile"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(home / ".local" / "share"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(home / ".cache"))
    monkeypatch.setenv("XDG_STATE_HOME", str(home / ".local" / "state"))
    configure_visuals(plain=False, unicode=True, color=True, motion=True, env={})
