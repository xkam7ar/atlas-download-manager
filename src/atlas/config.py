"""Configuration loading and defaults."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path
from typing import Any, cast

from pydantic import AliasChoices, Field, ValidationError, field_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)

from atlas.errors import ConfigError
from atlas.models import (
    DEFAULT_DIRECTORY_USER_AGENT,
    Aria2UriSelector,
    AudioCodec,
    Container,
    FileBackendChoice,
    MetalinkPreferredProtocol,
    SiteBackendChoice,
)
from atlas.paths import archive_path, config_path, default_output_dir
from atlas.redaction import is_sensitive_key, text_contains_secret

_DOCUMENTED_TOML_ALIASES = {
    "default_output_dir": "output_dir",
    "default_video_container": "video_container",
    "default_audio_codec": "audio_codec",
    "use_aria2": "aria2",
}


class _AtlasTomlSettingsSource(TomlConfigSettingsSource):
    """Normalize Atlas's documented TOML names across supported settings releases."""

    def __call__(self) -> dict[str, Any]:
        values = dict(super().__call__())
        for documented_name, field_name in _DOCUMENTED_TOML_ALIASES.items():
            if documented_name not in values:
                continue
            values.setdefault(field_name, values[documented_name])
            del values[documented_name]
        return values


class AtlasSettings(BaseSettings):
    """Runtime settings loaded from defaults, optional TOML, and environment."""

    model_config = SettingsConfigDict(
        env_prefix="ATLAS_",
        extra="forbid",
        populate_by_name=True,
        toml_file=config_path(),
    )

    output_dir: Path = Field(
        default_factory=default_output_dir,
        validation_alias=AliasChoices("output_dir", "default_output_dir"),
    )
    archive: bool = True
    archive_file: Path = Field(default_factory=archive_path)
    video_container: Container = Field(
        default=Container.auto,
        validation_alias=AliasChoices("video_container", "default_video_container"),
    )
    audio_codec: AudioCodec = Field(
        default=AudioCodec.best,
        validation_alias=AliasChoices("audio_codec", "default_audio_codec"),
    )
    audio_quality: int = Field(default=0, ge=0, le=10)
    aria2: bool = Field(default=True, validation_alias=AliasChoices("aria2", "use_aria2"))
    aria2_connections: int = Field(default=16, ge=1, le=64)
    aria2_splits: int = Field(default=16, ge=1, le=64)
    aria2_chunk_size: str = "1M"
    media_concurrent_fragments: int = Field(default=4, ge=1, le=64)
    media_file_access_retries: int = Field(default=3, ge=0)
    media_retry_sleep: list[str] = Field(default_factory=list)
    media_skip_unavailable_fragments: bool = True
    media_throttled_rate: str | None = None
    media_http_chunk_size: str | None = None
    media_socket_timeout: float | None = Field(default=None, ge=0)
    media_source_address: str | None = None
    media_impersonate: str | None = None
    media_extractor_args: list[str] = Field(default_factory=list)
    media_match_filters: list[str] = Field(default_factory=list)
    media_break_match_filters: list[str] = Field(default_factory=list)
    media_max_downloads: int | None = Field(default=None, ge=1)
    media_break_on_existing: bool = False
    media_break_on_reject: bool = False
    media_break_per_input: bool = False
    media_date: str | None = None
    media_date_before: str | None = None
    media_date_after: str | None = None
    media_min_filesize: str | None = None
    media_max_filesize: str | None = None
    media_reject_live: bool = False
    media_reject_upcoming: bool = False
    media_live_from_start: bool = False
    media_download_sections: list[str] = Field(default_factory=list)
    media_sponsorblock_mark: list[str] = Field(default_factory=list)
    media_sponsorblock_remove: list[str] = Field(default_factory=list)
    media_sponsorblock_chapter_title: str | None = None
    media_sponsorblock_api: str | None = None
    batch_concurrency: int = Field(default=2, ge=1, le=16)
    file_backend: FileBackendChoice = Field(default=FileBackendChoice.auto)
    file_trust_server_names: bool = False
    file_content_disposition: bool = True
    file_timestamping: bool = False
    file_use_server_timestamps: bool = True
    file_timeout: float = Field(default=30.0, ge=0)
    file_lowest_speed_limit: str | None = None
    file_max_tries: int | None = Field(default=None, ge=0)
    file_retry_wait: float | None = Field(default=None, ge=0)
    file_connect_timeout: float | None = Field(default=None, ge=0)
    file_file_allocation: str | None = None
    file_check_integrity: bool = False
    file_remote_time: bool = False
    file_conditional_get: bool = False
    file_http_accept_gzip: bool = True
    file_input_file: Path | None = None
    file_save_session: Path | None = None
    file_save_session_interval: int | None = Field(default=None, ge=0)
    file_metalink_preferred_protocol: MetalinkPreferredProtocol | None = None
    file_metalink_language: str | None = None
    file_metalink_os: str | None = None
    file_metalink_location: str | None = None
    file_metalink_base_uri: str | None = None
    file_metalink_enable_unique_protocol: bool | None = None
    file_server_stat_if: Path | None = None
    file_server_stat_of: Path | None = None
    file_server_stat_timeout: int | None = Field(default=None, ge=0)
    file_uri_selector: Aria2UriSelector | None = None
    site_backend: SiteBackendChoice = Field(default=SiteBackendChoice.auto)
    dir_backend: SiteBackendChoice = Field(default=SiteBackendChoice.auto)
    site_depth: int = Field(default=2, ge=1, le=20)
    dir_depth: int = Field(default=2, ge=1, le=20)
    site_page_requisites: bool = True
    site_convert_links: bool = True
    site_span_hosts: bool = False
    site_wait: float | None = Field(default=1.0, ge=0)
    dir_wait: float | None = Field(default=1.0, ge=0)
    dir_user_agent: str | None = DEFAULT_DIRECTORY_USER_AGENT
    dir_if_modified_since: bool = False
    dir_timestamping: bool = True
    site_accept: str | None = None
    site_reject: str | None = None
    site_robots: bool = True
    site_follow_sitemaps: bool = True
    site_no_parent: bool = True
    site_domains: str | None = None
    site_exclude_domains: str | None = None
    site_include_directories: str | None = None
    site_exclude_directories: str | None = None
    site_accept_regex: str | None = None
    site_reject_regex: str | None = None
    site_filter_mime_type: str | None = None
    site_ignore_case: bool = False
    site_max_files: int | None = Field(default=None, ge=1)
    site_max_total_size: str | None = None
    site_max_runtime: float | None = Field(default=None, ge=0)
    site_max_threads: int = Field(default=5, ge=1, le=100)
    site_tries: int = Field(default=20, ge=0)
    site_waitretry: float = Field(default=10.0, ge=0)
    site_retry_on_http_error: str | None = None
    site_max_redirect: int = Field(default=20, ge=0)
    site_timeout: float | None = Field(default=None, ge=0)
    site_dns_timeout: float | None = Field(default=None, ge=0)
    site_connect_timeout: float | None = Field(default=None, ge=0)
    site_read_timeout: float | None = Field(default=None, ge=0)
    site_random_wait: bool = False
    site_timestamping: bool = False
    site_stats: bool = True
    write_info_json: bool = True
    write_thumbnail: bool = True
    embed_thumbnail: bool = True
    embed_metadata: bool = True

    @field_validator(
        "output_dir",
        "archive_file",
        "file_input_file",
        "file_save_session",
        "file_server_stat_if",
        "file_server_stat_of",
        mode="before",
    )
    @classmethod
    def expand_user_paths(cls, value: object) -> object:
        if isinstance(value, str):
            return Path(value).expanduser()
        if isinstance(value, Path):
            return value.expanduser()
        return value

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (
            init_settings,
            env_settings,
            _AtlasTomlSettingsSource(settings_cls),
            dotenv_settings,
            file_secret_settings,
        )


def _settings_class_for_toml(path: Path) -> type[AtlasSettings]:
    config_values = dict(AtlasSettings.model_config)
    config_values["toml_file"] = path

    class FileAtlasSettings(AtlasSettings):
        model_config = cast(SettingsConfigDict, config_values)

    return FileAtlasSettings


def load_config(path: Path | None = None) -> AtlasSettings:
    """Load settings from pydantic-settings sources, including TOML."""

    cfg_path = path or config_path()
    try:
        return _settings_class_for_toml(cfg_path)()
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"Invalid TOML in {cfg_path}: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"Could not read config at {cfg_path}: {exc}") from exc
    except ValidationError as exc:
        raise ConfigError(f"Invalid config in {cfg_path}: {exc}") from exc


def settings_as_plain_dict(settings: AtlasSettings) -> dict[str, Any]:
    """Return config values in a JSON-friendly shape."""

    return {
        key: str(value) if isinstance(value, Path) else value
        for key, value in settings.model_dump().items()
    }


def settings_as_toml(settings: AtlasSettings, *, redact_sensitive: bool = True) -> str:
    """Return a valid TOML view of the effective config.

    Human-facing configuration views hide credentials by default. Setup can explicitly
    opt in to retaining configured values when it writes the user's config file.
    """

    values: dict[str, Any] = {
        "default_output_dir": settings.output_dir,
        "archive_file": settings.archive_file,
        "archive": settings.archive,
        "default_video_container": settings.video_container.value,
        "default_audio_codec": settings.audio_codec.value,
        "audio_quality": settings.audio_quality,
        "use_aria2": settings.aria2,
        "aria2_connections": settings.aria2_connections,
        "aria2_splits": settings.aria2_splits,
        "aria2_chunk_size": settings.aria2_chunk_size,
        "media_concurrent_fragments": settings.media_concurrent_fragments,
        "media_file_access_retries": settings.media_file_access_retries,
        "media_retry_sleep": settings.media_retry_sleep,
        "media_skip_unavailable_fragments": settings.media_skip_unavailable_fragments,
        "media_throttled_rate": settings.media_throttled_rate,
        "media_http_chunk_size": settings.media_http_chunk_size,
        "media_socket_timeout": settings.media_socket_timeout,
        "media_source_address": settings.media_source_address,
        "media_impersonate": settings.media_impersonate,
        "media_extractor_args": settings.media_extractor_args,
        "media_match_filters": settings.media_match_filters,
        "media_break_match_filters": settings.media_break_match_filters,
        "media_max_downloads": settings.media_max_downloads,
        "media_break_on_existing": settings.media_break_on_existing,
        "media_break_on_reject": settings.media_break_on_reject,
        "media_break_per_input": settings.media_break_per_input,
        "media_date": settings.media_date,
        "media_date_before": settings.media_date_before,
        "media_date_after": settings.media_date_after,
        "media_min_filesize": settings.media_min_filesize,
        "media_max_filesize": settings.media_max_filesize,
        "media_reject_live": settings.media_reject_live,
        "media_reject_upcoming": settings.media_reject_upcoming,
        "media_live_from_start": settings.media_live_from_start,
        "media_download_sections": settings.media_download_sections,
        "media_sponsorblock_mark": settings.media_sponsorblock_mark,
        "media_sponsorblock_remove": settings.media_sponsorblock_remove,
        "media_sponsorblock_chapter_title": settings.media_sponsorblock_chapter_title,
        "media_sponsorblock_api": settings.media_sponsorblock_api,
        "batch_concurrency": settings.batch_concurrency,
        "file_backend": settings.file_backend.value,
        "file_trust_server_names": settings.file_trust_server_names,
        "file_content_disposition": settings.file_content_disposition,
        "file_timestamping": settings.file_timestamping,
        "file_use_server_timestamps": settings.file_use_server_timestamps,
        "file_timeout": settings.file_timeout,
        "file_lowest_speed_limit": settings.file_lowest_speed_limit,
        "file_max_tries": settings.file_max_tries,
        "file_retry_wait": settings.file_retry_wait,
        "file_connect_timeout": settings.file_connect_timeout,
        "file_file_allocation": settings.file_file_allocation,
        "file_check_integrity": settings.file_check_integrity,
        "file_remote_time": settings.file_remote_time,
        "file_conditional_get": settings.file_conditional_get,
        "file_http_accept_gzip": settings.file_http_accept_gzip,
        "file_input_file": settings.file_input_file,
        "file_save_session": settings.file_save_session,
        "file_save_session_interval": settings.file_save_session_interval,
        "file_metalink_preferred_protocol": (
            settings.file_metalink_preferred_protocol.value
            if settings.file_metalink_preferred_protocol
            else None
        ),
        "file_metalink_language": settings.file_metalink_language,
        "file_metalink_os": settings.file_metalink_os,
        "file_metalink_location": settings.file_metalink_location,
        "file_metalink_base_uri": settings.file_metalink_base_uri,
        "file_metalink_enable_unique_protocol": settings.file_metalink_enable_unique_protocol,
        "file_server_stat_if": settings.file_server_stat_if,
        "file_server_stat_of": settings.file_server_stat_of,
        "file_server_stat_timeout": settings.file_server_stat_timeout,
        "file_uri_selector": (
            settings.file_uri_selector.value if settings.file_uri_selector else None
        ),
        "site_backend": settings.site_backend.value,
        "dir_backend": settings.dir_backend.value,
        "site_depth": settings.site_depth,
        "dir_depth": settings.dir_depth,
        "site_page_requisites": settings.site_page_requisites,
        "site_convert_links": settings.site_convert_links,
        "site_span_hosts": settings.site_span_hosts,
        "site_wait": settings.site_wait,
        "dir_wait": settings.dir_wait,
        "dir_user_agent": settings.dir_user_agent,
        "dir_if_modified_since": settings.dir_if_modified_since,
        "dir_timestamping": settings.dir_timestamping,
        "site_accept": settings.site_accept,
        "site_reject": settings.site_reject,
        "site_robots": settings.site_robots,
        "site_follow_sitemaps": settings.site_follow_sitemaps,
        "site_no_parent": settings.site_no_parent,
        "site_domains": settings.site_domains,
        "site_exclude_domains": settings.site_exclude_domains,
        "site_include_directories": settings.site_include_directories,
        "site_exclude_directories": settings.site_exclude_directories,
        "site_accept_regex": settings.site_accept_regex,
        "site_reject_regex": settings.site_reject_regex,
        "site_filter_mime_type": settings.site_filter_mime_type,
        "site_ignore_case": settings.site_ignore_case,
        "site_max_files": settings.site_max_files,
        "site_max_total_size": settings.site_max_total_size,
        "site_max_runtime": settings.site_max_runtime,
        "site_max_threads": settings.site_max_threads,
        "site_tries": settings.site_tries,
        "site_waitretry": settings.site_waitretry,
        "site_retry_on_http_error": settings.site_retry_on_http_error,
        "site_max_redirect": settings.site_max_redirect,
        "site_timeout": settings.site_timeout,
        "site_dns_timeout": settings.site_dns_timeout,
        "site_connect_timeout": settings.site_connect_timeout,
        "site_read_timeout": settings.site_read_timeout,
        "site_random_wait": settings.site_random_wait,
        "site_timestamping": settings.site_timestamping,
        "site_stats": settings.site_stats,
        "embed_metadata": settings.embed_metadata,
        "embed_thumbnail": settings.embed_thumbnail,
        "write_thumbnail": settings.write_thumbnail,
        "write_info_json": settings.write_info_json,
    }

    lines: list[str] = []
    for key, value in values.items():
        if value is None:
            continue
        if redact_sensitive and _config_value_is_sensitive(key, value):
            rendered = _toml_list(["<redacted>"]) if isinstance(value, list) else '"<redacted>"'
            lines.append(f"{key} = {rendered}")
            continue
        if isinstance(value, Path):
            rendered = str(value)
            home = str(Path.home())
            if rendered == home or rendered.startswith(f"{home}/"):
                rendered = rendered.replace(home, "~", 1)
            lines.append(f"{key} = {_toml_string(rendered)}")
        elif isinstance(value, bool):
            lines.append(f"{key} = {str(value).lower()}")
        elif isinstance(value, int | float):
            lines.append(f"{key} = {value}")
        elif isinstance(value, list):
            lines.append(f"{key} = {_toml_list(value)}")
        else:
            lines.append(f"{key} = {_toml_string(str(value))}")
    return "\n".join(lines)


def _config_value_is_sensitive(key: str, value: object) -> bool:
    if is_sensitive_key(key):
        return True
    if isinstance(value, str):
        return text_contains_secret(value)
    if isinstance(value, list):
        return any(isinstance(item, str) and text_contains_secret(item) for item in value)
    return False


def _toml_string(value: str) -> str:
    """Render a JSON-compatible TOML basic string with safe escaping."""

    return json.dumps(value, ensure_ascii=False)


def _toml_list(values: list[object]) -> str:
    rendered = [str(item) if isinstance(item, Path) else item for item in values]
    return json.dumps(rendered, ensure_ascii=False)
