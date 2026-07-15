# Configuration

`atlas` uses `pydantic-settings`, TOML, environment variables, and macOS-aware
paths from `platformdirs`.

The TOML file is loaded by `pydantic-settings` through
`TomlConfigSettingsSource`, not by a separate ad hoc parser. Source priority is:

1. Explicit initialization values
2. Environment variables
3. TOML config file
4. Dotenv
5. File secrets
6. Defaults

## macOS paths

| Purpose | Default |
| --- | --- |
| Config file | `~/Library/Application Support/atlas/config.toml` |
| Data dir | `~/Library/Application Support/atlas` |
| Cache dir | `~/Library/Caches/atlas` |
| Log dir | `~/Library/Logs/atlas` |
| Output dir | `~/Downloads/atlas` |
| Archive file | `~/Library/Application Support/atlas/download-archive.txt` |

See [Atlas Migration Notes](migration.md) before moving config, archive, or
output directories.

Commands:

```bash
atlas config path
atlas config show
atlas setup --no-install
```

UI behavior is intentionally runtime-controlled rather than persisted in the
config file. Theme, plain mode, Unicode fallback, and animation settings are
selected per invocation with global flags such as `--theme`, `--plain`,
`--no-unicode`, and `--no-animation`, plus the documented environment
variables below.

`atlas setup` creates the config directory, output directory, and a default
`config.toml` when one does not already exist. It is safe to rerun; existing
config files are not overwritten.

`atlas config show` renders valid TOML and redacts credential-like values,
including token-bearing URLs and extractor tokens. The setup writer preserves
explicit configuration values when it creates a new config file and creates it
with owner-only permissions.

## Config file

Example:

```toml
default_output_dir = "~/Downloads/atlas"
archive = true
archive_file = "~/Library/Application Support/atlas/download-archive.txt"
default_video_container = "auto"
default_audio_codec = "best"
audio_quality = 0
use_aria2 = true
aria2_connections = 16
aria2_splits = 16
aria2_chunk_size = "1M"
media_concurrent_fragments = 4
media_file_access_retries = 3
media_retry_sleep = []
media_skip_unavailable_fragments = true
media_match_filters = []
media_break_match_filters = []
media_download_sections = []
media_sponsorblock_mark = []
media_sponsorblock_remove = []
batch_concurrency = 2
file_backend = "auto"
file_check_integrity = false
file_http_accept_gzip = true
site_backend = "auto"
dir_backend = "auto"
site_depth = 2
dir_depth = 2
site_max_files = 500
site_max_total_size = "5G"
site_max_runtime = 1800
site_wait = 1.0
dir_wait = 1.0
site_stats = true
write_info_json = true
write_thumbnail = true
embed_thumbnail = true
embed_metadata = true
```

## Settings keys

| Display Key | Model Field | Default |
| --- | --- | --- |
| `default_output_dir` | `output_dir` | `~/Downloads/atlas` |
| `archive` | `archive` | `true` |
| `archive_file` | `archive_file` | app data archive path |
| `default_video_container` | `video_container` | `auto` |
| `default_audio_codec` | `audio_codec` | `best` |
| `audio_quality` | `audio_quality` | `0` |
| `use_aria2` | `aria2` | `true` |
| `aria2_connections` | `aria2_connections` | `16` |
| `aria2_splits` | `aria2_splits` | `16` |
| `aria2_chunk_size` | `aria2_chunk_size` | `1M` |
| `media_concurrent_fragments` | `media_concurrent_fragments` | `4` |
| `media_file_access_retries` | `media_file_access_retries` | `3` |
| `media_retry_sleep` | `media_retry_sleep` | `[]` |
| `media_skip_unavailable_fragments` | `media_skip_unavailable_fragments` | `true` |
| `media_throttled_rate` | `media_throttled_rate` | unset |
| `media_http_chunk_size` | `media_http_chunk_size` | unset |
| `media_socket_timeout` | `media_socket_timeout` | unset |
| `media_source_address` | `media_source_address` | unset |
| `media_impersonate` | `media_impersonate` | unset |
| `media_extractor_args` | `media_extractor_args` | `[]` |
| `media_match_filters` | `media_match_filters` | `[]` |
| `media_break_match_filters` | `media_break_match_filters` | `[]` |
| `media_max_downloads` | `media_max_downloads` | unset |
| `media_break_on_existing` | `media_break_on_existing` | `false` |
| `media_break_on_reject` | `media_break_on_reject` | `false` |
| `media_break_per_input` | `media_break_per_input` | `false` |
| `media_date` | `media_date` | unset |
| `media_date_before` | `media_date_before` | unset |
| `media_date_after` | `media_date_after` | unset |
| `media_min_filesize` | `media_min_filesize` | unset |
| `media_max_filesize` | `media_max_filesize` | unset |
| `media_reject_live` | `media_reject_live` | `false` |
| `media_reject_upcoming` | `media_reject_upcoming` | `false` |
| `media_live_from_start` | `media_live_from_start` | `false` |
| `media_download_sections` | `media_download_sections` | `[]` |
| `media_sponsorblock_mark` | `media_sponsorblock_mark` | `[]` |
| `media_sponsorblock_remove` | `media_sponsorblock_remove` | `[]` |
| `media_sponsorblock_chapter_title` | `media_sponsorblock_chapter_title` | unset |
| `media_sponsorblock_api` | `media_sponsorblock_api` | unset |
| `batch_concurrency` | `batch_concurrency` | `2` |
| `file_backend` | `file_backend` | `auto` (`auto`, `native`, `aria2`, `wget2`) |
| `file_trust_server_names` | `file_trust_server_names` | `false` |
| `file_content_disposition` | `file_content_disposition` | `true` |
| `file_timestamping` | `file_timestamping` | `false` |
| `file_use_server_timestamps` | `file_use_server_timestamps` | `true` |
| `file_timeout` | `file_timeout` | `30.0` |
| `file_lowest_speed_limit` | `file_lowest_speed_limit` | unset |
| `file_max_tries` | `file_max_tries` | unset |
| `file_retry_wait` | `file_retry_wait` | unset |
| `file_connect_timeout` | `file_connect_timeout` | unset |
| `file_file_allocation` | `file_file_allocation` | unset |
| `file_check_integrity` | `file_check_integrity` | `false` |
| `file_remote_time` | `file_remote_time` | `false` |
| `file_conditional_get` | `file_conditional_get` | `false` |
| `file_http_accept_gzip` | `file_http_accept_gzip` | `true` |
| `file_input_file` | `file_input_file` | unset |
| `file_save_session` | `file_save_session` | unset |
| `file_save_session_interval` | `file_save_session_interval` | unset |
| `file_metalink_preferred_protocol` | `file_metalink_preferred_protocol` | unset |
| `file_metalink_language` | `file_metalink_language` | unset |
| `file_metalink_os` | `file_metalink_os` | unset |
| `file_metalink_location` | `file_metalink_location` | unset |
| `file_metalink_base_uri` | `file_metalink_base_uri` | unset |
| `file_metalink_enable_unique_protocol` | `file_metalink_enable_unique_protocol` | unset |
| `file_server_stat_if` | `file_server_stat_if` | unset |
| `file_server_stat_of` | `file_server_stat_of` | unset |
| `file_server_stat_timeout` | `file_server_stat_timeout` | unset |
| `file_uri_selector` | `file_uri_selector` | unset |
| `site_backend` | `site_backend` | `auto` (`auto`, `wget2`, `wget`) |
| `dir_backend` | `dir_backend` | `auto` (`auto`, `wget2`, `wget`) |
| `site_depth` | `site_depth` | `2` |
| `dir_depth` | `dir_depth` | `2` |
| `site_page_requisites` | `site_page_requisites` | `true` |
| `site_convert_links` | `site_convert_links` | `true` |
| `site_span_hosts` | `site_span_hosts` | `false` |
| `site_accept` | `site_accept` | unset |
| `site_reject` | `site_reject` | unset |
| `site_robots` | `site_robots` | `true` |
| `site_follow_sitemaps` | `site_follow_sitemaps` | `true` |
| `site_no_parent` | `site_no_parent` | `true` |
| `site_domains` | `site_domains` | unset |
| `site_exclude_domains` | `site_exclude_domains` | unset |
| `site_include_directories` | `site_include_directories` | unset |
| `site_exclude_directories` | `site_exclude_directories` | unset |
| `site_accept_regex` | `site_accept_regex` | unset |
| `site_reject_regex` | `site_reject_regex` | unset |
| `site_filter_mime_type` | `site_filter_mime_type` | unset |
| `site_ignore_case` | `site_ignore_case` | `false` |
| `site_max_files` | `site_max_files` | unset |
| `site_max_total_size` | `site_max_total_size` | unset |
| `site_max_runtime` | `site_max_runtime` | unset |
| `site_max_threads` | `site_max_threads` | `5` |
| `site_tries` | `site_tries` | `20` |
| `site_waitretry` | `site_waitretry` | `10.0` |
| `site_retry_on_http_error` | `site_retry_on_http_error` | unset |
| `site_max_redirect` | `site_max_redirect` | `20` |
| `site_timeout` | `site_timeout` | unset |
| `site_dns_timeout` | `site_dns_timeout` | unset |
| `site_connect_timeout` | `site_connect_timeout` | unset |
| `site_read_timeout` | `site_read_timeout` | unset |
| `site_wait` | `site_wait` | `1.0` |
| `dir_wait` | `dir_wait` | `1.0` |
| `dir_user_agent` | `dir_user_agent` | `Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36` |
| `dir_if_modified_since` | `dir_if_modified_since` | `false` |
| `dir_timestamping` | `dir_timestamping` | `true` |
| `site_random_wait` | `site_random_wait` | `false` |
| `site_timestamping` | `site_timestamping` | `false` |
| `site_stats` | `site_stats` | `true` |
| `write_info_json` | `write_info_json` | `true` |
| `write_thumbnail` | `write_thumbnail` | `true` |
| `embed_thumbnail` | `embed_thumbnail` | `true` |
| `embed_metadata` | `embed_metadata` | `true` |

Aliases are supported so older internal names and user-facing names both load.

Adaptive controls are per command rather than persistent config defaults:

```text
--adaptive
--max-concurrency N
--per-host-concurrency N
--politeness normal|fast|aggressive
--explain
```

This keeps large batch and mirror tuning explicit at the point of use. The
config still supplies conservative defaults such as `batch_concurrency`,
`site_wait`, `dir_wait`, and backend preferences. Adaptive explain output records
the chosen queue/per-host/segment limits, total connection budget, and manifest
buckets; live `--progress json` events expose the current scheduler fields for
automation, including queue concurrency, dynamic per-host caps, per-file
segments, total connection budget, per-host connection budget, active
postprocessor budget, lane/bucket, backend, priority, and scheduler decision.
The scheduler uses evidence from normalized progress events rather than raw
backend logs: speed samples, active connections, retry/error signals, and
pressure reasons such as disk, CPU, timeout, 429, 503, or postprocess backlog.

## Environment variables

Environment variables use the `ATLAS_` prefix.

Examples:

```bash
export ATLAS_OUTPUT_DIR="$HOME/Downloads/Media"
export ATLAS_ARCHIVE=false
export ATLAS_ARIA2=false
export ATLAS_FILE_BACKEND=wget2
export ATLAS_SITE_BACKEND=wget2
export ATLAS_DIR_BACKEND=wget2
export ATLAS_SITE_MAX_FILES=500
export ATLAS_SITE_MAX_TOTAL_SIZE=5G
export ATLAS_SITE_MAX_RUNTIME=1800
```

Terminal accessibility also honors standard environment conventions:

| Environment | Effect |
| --- | --- |
| `NO_COLOR=1` | Disables ANSI color while keeping Unicode boxes/bars when the terminal supports them. |
| `TERM=dumb` | Disables ANSI color and Unicode glyphs. |
| `ATLAS_NO_ANIMATION=1` | Disables shimmer and moving pulse bars while preserving other visual settings. |
| `ATLAS_NO_MENU=1` | Prevents the no-argument interactive launcher in automation. |
| `ATLAS_MENU=0` | Also prevents the no-argument interactive launcher. |

Use global command flags for explicit UI control:

```bash
atlas --theme high-contrast COMMAND ...
atlas --plain COMMAND ...
atlas --no-unicode COMMAND ...
atlas --no-animation COMMAND ...
```

Config file values and environment variables are validated by Pydantic.

## Output organization

Default template:

```text
%(uploader|unknown)s/%(upload_date>%Y-%m-%d|unknown)s - %(title).200B [%(id)s].%(ext)s
```

Organization modes:

| Mode | Template Shape |
| --- | --- |
| `flat` | date, title, id in output dir |
| `channel` | channel directory, title and id |
| `channel-date` | channel directory, date, title, id |
| `playlist` | playlist title directory, playlist index, title, id |

Use `--filename-template` only when the built-in organization modes are not
enough.

## Download archive

Archive is enabled by default. It prevents accidental redownloads through
yt-dlp's `download_archive` option.

Disable it for one command:

```bash
atlas video URL --no-archive
```

Use a custom archive:

```bash
atlas video URL --archive ~/Downloads/my-archive.txt
```

## Cookies

Supported mechanisms:

```text
--cookies-from-browser safari|chrome|firefox|brave|edge
--cookies-file PATH
```

For media commands, cookies are passed through normal yt-dlp mechanisms. For
`atlas site`, `--cookies-from-browser` exports a temporary Netscape cookie jar
and passes it to Wget2 with `--load-cookies`. Cookies are for user-authorized
access only, not bypassing access controls. Atlas does not support stolen
sessions, fake browser fingerprinting, browser automation to defeat bot
challenges, or DRM circumvention.
