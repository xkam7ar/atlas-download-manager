from __future__ import annotations

import logging

import pytest
from yt_dlp.utils import MaxDownloadsReached

from atlas.config import AtlasSettings
from atlas.engine import MediaProbe, YtdlpEngine, YtdlpLogBridge, clean_ytdlp_error_message
from atlas.errors import EngineError
from atlas.models import DownloadStatus, FormatInfo, InfoOptions, MediaInfo


def test_clean_ytdlp_error_message_removes_prefix() -> None:
    assert clean_ytdlp_error_message(Exception("ERROR: example failed")) == "example failed"


def test_clean_ytdlp_error_message_has_fallback() -> None:
    assert clean_ytdlp_error_message(Exception("")) == "Exception"


def test_clean_ytdlp_error_message_suggests_authorized_cookies() -> None:
    message = clean_ytdlp_error_message(Exception("ERROR: Sign in to confirm your age"))

    assert "user-authorized session" in message
    assert "--cookies-from-browser" in message


def test_clean_ytdlp_error_message_explains_members_only() -> None:
    message = clean_ytdlp_error_message(Exception("ERROR: This video is members-only"))

    assert "members-only" in message
    assert "will not bypass access controls" in message


def test_clean_ytdlp_error_message_explains_live_and_postprocess_failures() -> None:
    live = clean_ytdlp_error_message(Exception("ERROR: This livestream is currently live"))
    postprocess = clean_ytdlp_error_message(Exception("ERROR: ffmpeg postprocess failed"))

    assert "currently live" in live
    assert "--live-from-start" in live
    assert "Post-processing failed after transfer" in postprocess
    assert "not complete until merge" in postprocess


def test_ytdlp_log_bridge_redacts_signed_urls(caplog) -> None:
    caplog.set_level(logging.DEBUG)
    bridge = YtdlpLogBridge(logging.getLogger("atlas.ytdlp-test"))

    bridge.debug("requesting https://cdn.example/file?Signature=TOPSECRET&Expires=999999")

    assert "TOPSECRET" not in caplog.text
    assert "Signature=<redacted>" in caplog.text


def test_media_probe_wraps_engine() -> None:
    class Engine:
        def get_info(self, options: InfoOptions) -> MediaInfo:
            return MediaInfo(title=options.url)

        def list_formats(self, _options: InfoOptions) -> list[FormatInfo]:
            return [FormatInfo(format_id="140")]

    probe = MediaProbe(Engine())  # type: ignore[arg-type]

    assert probe.probe(InfoOptions(url="https://example.com")).title == "https://example.com"
    assert probe.formats(InfoOptions(url="https://example.com"))[0].format_id == "140"


def test_max_downloads_reached_after_finished_hooks_is_success(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[str] = []

    class FakeYoutubeDL:
        def __init__(self, opts) -> None:
            self.opts = opts

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def download(self, _urls) -> int:
            for index in range(2):
                event = {
                    "status": "finished",
                    "filename": f"video-{index}.mp4",
                    "info_dict": {"id": str(index), "playlist_index": index + 1},
                }
                for hook in self.opts["progress_hooks"]:
                    hook(event)
            raise MaxDownloadsReached

    monkeypatch.setattr("atlas.engine.YoutubeDL", FakeYoutubeDL)
    engine = YtdlpEngine(AtlasSettings(output_dir=tmp_path))

    result = engine._download(
        "https://example.com/playlist",
        {
            "max_downloads": 2,
            "progress_hooks": [lambda event: observed.append(str(event["filename"]))],
        },
    )

    assert result.status == DownloadStatus.success
    assert result.message == "Requested download limit reached; 2 downloads complete."
    assert observed == ["video-0.mp4", "video-1.mp4"]


def test_max_downloads_reached_before_finished_hooks_remains_failure(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeYoutubeDL:
        def __init__(self, _opts) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def download(self, _urls) -> int:
            raise MaxDownloadsReached

    monkeypatch.setattr("atlas.engine.YoutubeDL", FakeYoutubeDL)
    engine = YtdlpEngine(AtlasSettings(output_dir=tmp_path))

    with pytest.raises(EngineError, match="Maximum number of downloads reached"):
        engine._download(
            "https://example.com/playlist",
            {"max_downloads": 1},
        )


def test_unexpected_download_failures_remain_failures(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeYoutubeDL:
        def __init__(self, _opts) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def download(self, _urls) -> int:
            raise RuntimeError("sentinel failure")

    monkeypatch.setattr("atlas.engine.YoutubeDL", FakeYoutubeDL)
    engine = YtdlpEngine(AtlasSettings(output_dir=tmp_path))

    with pytest.raises(EngineError, match="sentinel failure"):
        engine._download("https://example.com/watch", {"max_downloads": 1})
