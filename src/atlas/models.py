"""Typed models used across the application."""

from __future__ import annotations

import re
from enum import StrEnum
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

DEFAULT_DIRECTORY_USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"


def _normalize_http_url(value: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        msg = "URL cannot be blank"
        raise ValueError(msg)
    if any(ord(character) < 32 or ord(character) == 127 for character in cleaned):
        msg = "URL cannot contain control characters"
        raise ValueError(msg)
    try:
        parsed = urlsplit(cleaned)
        host = parsed.hostname
        _ = parsed.port
    except ValueError as exc:
        msg = "URL must be a valid absolute HTTP or HTTPS URL"
        raise ValueError(msg) from exc
    if parsed.scheme.lower() not in {"http", "https"} or not host:
        msg = "URL must be an absolute HTTP or HTTPS URL with a host"
        raise ValueError(msg)
    if parsed.username is not None or parsed.password is not None:
        msg = "URL user information is not allowed; use explicit authentication options"
        raise ValueError(msg)
    return cleaned


class Container(StrEnum):
    auto = "auto"
    mkv = "mkv"
    mp4 = "mp4"
    webm = "webm"


class AudioCodec(StrEnum):
    best = "best"
    opus = "opus"
    m4a = "m4a"
    mp3 = "mp3"
    flac = "flac"
    wav = "wav"


class BatchKind(StrEnum):
    auto = "auto"
    file = "file"
    video = "video"
    audio = "audio"
    site = "site"
    dir = "dir"


class FormatSort(StrEnum):
    quality = "quality"
    size = "size"
    codec = "codec"


class QualityIntent(StrEnum):
    max = "max"
    balanced = "balanced"
    compatible = "compatible"
    small = "small"


class ResolutionChoice(StrEnum):
    max = "max"
    r4320 = "4320"
    r2160 = "2160"
    r1440 = "1440"
    r1080 = "1080"
    r720 = "720"
    r480 = "480"


class VideoCodecChoice(StrEnum):
    auto = "auto"
    av1 = "av1"
    vp9 = "vp9"
    h264 = "h264"
    hevc = "hevc"


class HdrChoice(StrEnum):
    auto = "auto"
    prefer = "prefer"
    avoid = "avoid"
    only = "only"


class FpsChoice(StrEnum):
    max = "max"
    f60 = "60"
    f30 = "30"


class SubtitleMode(StrEnum):
    none = "none"
    manual = "manual"
    auto = "auto"
    all = "all"


class OrganizeMode(StrEnum):
    flat = "flat"
    channel = "channel"
    channel_date = "channel-date"
    playlist = "playlist"


class DownloadEngineChoice(StrEnum):
    auto = "auto"
    native = "native"
    aria2 = "aria2"


class FileBackendChoice(StrEnum):
    auto = "auto"
    native = "native"
    aria2 = "aria2"
    wget2 = "wget2"


class MetalinkPreferredProtocol(StrEnum):
    none = "none"
    http = "http"
    https = "https"
    ftp = "ftp"


class Aria2UriSelector(StrEnum):
    inorder = "inorder"
    feedback = "feedback"
    adaptive = "adaptive"


class SiteBackendChoice(StrEnum):
    auto = "auto"
    wget2 = "wget2"
    wget = "wget"


class DownloadAttrMode(StrEnum):
    strip_path = "strippath"
    use_path = "usepath"


class HttpsEnforceMode(StrEnum):
    hard = "hard"
    soft = "soft"


class PreferFamily(StrEnum):
    none = "none"
    ipv4 = "IPv4"
    ipv6 = "IPv6"


class CertificateType(StrEnum):
    pem = "PEM"
    der = "DER"


class VerifySigMode(StrEnum):
    fail = "fail"
    no_fail = "no-fail"


class EngineKind(StrEnum):
    ytdlp = "yt-dlp"
    aria2 = "aria2c"
    native = "native"
    curl = "curl"
    wget2 = "wget2"
    wget = "wget"
    unknown = "unknown"


class HubKind(StrEnum):
    auto = "auto"
    video = "video"
    audio = "audio"
    file = "file"
    manifest = "manifest"
    site = "site"
    dir = "dir"


class ProgressPhase(StrEnum):
    probe = "probe"
    extract = "extract"
    download = "download"
    merge = "merge"
    postprocess = "postprocess"
    verify = "verify"
    finalize = "finalize"
    done = "done"
    error = "error"


class ProgressMode(StrEnum):
    auto = "auto"
    compact = "compact"
    full = "full"
    json = "json"
    none = "none"


class FileSizeClass(StrEnum):
    tiny = "tiny"
    small = "small"
    medium = "medium"
    large = "large"
    huge = "huge"
    unknown = "unknown"


class WorkBucket(StrEnum):
    tiny = "tiny"
    small = "small"
    medium = "medium"
    large = "large"
    huge = "huge"
    unknown = "unknown"
    media = "media"
    recursive_mirror = "recursive_mirror"


class AdaptivePoliteness(StrEnum):
    normal = "normal"
    fast = "fast"
    aggressive = "aggressive"


class DownloadStatus(StrEnum):
    success = "success"
    failed = "failed"
    skipped = "skipped"
    canceled = "canceled"
    dry_run = "dry-run"


class ScanStatus(StrEnum):
    success = "success"
    partial = "partial"
    failed = "failed"
    empty = "empty"


class ScanErrorCode(StrEnum):
    tls_failed = "tls_failed"
    # Backward-compatible alias for saved manifests created before the shorter
    # scan error code was introduced.
    tls_cert_verify_failed = "tls_cert_verify_failed"
    timeout = "timeout"
    connection_failed = "connection_failed"
    http_error = "http_error"
    parse_error = "parse_error"
    no_links = "no_links"


_PLAYLIST_ITEMS_PATTERN = re.compile(r"^\d+(?:-\d*)?(?:,\d+(?:-\d*)?)*$")
_CHUNK_SIZE_PATTERN = re.compile(r"^\d+(?:\.\d+)?[kmgt]?b?$", re.IGNORECASE)
_RETRY_SLEEP_PATTERN = re.compile(
    r"^(?:(?:http|fragment|file_access|extractor):)?"
    r"(?:(?:linear|exp)=)?\d+(?:\.\d+)?(?::\d*(?:\.\d+)?)?(?::\d+(?:\.\d+)?)?$",
    re.IGNORECASE,
)
_EXTRACTOR_ARG_PATTERN = re.compile(r"^[A-Za-z0-9_-]+:[A-Za-z0-9_-]+=")
_SPONSORBLOCK_ACTUAL_CATEGORIES = {
    "sponsor",
    "intro",
    "outro",
    "selfpromo",
    "preview",
    "filler",
    "interaction",
    "music_offtopic",
    "hook",
    "poi_highlight",
    "chapter",
}
_SPONSORBLOCK_CATEGORIES = {*_SPONSORBLOCK_ACTUAL_CATEGORIES, "all", "default"}
_SPONSORBLOCK_NON_SKIPPABLE = {"poi_highlight", "chapter"}
_SPONSORBLOCK_SKIPPABLE_CATEGORIES = _SPONSORBLOCK_ACTUAL_CATEGORIES - _SPONSORBLOCK_NON_SKIPPABLE


def _expand_optional_path(value: object) -> object:
    if value is None:
        return None
    if isinstance(value, str):
        return Path(value).expanduser()
    if isinstance(value, Path):
        return value.expanduser()
    return value


def _normalize_http_headers(value: tuple[str, ...]) -> tuple[str, ...]:
    normalized: list[str] = []
    for header in value:
        cleaned = header.strip()
        if not cleaned:
            continue
        name, separator, header_value = cleaned.partition(":")
        if not separator or not name.strip() or not header_value.strip():
            msg = "headers must look like 'Name: value'"
            raise ValueError(msg)
        normalized.append(f"{name.strip()}: {header_value.strip()}")
    return tuple(normalized)


def _normalize_nonblank_strings(values: list[str]) -> list[str]:
    return [value.strip() for value in values if value.strip()]


def _normalize_optional_string(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None


def _normalize_optional_size(value: str | None, *, label: str) -> str | None:
    cleaned = _normalize_optional_string(value)
    if cleaned is None:
        return None
    if not _CHUNK_SIZE_PATTERN.fullmatch(cleaned):
        msg = f"{label} must be a positive size such as 512K, 10M, or 1G"
        raise ValueError(msg)
    return cleaned.upper()


def _normalize_sponsorblock_categories(
    values: list[str],
    *,
    allow_non_skippable: bool,
) -> list[str]:
    normalized: list[str] = []
    for value in values:
        for raw_category in value.split(","):
            category = raw_category.strip().lower()
            if not category:
                continue
            if category not in _SPONSORBLOCK_CATEGORIES:
                allowed = ", ".join(sorted(_SPONSORBLOCK_CATEGORIES))
                msg = f"SponsorBlock category must be one of: {allowed}"
                raise ValueError(msg)
            if category == "all":
                categories = (
                    _SPONSORBLOCK_ACTUAL_CATEGORIES
                    if allow_non_skippable
                    else _SPONSORBLOCK_SKIPPABLE_CATEGORIES
                )
                normalized.extend(sorted(categories))
                continue
            if category == "default":
                categories = (
                    _SPONSORBLOCK_ACTUAL_CATEGORIES
                    if allow_non_skippable
                    else (_SPONSORBLOCK_SKIPPABLE_CATEGORIES - {"filler"})
                )
                normalized.extend(sorted(categories))
                continue
            if not allow_non_skippable and category in _SPONSORBLOCK_NON_SKIPPABLE:
                msg = "sponsorblock_remove cannot include chapter or poi_highlight"
                raise ValueError(msg)
            normalized.append(category)
    return list(dict.fromkeys(normalized))


class BaseDownloadOptions(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    url: str
    output_dir: Path
    archive: bool = True
    archive_file: Path | None = None
    cookies_file: Path | None = None
    use_aria2: bool = True
    download_engine: DownloadEngineChoice = DownloadEngineChoice.auto
    connections: int = Field(default=16, ge=1, le=64)
    splits: int = Field(default=16, ge=1, le=64)
    chunk_size: str = "1M"
    browser_cookies: str | None = None
    playlist: bool = False
    playlist_items: str | None = None
    playlist_start: int | None = Field(default=None, ge=1)
    playlist_end: int | None = Field(default=None, ge=1)
    organize: OrganizeMode = OrganizeMode.channel_date
    filename_template: str | None = None
    restrict_filenames: bool = False
    overwrite: bool = False
    continue_download: bool = True
    retries: int = Field(default=10, ge=0)
    fragment_retries: int = Field(default=10, ge=0)
    file_access_retries: int = Field(default=3, ge=0)
    concurrent_fragments: int = Field(default=4, ge=1, le=64)
    retry_sleep: list[str] = Field(default_factory=list)
    skip_unavailable_fragments: bool = True
    rate_limit: str | None = None
    throttled_rate: str | None = None
    http_chunk_size: str | None = None
    socket_timeout: float | None = Field(default=None, ge=0)
    source_address: str | None = None
    impersonate: str | None = None
    extractor_args: list[str] = Field(default_factory=list)
    sleep: float | None = Field(default=None, ge=0)
    proxy: str | None = None
    match_filters: list[str] = Field(default_factory=list)
    break_match_filters: list[str] = Field(default_factory=list)
    max_downloads: int | None = Field(default=None, ge=1)
    break_on_existing: bool = False
    break_on_reject: bool = False
    break_per_input: bool = False
    date: str | None = None
    date_before: str | None = None
    date_after: str | None = None
    min_filesize: str | None = None
    max_filesize: str | None = None
    reject_live: bool = False
    reject_upcoming: bool = False
    live_from_start: bool = False
    download_sections: list[str] = Field(default_factory=list)
    sponsorblock_mark: list[str] = Field(default_factory=list)
    sponsorblock_remove: list[str] = Field(default_factory=list)
    sponsorblock_chapter_title: str | None = None
    sponsorblock_api: str | None = None
    skip_download: bool = False
    subtitle_only: bool = False
    thumbnail_only: bool = False
    info_only: bool = False
    ignore_unavailable_playlist_entries: bool = True
    write_info_json: bool = True
    write_thumbnail: bool = True
    embed_thumbnail: bool = True
    embed_metadata: bool = True
    subtitle_mode: SubtitleMode = SubtitleMode.none
    sub_lang: str | None = None
    embed_subs: bool = False
    chapters: bool = True
    split_chapters: bool = False
    dry_run: bool = False
    quiet: bool = False
    json_output: bool = False
    progress_mode: ProgressMode = ProgressMode.auto
    verbose: bool = False

    @field_validator("url")
    @classmethod
    def url_must_not_be_blank(cls, value: str) -> str:
        return _normalize_http_url(value)

    @field_validator("filename_template")
    @classmethod
    def filename_template_must_stay_under_output(
        cls,
        value: str | None,
    ) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned:
            return None
        if any(ord(character) < 32 or ord(character) == 127 for character in cleaned):
            msg = "filename_template cannot contain control characters"
            raise ValueError(msg)
        posix = PurePosixPath(cleaned.replace("\\", "/"))
        windows = PureWindowsPath(cleaned)
        if (
            posix.is_absolute()
            or windows.is_absolute()
            or bool(windows.drive)
            or ".." in posix.parts
        ):
            msg = "filename_template must be a relative path under output_dir"
            raise ValueError(msg)
        return cleaned

    @field_validator("output_dir", "archive_file", "cookies_file", mode="before")
    @classmethod
    def expand_user_paths(cls, value: object) -> object:
        if isinstance(value, str):
            return Path(value).expanduser()
        if isinstance(value, Path):
            return value.expanduser()
        return value

    @field_validator("chunk_size")
    @classmethod
    def chunk_size_must_be_aria2_friendly(cls, value: str) -> str:
        if not _CHUNK_SIZE_PATTERN.fullmatch(value.strip()):
            msg = "chunk_size must be a positive size such as 512K, 1M, or 4M"
            raise ValueError(msg)
        return value.strip().upper()

    @field_validator("http_chunk_size")
    @classmethod
    def http_chunk_size_must_be_yt_dlp_friendly(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned:
            return None
        if not _CHUNK_SIZE_PATTERN.fullmatch(cleaned):
            msg = "http_chunk_size must be a positive size such as 512K, 10M, or 1G"
            raise ValueError(msg)
        return cleaned.upper()

    @field_validator("retry_sleep")
    @classmethod
    def retry_sleep_must_use_supported_expressions(cls, values: list[str]) -> list[str]:
        cleaned: list[str] = []
        for value in values:
            entry = value.strip()
            if not entry:
                continue
            if not _RETRY_SLEEP_PATTERN.fullmatch(entry):
                msg = (
                    "retry_sleep entries must look like http:1, fragment:linear=1::10, "
                    "or extractor:exp=1:2:60"
                )
                raise ValueError(msg)
            cleaned.append(entry)
        return cleaned

    @field_validator("extractor_args")
    @classmethod
    def extractor_args_must_be_key_value_entries(cls, values: list[str]) -> list[str]:
        cleaned: list[str] = []
        for value in values:
            entry = value.strip()
            if not entry:
                continue
            if not _EXTRACTOR_ARG_PATTERN.match(entry):
                msg = "extractor_args entries must look like youtube:player_client=android,ios"
                raise ValueError(msg)
            cleaned.append(entry)
        return cleaned

    @field_validator("match_filters", "break_match_filters", "download_sections")
    @classmethod
    def media_string_lists_must_not_contain_blanks(cls, values: list[str]) -> list[str]:
        return _normalize_nonblank_strings(values)

    @field_validator(
        "date",
        "date_before",
        "date_after",
        "sponsorblock_chapter_title",
        "sponsorblock_api",
    )
    @classmethod
    def optional_media_strings_must_not_be_blank(cls, value: str | None) -> str | None:
        return _normalize_optional_string(value)

    @field_validator("min_filesize")
    @classmethod
    def min_filesize_must_be_yt_dlp_friendly(cls, value: str | None) -> str | None:
        return _normalize_optional_size(value, label="min_filesize")

    @field_validator("max_filesize")
    @classmethod
    def max_filesize_must_be_yt_dlp_friendly(cls, value: str | None) -> str | None:
        return _normalize_optional_size(value, label="max_filesize")

    @field_validator("sponsorblock_mark")
    @classmethod
    def sponsorblock_mark_must_be_supported(cls, values: list[str]) -> list[str]:
        return _normalize_sponsorblock_categories(values, allow_non_skippable=True)

    @field_validator("sponsorblock_remove")
    @classmethod
    def sponsorblock_remove_must_be_supported(cls, values: list[str]) -> list[str]:
        return _normalize_sponsorblock_categories(values, allow_non_skippable=False)

    @field_validator("playlist_items")
    @classmethod
    def playlist_items_must_be_simple_ranges(cls, value: str | None) -> str | None:
        return _normalize_playlist_items(value)

    @model_validator(mode="after")
    def playlist_range_must_be_ordered(self) -> BaseDownloadOptions:
        if (
            self.playlist_start is not None
            and self.playlist_end is not None
            and self.playlist_start > self.playlist_end
        ):
            msg = "playlist_start cannot be greater than playlist_end"
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def media_sidecar_modes_must_be_consistent(self) -> BaseDownloadOptions:
        modes = [
            self.subtitle_only,
            self.thumbnail_only,
            self.info_only,
        ]
        if sum(1 for selected in modes if selected) > 1:
            msg = "Choose only one of subtitle_only, thumbnail_only, or info_only"
            raise ValueError(msg)
        if self.subtitle_only:
            self.skip_download = True
            if self.subtitle_mode == SubtitleMode.none:
                self.subtitle_mode = SubtitleMode.manual
            self.embed_subs = False
            self.write_info_json = False
            self.write_thumbnail = False
            self.embed_thumbnail = False
            self.embed_metadata = False
        if self.thumbnail_only:
            self.skip_download = True
            self.write_thumbnail = True
            self.embed_thumbnail = False
            self.write_info_json = False
        if self.info_only:
            self.skip_download = True
            self.write_info_json = True
            self.write_thumbnail = False
            self.embed_thumbnail = False
            self.embed_metadata = False
        if self.skip_download:
            self.embed_thumbnail = False
            self.embed_metadata = False
        return self


class DownloadRequest(BaseDownloadOptions):
    """User intent for a download before planning."""


class VideoDownloadOptions(DownloadRequest):
    quality: QualityIntent = QualityIntent.max
    container: Container = Container.auto
    resolution: ResolutionChoice = ResolutionChoice.max
    video_codec: VideoCodecChoice = VideoCodecChoice.auto
    hdr: HdrChoice = HdrChoice.auto
    fps: FpsChoice = FpsChoice.max
    format: str | None = None


class AudioDownloadOptions(DownloadRequest):
    codec: AudioCodec = AudioCodec.best
    quality: int = Field(default=0, ge=0, le=10)
    format: str | None = None


class InfoOptions(BaseModel):
    url: str
    browser_cookies: str | None = None
    cookies_file: Path | None = None
    playlist: bool = False
    playlist_items: str | None = None
    playlist_start: int | None = Field(default=None, ge=1)
    playlist_end: int | None = Field(default=None, ge=1)
    socket_timeout: float | None = Field(default=None, ge=0)
    flat_playlist: bool = True
    json_output: bool = False
    verbose: bool = False

    @field_validator("url")
    @classmethod
    def url_must_not_be_blank(cls, value: str) -> str:
        return _normalize_http_url(value)

    @field_validator("cookies_file", mode="before")
    @classmethod
    def expand_cookie_file(cls, value: object) -> object:
        if isinstance(value, str):
            return Path(value).expanduser()
        if isinstance(value, Path):
            return value.expanduser()
        return value

    @field_validator("playlist_items")
    @classmethod
    def playlist_items_must_be_simple_ranges(cls, value: str | None) -> str | None:
        return _normalize_playlist_items(value)

    @model_validator(mode="after")
    def playlist_range_must_be_ordered(self) -> InfoOptions:
        if (
            self.playlist_start is not None
            and self.playlist_end is not None
            and self.playlist_start > self.playlist_end
        ):
            msg = "playlist_start cannot be greater than playlist_end"
            raise ValueError(msg)
        return self


class WorkItem(BaseModel):
    """One scanned download candidate used by the adaptive planner."""

    url: str
    host: str | None = None
    final_url: str | None = None
    final_host: str | None = None
    redirect_target: str | None = None
    kind: HubKind = HubKind.file
    content_type: str | None = None
    content_length: int | None = Field(default=None, ge=0)
    content_disposition: str | None = None
    content_disposition_filename: str | None = None
    filename: str | None = None
    file_extension: str | None = None
    accept_ranges: str | None = None
    supports_ranges: bool = False
    etag: str | None = None
    last_modified: str | None = None
    discovered_links: list[str] = Field(default_factory=list)
    discovered_work_items: list[WorkItem] = Field(default_factory=list)
    sitemap_urls: list[str] = Field(default_factory=list)
    robots_url: str | None = None
    url_fingerprint: str | None = None
    mirror_fingerprint: str | None = None
    classification_notes: list[str] = Field(default_factory=list)
    warning_flags: list[str] = Field(default_factory=list)
    same_host: bool = True
    external_host: bool = False
    scan_type: str | None = None
    scan_recommended_mode: str | None = None
    scan_recommended_strategy: str | None = None
    scan_counts: dict[str, int] = Field(default_factory=dict)
    scan_estimated_bytes: int | None = Field(default=None, ge=0)
    scan_warnings: list[str] = Field(default_factory=list)
    scan_status: ScanStatus = ScanStatus.success
    scan_errors: list[dict[str, Any]] = Field(default_factory=list)
    size_class: FileSizeClass = FileSizeClass.unknown
    bucket: WorkBucket | None = None
    selected_backend: str | None = None
    priority: int = Field(default=100, ge=0)
    recursion_depth: int | None = Field(default=None, ge=0)
    checksum_metadata: dict[str, str] = Field(default_factory=dict)
    scheduler_decision: str | None = None
    probed: bool = True
    error: str | None = None


class AdaptiveDownloadPlan(BaseModel):
    """Explainable concurrency and segmentation choices for scanned work."""

    enabled: bool = False
    politeness: AdaptivePoliteness = AdaptivePoliteness.normal
    global_min_concurrency: int = Field(default=2, ge=1, le=100)
    global_max_concurrency: int = Field(default=100, ge=2, le=100)
    queue_concurrency: int = Field(default=2, ge=1, le=100)
    per_host_concurrency: int = Field(default=2, ge=1, le=100)
    per_file_segments: int = Field(default=1, ge=1, le=64)
    per_file_segment_cap: int = Field(default=16, ge=1, le=64)
    max_active_files: int = Field(default=2, ge=1, le=100)
    max_total_connections: int = Field(default=2, ge=1, le=6400)
    max_per_host_connections: int = Field(default=2, ge=1, le=6400)
    max_active_postprocessors: int = Field(default=0, ge=0, le=64)
    max_disk_write_bytes_per_sec: int | None = Field(default=None, ge=0)
    speed_limit: str | None = None
    backend: str = "auto"
    strategy: str = "fixed"
    size_counts: dict[str, int] = Field(default_factory=dict)
    bucket_counts: dict[str, int] = Field(default_factory=dict)
    hosts: dict[str, int] = Field(default_factory=dict)
    work_items: list[WorkItem] = Field(default_factory=list)
    safety_notes: list[str] = Field(default_factory=list)
    scan_status: ScanStatus | None = None
    scan_type: str | None = None
    scan_counts: dict[str, int] = Field(default_factory=dict)
    scan_warnings: list[str] = Field(default_factory=list)
    scan_errors: list[dict[str, Any]] = Field(default_factory=list)


class SmartDownloadSession(BaseModel):
    """Shared scan-plan-execute contract for every download intent."""

    source: str
    detected_kind: HubKind
    intent: str
    session_type: str
    manifest: list[WorkItem] = Field(default_factory=list)
    plan: AdaptiveDownloadPlan | None = None
    customization: dict[str, Any] = Field(default_factory=dict)
    scheduler_policy: dict[str, Any] = Field(default_factory=dict)
    progress_reporter: str = "rich"
    final_summary: dict[str, Any] = Field(default_factory=dict)


class FileDownloadOptions(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    url: str
    output_dir: Path
    backend: FileBackendChoice = FileBackendChoice.auto
    filename: str | None = None
    probe: DirectFileProbe | None = None
    trust_server_names: bool = False
    content_disposition: bool = True
    timestamping: bool = False
    use_server_timestamps: bool = True
    timeout: float = Field(default=30.0, ge=0)
    connections: int = Field(default=16, ge=1, le=64)
    splits: int = Field(default=16, ge=1, le=64)
    chunk_size: str = "1M"
    overwrite: bool = False
    continue_download: bool = True
    rate_limit: str | None = None
    checksum: str | None = None
    metalink: bool = True
    force_metalink: bool = False
    input_file: Path | None = None
    save_session: Path | None = None
    save_session_interval: int | None = Field(default=None, ge=0)
    metalink_preferred_protocol: MetalinkPreferredProtocol | None = None
    metalink_language: str | None = None
    metalink_os: str | None = None
    metalink_location: str | None = None
    metalink_base_uri: str | None = None
    metalink_enable_unique_protocol: bool | None = None
    server_stat_if: Path | None = None
    server_stat_of: Path | None = None
    server_stat_timeout: int | None = Field(default=None, ge=0)
    uri_selector: Aria2UriSelector | None = None
    user_agent: str | None = None
    headers: tuple[str, ...] = ()
    referer: str | None = None
    cache: bool | None = None
    compression: str | None = None
    no_compression: bool = False
    method: str = "GET"
    body_data: str | None = None
    body_file: Path | None = None
    load_cookies: Path | None = None
    proxy: str | None = None
    lowest_speed_limit: str | None = None
    max_tries: int | None = Field(default=None, ge=0)
    retry_wait: float | None = Field(default=None, ge=0)
    connect_timeout: float | None = Field(default=None, ge=0)
    file_allocation: str | None = None
    check_integrity: bool = False
    remote_time: bool = False
    conditional_get: bool = False
    http_accept_gzip: bool = True
    http_user: str | None = None
    http_password: str | None = None
    check_certificate: bool | None = None
    ca_certificate: Path | None = None
    ca_directory: Path | None = None
    certificate: Path | None = None
    private_key: Path | None = None
    secure_protocol: str | None = None
    dry_run: bool = False
    adaptive: bool = False
    max_concurrency: int | None = Field(default=None, ge=2, le=100)
    per_host_concurrency: int | None = Field(default=None, ge=1, le=100)
    politeness: AdaptivePoliteness = AdaptivePoliteness.normal
    explain: bool = False
    adaptive_plan: AdaptiveDownloadPlan | None = None
    quiet: bool = False
    json_output: bool = False
    progress_mode: ProgressMode = ProgressMode.auto
    verbose: bool = False

    @field_validator("url")
    @classmethod
    def file_url_must_not_be_blank(cls, value: str) -> str:
        return _normalize_http_url(value)

    @field_validator("output_dir", mode="before")
    @classmethod
    def expand_file_output_dir(cls, value: object) -> object:
        if isinstance(value, str):
            return Path(value).expanduser()
        if isinstance(value, Path):
            return value.expanduser()
        return value

    @field_validator("chunk_size")
    @classmethod
    def file_chunk_size_must_be_aria2_friendly(cls, value: str) -> str:
        if not _CHUNK_SIZE_PATTERN.fullmatch(value.strip()):
            msg = "chunk_size must be a positive size such as 512K, 1M, or 4M"
            raise ValueError(msg)
        return value.strip().upper()

    @field_validator("lowest_speed_limit")
    @classmethod
    def lowest_speed_limit_must_be_aria2_friendly(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned:
            return None
        if cleaned == "0":
            return cleaned
        if not _CHUNK_SIZE_PATTERN.fullmatch(cleaned):
            msg = "lowest_speed_limit must be 0 or a size such as 4K or 1M"
            raise ValueError(msg)
        return cleaned.upper()

    @field_validator("file_allocation")
    @classmethod
    def file_allocation_must_be_known(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip().lower()
        if not cleaned:
            return None
        if cleaned not in {"none", "prealloc", "trunc", "falloc"}:
            msg = "file_allocation must be none, prealloc, trunc, or falloc"
            raise ValueError(msg)
        return cleaned

    @field_validator("headers")
    @classmethod
    def file_headers_must_be_name_value(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _normalize_http_headers(value)

    @field_validator(
        "input_file",
        "save_session",
        "server_stat_if",
        "server_stat_of",
        "body_file",
        "load_cookies",
        "ca_certificate",
        "ca_directory",
        "certificate",
        "private_key",
        mode="before",
    )
    @classmethod
    def expand_file_policy_paths(cls, value: object) -> object:
        return _expand_optional_path(value)

    @field_validator(
        "metalink_language",
        "metalink_os",
        "metalink_location",
        "metalink_base_uri",
    )
    @classmethod
    def optional_file_strings_must_not_be_blank(cls, value: str | None) -> str | None:
        return _normalize_optional_string(value)

    @field_validator("method")
    @classmethod
    def file_method_must_not_be_blank(cls, value: str) -> str:
        cleaned = value.strip().upper()
        if not cleaned:
            msg = "method cannot be blank"
            raise ValueError(msg)
        return cleaned

    @field_validator("checksum")
    @classmethod
    def checksum_must_be_supported(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned:
            return None
        separator = ":" if ":" in cleaned else "=" if "=" in cleaned else ""
        if not separator:
            msg = "checksum must look like sha256:<hex-digest>"
            raise ValueError(msg)
        algorithm, digest = cleaned.split(separator, 1)
        normalized = algorithm.lower().replace("-", "")
        if normalized not in {"sha256", "sha512", "sha1", "md5"}:
            msg = "checksum algorithm must be sha256, sha512, sha1, or md5"
            raise ValueError(msg)
        if not digest or any(char not in "0123456789abcdefABCDEF" for char in digest):
            msg = "checksum digest must be hexadecimal"
            raise ValueError(msg)
        return f"{normalized}:{digest.lower()}"


class DirectFileProbe(BaseModel):
    """HTTP metadata collected before direct-file backend selection."""

    url: str
    final_url: str | None = None
    redirected: bool = False
    content_type: str | None = None
    content_length: int | None = Field(default=None, ge=0)
    content_disposition: str | None = None
    filename: str | None = None
    accept_ranges: str | None = None
    supports_ranges: bool = False
    etag: str | None = None
    last_modified: str | None = None
    file_extension: str | None = None
    host: str | None = None
    final_host: str | None = None
    redirect_target: str | None = None
    metalink_url: str | None = None
    metalink_source: str | None = None
    discovered_links: list[str] = Field(default_factory=list)
    sitemap_urls: list[str] = Field(default_factory=list)
    robots_url: str | None = None
    url_fingerprint: str | None = None
    mirror_fingerprint: str | None = None
    classification_notes: list[str] = Field(default_factory=list)
    warning_flags: list[str] = Field(default_factory=list)
    same_host: bool = True
    external_host: bool = False
    probed: bool = True
    error: str | None = None


class SiteDownloadOptions(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    url: str
    output_dir: Path
    backend: SiteBackendChoice = SiteBackendChoice.auto
    depth: int = Field(default=2, ge=1, le=20)
    page_requisites: bool = True
    convert_links: bool = True
    span_hosts: bool = False
    wait: float | None = Field(default=1.0, ge=0)
    accept: str | None = None
    reject: str | None = None
    robots: bool = True
    follow_sitemaps: bool = True
    no_parent: bool = True
    domains: str | None = None
    exclude_domains: str | None = None
    include_directories: str | None = None
    exclude_directories: str | None = None
    accept_regex: str | None = None
    reject_regex: str | None = None
    filter_mime_type: str | None = None
    filter_urls: bool = False
    ignore_case: bool = False
    follow_tags: str | None = None
    ignore_tags: str | None = None
    directories: bool | None = None
    host_directories: bool | None = None
    protocol_directories: bool | None = None
    cut_dirs: int | None = Field(default=None, ge=0)
    default_page: str | None = None
    adjust_extension: bool = False
    convert_file_only: bool = False
    cut_url_get_vars: bool = False
    cut_file_get_vars: bool = False
    keep_extension: bool = False
    unlink: bool = False
    backups: int | None = Field(default=None, ge=0)
    backup_converted: bool = False
    restrict_file_names: str | None = None
    download_attr: DownloadAttrMode | None = None
    input_file: Path | None = None
    input_file_only: bool = False
    base: str | None = None
    force_html: bool = False
    force_css: bool = False
    force_sitemap: bool = False
    force_atom: bool = False
    force_rss: bool = False
    force_metalink: bool = False
    user_agent: str | None = None
    if_modified_since: bool | None = None
    headers: tuple[str, ...] = ()
    referer: str | None = None
    cache: bool | None = None
    compression: str | None = None
    no_compression: bool = False
    method: str | None = None
    body_data: str | None = None
    body_file: Path | None = None
    post_data: str | None = None
    post_file: Path | None = None
    cookies: bool | None = None
    browser_cookies: str | None = None
    load_cookies: Path | None = None
    save_cookies: Path | None = None
    keep_session_cookies: bool = False
    cookie_suffixes: str | None = None
    netrc: bool | None = None
    netrc_file: Path | None = None
    proxy: bool | None = None
    proxy_url: str | None = None
    http_user: str | None = None
    http_password: str | None = None
    proxy_user: str | None = None
    proxy_password: str | None = None
    https_only: bool = False
    https_enforce: HttpsEnforceMode | None = None
    hsts: bool | None = None
    hsts_file: Path | None = None
    check_certificate: bool | None = None
    check_hostname: bool | None = None
    ca_certificate: Path | None = None
    ca_directory: Path | None = None
    certificate: Path | None = None
    certificate_type: CertificateType | None = None
    private_key: Path | None = None
    private_key_type: CertificateType | None = None
    crl_file: Path | None = None
    secure_protocol: str | None = None
    ocsp: bool | None = None
    ocsp_date: bool | None = None
    ocsp_file: Path | None = None
    ocsp_nonce: bool | None = None
    ocsp_server: str | None = None
    ocsp_stapling: bool | None = None
    tls_false_start: bool | None = None
    tls_resume: bool | None = None
    tls_session_file: Path | None = None
    http2: bool | None = None
    http2_only: bool = False
    http2_request_window: int | None = Field(default=None, ge=1)
    content_on_error: bool = False
    save_content_on: str | None = None
    save_headers: bool = False
    server_response: bool = False
    ignore_length: bool = False
    verify_sig: VerifySigMode | None = None
    signature_extensions: str | None = None
    gnupg_homedir: Path | None = None
    verify_save_failed: bool = False
    max_files: int | None = Field(default=None, ge=1)
    max_total_size: str | None = None
    max_runtime: float | None = Field(default=None, ge=0)
    planning_runtime_seconds: float = Field(default=0.0, ge=0, exclude=True, repr=False)
    quota: str | None = None
    limit_rate: str | None = None
    warc_file: Path | None = None
    warc_compression: bool | None = None
    warc_cdx: bool = False
    warc_max_size: str | None = None
    retry_connrefused: bool = False
    start_pos: str | None = None
    inet4_only: bool = False
    inet6_only: bool = False
    bind_address: str | None = None
    bind_interface: str | None = None
    prefer_family: PreferFamily | None = None
    dns_cache: bool | None = None
    dns_cache_preload: Path | None = None
    tcp_fastopen: bool | None = None
    max_threads: int = Field(default=5, ge=1, le=100)
    tries: int = Field(default=20, ge=0)
    waitretry: float = Field(default=10.0, ge=0)
    retry_on_http_error: str | None = None
    max_redirect: int = Field(default=20, ge=0)
    timeout: float | None = Field(default=None, ge=0)
    dns_timeout: float | None = Field(default=None, ge=0)
    connect_timeout: float | None = Field(default=None, ge=0)
    read_timeout: float | None = Field(default=None, ge=0)
    random_wait: bool = False
    timestamping: bool = False
    overwrite: bool = False
    continue_download: bool = True
    spider: bool = False
    stats: bool = True
    dry_run: bool = False
    adaptive: bool = False
    max_concurrency: int | None = Field(default=None, ge=2, le=100)
    per_host_concurrency: int | None = Field(default=None, ge=1, le=100)
    politeness: AdaptivePoliteness = AdaptivePoliteness.normal
    explain: bool = False
    adaptive_plan: AdaptiveDownloadPlan | None = None
    quiet: bool = False
    json_output: bool = False
    progress_mode: ProgressMode = ProgressMode.auto
    verbose: bool = False

    @field_validator("url")
    @classmethod
    def site_url_must_not_be_blank(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            msg = "URL or parser input path cannot be blank"
            raise ValueError(msg)
        if any(ord(character) < 32 or ord(character) == 127 for character in cleaned):
            msg = "URL or parser input path cannot contain control characters"
            raise ValueError(msg)
        return cleaned

    @model_validator(mode="after")
    def site_url_must_match_input_mode(self) -> SiteDownloadOptions:
        if self.base is not None:
            self.base = _normalize_http_url(self.base)
        if not self.input_file_only or self.base is not None:
            self.url = _normalize_http_url(self.url)
        return self

    @field_validator("output_dir", mode="before")
    @classmethod
    def expand_site_output_dir(cls, value: object) -> object:
        if isinstance(value, str):
            return Path(value).expanduser()
        if isinstance(value, Path):
            return value.expanduser()
        return value

    @field_validator("headers")
    @classmethod
    def site_headers_must_be_name_value(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _normalize_http_headers(value)

    @field_validator(
        "input_file",
        "warc_file",
        "body_file",
        "post_file",
        "dns_cache_preload",
        "gnupg_homedir",
        "hsts_file",
        "load_cookies",
        "save_cookies",
        "netrc_file",
        "ca_certificate",
        "ca_directory",
        "certificate",
        "crl_file",
        "ocsp_file",
        "private_key",
        "tls_session_file",
        mode="before",
    )
    @classmethod
    def expand_site_policy_paths(cls, value: object) -> object:
        return _expand_optional_path(value)

    @field_validator("method")
    @classmethod
    def site_method_must_not_be_blank(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip().upper()
        if not cleaned:
            msg = "method cannot be blank"
            raise ValueError(msg)
        return cleaned

    @field_validator("max_total_size", "quota")
    @classmethod
    def mirror_size_limits_must_be_wget_friendly(cls, value: str | None) -> str | None:
        return _normalize_optional_size(value, label="mirror size limit")


class DirectoryMirrorOptions(SiteDownloadOptions):
    """Restrained open HTTP directory/file-tree mirroring options."""

    page_requisites: bool = False
    convert_links: bool = False
    span_hosts: bool = False
    follow_sitemaps: bool = False
    no_parent: bool = True
    directories: bool | None = True
    user_agent: str | None = DEFAULT_DIRECTORY_USER_AGENT
    if_modified_since: bool | None = False
    timestamping: bool = True
    exact_directory_index: bool = False
    exact_directory_base_url: str | None = None
    exact_directory_items: tuple[WorkItem, ...] = ()


class FormatInfo(BaseModel):
    format_id: str
    ext: str | None = None
    resolution: str | None = None
    fps: float | None = None
    vcodec: str | None = None
    acodec: str | None = None
    filesize: int | None = None
    tbr: float | None = None
    protocol: str | None = None
    note: str | None = None


class MediaFormatChoice(BaseModel):
    label: str
    format: str
    container: str
    resolution: str
    video_codec: str
    audio_codec: str | None = None
    video_format_id: str
    audio_format_id: str | None = None
    filesize: int | None = None
    note: str | None = None


class MediaInfo(BaseModel):
    id: str | None = None
    title: str | None = None
    uploader: str | None = None
    channel: str | None = None
    duration: int | None = None
    webpage_url: str | None = None
    extractor: str | None = None
    upload_date: str | None = None
    view_count: int | None = None
    availability: str | None = None
    is_playlist: bool = False
    playlist_count: int | None = None
    best_video: str | None = None
    best_audio: str | None = None
    formats: list[FormatInfo] = Field(default_factory=list)


class DownloadResult(BaseModel):
    status: DownloadStatus
    url: str
    message: str | None = None
    ydl_opts: dict[str, Any] | None = None


class ProgressEvent(BaseModel):
    """Neutral progress event emitted by every backend."""

    engine: EngineKind
    status: str
    phase: ProgressPhase = ProgressPhase.download
    kind: HubKind | None = None
    filename: str | None = None
    title: str | None = None
    url: str | None = None
    item_id: str | None = None
    line_no: int | None = None
    downloaded_bytes: int | None = Field(default=None, ge=0)
    total_bytes: int | None = Field(default=None, ge=0)
    estimated_bytes: int | None = Field(default=None, ge=0)
    fragment_index: int | None = Field(default=None, ge=0)
    fragment_count: int | None = Field(default=None, ge=0)
    files_done: int | None = Field(default=None, ge=0)
    files_total: int | None = Field(default=None, ge=0)
    percent: float | None = Field(default=None, ge=0, le=100)
    retry_count: int | None = Field(default=None, ge=0)
    active_connections: int | None = Field(default=None, ge=0)
    queue_concurrency: int | None = Field(default=None, ge=0)
    per_host_concurrency: int | None = Field(default=None, ge=0)
    per_file_segments: int | None = Field(default=None, ge=0)
    max_total_connections: int | None = Field(default=None, ge=0)
    max_per_host_connections: int | None = Field(default=None, ge=0)
    max_active_postprocessors: int | None = Field(default=None, ge=0)
    priority: int | None = Field(default=None, ge=0)
    recursion_depth: int | None = Field(default=None, ge=0)
    size_class: FileSizeClass | None = None
    work_bucket: WorkBucket | None = None
    selected_backend: str | None = None
    scheduler_decision: str | None = None
    speed_limit: str | None = None
    reclassified_from: str | None = None
    speed_bytes_per_sec: float | None = Field(default=None, ge=0)
    eta_seconds: float | None = Field(default=None, ge=0)
    backend_id: str | None = None
    error_code: str | None = None
    verified_bytes: int | None = Field(default=None, ge=0)
    verification_pending: bool | None = None
    piece_length: int | None = Field(default=None, ge=0)
    piece_count: int | None = Field(default=None, ge=0)
    bitfield: str | None = None
    followed_by: list[str] = Field(default_factory=list)
    following: str | None = None
    belongs_to: str | None = None
    backend_files: list[dict[str, Any]] = Field(default_factory=list)
    message: str | None = None


class HubRequest(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    url: str
    requested_kind: HubKind = HubKind.auto
    output_dir: Path
    backend: str = "auto"
    audio: bool = False
    video_codec: VideoCodecChoice = VideoCodecChoice.auto
    audio_codec: AudioCodec | None = None
    audio_quality: int | None = Field(default=None, ge=0, le=10)
    dry_run: bool = False
    adaptive: bool = False
    max_concurrency: int | None = Field(default=None, ge=2, le=100)
    per_host_concurrency: int | None = Field(default=None, ge=1, le=100)
    politeness: AdaptivePoliteness = AdaptivePoliteness.normal
    explain: bool = False
    quiet: bool = False
    json_output: bool = False
    progress_mode: ProgressMode = ProgressMode.auto
    verbose: bool = False

    @field_validator("url")
    @classmethod
    def hub_url_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            msg = "URL cannot be blank"
            raise ValueError(msg)
        return value.strip()

    @field_validator("output_dir", mode="before")
    @classmethod
    def expand_hub_output_dir(cls, value: object) -> object:
        if isinstance(value, str):
            return Path(value).expanduser()
        if isinstance(value, Path):
            return value.expanduser()
        return value


class EngineRoute(BaseModel):
    kind: HubKind
    engine: EngineKind
    reason: str
    url: str
    output_dir: Path
    is_media_host: bool = False
    file_suffix: str | None = None
    safety: list[str] = Field(default_factory=list)


class OptimizedDownloadPlan(BaseModel):
    route: EngineRoute
    output: Path | None = None
    summary: dict[str, Any] = Field(default_factory=dict)
    args: list[str] = Field(default_factory=list)
    session: SmartDownloadSession | None = None


class DownloadPlan(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    url: str
    output_dir: Path
    outtmpl: str
    format: str
    noplaylist: bool
    merge_output_format: str | None = None
    postprocessors: list[dict[str, Any]] = Field(default_factory=list)
    archive_file: Path | None = None
    browser_cookies: str | None = None
    cookies_file: Path | None = None
    use_aria2: bool = False
    require_aria2: bool = False
    connections: int = 16
    splits: int = 16
    chunk_size: str = "1M"
    write_info_json: bool = True
    write_thumbnail: bool = True
    restrict_filenames: bool = False
    overwrite: bool = False
    continue_download: bool = True
    retries: int = 10
    fragment_retries: int = 10
    file_access_retries: int = 3
    concurrent_fragment_downloads: int = 4
    retry_sleep: list[str] = Field(default_factory=list)
    skip_unavailable_fragments: bool = True
    skip_download: bool = False
    ignore_unavailable_playlist_entries: bool = False
    rate_limit: int | None = None
    throttled_rate: int | None = None
    http_chunk_size: int | None = None
    socket_timeout: float | None = None
    source_address: str | None = None
    impersonate: str | None = None
    extractor_args: list[str] = Field(default_factory=list)
    sleep: float | None = None
    proxy: str | None = None
    playlist_items: str | None = None
    playlist_start: int | None = None
    playlist_end: int | None = None
    subtitle_mode: SubtitleMode = SubtitleMode.none
    sub_lang: str | None = None
    embed_subs: bool = False
    split_chapters: bool = False
    format_sort: list[str] = Field(default_factory=list)
    match_filters: list[str] = Field(default_factory=list)
    break_match_filters: list[str] = Field(default_factory=list)
    max_downloads: int | None = None
    break_on_existing: bool = False
    break_on_reject: bool = False
    break_per_input: bool = False
    date: str | None = None
    date_before: str | None = None
    date_after: str | None = None
    min_filesize: int | None = None
    max_filesize: int | None = None
    live_from_start: bool = False
    download_sections: list[str] = Field(default_factory=list)
    sponsorblock_mark: list[str] = Field(default_factory=list)
    sponsorblock_remove: list[str] = Field(default_factory=list)
    sponsorblock_chapter_title: str | None = None
    sponsorblock_api: str | None = None
    playlist_url_detected: bool = False
    youtube_collection_url_detected: bool = False
    watch_playlist_params_detected: bool = False
    planner_notes: list[str] = Field(default_factory=list)
    json_output: bool = False
    verbose: bool = False


class BatchEntry(BaseModel):
    line_no: int
    url: str


class BatchItemResult(BaseModel):
    entry: BatchEntry
    status: DownloadStatus
    message: str | None = None
    plan: dict[str, Any] | None = None


class BatchSummary(BaseModel):
    kind: BatchKind
    total: int
    succeeded: int = 0
    failed: int = 0
    skipped: int = 0
    canceled: int = 0
    results: list[BatchItemResult] = Field(default_factory=list)


class DoctorCheck(BaseModel):
    name: str
    ok: bool
    required: bool = True
    detail: str
    hint: str | None = None


class DoctorReport(BaseModel):
    checks: list[DoctorCheck]

    @property
    def ok(self) -> bool:
        return all(check.ok for check in self.checks if check.required)


def _normalize_playlist_items(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    if not _PLAYLIST_ITEMS_PATTERN.fullmatch(cleaned):
        msg = "playlist_items must look like 1-10,15,20-"
        raise ValueError(msg)
    for segment in cleaned.split(","):
        start_text, separator, end_text = segment.partition("-")
        if int(start_text) < 1 or (end_text and int(end_text) < 1):
            msg = "playlist item indexes must start at 1"
            raise ValueError(msg)
        if separator and end_text and int(start_text) > int(end_text):
            msg = "playlist item ranges cannot count backwards"
            raise ValueError(msg)
    return cleaned
