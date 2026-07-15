from __future__ import annotations

import logging

from atlas.engine import MediaProbe, YtdlpLogBridge, clean_ytdlp_error_message
from atlas.models import FormatInfo, InfoOptions, MediaInfo


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

    bridge.debug(
        "requesting https://cdn.example/file?Signature=TOPSECRET&Expires=999999"
    )

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
