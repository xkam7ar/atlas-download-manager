"""yt-dlp Python API engine."""

from __future__ import annotations

import logging
import re
from typing import Any

from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError, ExtractorError, MaxDownloadsReached

from atlas.config import AtlasSettings
from atlas.errors import EngineError
from atlas.formats import media_info_from_raw
from atlas.models import (
    AudioDownloadOptions,
    DownloadResult,
    DownloadStatus,
    FormatInfo,
    InfoOptions,
    MediaInfo,
    VideoDownloadOptions,
)
from atlas.presets import (
    PostprocessorHook,
    ProgressHook,
    YtdlpLogger,
    build_audio_opts,
    build_info_opts,
    build_video_opts,
    redact_ydl_opts,
)
from atlas.redaction import redact_text

_YTDLP_ERROR_PREFIX = re.compile(r"^ERROR:\s*")


def clean_ytdlp_error_message(exc: BaseException) -> str:
    """Normalize yt-dlp exception text for friendly CLI display."""

    message = str(exc).strip()
    cleaned = _YTDLP_ERROR_PREFIX.sub("", message).strip() or exc.__class__.__name__
    lowered = cleaned.lower()
    if any(term in lowered for term in ("members-only", "members only", "member-only")):
        return (
            "This media is members-only and is not available to the current session. "
            "Use authorized browser cookies if the account has access; Atlas will not bypass "
            "access controls."
        )
    if any(term in lowered for term in ("private video", "this video is private", "private")):
        return (
            "This media is private or unavailable to the current session. Use authorized "
            "browser cookies if the account has access; playlist sessions skip unavailable "
            "download entries without hiding post-processing failures."
        )
    if any(
        term in lowered
        for term in (
            "age-restricted",
            "confirm your age",
            "sign in to confirm your age",
            "login required",
            "log in",
            "sign in",
            "cookies",
        )
    ):
        return (
            "This media requires a user-authorized session. Re-run with "
            "--cookies-from-browser safari|chrome|firefox or --cookies-file PATH if you have "
            "permission to access it."
        )
    if any(term in lowered for term in ("scheduled", "premiere", "upcoming")):
        return (
            "This media is scheduled or upcoming. Wait until it is available, or use "
            "--allow-upcoming only when you want Atlas to try scheduled media."
        )
    if any(term in lowered for term in ("is live", "livestream", "live stream", "currently live")):
        return (
            "This media is currently live. Use --live-from-start when supported, "
            "--allow-live to permit active livestreams, or retry after the archive is available."
        )
    if any(
        term in lowered
        for term in (
            "postprocess",
            "post-process",
            "post processing",
            "ffmpeg",
            "extractaudio",
            "merger",
        )
    ):
        return (
            "Post-processing failed after transfer. Check ffmpeg availability, disk space, "
            "and the requested container/codec; the media is not complete until merge, extract, "
            "metadata, thumbnail, and finalize phases finish."
        )
    return cleaned


class YtdlpLogBridge:
    """Route yt-dlp messages through stdlib logging instead of raw stderr."""

    def __init__(self, logger: logging.Logger) -> None:
        self._logger = logger

    def debug(self, msg: str) -> None:
        self._logger.debug("%s", redact_text(msg))

    def warning(self, msg: str) -> None:
        self._logger.debug("yt-dlp warning: %s", redact_text(msg))

    def error(self, msg: str) -> None:
        self._logger.debug("yt-dlp error: %s", redact_text(msg))


class YtdlpEngine:
    """Small wrapper around yt-dlp's Python API with UI-free behavior."""

    def __init__(self, settings: AtlasSettings, logger: logging.Logger | None = None) -> None:
        self._settings = settings
        self._logger = logger or logging.getLogger(__name__)
        self._ydl_logger: YtdlpLogger = YtdlpLogBridge(self._logger)

    def _extract_raw_info(self, options: InfoOptions) -> dict[str, Any]:
        ydl_opts = build_info_opts(options, logger=self._ydl_logger)
        try:
            with YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(options.url, download=False)
        except (DownloadError, ExtractorError) as exc:
            raise EngineError(clean_ytdlp_error_message(exc)) from exc
        except Exception as exc:
            self._logger.debug("Unexpected yt-dlp info failure", exc_info=exc)
            raise EngineError(clean_ytdlp_error_message(exc)) from exc
        if not isinstance(info, dict):
            raise EngineError("yt-dlp did not return an info dictionary")
        return info

    def get_info(self, options: InfoOptions) -> MediaInfo:
        return media_info_from_raw(self._extract_raw_info(options))

    def list_formats(self, options: InfoOptions) -> list[FormatInfo]:
        return self.get_info(options).formats

    def download_video(
        self,
        options: VideoDownloadOptions,
        progress_hooks: list[ProgressHook] | None = None,
        postprocessor_hooks: list[PostprocessorHook] | None = None,
    ) -> DownloadResult:
        ydl_opts = build_video_opts(
            options,
            self._settings,
            progress_hooks=progress_hooks,
            postprocessor_hooks=postprocessor_hooks,
            logger=self._ydl_logger,
        )
        if options.dry_run:
            return DownloadResult(
                status=DownloadStatus.dry_run,
                url=options.url,
                message="Dry run; no network request or download performed.",
                ydl_opts=redact_ydl_opts(ydl_opts),
            )
        return self._download(options.url, ydl_opts)

    def download_audio(
        self,
        options: AudioDownloadOptions,
        progress_hooks: list[ProgressHook] | None = None,
        postprocessor_hooks: list[PostprocessorHook] | None = None,
    ) -> DownloadResult:
        ydl_opts = build_audio_opts(
            options,
            self._settings,
            progress_hooks=progress_hooks,
            postprocessor_hooks=postprocessor_hooks,
            logger=self._ydl_logger,
        )
        if options.dry_run:
            return DownloadResult(
                status=DownloadStatus.dry_run,
                url=options.url,
                message="Dry run; no network request or download performed.",
                ydl_opts=redact_ydl_opts(ydl_opts),
            )
        return self._download(options.url, ydl_opts)

    def _download(self, url: str, ydl_opts: dict[str, Any]) -> DownloadResult:
        max_downloads = ydl_opts.get("max_downloads")
        requested_limit = (
            max_downloads
            if isinstance(max_downloads, int) and not isinstance(max_downloads, bool)
            else None
        )
        finished_downloads: set[tuple[object, ...]] = set()

        def track_finished(event: dict[str, Any]) -> None:
            if event.get("status") != "finished":
                return
            info = event.get("info_dict")
            if isinstance(info, dict):
                key = (
                    info.get("id"),
                    info.get("playlist_index"),
                    info.get("section_number"),
                    info.get("section_start"),
                    info.get("section_end"),
                )
                if key[0] is not None:
                    finished_downloads.add(key)
                    return
            filename = event.get("filename")
            if filename is not None:
                finished_downloads.add((filename,))

        download_opts = dict(ydl_opts)
        if requested_limit is not None:
            hooks = list(download_opts.get("progress_hooks") or [])
            hooks.append(track_finished)
            download_opts["progress_hooks"] = hooks
        try:
            with YoutubeDL(download_opts) as ydl:
                code = ydl.download([url])
        except MaxDownloadsReached as exc:
            if requested_limit is None or len(finished_downloads) < requested_limit:
                raise EngineError(clean_ytdlp_error_message(exc)) from exc
            noun = "download" if requested_limit == 1 else "downloads"
            return DownloadResult(
                status=DownloadStatus.success,
                url=url,
                message=(f"Requested download limit reached; {requested_limit} {noun} complete."),
            )
        except (DownloadError, ExtractorError) as exc:
            raise EngineError(clean_ytdlp_error_message(exc)) from exc
        except Exception as exc:
            self._logger.debug("Unexpected yt-dlp download failure", exc_info=exc)
            raise EngineError(clean_ytdlp_error_message(exc)) from exc
        if code:
            raise EngineError(f"yt-dlp exited with status {code}")
        return DownloadResult(status=DownloadStatus.success, url=url, message="Download complete.")


class MediaProbe:
    """Explicit probing stage for metadata and format discovery."""

    def __init__(self, engine: YtdlpEngine) -> None:
        self._engine = engine

    def probe(self, options: InfoOptions) -> MediaInfo:
        return self._engine.get_info(options)

    def formats(self, options: InfoOptions) -> list[FormatInfo]:
        return self._engine.list_formats(options)
