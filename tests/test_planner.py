from __future__ import annotations

from pathlib import Path

import pytest

from atlas.config import AtlasSettings
from atlas.errors import PlanningError
from atlas.models import (
    AudioCodec,
    AudioDownloadOptions,
    Container,
    DownloadEngineChoice,
    FpsChoice,
    HdrChoice,
    InfoOptions,
    OrganizeMode,
    QualityIntent,
    ResolutionChoice,
    SubtitleMode,
    VideoCodecChoice,
    VideoDownloadOptions,
)
from atlas.planner import SmartPlanner


def _settings(tmp_path: Path) -> AtlasSettings:
    return AtlasSettings(output_dir=tmp_path, archive_file=tmp_path / "archive.txt")


def _video(tmp_path: Path, **kwargs: object) -> VideoDownloadOptions:
    url = str(kwargs.pop("url", "https://example.com/watch?v=1"))
    return VideoDownloadOptions(
        url=url,
        output_dir=tmp_path,
        archive_file=tmp_path / "archive.txt",
        **kwargs,
    )


def _audio(tmp_path: Path, **kwargs: object) -> AudioDownloadOptions:
    url = str(kwargs.pop("url", "https://example.com/watch?v=1"))
    return AudioDownloadOptions(
        url=url,
        output_dir=tmp_path,
        archive_file=tmp_path / "archive.txt",
        **kwargs,
    )


def test_max_quality_auto_container_resolves_to_mkv(tmp_path: Path) -> None:
    plan = SmartPlanner(_settings(tmp_path)).plan_video(_video(tmp_path))

    assert plan.format == "bestvideo*+bestaudio/best"
    assert plan.merge_output_format == "mkv"


def test_balanced_quality_caps_resolution(tmp_path: Path) -> None:
    plan = SmartPlanner(_settings(tmp_path)).plan_video(
        _video(tmp_path, quality=QualityIntent.balanced)
    )

    assert "[height<=1440]" in plan.format
    assert plan.merge_output_format == "mkv"


def test_compatible_quality_prefers_mp4_codecs(tmp_path: Path) -> None:
    plan = SmartPlanner(_settings(tmp_path)).plan_video(
        _video(tmp_path, quality=QualityIntent.compatible)
    )

    assert plan.merge_output_format == "mp4"
    assert "ext=mp4" in plan.format
    assert "vcodec~=" in plan.format
    assert "bestaudio[ext=m4a]" in plan.format


def test_compatible_quality_rejects_non_mp4_container(tmp_path: Path) -> None:
    with pytest.raises(PlanningError, match="compatible quality requires"):
        SmartPlanner(_settings(tmp_path)).plan_video(
            _video(tmp_path, quality=QualityIntent.compatible, container=Container.webm)
        )


def test_compatible_quality_rejects_web_codecs(tmp_path: Path) -> None:
    with pytest.raises(PlanningError, match="compatible quality can use"):
        SmartPlanner(_settings(tmp_path)).plan_video(
            _video(
                tmp_path,
                quality=QualityIntent.compatible,
                video_codec=VideoCodecChoice.av1,
            )
        )


def test_small_quality_uses_size_friendly_filters(tmp_path: Path) -> None:
    plan = SmartPlanner(_settings(tmp_path)).plan_video(
        _video(tmp_path, quality=QualityIntent.small)
    )

    assert "[height<=720]" in plan.format
    assert "[tbr<=2500]" in plan.format
    assert plan.format_sort == ["+size", "res", "fps"]


def test_video_controls_build_format_filters(tmp_path: Path) -> None:
    plan = SmartPlanner(_settings(tmp_path)).plan_video(
        _video(
            tmp_path,
            resolution=ResolutionChoice.r1080,
            video_codec=VideoCodecChoice.h264,
            fps=FpsChoice.f30,
            hdr=HdrChoice.avoid,
        )
    )

    assert "[height<=1080]" in plan.format
    assert "[vcodec^=avc1]" in plan.format
    assert "[fps<=30]" in plan.format
    assert "[dynamic_range=SDR]" in plan.format


def test_filtered_max_quality_does_not_fall_back_to_unfiltered_best(
    tmp_path: Path,
) -> None:
    plan = SmartPlanner(_settings(tmp_path)).plan_video(
        _video(
            tmp_path,
            container=Container.mp4,
            video_codec=VideoCodecChoice.hevc,
        )
    )

    assert "bestvideo*[vcodec~='^(hvc1|hev1)']+bestaudio" in plan.format
    assert "best[vcodec~='^(hvc1|hev1)']" in plan.format
    assert not plan.format.endswith("/best")


def test_subtitle_and_playlist_options_are_planned_for_explicit_playlists(tmp_path: Path) -> None:
    request = _video(
        tmp_path,
        url="https://www.youtube.com/playlist?list=PL123",
        playlist=True,
        playlist_items="1-10,15",
        playlist_start=2,
        playlist_end=25,
        subtitle_mode=SubtitleMode.auto,
        sub_lang="en,en-US",
        embed_subs=True,
        split_chapters=True,
    )

    plan = SmartPlanner(_settings(tmp_path)).plan_video(request)

    assert plan.noplaylist is False
    assert plan.playlist_items == "1-10,15"
    assert plan.playlist_start == 2
    assert plan.playlist_end == 25
    assert plan.subtitle_mode == SubtitleMode.auto
    assert plan.sub_lang == "en,en-US"
    assert plan.embed_subs is True
    assert plan.split_chapters is True
    assert plan.ignore_unavailable_playlist_entries is True
    assert "explicit playlist URL accepted" in plan.planner_notes


def test_explicit_playlist_requires_playlist_intent_in_video_flow(tmp_path: Path) -> None:
    with pytest.raises(PlanningError, match="explicit playlist URL"):
        SmartPlanner(_settings(tmp_path)).plan_video(
            _video(tmp_path, url="https://www.youtube.com/playlist?list=PL123")
        )


def test_playlist_ranges_are_ignored_for_watch_urls(tmp_path: Path) -> None:
    request = _video(
        tmp_path,
        url="https://www.youtube.com/watch?v=abc&list=RDabc&start_radio=1",
        playlist=True,
        playlist_items="1-50",
    )

    plan = SmartPlanner(_settings(tmp_path)).plan_video(request)

    assert plan.noplaylist is True
    assert plan.playlist_items is None
    assert plan.watch_playlist_params_detected is True
    assert "watch URL playlist/radio parameters kept single-item" in plan.planner_notes


def test_youtube_channel_collection_requires_explicit_playlist_intent(tmp_path: Path) -> None:
    with pytest.raises(PlanningError, match="YouTube collection URL"):
        SmartPlanner(_settings(tmp_path)).plan_video(
            _video(tmp_path, url="https://www.youtube.com/@AveryYapps/videos")
        )


def test_youtube_channel_collection_requires_finite_selection_bound(tmp_path: Path) -> None:
    with pytest.raises(PlanningError, match="finite selection bound"):
        SmartPlanner(_settings(tmp_path)).plan_video(
            _video(
                tmp_path,
                url="https://www.youtube.com/@AveryYapps/videos",
                playlist=True,
            )
        )

    with pytest.raises(PlanningError, match="finite selection bound"):
        SmartPlanner(_settings(tmp_path)).plan_video(
            _video(
                tmp_path,
                url="https://www.youtube.com/@AveryYapps/videos",
                playlist=True,
                playlist_items="20-",
            )
        )


def test_youtube_channel_collection_preserves_playlist_bounds(tmp_path: Path) -> None:
    plan = SmartPlanner(_settings(tmp_path)).plan_video(
        _video(
            tmp_path,
            url="https://www.youtube.com/@AveryYapps/videos",
            playlist=True,
            playlist_items="1-3,5",
            socket_timeout=12,
            json_output=True,
        )
    )

    assert plan.noplaylist is False
    assert plan.playlist_items == "1-3,5"
    assert plan.socket_timeout == 12
    assert plan.youtube_collection_url_detected is True
    assert plan.playlist_url_detected is False
    assert plan.json_output is True
    assert "bounded YouTube collection URL accepted" in plan.planner_notes


def test_youtube_channel_collection_accepts_playlist_end_bound(tmp_path: Path) -> None:
    plan = SmartPlanner(_settings(tmp_path)).plan_audio(
        _audio(
            tmp_path,
            url="https://www.youtube.com/channel/UCU28LWFMn1GN0coMTBFTo2w/videos",
            playlist=True,
            playlist_start=2,
            playlist_end=4,
        )
    )

    assert plan.noplaylist is False
    assert plan.playlist_start == 2
    assert plan.playlist_end == 4


def test_media_sidecar_modes_skip_primary_download(tmp_path: Path) -> None:
    subtitle_plan = SmartPlanner(_settings(tmp_path)).plan_video(
        _video(tmp_path, subtitle_only=True)
    )
    thumbnail_plan = SmartPlanner(_settings(tmp_path)).plan_video(
        _video(tmp_path, thumbnail_only=True)
    )
    info_plan = SmartPlanner(_settings(tmp_path)).plan_audio(
        _audio(tmp_path, codec=AudioCodec.mp3, info_only=True)
    )

    assert subtitle_plan.skip_download is True
    assert subtitle_plan.subtitle_mode == SubtitleMode.manual
    assert subtitle_plan.postprocessors == []
    assert thumbnail_plan.skip_download is True
    assert thumbnail_plan.write_thumbnail is True
    assert thumbnail_plan.write_info_json is False
    assert info_plan.skip_download is True
    assert info_plan.write_info_json is True
    assert info_plan.postprocessors == []


def test_output_organization_and_filename_template(tmp_path: Path) -> None:
    playlist_plan = SmartPlanner(_settings(tmp_path)).plan_video(
        _video(tmp_path, organize=OrganizeMode.playlist)
    )
    flat_plan = SmartPlanner(_settings(tmp_path)).plan_video(
        _video(tmp_path, filename_template="%(title)s.%(ext)s")
    )

    assert "%(playlist_title|playlist)s" in playlist_plan.outtmpl
    assert flat_plan.outtmpl == str(tmp_path / "%(title)s.%(ext)s")


def test_rate_limit_and_cookie_file_are_planned(tmp_path: Path) -> None:
    cookies = tmp_path / "cookies.txt"
    plan = SmartPlanner(_settings(tmp_path)).plan_video(
        _video(tmp_path, cookies_file=cookies, rate_limit="5M")
    )

    assert plan.cookies_file == cookies
    assert plan.rate_limit == 5 * 1024 * 1024


def test_advanced_media_reliability_options_are_planned(tmp_path: Path) -> None:
    plan = SmartPlanner(_settings(tmp_path)).plan_video(
        _video(
            tmp_path,
            file_access_retries=7,
            concurrent_fragments=8,
            retry_sleep=["http:1", "fragment:linear=1::10"],
            skip_unavailable_fragments=False,
            throttled_rate="64K",
            http_chunk_size="10M",
            socket_timeout=12.5,
            source_address="127.0.0.1",
            impersonate="chrome",
            extractor_args=["youtube:player_client=android,ios"],
        )
    )

    assert plan.file_access_retries == 7
    assert plan.concurrent_fragment_downloads == 8
    assert plan.retry_sleep == ["http:1", "fragment:linear=1::10"]
    assert plan.skip_unavailable_fragments is False
    assert plan.throttled_rate == 64 * 1024
    assert plan.http_chunk_size == 10 * 1024 * 1024
    assert plan.socket_timeout == 12.5
    assert plan.source_address == "127.0.0.1"
    assert plan.impersonate == "chrome"
    assert plan.extractor_args == ["youtube:player_client=android,ios"]


def test_media_selection_sections_and_sponsorblock_are_planned(tmp_path: Path) -> None:
    plan = SmartPlanner(_settings(tmp_path)).plan_video(
        _video(
            tmp_path,
            match_filters=["duration>?60"],
            break_match_filters=["view_count<10"],
            max_downloads=3,
            break_on_existing=True,
            break_on_reject=True,
            break_per_input=True,
            date_after="20240101",
            date_before="20240601",
            min_filesize="10M",
            max_filesize="1G",
            reject_live=True,
            reject_upcoming=True,
            live_from_start=True,
            download_sections=["intro", "*10:15-inf"],
            sponsorblock_mark=["sponsor"],
            sponsorblock_remove=["selfpromo"],
            sponsorblock_chapter_title="[SB] %(category_names)l",
            sponsorblock_api="https://sb.example",
        )
    )

    assert plan.match_filters == ["duration>?60", "!is_live", "!is_upcoming"]
    assert plan.break_match_filters == ["view_count<10"]
    assert plan.max_downloads == 3
    assert plan.break_on_existing is True
    assert plan.break_on_reject is True
    assert plan.break_per_input is True
    assert plan.date_after == "20240101"
    assert plan.date_before == "20240601"
    assert plan.min_filesize == 10 * 1024 * 1024
    assert plan.max_filesize == 1024**3
    assert plan.live_from_start is True
    assert plan.download_sections == ["intro", "*10:15-inf"]
    assert plan.postprocessors[0] == {
        "key": "SponsorBlock",
        "categories": ["selfpromo", "sponsor"],
        "when": "after_filter",
        "api": "https://sb.example",
    }
    assert plan.postprocessors[1]["key"] == "ModifyChapters"
    assert plan.postprocessors[1]["remove_sponsor_segments"] == ["selfpromo"]
    assert plan.postprocessors[1]["sponsorblock_chapter_title"] == "[SB] %(category_names)l"


def test_advanced_media_option_validation(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="retry_sleep entries"):
        _video(tmp_path, retry_sleep=["fragment:jitter=1"])

    with pytest.raises(ValueError, match="extractor_args entries"):
        _video(tmp_path, extractor_args=["youtube"])

    with pytest.raises(ValueError, match="sponsorblock_remove"):
        _video(tmp_path, sponsorblock_remove=["chapter"])

    with pytest.raises(PlanningError, match="min_filesize"):
        SmartPlanner(_settings(tmp_path)).plan_video(
            _video(tmp_path, min_filesize="2G", max_filesize="1G")
        )


def test_forced_aria2_requires_binary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("atlas.planner.which", lambda _name: None)

    with pytest.raises(PlanningError):
        SmartPlanner(_settings(tmp_path)).plan_video(
            _video(tmp_path, download_engine=DownloadEngineChoice.aria2)
        )


def test_native_downloader_disables_aria2(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("atlas.planner.which", lambda _name: "/opt/homebrew/bin/aria2c")

    plan = SmartPlanner(_settings(tmp_path)).plan_video(
        _video(tmp_path, download_engine=DownloadEngineChoice.native)
    )

    assert plan.use_aria2 is False


def test_playlist_range_validation(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        _video(tmp_path, playlist_start=10, playlist_end=2)


def test_playlist_item_validation(tmp_path: Path) -> None:
    assert _video(tmp_path, playlist_items="1-10,15,20-").playlist_items == "1-10,15,20-"
    with pytest.raises(ValueError):
        _video(tmp_path, playlist_items="10-2")
    with pytest.raises(ValueError, match="must start at 1"):
        _video(tmp_path, playlist_items="0")
    with pytest.raises(ValueError, match="must start at 1"):
        InfoOptions(url="https://example.com/playlist", playlist_items="0-2")


def test_chunk_size_validation_normalizes_suffix(tmp_path: Path) -> None:
    assert _video(tmp_path, chunk_size="512k").chunk_size == "512K"
    with pytest.raises(ValueError):
        _video(tmp_path, chunk_size="fast")
