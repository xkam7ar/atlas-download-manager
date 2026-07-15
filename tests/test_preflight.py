from __future__ import annotations

from pathlib import Path

import pytest

from atlas.config import AtlasSettings
from atlas.errors import DependencyMissingError
from atlas.models import BatchKind, DownloadPlan
from atlas.preflight import ensure_download_dependencies


def _settings(tmp_path: Path) -> AtlasSettings:
    return AtlasSettings(output_dir=tmp_path)


def _plan(tmp_path: Path) -> DownloadPlan:
    return DownloadPlan(
        url="https://example.com/watch?v=1",
        output_dir=tmp_path,
        outtmpl=str(tmp_path / "%(title)s.%(ext)s"),
        format="bestaudio/best",
        noplaylist=True,
    )


def test_preflight_allows_present_dependencies(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("atlas.preflight.which", lambda tool: f"/usr/local/bin/{tool}")

    ensure_download_dependencies(_settings(tmp_path), BatchKind.audio, _plan(tmp_path))


def test_preflight_reports_missing_audio_dependency(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "atlas.preflight.which",
        lambda tool: None if tool == "ffmpeg" else f"/usr/local/bin/{tool}",
    )

    with pytest.raises(DependencyMissingError, match="ffmpeg is required for audio extraction"):
        ensure_download_dependencies(_settings(tmp_path), BatchKind.audio, _plan(tmp_path))


def test_preflight_reports_multiple_missing_video_dependencies(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("atlas.preflight.which", lambda _tool: None)

    with pytest.raises(DependencyMissingError, match="ffmpeg and ffprobe are required"):
        ensure_download_dependencies(_settings(tmp_path), BatchKind.video, _plan(tmp_path))
