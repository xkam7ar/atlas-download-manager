"""Centralized yt-dlp option construction."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Protocol, cast

from yt_dlp.utils import DateRange, download_range_func, match_filter_func, parse_duration

from atlas.config import AtlasSettings
from atlas.models import (
    AudioCodec,
    AudioDownloadOptions,
    DownloadPlan,
    InfoOptions,
    SubtitleMode,
    VideoDownloadOptions,
)
from atlas.planner import SmartPlanner
from atlas.redaction import is_sensitive_key, text_contains_secret
from atlas.urls import is_explicit_playlist_url

ProgressHook = Callable[[dict[str, Any]], None]
PostprocessorHook = Callable[[dict[str, Any]], None]
BrowserCookies = tuple[str, str | None, str | None, str | None]
DownloadRange = Callable[[dict[str, Any], Any], object]


class YtdlpLogger(Protocol):
    def debug(self, msg: str) -> None: ...

    def warning(self, msg: str) -> None: ...

    def error(self, msg: str) -> None: ...

OUTTMPL = "%(uploader|unknown)s/%(upload_date>%Y-%m-%d|unknown)s - %(title).200B [%(id)s].%(ext)s"
DEFAULT_VIDEO_FORMAT = "bestvideo*+bestaudio/best"
DEFAULT_AUDIO_FORMAT = "bestaudio/best"
_RETRY_SLEEP_TYPES = {"http", "fragment", "file_access", "extractor"}
_RETRY_SLEEP_PATTERN = re.compile(
    r"(?:(linear|exp)=)?(\d+(?:\.\d+)?)(?::(\d+(?:\.\d+)?)?)?(?::(\d+(?:\.\d+)?))?"
)
_BROWSER_COOKIE_PATTERN = re.compile(
    r"^\s*(?P<name>[^+:]+)(?:\+(?P<keyring>[^:]+))?(?:\s*:\s*(?!:)(?P<profile>.+?))?(?:\s*::\s*(?P<container>.+))?\s*$"
)
_SECTION_RANGE_PATTERN = re.compile(
    r"(?x)(?:(?P<start_sign>-?)(?P<start>[^-]+))?\s*-\s*"
    r"(?:(?P<end_sign>-?)(?P<end>[^-]+))?"
)


def _archive_file(options: VideoDownloadOptions | AudioDownloadOptions) -> str | None:
    if not options.archive:
        return None
    return str(options.archive_file) if options.archive_file else None


def _cookies_from_browser(browser: str | None) -> BrowserCookies | None:
    if not browser:
        return None
    match = _BROWSER_COOKIE_PATTERN.fullmatch(browser)
    if not match:
        raise ValueError(f"Invalid browser cookie selector: {browser}")
    browser_name, keyring, profile, container = match.group(
        "name",
        "keyring",
        "profile",
        "container",
    )
    if browser_name is None:
        raise ValueError(f"Invalid browser cookie selector: {browser}")
    return (
        browser_name.strip().lower(),
        profile.strip() if profile else None,
        keyring.strip().upper() if keyring else None,
        container.strip() if container else None,
    )


def _retry_sleep_functions(entries: list[str]) -> dict[str, Callable[[int], float]]:
    functions: dict[str, Callable[[int], float]] = {}
    for entry in entries:
        key, expression = _split_retry_sleep_entry(entry)
        functions[key] = _retry_sleep_function(expression)
    return functions


def _split_retry_sleep_entry(entry: str) -> tuple[str, str]:
    candidate, separator, rest = entry.partition(":")
    if separator and candidate.lower() in _RETRY_SLEEP_TYPES:
        return candidate.lower(), rest
    return "http", entry


def _retry_sleep_function(expression: str) -> Callable[[int], float]:
    match = _RETRY_SLEEP_PATTERN.fullmatch(expression.strip())
    if not match:
        raise ValueError(f"Invalid retry sleep expression: {expression}")
    op, start_text, limit_text, step_text = match.groups()
    start = float(start_text)
    if op == "exp":
        base = float(limit_text) if limit_text else 2.0
        limit = float(step_text) if step_text else float("inf")

        def exp_sleep(attempt: int) -> float:
            return min(start * (base ** max(attempt - 1, 0)), limit)

        return exp_sleep
    if op == "linear":
        if limit_text:
            limit = float(limit_text)
            step = float(step_text) if step_text else 1.0
        elif step_text:
            limit = float(step_text)
            step = 1.0
        else:
            limit = float("inf")
            step = 1.0

        def linear_sleep(attempt: int) -> float:
            return min(start + step * max(attempt - 1, 0), limit)

        return linear_sleep

    def constant_sleep(_attempt: int) -> float:
        return start

    return constant_sleep


def _extractor_args(entries: list[str]) -> dict[str, dict[str, list[str]]]:
    parsed: dict[str, dict[str, list[str]]] = {}
    for entry in entries:
        extractor, _, payload = entry.partition(":")
        if not payload:
            raise ValueError(f"Invalid extractor args entry: {entry}")
        target = parsed.setdefault(extractor.strip().lower(), {})
        for segment in payload.split(";"):
            key, separator, values = segment.partition("=")
            if not separator:
                raise ValueError(f"Invalid extractor args entry: {entry}")
            target[key.strip().lower().replace("-", "_")] = [
                value.replace(r"\,", ",").strip()
                for value in re.split(r"(?<!\\),", values)
                if value.strip()
            ]
    return parsed


def _download_ranges(sections: list[str]) -> DownloadRange | None:
    if not sections:
        return None
    chapters: list[re.Pattern[str]] = []
    ranges: list[list[float]] = []
    from_url = False
    for section in sections:
        if section == "*from-url":
            from_url = True
            continue
        if not section.startswith("*"):
            try:
                chapters.append(re.compile(section))
            except re.error as exc:
                raise ValueError(f"Invalid download section regex {section!r}: {exc}") from exc
            continue
        for range_text in map(str.strip, section[1:].split(",")):
            if not range_text:
                continue
            match = _SECTION_RANGE_PATTERN.fullmatch(range_text)
            if not match:
                raise ValueError(f"Invalid download section time range: {section}")
            start = _section_timestamp(match.group("start") or "0")
            end = _section_timestamp(match.group("end") or "inf")
            if start is None or end is None:
                raise ValueError(f"Invalid download section time range: {section}")
            if match.group("start_sign"):
                start *= -1
            if match.group("end_sign"):
                end *= -1
            if end == float("-inf"):
                raise ValueError(f"Invalid download section end time: {section}")
            ranges.append([start, end])
    return cast(DownloadRange, download_range_func(chapters, ranges, from_url))


def _section_timestamp(value: str) -> float | None:
    if value in {"inf", "infinite"}:
        return float("inf")
    parsed = parse_duration(value)
    return float(parsed) if parsed is not None else None


def _date_range(plan: DownloadPlan) -> DateRange | None:
    if plan.date and (plan.date_before or plan.date_after):
        raise ValueError("date cannot be combined with date_before or date_after")
    if plan.date:
        return DateRange.day(plan.date)
    if plan.date_before or plan.date_after:
        return DateRange(plan.date_after, plan.date_before)
    return None


def aria2_downloader_opts(
    settings: AtlasSettings,
    enabled: bool,
    *,
    connections: int | None = None,
    splits: int | None = None,
    chunk_size: str | None = None,
) -> dict[str, Any]:
    if not enabled:
        return {}
    resolved_connections = connections or settings.aria2_connections
    resolved_splits = splits or settings.aria2_splits
    resolved_chunk_size = chunk_size or settings.aria2_chunk_size
    return {
        "external_downloader": {"http": "aria2c", "https": "aria2c"},
        "external_downloader_args": {
            "aria2c": [
                f"-x{resolved_connections}",
                f"-s{resolved_splits}",
                f"-k{resolved_chunk_size}",
                "--continue=true",
                "--console-log-level=warn",
                "--summary-interval=0",
                "--show-console-readout=false",
                "--download-result=hide",
            ]
        },
    }


def _postprocessors_for_media(
    *,
    embed_metadata: bool,
    write_thumbnail: bool,
    embed_thumbnail: bool,
) -> list[dict[str, Any]]:
    postprocessors: list[dict[str, Any]] = []
    if embed_metadata:
        postprocessors.append(
            {"key": "FFmpegMetadata", "add_chapters": True, "add_metadata": True}
        )
    if write_thumbnail and embed_thumbnail:
        postprocessors.append({"key": "EmbedThumbnail", "already_have_thumbnail": False})
    return postprocessors


def _base_download_opts(
    *,
    settings: AtlasSettings,
    plan: DownloadPlan,
    progress_hooks: list[ProgressHook] | None,
    postprocessor_hooks: list[PostprocessorHook] | None,
    logger: YtdlpLogger | None,
) -> dict[str, Any]:
    opts: dict[str, Any] = {
        "outtmpl": plan.outtmpl,
        "noplaylist": plan.noplaylist,
        "writeinfojson": plan.write_info_json,
        "writethumbnail": plan.write_thumbnail,
        "continuedl": plan.continue_download,
        "overwrites": plan.overwrite,
        "ignoreerrors": "only_download" if plan.ignore_unavailable_playlist_entries else False,
        "no_warnings": not plan.verbose,
        "quiet": not plan.verbose,
        "noprogress": True,
        "windowsfilenames": not plan.restrict_filenames,
        "restrictfilenames": plan.restrict_filenames,
        "trim_file_name": 240,
        "retries": plan.retries,
        "fragment_retries": plan.fragment_retries,
        "file_access_retries": plan.file_access_retries,
        "concurrent_fragment_downloads": plan.concurrent_fragment_downloads,
        "skip_unavailable_fragments": plan.skip_unavailable_fragments,
        "format": plan.format,
        "postprocessors": plan.postprocessors,
    }
    if plan.skip_download:
        opts["skip_download"] = True
    if plan.archive_file:
        opts["download_archive"] = str(plan.archive_file)
    cookies = _cookies_from_browser(plan.browser_cookies)
    if cookies:
        opts["cookiesfrombrowser"] = cookies
    if plan.cookies_file:
        opts["cookiefile"] = str(plan.cookies_file)
    if plan.rate_limit is not None:
        opts["ratelimit"] = plan.rate_limit
    if plan.throttled_rate is not None:
        opts["throttledratelimit"] = plan.throttled_rate
    if plan.http_chunk_size is not None:
        opts["http_chunk_size"] = plan.http_chunk_size
    if plan.socket_timeout is not None:
        opts["socket_timeout"] = plan.socket_timeout
    if plan.source_address:
        opts["source_address"] = plan.source_address
    if plan.impersonate is not None:
        opts["impersonate"] = plan.impersonate
    if plan.retry_sleep:
        opts["retry_sleep_functions"] = _retry_sleep_functions(plan.retry_sleep)
    if plan.extractor_args:
        opts["extractor_args"] = _extractor_args(plan.extractor_args)
    if plan.sleep is not None:
        opts["sleep_interval"] = plan.sleep
    if plan.proxy:
        opts["proxy"] = plan.proxy
    match_filter = match_filter_func(plan.match_filters, plan.break_match_filters)
    if match_filter is not None:
        opts["match_filter"] = match_filter
    date_range = _date_range(plan)
    if date_range is not None:
        opts["daterange"] = date_range
    if plan.min_filesize is not None:
        opts["min_filesize"] = plan.min_filesize
    if plan.max_filesize is not None:
        opts["max_filesize"] = plan.max_filesize
    if plan.max_downloads is not None:
        opts["max_downloads"] = plan.max_downloads
    if plan.break_on_existing:
        opts["break_on_existing"] = True
    if plan.break_on_reject:
        opts["break_on_reject"] = True
    if plan.break_per_input:
        opts["break_per_url"] = True
    if plan.live_from_start:
        opts["live_from_start"] = True
    download_ranges = _download_ranges(plan.download_sections)
    if download_ranges is not None:
        opts["download_ranges"] = download_ranges
    if plan.playlist_items:
        opts["playlist_items"] = plan.playlist_items
    if plan.playlist_start is not None:
        opts["playliststart"] = plan.playlist_start
    if plan.playlist_end is not None:
        opts["playlistend"] = plan.playlist_end
    if plan.merge_output_format:
        opts["merge_output_format"] = plan.merge_output_format
    if plan.subtitle_mode != SubtitleMode.none:
        opts.update(_subtitle_opts(plan))
    if plan.split_chapters:
        opts["split_chapters"] = True
    if plan.format_sort:
        opts["format_sort"] = plan.format_sort
    if progress_hooks:
        opts["progress_hooks"] = progress_hooks
    if postprocessor_hooks:
        opts["postprocessor_hooks"] = postprocessor_hooks
    if logger:
        opts["logger"] = logger
    opts.update(
        aria2_downloader_opts(
            settings,
            plan.use_aria2,
            connections=plan.connections,
            splits=plan.splits,
            chunk_size=plan.chunk_size,
        )
    )
    return opts


def _subtitle_opts(plan: DownloadPlan) -> dict[str, Any]:
    opts: dict[str, Any] = {}
    if plan.subtitle_mode == SubtitleMode.manual:
        opts["writesubtitles"] = True
    elif plan.subtitle_mode == SubtitleMode.auto:
        opts["writeautomaticsub"] = True
    elif plan.subtitle_mode == SubtitleMode.all:
        opts["writesubtitles"] = True
        opts["writeautomaticsub"] = True
        opts["allsubtitles"] = True
    if plan.sub_lang:
        opts["subtitleslangs"] = [lang.strip() for lang in plan.sub_lang.split(",") if lang.strip()]
    if plan.embed_subs:
        opts["embedsubtitles"] = True
    return opts


def _effective_playlist(url: str, requested: bool) -> bool:
    return requested and is_explicit_playlist_url(url)


def build_video_opts(
    options: VideoDownloadOptions,
    settings: AtlasSettings,
    progress_hooks: list[ProgressHook] | None = None,
    postprocessor_hooks: list[PostprocessorHook] | None = None,
    logger: YtdlpLogger | None = None,
) -> dict[str, Any]:
    return PresetBuilder(settings).video_opts(
        options,
        progress_hooks=progress_hooks,
        postprocessor_hooks=postprocessor_hooks,
        logger=logger,
    )


def build_audio_opts(
    options: AudioDownloadOptions,
    settings: AtlasSettings,
    progress_hooks: list[ProgressHook] | None = None,
    postprocessor_hooks: list[PostprocessorHook] | None = None,
    logger: YtdlpLogger | None = None,
) -> dict[str, Any]:
    return PresetBuilder(settings).audio_opts(
        options,
        progress_hooks=progress_hooks,
        postprocessor_hooks=postprocessor_hooks,
        logger=logger,
    )


class PresetBuilder:
    """Convert smart download plans into concrete yt-dlp option dictionaries."""

    def __init__(self, settings: AtlasSettings) -> None:
        self._settings = settings
        self._planner = SmartPlanner(settings)

    def video_opts(
        self,
        options: VideoDownloadOptions,
        progress_hooks: list[ProgressHook] | None = None,
        postprocessor_hooks: list[PostprocessorHook] | None = None,
        logger: YtdlpLogger | None = None,
    ) -> dict[str, Any]:
        return _base_download_opts(
            settings=self._settings,
            progress_hooks=progress_hooks,
            postprocessor_hooks=postprocessor_hooks,
            logger=logger,
            plan=self._planner.plan_video(options),
        )

    def audio_opts(
        self,
        options: AudioDownloadOptions,
        progress_hooks: list[ProgressHook] | None = None,
        postprocessor_hooks: list[PostprocessorHook] | None = None,
        logger: YtdlpLogger | None = None,
    ) -> dict[str, Any]:
        return _base_download_opts(
            settings=self._settings,
            progress_hooks=progress_hooks,
            postprocessor_hooks=postprocessor_hooks,
            logger=logger,
            plan=self._planner.plan_audio(options),
        )


def build_info_opts(options: InfoOptions, logger: YtdlpLogger | None = None) -> dict[str, Any]:
    opts: dict[str, Any] = {
        "quiet": not options.verbose,
        "no_warnings": not options.verbose,
        "skip_download": True,
        "noplaylist": not _effective_playlist(options.url, options.playlist),
    }
    cookies = _cookies_from_browser(options.browser_cookies)
    if cookies:
        opts["cookiesfrombrowser"] = cookies
    if options.cookies_file:
        opts["cookiefile"] = str(options.cookies_file)
    if logger:
        opts["logger"] = logger
    return opts


def redact_ydl_opts(opts: Mapping[str, Any]) -> dict[str, Any]:
    """Return a JSON-friendly copy of yt-dlp options for dry-run output."""

    redacted: dict[str, Any] = {}
    for key, value in opts.items():
        if key in {"progress_hooks", "postprocessor_hooks"}:
            redacted[key] = ["<progress-hook>"]
        elif key in {"logger"}:
            redacted[key] = "<yt-dlp-logger>"
        elif is_sensitive_key(key):
            redacted[key] = "<redacted>"
        elif callable(value):
            redacted[key] = "<callable>"
        elif isinstance(value, Path):
            redacted[key] = str(value)
        elif isinstance(value, tuple):
            redacted[key] = [
                "<redacted>" if isinstance(item, str) and text_contains_secret(item) else item
                for item in value
            ]
        elif isinstance(value, list):
            redacted[key] = [
                redact_ydl_opts(item)
                if isinstance(item, dict)
                else "<redacted>"
                if isinstance(item, str) and text_contains_secret(item)
                else item
                for item in value
            ]
        elif isinstance(value, dict):
            redacted[key] = redact_ydl_opts(value)
        elif isinstance(value, str):
            redacted[key] = "<redacted>" if text_contains_secret(value) else value
        elif isinstance(value, int | float | bool) or value is None:
            redacted[key] = value
        else:
            redacted[key] = repr(value)
    return redacted


def is_lossless_audio(codec: AudioCodec) -> bool:
    return codec in {AudioCodec.flac, AudioCodec.wav}
