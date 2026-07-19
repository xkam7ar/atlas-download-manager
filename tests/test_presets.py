from __future__ import annotations

from pathlib import Path

import pytest

from atlas.config import AtlasSettings
from atlas.errors import PlanningError
from atlas.models import (
    AudioCodec,
    AudioDownloadOptions,
    Container,
    InfoOptions,
    VideoDownloadOptions,
)
from atlas.presets import (
    DEFAULT_AUDIO_FORMAT,
    DEFAULT_VIDEO_FORMAT,
    OUTTMPL,
    PresetBuilder,
    build_audio_opts,
    build_info_opts,
    build_video_opts,
    redact_ydl_opts,
)
from atlas.urls import (
    YoutubeUrlKind,
    classify_youtube_url,
    is_explicit_playlist_url,
    is_youtube_collection_url,
)


def test_video_preset_defaults(tmp_path: Path) -> None:
    settings = AtlasSettings(output_dir=tmp_path, archive_file=tmp_path / "archive.txt")
    options = VideoDownloadOptions(
        url="https://example.com/watch?v=1",
        output_dir=tmp_path,
        archive_file=settings.archive_file,
    )

    opts = build_video_opts(options, settings)

    assert opts["format"] == DEFAULT_VIDEO_FORMAT
    assert opts["merge_output_format"] == Container.mkv.value
    assert opts["noplaylist"] is True
    assert opts["noprogress"] is True
    assert opts["writeinfojson"] is True
    assert opts["writethumbnail"] is True
    assert opts["download_archive"] == str(settings.archive_file)
    assert OUTTMPL in opts["outtmpl"]


def test_json_media_preset_stays_quiet_even_when_verbose(tmp_path: Path) -> None:
    settings = AtlasSettings(output_dir=tmp_path, archive_file=tmp_path / "archive.txt")
    opts = build_video_opts(
        VideoDownloadOptions(
            url="https://example.com/watch?v=1",
            output_dir=tmp_path,
            archive_file=settings.archive_file,
            json_output=True,
            verbose=True,
        ),
        settings,
    )

    assert opts["quiet"] is True
    assert opts["no_warnings"] is True
    assert opts["noprogress"] is True


def test_preset_builder_exposes_video_and_audio_opts(tmp_path: Path) -> None:
    settings = AtlasSettings(output_dir=tmp_path, archive_file=tmp_path / "archive.txt")
    builder = PresetBuilder(settings)

    video_opts = builder.video_opts(
        VideoDownloadOptions(
            url="https://example.com/watch?v=1",
            output_dir=tmp_path,
            archive_file=settings.archive_file,
        )
    )
    audio_opts = builder.audio_opts(
        AudioDownloadOptions(
            url="https://example.com/watch?v=1",
            output_dir=tmp_path,
            archive_file=settings.archive_file,
        )
    )

    assert video_opts["format"] == DEFAULT_VIDEO_FORMAT
    assert audio_opts["format"] == DEFAULT_AUDIO_FORMAT


def test_video_preset_attaches_postprocessor_hooks(tmp_path: Path) -> None:
    settings = AtlasSettings(output_dir=tmp_path, archive_file=tmp_path / "archive.txt")
    options = VideoDownloadOptions(
        url="https://example.com/watch?v=1",
        output_dir=tmp_path,
        archive_file=settings.archive_file,
    )

    def hook(_event) -> None:
        return None

    opts = build_video_opts(options, settings, postprocessor_hooks=[hook])

    assert opts["postprocessor_hooks"] == [hook]


def test_audio_preset_defaults(tmp_path: Path) -> None:
    settings = AtlasSettings(output_dir=tmp_path, archive_file=tmp_path / "archive.txt")
    options = AudioDownloadOptions(
        url="https://example.com/watch?v=1",
        output_dir=tmp_path,
        archive_file=settings.archive_file,
        codec=AudioCodec.mp3,
        quality=2,
    )

    opts = build_audio_opts(options, settings)

    assert opts["format"] == DEFAULT_AUDIO_FORMAT
    extract = opts["postprocessors"][0]
    assert extract["key"] == "FFmpegExtractAudio"
    assert extract["preferredcodec"] == "mp3"
    assert extract["preferredquality"] == "2"


def test_playlist_preset_skips_unavailable_download_entries(tmp_path: Path) -> None:
    settings = AtlasSettings(output_dir=tmp_path, archive_file=tmp_path / "archive.txt")
    options = VideoDownloadOptions(
        url="https://www.youtube.com/playlist?list=PL123",
        output_dir=tmp_path,
        archive_file=settings.archive_file,
        playlist=True,
    )

    opts = build_video_opts(options, settings)

    assert opts["noplaylist"] is False
    assert opts["ignoreerrors"] == "only_download"


def test_media_sidecar_only_modes_pass_skip_download_to_ytdlp(tmp_path: Path) -> None:
    settings = AtlasSettings(output_dir=tmp_path, archive_file=tmp_path / "archive.txt")

    subtitle_opts = build_video_opts(
        VideoDownloadOptions(
            url="https://example.com/watch?v=1",
            output_dir=tmp_path,
            archive_file=settings.archive_file,
            subtitle_only=True,
            sub_lang="en",
        ),
        settings,
    )
    thumbnail_opts = build_video_opts(
        VideoDownloadOptions(
            url="https://example.com/watch?v=1",
            output_dir=tmp_path,
            archive_file=settings.archive_file,
            thumbnail_only=True,
        ),
        settings,
    )
    info_opts = build_audio_opts(
        AudioDownloadOptions(
            url="https://example.com/watch?v=1",
            output_dir=tmp_path,
            archive_file=settings.archive_file,
            info_only=True,
            codec=AudioCodec.mp3,
        ),
        settings,
    )

    assert subtitle_opts["skip_download"] is True
    assert subtitle_opts["writesubtitles"] is True
    assert subtitle_opts["subtitleslangs"] == ["en"]
    assert subtitle_opts["postprocessors"] == []
    assert thumbnail_opts["skip_download"] is True
    assert thumbnail_opts["writethumbnail"] is True
    assert thumbnail_opts["writeinfojson"] is False
    assert thumbnail_opts["postprocessors"] == []
    assert info_opts["skip_download"] is True
    assert info_opts["writeinfojson"] is True
    assert info_opts["writethumbnail"] is False
    assert info_opts["postprocessors"] == []


def test_aria2_can_be_disabled(tmp_path: Path) -> None:
    settings = AtlasSettings(output_dir=tmp_path, archive_file=tmp_path / "archive.txt")
    options = VideoDownloadOptions(
        url="https://example.com/watch?v=1",
        output_dir=tmp_path,
        archive_file=settings.archive_file,
        use_aria2=False,
    )

    opts = build_video_opts(options, settings)

    assert "external_downloader" not in opts
    assert "external_downloader_args" not in opts


def test_aria2_options_are_protocol_scoped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("atlas.planner.which", lambda name: f"/opt/bin/{name}")
    settings = AtlasSettings(
        output_dir=tmp_path,
        archive_file=tmp_path / "archive.txt",
        aria2_connections=16,
        aria2_splits=16,
        aria2_chunk_size="1M",
    )
    options = VideoDownloadOptions(
        url="https://example.com/watch?v=1",
        output_dir=tmp_path,
        archive_file=settings.archive_file,
    )

    opts = build_video_opts(options, settings)

    assert opts["external_downloader"] == {"http": "aria2c", "https": "aria2c"}
    assert opts["external_downloader_args"]["aria2c"] == [
        "-x16",
        "-s16",
        "-k1M",
        "--continue=true",
        "--console-log-level=warn",
        "--summary-interval=0",
        "--show-console-readout=false",
        "--download-result=hide",
    ]


def test_media_reliability_options_are_passed_to_ytdlp(tmp_path: Path) -> None:
    settings = AtlasSettings(output_dir=tmp_path, archive_file=tmp_path / "archive.txt")
    options = VideoDownloadOptions(
        url="https://example.com/watch?v=1",
        output_dir=tmp_path,
        archive_file=settings.archive_file,
        file_access_retries=5,
        concurrent_fragments=8,
        retry_sleep=["http:1", "fragment:linear=1::10", "extractor:exp=1:2:8"],
        skip_unavailable_fragments=False,
        throttled_rate="64K",
        http_chunk_size="10M",
        socket_timeout=12,
        source_address="127.0.0.1",
        impersonate="chrome",
        extractor_args=["youtube:player_client=android,ios;player_skip=webpage"],
    )

    opts = build_video_opts(options, settings)

    assert opts["file_access_retries"] == 5
    assert opts["concurrent_fragment_downloads"] == 8
    assert opts["skip_unavailable_fragments"] is False
    assert opts["throttledratelimit"] == 64 * 1024
    assert opts["http_chunk_size"] == 10 * 1024 * 1024
    assert opts["socket_timeout"] == 12
    assert opts["source_address"] == "127.0.0.1"
    assert opts["impersonate"] == "chrome"
    assert opts["extractor_args"] == {
        "youtube": {
            "player_client": ["android", "ios"],
            "player_skip": ["webpage"],
        }
    }
    retry_sleep = opts["retry_sleep_functions"]
    assert retry_sleep["http"](3) == 1.0
    assert retry_sleep["fragment"](3) == 3.0
    assert retry_sleep["extractor"](4) == 8.0


def test_media_selection_sections_and_sponsorblock_are_passed_to_ytdlp(
    tmp_path: Path,
) -> None:
    settings = AtlasSettings(output_dir=tmp_path, archive_file=tmp_path / "archive.txt")
    options = VideoDownloadOptions(
        url="https://example.com/watch?v=1",
        output_dir=tmp_path,
        archive_file=settings.archive_file,
        match_filters=["duration>?60"],
        break_match_filters=["view_count<10"],
        max_downloads=2,
        break_on_existing=True,
        break_on_reject=True,
        break_per_input=True,
        date_after="20240101",
        date_before="20240601",
        min_filesize="10M",
        max_filesize="1G",
        live_from_start=True,
        download_sections=["intro", "*10:15-inf"],
        sponsorblock_mark=["sponsor"],
        sponsorblock_remove=["selfpromo"],
        sponsorblock_chapter_title="[SB] %(category_names)l",
        sponsorblock_api="https://sb.example",
    )

    opts = build_video_opts(options, settings)

    assert callable(opts["match_filter"])
    assert "2024-01-01" in str(opts["daterange"])
    assert "2024-06-01" in str(opts["daterange"])
    assert opts["min_filesize"] == 10 * 1024 * 1024
    assert opts["max_filesize"] == 1024**3
    assert opts["max_downloads"] == 2
    assert opts["break_on_existing"] is True
    assert opts["break_on_reject"] is True
    assert opts["break_per_url"] is True
    assert opts["live_from_start"] is True
    assert callable(opts["download_ranges"])
    assert opts["postprocessors"][0]["key"] == "SponsorBlock"
    assert opts["postprocessors"][0]["api"] == "https://sb.example"
    assert opts["postprocessors"][1]["key"] == "ModifyChapters"
    assert opts["postprocessors"][1]["remove_sponsor_segments"] == ["selfpromo"]

    redacted = redact_ydl_opts(opts)
    assert redacted["match_filter"] == "<callable>"
    assert redacted["download_ranges"] == "<callable>"


def test_download_options_expand_paths() -> None:
    options = VideoDownloadOptions(
        url=" https://example.com/watch?v=1 ",
        output_dir=Path("~/Movies/custom-atlas"),
        archive_file=Path("~/Library/atlas/archive.txt"),
    )

    assert options.url == "https://example.com/watch?v=1"
    assert options.output_dir == Path("~/Movies/custom-atlas").expanduser()
    assert options.archive_file == Path("~/Library/atlas/archive.txt").expanduser()


def test_redact_ydl_opts_hides_runtime_logger() -> None:
    class Logger:
        def debug(self, msg: str) -> None:
            _ = msg

        def warning(self, msg: str) -> None:
            _ = msg

        def error(self, msg: str) -> None:
            _ = msg

    redacted = redact_ydl_opts(
        {
            "logger": Logger(),
            "progress_hooks": [lambda _event: None],
            "postprocessor_hooks": [lambda _event: None],
        }
    )

    assert redacted["logger"] == "<yt-dlp-logger>"
    assert redacted["progress_hooks"] == ["<progress-hook>"]
    assert redacted["postprocessor_hooks"] == ["<progress-hook>"]


def test_redact_ydl_opts_hides_retry_sleep_callables() -> None:
    redacted = redact_ydl_opts({"retry_sleep_functions": {"http": lambda _attempt: 1.0}})

    assert redacted["retry_sleep_functions"] == {"http": "<callable>"}


def test_redact_ydl_opts_hides_proxy_credentials_and_nested_tokens() -> None:
    redacted = redact_ydl_opts(
        {
            "proxy": "http://alice:sentinel-secret@proxy.example:8080",
            "extractor_args": {"youtube": {"po_token": "sentinel-secret"}},
            "referer": "https://example.com/?access_token=sentinel-secret",
        }
    )

    assert "sentinel-secret" not in repr(redacted)
    assert redacted["proxy"] == "<redacted>"
    assert redacted["extractor_args"]["youtube"]["po_token"] == "<redacted>"
    assert redacted["referer"] == "<redacted>"


def test_watch_url_with_radio_playlist_param_stays_single_video(tmp_path: Path) -> None:
    settings = AtlasSettings(output_dir=tmp_path, archive_file=tmp_path / "archive.txt")
    options = VideoDownloadOptions(
        url="https://www.youtube.com/watch?v=-EdRTo61T84&list=RD-EdRTo61T84&start_radio=1",
        output_dir=tmp_path,
        archive_file=settings.archive_file,
        playlist=True,
    )

    opts = build_video_opts(options, settings)

    assert opts["noplaylist"] is True


def test_audio_watch_url_with_radio_playlist_param_stays_single_video(tmp_path: Path) -> None:
    settings = AtlasSettings(output_dir=tmp_path, archive_file=tmp_path / "archive.txt")
    options = AudioDownloadOptions(
        url="https://www.youtube.com/watch?v=-EdRTo61T84&list=RD-EdRTo61T84&start_radio=1",
        output_dir=tmp_path,
        archive_file=settings.archive_file,
        playlist=True,
    )

    opts = build_audio_opts(options, settings)

    assert opts["noplaylist"] is True


def test_info_watch_url_with_radio_playlist_param_stays_single_video() -> None:
    opts = build_info_opts(
        InfoOptions(
            url="https://www.youtube.com/watch?v=-EdRTo61T84&list=RD-EdRTo61T84&start_radio=1",
            playlist=True,
        )
    )

    assert opts["noplaylist"] is True


def test_info_options_include_cookie_file(tmp_path: Path) -> None:
    cookie_file = tmp_path / "cookies.txt"

    opts = build_info_opts(
        InfoOptions(
            url="https://example.com/watch?v=1",
            cookies_file=cookie_file,
        )
    )

    assert opts["cookiefile"] == str(cookie_file)


def test_info_options_bound_and_flatten_youtube_collection_probe() -> None:
    opts = build_info_opts(
        InfoOptions(
            url="https://www.youtube.com/@AveryYapps/videos",
            playlist=True,
            playlist_items="2-4",
            playlist_start=2,
            playlist_end=4,
            socket_timeout=8,
            flat_playlist=True,
            verbose=True,
            json_output=True,
        )
    )

    assert opts["noplaylist"] is False
    assert opts["playlist_items"] == "2-4"
    assert opts["playliststart"] == 2
    assert opts["playlistend"] == 4
    assert opts["socket_timeout"] == 8
    assert opts["extract_flat"] == "in_playlist"
    assert opts["lazy_playlist"] is True
    assert opts["quiet"] is True
    assert opts["no_warnings"] is True


def test_info_options_reject_unbounded_youtube_collection_probe() -> None:
    with pytest.raises(PlanningError, match="finite selection bound"):
        build_info_opts(
            InfoOptions(
                url="https://www.youtube.com/@AveryYapps/videos",
                playlist=True,
            )
        )


def test_explicit_youtube_playlist_url_can_enable_playlist(tmp_path: Path) -> None:
    settings = AtlasSettings(output_dir=tmp_path, archive_file=tmp_path / "archive.txt")
    options = VideoDownloadOptions(
        url="https://www.youtube.com/playlist?list=PL123",
        output_dir=tmp_path,
        archive_file=settings.archive_file,
        playlist=True,
    )

    opts = build_video_opts(options, settings)

    assert opts["noplaylist"] is False


def test_explicit_playlist_url_still_requires_playlist_request(tmp_path: Path) -> None:
    settings = AtlasSettings(output_dir=tmp_path, archive_file=tmp_path / "archive.txt")
    options = VideoDownloadOptions(
        url="https://www.youtube.com/playlist?list=PL123",
        output_dir=tmp_path,
        archive_file=settings.archive_file,
    )

    with pytest.raises(PlanningError, match="explicit playlist URL"):
        build_video_opts(options, settings)


def test_playlist_url_classifier() -> None:
    assert is_explicit_playlist_url("https://www.youtube.com/playlist?list=PL123") is True
    assert is_explicit_playlist_url("https://www.youtube.com/embed/videoseries?list=PL123") is True
    assert (
        is_explicit_playlist_url(
            "https://www.youtube.com/watch?v=-EdRTo61T84&list=RD-EdRTo61T84&start_radio=1"
        )
        is False
    )
    assert is_explicit_playlist_url("https://youtu.be/abc123?list=PL123") is False


@pytest.mark.parametrize(
    ("url", "kind"),
    [
        ("https://www.youtube.com/playlist?list=PL123", YoutubeUrlKind.playlist),
        (
            "https://www.youtube.com/watch?v=abc&list=RDabc&start_radio=1",
            YoutubeUrlKind.watch_playlist_context,
        ),
        ("https://youtu.be/abc123", YoutubeUrlKind.single),
        ("https://www.youtube.com/@AveryYapps/videos", YoutubeUrlKind.collection),
        (
            "https://www.youtube.com/channel/UCU28LWFMn1GN0coMTBFTo2w/shorts",
            YoutubeUrlKind.collection,
        ),
        ("https://www.youtube.com/feed/subscriptions", YoutubeUrlKind.collection),
        ("https://example.com/channel/videos", YoutubeUrlKind.other),
    ],
)
def test_youtube_url_classifier_distinguishes_collections(
    url: str,
    kind: YoutubeUrlKind,
) -> None:
    assert classify_youtube_url(url) == kind
    assert is_youtube_collection_url(url) is (kind == YoutubeUrlKind.collection)
