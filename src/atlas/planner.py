"""Intent-based download planning."""

from __future__ import annotations

import re
from shutil import which
from typing import Any

from atlas.config import AtlasSettings
from atlas.errors import PlanningError
from atlas.models import (
    AudioDownloadOptions,
    Container,
    DownloadEngineChoice,
    DownloadPlan,
    FpsChoice,
    HdrChoice,
    OrganizeMode,
    QualityIntent,
    ResolutionChoice,
    VideoCodecChoice,
    VideoDownloadOptions,
)
from atlas.urls import is_explicit_playlist_url, is_watch_url_with_playlist_params

DEFAULT_VIDEO_FORMAT = "bestvideo*+bestaudio/best"
DEFAULT_AUDIO_FORMAT = "bestaudio/best"
DEFAULT_OUTTMPL = (
    "%(uploader|unknown)s/%(upload_date>%Y-%m-%d|unknown)s - "
    "%(title).200B [%(id)s].%(ext)s"
)


class SmartPlanner:
    """Translate user intent into a safe, concrete download plan."""

    def __init__(self, settings: AtlasSettings) -> None:
        self._settings = settings

    def plan_video(self, request: VideoDownloadOptions) -> DownloadPlan:
        effective_playlist = _effective_playlist(request.url, request.playlist)
        container = _resolve_container(request.container, request.quality)
        _validate_playlist_intent(request.url, request.playlist)
        _validate_video_intent(request, container)
        return DownloadPlan(
            url=request.url,
            output_dir=request.output_dir,
            outtmpl=str(request.output_dir / _outtmpl(request.organize, request.filename_template)),
            format=request.format or _video_format(request),
            noplaylist=not effective_playlist,
            merge_output_format=container.value,
            postprocessors=_media_postprocessors(
                request.embed_metadata,
                request.chapters,
                request.write_thumbnail,
                request.embed_thumbnail,
                sponsorblock_mark=request.sponsorblock_mark,
                sponsorblock_remove=request.sponsorblock_remove,
                sponsorblock_chapter_title=request.sponsorblock_chapter_title,
                sponsorblock_api=request.sponsorblock_api,
            ),
            archive_file=request.archive_file if request.archive else None,
            browser_cookies=request.browser_cookies,
            cookies_file=request.cookies_file,
            **_common_plan_kwargs(request, effective_playlist, self._settings),
            format_sort=_format_sort(request),
        )

    def plan_audio(self, request: AudioDownloadOptions) -> DownloadPlan:
        effective_playlist = _effective_playlist(request.url, request.playlist)
        _validate_playlist_intent(request.url, request.playlist)
        postprocessors: list[dict[str, object]] = []
        if not request.skip_download:
            postprocessors.append(
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": request.codec.value,
                    "preferredquality": str(request.quality),
                }
            )
        postprocessors.extend(
            _media_postprocessors(
                request.embed_metadata,
                request.chapters,
                request.write_thumbnail,
                request.embed_thumbnail,
                sponsorblock_mark=request.sponsorblock_mark,
                sponsorblock_remove=request.sponsorblock_remove,
                sponsorblock_chapter_title=request.sponsorblock_chapter_title,
                sponsorblock_api=request.sponsorblock_api,
            )
        )
        return DownloadPlan(
            url=request.url,
            output_dir=request.output_dir,
            outtmpl=str(request.output_dir / _outtmpl(request.organize, request.filename_template)),
            format=request.format or DEFAULT_AUDIO_FORMAT,
            noplaylist=not effective_playlist,
            postprocessors=postprocessors,
            archive_file=request.archive_file if request.archive else None,
            browser_cookies=request.browser_cookies,
            cookies_file=request.cookies_file,
            **_common_plan_kwargs(request, effective_playlist, self._settings),
        )


def _common_plan_kwargs(
    request: VideoDownloadOptions | AudioDownloadOptions,
    effective_playlist: bool,
    settings: AtlasSettings,
) -> dict[str, Any]:
    use_aria2, require_aria2 = _resolve_downloader(request, settings)
    match_filters = _match_filters(request)
    min_filesize = _parse_rate_limit(request.min_filesize)
    max_filesize = _parse_rate_limit(request.max_filesize)
    if min_filesize is not None and max_filesize is not None and min_filesize > max_filesize:
        raise PlanningError("min_filesize cannot be greater than max_filesize.")
    return {
        "use_aria2": use_aria2,
        "require_aria2": require_aria2,
        "connections": request.connections,
        "splits": request.splits,
        "chunk_size": request.chunk_size,
        "write_info_json": request.write_info_json,
        "write_thumbnail": request.write_thumbnail,
        "restrict_filenames": request.restrict_filenames,
        "overwrite": request.overwrite,
        "continue_download": request.continue_download,
        "retries": request.retries,
        "fragment_retries": request.fragment_retries,
        "file_access_retries": request.file_access_retries,
        "concurrent_fragment_downloads": request.concurrent_fragments,
        "retry_sleep": request.retry_sleep,
        "skip_unavailable_fragments": request.skip_unavailable_fragments,
        "skip_download": request.skip_download,
        "ignore_unavailable_playlist_entries": (
            request.ignore_unavailable_playlist_entries and effective_playlist
        ),
        "rate_limit": _parse_rate_limit(request.rate_limit),
        "throttled_rate": _parse_rate_limit(request.throttled_rate),
        "http_chunk_size": _parse_rate_limit(request.http_chunk_size),
        "socket_timeout": request.socket_timeout,
        "source_address": request.source_address,
        "impersonate": request.impersonate,
        "extractor_args": request.extractor_args,
        "sleep": request.sleep,
        "proxy": request.proxy,
        "match_filters": match_filters,
        "break_match_filters": request.break_match_filters,
        "max_downloads": request.max_downloads,
        "break_on_existing": request.break_on_existing,
        "break_on_reject": request.break_on_reject,
        "break_per_input": request.break_per_input,
        "date": request.date,
        "date_before": request.date_before,
        "date_after": request.date_after,
        "min_filesize": min_filesize,
        "max_filesize": max_filesize,
        "live_from_start": request.live_from_start,
        "download_sections": request.download_sections,
        "sponsorblock_mark": request.sponsorblock_mark,
        "sponsorblock_remove": request.sponsorblock_remove,
        "sponsorblock_chapter_title": request.sponsorblock_chapter_title,
        "sponsorblock_api": request.sponsorblock_api,
        "playlist_url_detected": is_explicit_playlist_url(request.url),
        "watch_playlist_params_detected": is_watch_url_with_playlist_params(request.url),
        "planner_notes": _planner_notes(request, effective_playlist),
        "playlist_items": request.playlist_items if effective_playlist else None,
        "playlist_start": request.playlist_start if effective_playlist else None,
        "playlist_end": request.playlist_end if effective_playlist else None,
        "subtitle_mode": request.subtitle_mode,
        "sub_lang": request.sub_lang,
        "embed_subs": request.embed_subs,
        "split_chapters": request.split_chapters,
        "verbose": request.verbose,
    }


def _resolve_downloader(
    request: VideoDownloadOptions | AudioDownloadOptions,
    settings: AtlasSettings,
) -> tuple[bool, bool]:
    if request.download_engine == DownloadEngineChoice.native:
        return False, False
    if request.download_engine == DownloadEngineChoice.aria2:
        if which("aria2c") is None:
            raise PlanningError("aria2c was requested but is not installed.")
        return True, True
    return settings.aria2 and request.use_aria2 and which("aria2c") is not None, False


def _resolve_container(container: Container, quality: QualityIntent) -> Container:
    if container != Container.auto:
        return container
    if quality == QualityIntent.compatible:
        return Container.mp4
    return Container.mkv


def _validate_video_intent(request: VideoDownloadOptions, container: Container) -> None:
    if request.quality != QualityIntent.compatible:
        return
    if container != Container.mp4:
        raise PlanningError("compatible quality requires --container auto or --container mp4.")
    if request.video_codec in {VideoCodecChoice.av1, VideoCodecChoice.vp9}:
        raise PlanningError("compatible quality can use auto, h264, or hevc video codecs.")


def _validate_playlist_intent(url: str, requested: bool) -> None:
    if requested or not is_explicit_playlist_url(url):
        return
    raise PlanningError(
        "This is an explicit playlist URL. Use atlas playlist URL --type video|audio, "
        "or pass --playlist to atlas video/audio after confirming playlist intent."
    )


def _effective_playlist(url: str, requested: bool) -> bool:
    return requested and is_explicit_playlist_url(url)


def _planner_notes(
    request: VideoDownloadOptions | AudioDownloadOptions,
    effective_playlist: bool,
) -> list[str]:
    notes: list[str] = []
    if effective_playlist:
        notes.append("explicit playlist URL accepted")
        if request.ignore_unavailable_playlist_entries:
            notes.append("removed or private playlist entries will be skipped")
    elif is_watch_url_with_playlist_params(request.url):
        notes.append("watch URL playlist/radio parameters kept single-item")
    if request.browser_cookies or request.cookies_file:
        notes.append("user-authorized cookies enabled")
    if request.reject_live:
        notes.append("active livestreams rejected by policy")
    if request.reject_upcoming:
        notes.append("scheduled premieres rejected by policy")
    if request.live_from_start:
        notes.append("livestream capture from start requested")
    if request.split_chapters:
        notes.append("chapter splitting enabled")
    if request.subtitle_only:
        notes.append("subtitle-only sidecar mode")
    if request.thumbnail_only:
        notes.append("thumbnail-only sidecar mode")
    if request.info_only:
        notes.append("info-only sidecar mode")
    return notes


def _outtmpl(organize: OrganizeMode, filename_template: str | None) -> str:
    if filename_template:
        return filename_template
    if organize == OrganizeMode.flat:
        return "%(upload_date>%Y-%m-%d|unknown)s - %(title).200B [%(id)s].%(ext)s"
    if organize == OrganizeMode.channel:
        return "%(uploader|unknown)s/%(title).200B [%(id)s].%(ext)s"
    if organize == OrganizeMode.playlist:
        return "%(playlist_title|playlist)s/%(playlist_index)03d - %(title).200B [%(id)s].%(ext)s"
    return DEFAULT_OUTTMPL


def _media_postprocessors(
    embed_metadata: bool,
    chapters: bool,
    write_thumbnail: bool,
    embed_thumbnail: bool,
    *,
    sponsorblock_mark: list[str],
    sponsorblock_remove: list[str],
    sponsorblock_chapter_title: str | None,
    sponsorblock_api: str | None,
) -> list[dict[str, object]]:
    postprocessors: list[dict[str, object]] = []
    sponsorblock_query = sorted(set(sponsorblock_mark) | set(sponsorblock_remove))
    if sponsorblock_query:
        postprocessor: dict[str, object] = {
            "key": "SponsorBlock",
            "categories": sponsorblock_query,
            "when": "after_filter",
        }
        if sponsorblock_api:
            postprocessor["api"] = sponsorblock_api
        postprocessors.append(postprocessor)
        postprocessors.append(
            {
                "key": "ModifyChapters",
                "remove_chapters_patterns": [],
                "remove_sponsor_segments": sorted(set(sponsorblock_remove)),
                "remove_ranges": [],
                "sponsorblock_chapter_title": sponsorblock_chapter_title,
                "force_keyframes": False,
            }
        )
    if embed_metadata:
        postprocessors.append(
            {"key": "FFmpegMetadata", "add_chapters": chapters, "add_metadata": True}
        )
    if write_thumbnail and embed_thumbnail:
        postprocessors.append({"key": "EmbedThumbnail", "already_have_thumbnail": False})
    return postprocessors


def _video_format(request: VideoDownloadOptions) -> str:
    if request.quality == QualityIntent.compatible:
        return _compatible_format(request)
    if request.quality == QualityIntent.balanced:
        return _combined_format(request, default_cap=1440)
    if request.quality == QualityIntent.small:
        return _combined_format(request, default_cap=720, extra_video_filters="[tbr<=2500]")
    return _combined_format(request)


def _combined_format(
    request: VideoDownloadOptions,
    default_cap: int | None = None,
    extra_video_filters: str = "",
) -> str:
    video_filters = _video_filters(request, default_cap=default_cap) + extra_video_filters
    if video_filters:
        return f"bestvideo*{video_filters}+bestaudio/best{video_filters}"
    return DEFAULT_VIDEO_FORMAT


def _compatible_format(request: VideoDownloadOptions) -> str:
    cap = _resolution_cap(request.resolution, 1080)
    fps = _fps_filter(request.fps)
    hdr = _hdr_filter(request.hdr)
    codec = _compatible_codec_filter(request.video_codec)
    height = _height_filter(cap)
    video = f"bestvideo*[ext=mp4]{codec}{height}{fps}{hdr}"
    mp4_video = f"bestvideo*[ext=mp4]{height}{fps}{hdr}"
    capped_video = f"bestvideo*{height}{fps}{hdr}"
    capped_best = f"best{height}"
    return (
        f"{video}+bestaudio[ext=m4a]/"
        f"{mp4_video}+bestaudio[ext=m4a]/"
        f"best[ext=mp4]{height}/"
        f"{capped_video}+bestaudio/"
        f"{capped_best}/"
        "best"
    )


def _compatible_codec_filter(codec: VideoCodecChoice) -> str:
    if codec == VideoCodecChoice.h264:
        return "[vcodec^=avc1]"
    if codec == VideoCodecChoice.hevc:
        return "[vcodec~='^(hvc1|hev1)']"
    return "[vcodec~='^(avc1|hvc1|hev1)']"


def _video_filters(request: VideoDownloadOptions, default_cap: int | None) -> str:
    return (
        _codec_filter(request.video_codec)
        + _height_filter(_resolution_cap(request.resolution, default_cap))
        + _fps_filter(request.fps)
        + _hdr_filter(request.hdr)
    )


def _codec_filter(codec: VideoCodecChoice) -> str:
    if codec == VideoCodecChoice.av1:
        return "[vcodec^=av01]"
    if codec == VideoCodecChoice.vp9:
        return "[vcodec^=vp9]"
    if codec == VideoCodecChoice.h264:
        return "[vcodec^=avc1]"
    if codec == VideoCodecChoice.hevc:
        return "[vcodec~='^(hvc1|hev1)']"
    return ""


def _resolution_cap(resolution: ResolutionChoice, default_cap: int | None) -> int | None:
    if resolution == ResolutionChoice.max:
        return default_cap
    return int(resolution.value)


def _height_filter(cap: int | None) -> str:
    return f"[height<={cap}]" if cap else ""


def _fps_filter(fps: FpsChoice) -> str:
    if fps == FpsChoice.f60:
        return "[fps<=60]"
    if fps == FpsChoice.f30:
        return "[fps<=30]"
    return ""


def _hdr_filter(hdr: HdrChoice) -> str:
    if hdr == HdrChoice.avoid:
        return "[dynamic_range=SDR]"
    if hdr == HdrChoice.only:
        return "[dynamic_range!=SDR]"
    return ""


def _format_sort(request: VideoDownloadOptions) -> list[str]:
    if request.hdr == HdrChoice.prefer:
        return ["hdr", "res", "fps", "codec"]
    if request.quality == QualityIntent.small:
        return ["+size", "res", "fps"]
    if request.quality == QualityIntent.compatible:
        return ["codec:h264", "ext:mp4:m4a", "res", "fps"]
    return []


def _parse_rate_limit(value: str | None) -> int | None:
    if not value:
        return None
    match = re.fullmatch(r"(?i)\s*(\d+(?:\.\d+)?)\s*([kmgt]?)(?:i?b?)?\s*", value)
    if not match:
        raise PlanningError(f"Invalid rate limit: {value}")
    amount = float(match.group(1))
    unit = match.group(2).lower()
    multiplier = {"": 1, "k": 1024, "m": 1024**2, "g": 1024**3, "t": 1024**4}[unit]
    return int(amount * multiplier)


def _match_filters(request: VideoDownloadOptions | AudioDownloadOptions) -> list[str]:
    filters = list(request.match_filters)
    if request.reject_live:
        filters.append("!is_live")
    if request.reject_upcoming:
        filters.append("!is_upcoming")
    return filters
