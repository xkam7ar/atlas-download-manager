from __future__ import annotations

from atlas.media_capabilities import (
    CapabilityStatus,
    MediaCapabilityResolver,
    MediaProfile,
)
from atlas.models import FormatInfo, MediaInfo


def _resolver(formats: list[FormatInfo]) -> MediaCapabilityResolver:
    return MediaCapabilityResolver.from_info(MediaInfo(title="Example", formats=formats))


def test_compatible_profile_uses_available_h264_mp4_without_conversion() -> None:
    resolver = _resolver(
        [
            FormatInfo(
                format_id="137",
                ext="mp4",
                resolution="1920x1080",
                vcodec="avc1.640028",
                acodec="none",
                filesize=190_000_000,
            ),
            FormatInfo(
                format_id="140",
                ext="m4a",
                resolution="audio only",
                vcodec="none",
                acodec="mp4a.40.2",
                filesize=14_000_000,
            ),
        ]
    )

    choice = next(
        choice for choice in resolver.video_profiles() if choice.profile == MediaProfile.compatible
    )

    assert choice.status == CapabilityStatus.available
    assert choice.format_selector == "137+140"
    assert choice.container == "mp4"
    assert choice.video_codec == "H.264"
    assert choice.audio_codec == "AAC"
    assert choice.requires_transcode is False


def test_compatible_profile_warns_when_only_webm_vp9_is_available() -> None:
    resolver = _resolver(
        [
            FormatInfo(
                format_id="248",
                ext="webm",
                resolution="1920x1080",
                vcodec="vp9",
                acodec="none",
                filesize=160_000_000,
            ),
            FormatInfo(
                format_id="251",
                ext="webm",
                resolution="audio only",
                vcodec="none",
                acodec="opus",
                filesize=12_000_000,
            ),
        ]
    )

    choice = next(
        choice for choice in resolver.video_profiles() if choice.profile == MediaProfile.compatible
    )

    assert choice.status == CapabilityStatus.requires_transcode
    assert choice.container == "mp4"
    assert choice.video_codec == "H.264"
    assert choice.audio_codec == "AAC"
    assert choice.requires_ffmpeg is True
    assert choice.requires_transcode is True
    assert "re-encode" in choice.warnings[0]


def test_audio_only_source_hides_video_profiles_but_offers_audio() -> None:
    resolver = _resolver(
        [
            FormatInfo(
                format_id="251",
                ext="webm",
                resolution="audio only",
                vcodec="none",
                acodec="opus",
                filesize=12_000_000,
            )
        ]
    )

    assert {choice.profile for choice in resolver.video_profiles()} == {MediaProfile.audio_best}
    audio_choice = resolver.audio_profiles()[0]
    assert audio_choice.profile == MediaProfile.audio_best
    assert audio_choice.format_selector == "251"
    assert audio_choice.status == CapabilityStatus.available


def test_mp3_profile_requires_ffmpeg_conversion() -> None:
    resolver = _resolver(
        [
            FormatInfo(
                format_id="251",
                ext="webm",
                resolution="audio only",
                vcodec="none",
                acodec="opus",
                filesize=12_000_000,
            )
        ]
    )

    choice = next(
        choice for choice in resolver.audio_profiles() if choice.profile == MediaProfile.audio_mp3
    )

    assert choice.status == CapabilityStatus.requires_transcode
    assert choice.container == "mp3"
    assert choice.audio_codec == "MP3"
    assert choice.requires_ffmpeg is True
