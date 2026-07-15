from __future__ import annotations

import tomllib
import warnings
from pathlib import Path

import pytest

from atlas.config import AtlasSettings, load_config, settings_as_plain_dict, settings_as_toml
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
from atlas.paths import archive_path, default_output_dir


def test_config_defaults_are_sensible() -> None:
    settings = load_config(Path("/definitely/not/present.toml"))

    assert settings.output_dir == default_output_dir()
    assert settings.archive is True
    assert settings.archive_file == archive_path()
    assert settings.video_container == Container.auto
    assert settings.audio_codec == AudioCodec.best
    assert settings.aria2 is True
    assert settings.aria2_connections == 16
    assert settings.aria2_splits == 16
    assert settings.aria2_chunk_size == "1M"
    assert settings.media_concurrent_fragments == 4
    assert settings.media_file_access_retries == 3
    assert settings.media_retry_sleep == []
    assert settings.media_skip_unavailable_fragments is True
    assert settings.media_extractor_args == []
    assert settings.media_match_filters == []
    assert settings.media_break_match_filters == []
    assert settings.media_max_downloads is None
    assert settings.media_break_on_existing is False
    assert settings.media_break_on_reject is False
    assert settings.media_break_per_input is False
    assert settings.media_download_sections == []
    assert settings.media_sponsorblock_mark == []
    assert settings.media_sponsorblock_remove == []
    assert settings.batch_concurrency == 2
    assert settings.file_backend == FileBackendChoice.auto
    assert settings.file_trust_server_names is False
    assert settings.file_content_disposition is True
    assert settings.file_timestamping is False
    assert settings.file_use_server_timestamps is True
    assert settings.file_timeout == 30.0
    assert settings.file_lowest_speed_limit is None
    assert settings.file_max_tries is None
    assert settings.file_retry_wait is None
    assert settings.file_connect_timeout is None
    assert settings.file_file_allocation is None
    assert settings.file_check_integrity is False
    assert settings.file_remote_time is False
    assert settings.file_conditional_get is False
    assert settings.file_http_accept_gzip is True
    assert settings.file_input_file is None
    assert settings.file_save_session is None
    assert settings.file_save_session_interval is None
    assert settings.file_metalink_preferred_protocol is None
    assert settings.file_metalink_enable_unique_protocol is None
    assert settings.file_server_stat_if is None
    assert settings.file_server_stat_of is None
    assert settings.file_server_stat_timeout is None
    assert settings.file_uri_selector is None
    assert settings.site_backend == SiteBackendChoice.auto
    assert settings.dir_backend == SiteBackendChoice.auto
    assert settings.site_depth == 2
    assert settings.dir_depth == 2
    assert settings.site_page_requisites is True
    assert settings.site_convert_links is True
    assert settings.site_span_hosts is False
    assert settings.site_wait == 1.0
    assert settings.dir_wait == 1.0
    assert settings.dir_user_agent == DEFAULT_DIRECTORY_USER_AGENT
    assert settings.dir_if_modified_since is False
    assert settings.dir_timestamping is True
    assert settings.site_robots is True
    assert settings.site_follow_sitemaps is True
    assert settings.site_no_parent is True
    assert settings.site_max_files is None
    assert settings.site_max_total_size is None
    assert settings.site_max_runtime is None
    assert settings.site_max_threads == 5
    assert settings.site_tries == 20
    assert settings.site_waitretry == 10.0
    assert settings.site_max_redirect == 20
    assert settings.site_stats is True
    assert settings.write_info_json is True
    assert settings.write_thumbnail is True
    assert settings.embed_thumbnail is True
    assert settings.embed_metadata is True


def test_load_config_from_toml(tmp_path: Path) -> None:
    cfg = tmp_path / "config.toml"
    out = tmp_path / "out"
    archive = tmp_path / "archive.txt"
    cfg.write_text(
        "\n".join(
            [
                f'output_dir = "{out}"',
                f'archive_file = "{archive}"',
                'video_container = "mp4"',
                'audio_codec = "opus"',
                "audio_quality = 3",
                "aria2 = false",
                'media_retry_sleep = ["http:1"]',
                'media_extractor_args = ["youtube:player_client=android"]',
                'media_match_filters = ["duration>?60"]',
                'media_break_match_filters = ["view_count<10"]',
                "media_max_downloads = 2",
                "media_break_on_existing = true",
                'media_download_sections = ["intro"]',
                'media_sponsorblock_mark = ["sponsor"]',
                'file_backend = "wget2"',
                "file_trust_server_names = true",
                'file_lowest_speed_limit = "32K"',
                "file_max_tries = 5",
                "file_retry_wait = 2.5",
                "file_connect_timeout = 9.0",
                'file_file_allocation = "trunc"',
                "file_check_integrity = true",
                "file_remote_time = true",
                "file_conditional_get = true",
                "file_http_accept_gzip = false",
                f'file_input_file = "{tmp_path / "aria2.session"}"',
                f'file_save_session = "{tmp_path / "aria2.next"}"',
                "file_save_session_interval = 30",
                'file_metalink_preferred_protocol = "https"',
                'file_metalink_language = "en-US"',
                'file_metalink_os = "macos"',
                'file_metalink_location = "us"',
                'file_metalink_base_uri = "https://mirrors.example/releases/"',
                "file_metalink_enable_unique_protocol = false",
                f'file_server_stat_if = "{tmp_path / "servers.in"}"',
                f'file_server_stat_of = "{tmp_path / "servers.out"}"',
                "file_server_stat_timeout = 3600",
                'file_uri_selector = "adaptive"',
                "site_depth = 4",
                "dir_depth = 5",
                'dir_backend = "wget"',
                "dir_wait = 2.0",
                'dir_user_agent = "AtlasTest/1.0"',
                "dir_if_modified_since = true",
                "dir_timestamping = false",
                "site_robots = false",
                "site_domains = \"example.com\"",
                "site_max_files = 100",
                "site_max_total_size = \"10M\"",
                "site_max_runtime = 300.0",
            ]
        ),
        encoding="utf-8",
    )

    settings = load_config(cfg)

    assert settings.output_dir == out
    assert settings.archive_file == archive
    assert settings.video_container == Container.mp4
    assert settings.audio_codec == AudioCodec.opus
    assert settings.audio_quality == 3
    assert settings.aria2 is False
    assert settings.media_retry_sleep == ["http:1"]
    assert settings.media_extractor_args == ["youtube:player_client=android"]
    assert settings.media_match_filters == ["duration>?60"]
    assert settings.media_break_match_filters == ["view_count<10"]
    assert settings.media_max_downloads == 2
    assert settings.media_break_on_existing is True
    assert settings.media_download_sections == ["intro"]
    assert settings.media_sponsorblock_mark == ["sponsor"]
    assert settings.file_backend == FileBackendChoice.wget2
    assert settings.file_trust_server_names is True
    assert settings.file_lowest_speed_limit == "32K"
    assert settings.file_max_tries == 5
    assert settings.file_retry_wait == 2.5
    assert settings.file_connect_timeout == 9.0
    assert settings.file_file_allocation == "trunc"
    assert settings.file_check_integrity is True
    assert settings.file_remote_time is True
    assert settings.file_conditional_get is True
    assert settings.file_http_accept_gzip is False
    assert settings.file_input_file == tmp_path / "aria2.session"
    assert settings.file_save_session == tmp_path / "aria2.next"
    assert settings.file_save_session_interval == 30
    assert settings.file_metalink_preferred_protocol == MetalinkPreferredProtocol.https
    assert settings.file_metalink_language == "en-US"
    assert settings.file_metalink_os == "macos"
    assert settings.file_metalink_location == "us"
    assert settings.file_metalink_base_uri == "https://mirrors.example/releases/"
    assert settings.file_metalink_enable_unique_protocol is False
    assert settings.file_server_stat_if == tmp_path / "servers.in"
    assert settings.file_server_stat_of == tmp_path / "servers.out"
    assert settings.file_server_stat_timeout == 3600
    assert settings.file_uri_selector == Aria2UriSelector.adaptive
    assert settings.site_depth == 4
    assert settings.dir_depth == 5
    assert settings.dir_backend == SiteBackendChoice.wget
    assert settings.dir_wait == 2.0
    assert settings.dir_user_agent == "AtlasTest/1.0"
    assert settings.dir_if_modified_since is True
    assert settings.dir_timestamping is False
    assert settings.site_robots is False
    assert settings.site_domains == "example.com"
    assert settings.site_max_files == 100
    assert settings.site_max_total_size == "10M"
    assert settings.site_max_runtime == 300.0


def test_load_config_uses_pydantic_toml_source_without_warning(tmp_path: Path) -> None:
    cfg = tmp_path / "config.toml"
    out = tmp_path / "out"
    cfg.write_text(f'default_output_dir = "{out}"\nuse_aria2 = false\n', encoding="utf-8")

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        settings = load_config(cfg)

    assert settings.output_dir == out
    assert settings.aria2 is False
    assert not [
        warning
        for warning in caught
        if "toml_file" in str(warning.message) or "Config key" in str(warning.message)
    ]


def test_load_config_reports_invalid_toml_as_config_error(tmp_path: Path) -> None:
    cfg = tmp_path / "config.toml"
    cfg.write_text("default_output_dir = [", encoding="utf-8")

    with pytest.raises(ConfigError, match="Invalid TOML"):
        load_config(cfg)


def test_load_config_accepts_display_aliases(tmp_path: Path) -> None:
    cfg = tmp_path / "config.toml"
    out = tmp_path / "out"
    cfg.write_text(
        "\n".join(
            [
                f'default_output_dir = "{out}"',
                'default_video_container = "webm"',
                'default_audio_codec = "flac"',
                "use_aria2 = false",
            ]
        ),
        encoding="utf-8",
    )

    settings = load_config(cfg)

    assert settings.output_dir == out
    assert settings.video_container == Container.webm
    assert settings.audio_codec == AudioCodec.flac
    assert settings.aria2 is False


def test_load_config_expands_tilde_paths(tmp_path: Path) -> None:
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        'output_dir = "~/Movies/custom-atlas"\narchive_file = "~/Library/atlas/archive.txt"\n',
        encoding="utf-8",
    )

    settings = load_config(cfg)

    assert settings.output_dir == Path("~/Movies/custom-atlas").expanduser()
    assert settings.archive_file == Path("~/Library/atlas/archive.txt").expanduser()


def test_settings_as_plain_dict_has_strings() -> None:
    settings = load_config(Path("/definitely/not/present.toml"))
    plain = settings_as_plain_dict(settings)

    assert isinstance(plain["output_dir"], str)
    assert isinstance(plain["archive_file"], str)


def test_settings_as_toml_renders_list_values(tmp_path: Path) -> None:
    settings = AtlasSettings(
        output_dir=tmp_path,
        archive_file=tmp_path / "archive.txt",
        media_retry_sleep=["http:1"],
        media_extractor_args=["youtube:player_client=android"],
        media_match_filters=["duration>?60"],
        media_download_sections=["intro"],
        file_save_session=tmp_path / "aria2.next",
        file_uri_selector=Aria2UriSelector.adaptive,
    )

    rendered = settings_as_toml(settings)

    assert 'media_retry_sleep = ["http:1"]' in rendered
    assert 'media_extractor_args = ["youtube:player_client=android"]' in rendered
    assert 'media_match_filters = ["duration>?60"]' in rendered
    assert 'media_download_sections = ["intro"]' in rendered
    assert 'file_save_session = "' in rendered
    assert 'file_uri_selector = "adaptive"' in rendered


def test_settings_as_toml_redacts_secrets_and_round_trips_escaped_values(tmp_path: Path) -> None:
    settings = AtlasSettings(
        output_dir=tmp_path / "out",
        archive_file=tmp_path / "archive.txt",
        media_sponsorblock_api='https://api.example/?token=sentinel-secret&label="quoted"',
        media_extractor_args=["youtube:po_token=sentinel-secret", 'value="quoted"\nnext'],
    )

    displayed = settings_as_toml(settings)
    persisted = settings_as_toml(settings, redact_sensitive=False)
    config_file = tmp_path / "config.toml"
    config_file.write_text(persisted, encoding="utf-8")

    assert "sentinel-secret" not in displayed
    assert tomllib.loads(persisted)["media_extractor_args"] == settings.media_extractor_args
    restored = load_config(config_file)
    assert restored.media_sponsorblock_api == settings.media_sponsorblock_api
    assert restored.media_extractor_args == settings.media_extractor_args
