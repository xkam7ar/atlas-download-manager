from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from atlas.models import InfoOptions, VideoDownloadOptions


@pytest.mark.parametrize(
    "url",
    [
        "file:///tmp/private.mp4",
        "https://user:password@example.com/watch",
        "https://example.com/watch\nX-Test: injected",
    ],
)
def test_media_models_reject_unsafe_urls(url: str, tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        VideoDownloadOptions(url=url, output_dir=tmp_path)
    with pytest.raises(ValidationError):
        InfoOptions(url=url)


@pytest.mark.parametrize(
    "template",
    [
        "/tmp/%(title)s.%(ext)s",
        "../../outside/%(title)s.%(ext)s",
        r"C:\outside\%(title)s.%(ext)s",
        "folder/../outside/%(title)s.%(ext)s",
    ],
)
def test_media_filename_template_cannot_escape_output(
    template: str,
    tmp_path: Path,
) -> None:
    with pytest.raises(ValidationError, match="relative path under output_dir"):
        VideoDownloadOptions(
            url="https://example.com/watch?v=1",
            output_dir=tmp_path,
            filename_template=template,
        )


def test_media_filename_template_allows_relative_subdirectories(tmp_path: Path) -> None:
    options = VideoDownloadOptions(
        url="https://example.com/watch?v=1",
        output_dir=tmp_path,
        filename_template="channel/%(title)s.%(ext)s",
    )

    assert options.filename_template == "channel/%(title)s.%(ext)s"
