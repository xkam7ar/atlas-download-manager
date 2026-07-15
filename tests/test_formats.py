from __future__ import annotations

from atlas.formats import best_media_choices
from atlas.models import FormatInfo


def test_best_media_choices_group_best_formats_by_codec() -> None:
    choices = best_media_choices(
        [
            FormatInfo(
                format_id="401",
                ext="mp4",
                resolution="3840x2160",
                fps=24,
                vcodec="av01.0.12M.08",
                acodec="none",
                filesize=1_500_000_000,
                tbr=4100,
            ),
            FormatInfo(
                format_id="313",
                ext="webm",
                resolution="3840x2160",
                fps=24,
                vcodec="vp9",
                acodec="none",
                filesize=2_900_000_000,
                tbr=8200,
            ),
            FormatInfo(
                format_id="137",
                ext="mp4",
                resolution="1920x1080",
                fps=24,
                vcodec="avc1.640028",
                acodec="none",
                filesize=590_000_000,
                tbr=1600,
            ),
            FormatInfo(
                format_id="18",
                ext="mp4",
                resolution="640x360",
                fps=24,
                vcodec="avc1.42001E",
                acodec="mp4a.40.2",
                filesize=190_000_000,
                tbr=540,
            ),
            FormatInfo(
                format_id="251",
                ext="webm",
                resolution="audio only",
                vcodec="none",
                acodec="opus",
                filesize=42_000_000,
                tbr=118,
            ),
        ]
    )

    labels = [choice.label for choice in choices]
    assert labels[:3] == ["Max quality", "Best AV1", "Best VP9"]
    assert any(choice.label == "Best H.264" and choice.resolution == "1080p" for choice in choices)
    assert all("18" not in choice.format for choice in choices)
    assert choices[0].format == "401+251"
    assert choices[0].container == "mkv"
