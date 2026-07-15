"""Shared test isolation for machine-local Atlas settings."""

from __future__ import annotations

import os

import pytest

from atlas.theme import configure_visuals


@pytest.fixture(autouse=True)
def isolate_atlas_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep host configuration, visual state, and Atlas settings out of every test."""

    for name in tuple(os.environ):
        if name.startswith("ATLAS_"):
            monkeypatch.delenv(name, raising=False)
    configure_visuals(plain=False, unicode=True, color=True, motion=True, env={})
