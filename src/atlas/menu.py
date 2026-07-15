"""Interactive menu launcher for atlas."""

from __future__ import annotations

import importlib
import os
import re
import shlex
import shutil
import subprocess
import sys
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from enum import StrEnum
from functools import partial
from pathlib import Path
from typing import Protocol, cast
from urllib.parse import urljoin, urlparse
from uuid import uuid4

from pydantic import ValidationError
from rich.console import Console, Group, RenderableType
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from atlas.config import AtlasSettings
from atlas.directory_explorer import (
    DirectoryExplorerAction as DirectoryExplorerChoice,
)
from atlas.directory_explorer import (
    directory_explorer_actions,
)
from atlas.directory_index import DirectoryEntry, DirectoryIndex, directory_index_from_work_item
from atlas.errors import AtlasError
from atlas.formats import format_bytes, format_duration
from atlas.media_capabilities import (
    MediaCapabilityCatalog,
    MediaCapabilityResolver,
    MediaChoice,
    MediaProfile,
    format_choice_label,
    format_format_row,
)
from atlas.models import (
    AdaptivePoliteness,
    Aria2UriSelector,
    AudioCodec,
    AudioDownloadOptions,
    BatchKind,
    CertificateType,
    Container,
    DirectoryMirrorOptions,
    DownloadAttrMode,
    DownloadEngineChoice,
    FileBackendChoice,
    FileDownloadOptions,
    FormatInfo,
    FpsChoice,
    HdrChoice,
    HttpsEnforceMode,
    HubKind,
    MediaInfo,
    MetalinkPreferredProtocol,
    OrganizeMode,
    PreferFamily,
    ProgressMode,
    QualityIntent,
    ResolutionChoice,
    ScanStatus,
    SiteBackendChoice,
    SiteDownloadOptions,
    SubtitleMode,
    VerifySigMode,
    VideoCodecChoice,
    VideoDownloadOptions,
    WorkItem,
)
from atlas.optimizer import HubExecutionPlan
from atlas.passthrough import BackendTool
from atlas.paths import config_path
from atlas.private_files import ensure_private_directory, write_private_text
from atlas.redaction import redact_text
from atlas.setup import RuntimeTool, SetupMode, selected_tools
from atlas.theme import (
    ATLAS_ACTIVE_STYLE,
    ATLAS_ERROR_STYLE,
    ATLAS_MUTED_STYLE,
    ATLAS_PANEL_STYLE,
    ATLAS_PATH_STYLE,
    ATLAS_SUCCESS_STYLE,
    ATLAS_TITLE_STYLE,
    ATLAS_WARNING_STYLE,
    atlas_box,
    ensure_atlas_theme,
    questionary_style_map,
    status_glyph,
    themed_console,
    visual_join,
    visual_options,
    visual_separator,
)
from atlas.urls import is_explicit_playlist_url, is_watch_url_with_playlist_params
from atlas.views import SmartSessionView, ViewField

type MenuDownloadOptions = (
    VideoDownloadOptions
    | AudioDownloadOptions
    | FileDownloadOptions
    | SiteDownloadOptions
    | DirectoryMirrorOptions
)

_APP_TAGLINE = "Smart downloads for media, files, mirrors, and batches"
_TEMP_STREAM_RE = re.compile(r"\.f\d+(?=\.)")


class MenuUnavailable(AtlasError):
    """Raised when the interactive menu cannot be started."""


class MainMenuChoice(StrEnum):
    media = "media-menu"
    files = "files-menu"
    sessions = "sessions-menu"
    tools = "tools-menu"
    settings = "settings-menu"
    smart = "smart"
    video = "video"
    audio = "audio"
    file = "file"
    playlist = "playlist"
    site = "site"
    dir = "dir"
    info = "info"
    formats = "formats"
    batch = "batch"
    resume = "resume"
    retry = "retry"
    inspect = "inspect"
    export_failed = "export-failed"
    advanced = "advanced"
    doctor = "doctor"
    setup = "setup"
    update = "update"
    config = "config"
    shortcuts = "shortcuts"
    quit = "quit"


class PlanMenuChoice(StrEnum):
    start = "start"
    customize = "customize"
    formats = "formats"
    dry_run = "dry-run"
    back = "back"
    quit = "quit"


class PlanRecoveryChoice(StrEnum):
    retry = "retry"
    customize = "customize"
    doctor = "doctor"
    back = "back"
    quit = "quit"


class CompletionChoice(StrEnum):
    reveal = "reveal"
    open = "open"
    another = "another"
    back = "back"
    quit = "quit"


class FlowResult(StrEnum):
    back = "back"
    quit = "quit"
    retry = "retry"


class BatchSourceChoice(StrEnum):
    url_scan = "url-scan"
    url_file = "url-file"
    pasted_urls = "pasted-urls"
    playlist = "playlist"
    resume = "resume"
    retry = "retry"
    inspect = "inspect"
    export_failed = "export-failed"
    back = "back"
    quit = "quit"


class BatchUrlScanChoice(StrEnum):
    recursive = "recursive"
    direct_links = "direct-links"
    selected_files = "selected-files"
    folder = "folder"
    offline_site = "offline-site"
    file = "file"
    back = "back"


class ScanFailedChoice(StrEnum):
    retry = "retry"
    doctor = "doctor"
    backend_scan = "backend-scan"
    backend_mirror = "backend-mirror"
    details = "details"
    back = "back"


class ScanEmptyChoice(StrEnum):
    retry = "retry"
    website = "website"
    file = "file"
    back = "back"


class SetupGateChoice(StrEnum):
    install = "install"
    plan = "plan"
    limited = "limited"
    doctor = "doctor"
    quit = "quit"


@dataclass(frozen=True)
class MenuChoice:
    label: str
    value: object


@dataclass(frozen=True)
class RuntimeToolStatus:
    tool: RuntimeTool
    installed: bool


@dataclass(frozen=True)
class MenuCapability:
    """A normal operator capability exposed through the menu-first UX."""

    id: str
    label: str
    choice: MainMenuChoice
    command_names: tuple[str, ...]
    typed_options_model: type[object] | None = None
    advanced: bool = False


MENU_CAPABILITIES: tuple[MenuCapability, ...] = (
    MenuCapability(
        "download_video",
        "Download video",
        MainMenuChoice.video,
        ("video",),
        VideoDownloadOptions,
    ),
    MenuCapability(
        "extract_audio",
        "Extract audio",
        MainMenuChoice.audio,
        ("audio",),
        AudioDownloadOptions,
    ),
    MenuCapability(
        "download_playlist",
        "Download playlist",
        MainMenuChoice.playlist,
        ("playlist",),
    ),
    MenuCapability(
        "download_file",
        "Download file",
        MainMenuChoice.file,
        ("file",),
        FileDownloadOptions,
    ),
    MenuCapability(
        "mirror_website",
        "Mirror website",
        MainMenuChoice.site,
        ("site",),
        SiteDownloadOptions,
    ),
    MenuCapability(
        "mirror_directory",
        "Browse directory",
        MainMenuChoice.dir,
        ("dir",),
        DirectoryMirrorOptions,
    ),
    MenuCapability("batch_download", "Batch download", MainMenuChoice.batch, ("batch",)),
    MenuCapability("resume_session", "Resume session", MainMenuChoice.resume, ("resume",)),
    MenuCapability("retry_failed", "Retry failed", MainMenuChoice.retry, ("retry",)),
    MenuCapability(
        "inspect_session",
        "Inspect session",
        MainMenuChoice.inspect,
        ("inspect-session",),
    ),
    MenuCapability(
        "export_failed",
        "Export URLs",
        MainMenuChoice.export_failed,
        ("export-failed",),
    ),
    MenuCapability("show_info", "Show info", MainMenuChoice.info, ("info",)),
    MenuCapability("show_formats", "Show formats", MainMenuChoice.formats, ("formats",)),
    MenuCapability(
        "advanced_backend",
        "Advanced backend",
        MainMenuChoice.advanced,
        ("ytdlp", "aria2", "wget2", "wget"),
        advanced=True,
    ),
    MenuCapability("doctor", "Doctor", MainMenuChoice.doctor, ("doctor",)),
    MenuCapability("setup", "Setup tools", MainMenuChoice.setup, ("setup",)),
    MenuCapability("update", "Update Atlas", MainMenuChoice.update, ("update",)),
    MenuCapability("config", "Config", MainMenuChoice.config, ("config",)),
    MenuCapability("help", "Help", MainMenuChoice.shortcuts, ()),
)


SCRIPT_ONLY_COMMANDS = frozenset({"get", "menu"})
_MENU_REDRAW_ATTR = "_atlas_menu_redraw"
_DIRECTORY_FILE_PREVIEW_LIMIT = 8


def menu_capability_command_names(*, include_advanced: bool = True) -> set[str]:
    """Return command/group names covered by first-class menu capabilities."""

    return {
        command_name
        for capability in MENU_CAPABILITIES
        if include_advanced or not capability.advanced
        for command_name in capability.command_names
    }


@contextmanager
def _menu_session(console: Console) -> Iterator[None]:
    """Use an alternate screen and redraw semantics for Atlas-owned menu screens."""

    console = ensure_atlas_theme(console)
    previous = bool(getattr(console, _MENU_REDRAW_ATTR, False))
    redraw = bool(console.is_terminal and not visual_options().plain)
    setattr(console, _MENU_REDRAW_ATTR, redraw or previous)
    screen_context = console.screen(hide_cursor=False) if redraw else nullcontext()
    try:
        with screen_context:
            if redraw:
                console.clear(home=True)
            yield
    finally:
        setattr(console, _MENU_REDRAW_ATTR, previous)


def _refresh_menu_screen(console: Console) -> None:
    console = ensure_atlas_theme(console)
    if getattr(console, _MENU_REDRAW_ATTR, False):
        console.clear(home=True)


class PromptUI(Protocol):
    def select(self, message: str, choices: Sequence[MenuChoice]) -> object | None:
        """Return the selected choice value, or None when cancelled."""

    def multi_select(self, message: str, choices: Sequence[MenuChoice]) -> list[object] | None:
        """Return selected choice values, or None when cancelled."""

    def text(self, message: str, *, default: str = "") -> str | None:
        """Return user-entered text, or None when cancelled."""

    def confirm(self, message: str, *, default: bool = False) -> bool | None:
        """Return confirmation answer, or None when cancelled."""


class _QuestionaryPrompt(Protocol):
    def ask(self) -> object:
        """Ask a questionary prompt and return the answer."""


def _unsupported_questionary_kwarg(error: TypeError) -> str | None:
    marker = "unexpected keyword argument "
    message = str(error)
    if marker not in message:
        return None
    _prefix, _marker, suffix = message.partition(marker)
    return suffix.strip().strip("'\"") or None


def _ask_questionary(
    factory: Callable[..., _QuestionaryPrompt],
    message: str,
    choices: list[str],
    **kwargs: object,
) -> object:
    prompt_kwargs = dict(kwargs)
    while True:
        try:
            return factory(message, choices=choices, **prompt_kwargs).ask()
        except TypeError as exc:
            unsupported = _unsupported_questionary_kwarg(exc)
            if unsupported is None or unsupported not in prompt_kwargs:
                raise
            prompt_kwargs.pop(unsupported)


def _ask_questionary_prompt(
    factory: Callable[..., _QuestionaryPrompt],
    message: str,
    **kwargs: object,
) -> object:
    prompt_kwargs = dict(kwargs)
    while True:
        try:
            return factory(message, **prompt_kwargs).ask()
        except TypeError as exc:
            unsupported = _unsupported_questionary_kwarg(exc)
            if unsupported is None or unsupported not in prompt_kwargs:
                raise
            prompt_kwargs.pop(unsupported)


class MenuActions(Protocol):
    def build_plan(self, options: MenuDownloadOptions, kind: HubKind) -> HubExecutionPlan:
        """Build the optimized hub plan for typed menu options."""

    def print_plan(self, plan: HubExecutionPlan) -> None:
        """Render a human plan preview."""

    def execute_plan(self, plan: HubExecutionPlan) -> list[Path]:
        """Execute an optimized hub plan and return known saved paths."""

    def run_info(self, url: str) -> None:
        """Show media info for a URL."""

    def run_formats(self, url: str) -> None:
        """Show media formats for a URL."""

    def probe_media(self, url: str, *, playlist: bool = False) -> MediaInfo:
        """Probe media metadata and formats for profile-aware planning."""

    def run_batch(
        self,
        file: Path,
        *,
        kind: BatchKind,
        concurrency: int | None,
        allow_sites: bool,
        allow_dirs: bool,
        video_codec: VideoCodecChoice,
        audio_codec: AudioCodec,
        audio_quality: int,
        dry_run: bool,
    ) -> None:
        """Run a batch download."""

    def resume_session(self, session: Path | None, *, dry_run: bool) -> None:
        """Resume a saved Atlas session."""

    def retry_failed_session(self, session: Path | None, *, dry_run: bool) -> None:
        """Retry failed URLs from a saved Atlas session."""

    def inspect_session(self, session: Path | None) -> None:
        """Inspect a saved Atlas session."""

    def export_failed_session(self, session: Path | None, *, output: Path | None) -> None:
        """Export failed URLs from a saved Atlas session."""

    def scan_url(self, url: str) -> WorkItem:
        """Run a bounded URL scan for interactive URL-generated batches."""

    def run_backend_tool(self, tool: BackendTool, args: list[str], *, dry_run: bool) -> None:
        """Run an advanced backend pass-through command."""

    def run_doctor(self) -> None:
        """Run doctor."""

    def run_setup(self) -> None:
        """Run guided setup."""

    def show_setup_plan(self) -> None:
        """Show the setup plan without initializing paths or installing tools."""

    def run_setup_install(self) -> None:
        """Run guided setup with the install flow enabled."""

    def run_update(self) -> None:
        """Run Atlas update planning."""

    def show_config(self) -> None:
        """Show resolved config."""

    def show_config_path(self) -> None:
        """Show config path."""

    def open_config_file(self) -> None:
        """Open the config file or containing folder for editing."""


class QuestionaryPromptUI:
    """Small adapter over questionary so tests can inject a fake prompt UI."""

    def __init__(self) -> None:
        try:
            self._questionary = importlib.import_module("questionary")
        except ModuleNotFoundError as exc:
            msg = "Interactive menu requires questionary. Reinstall atlas with interactive support."
            raise MenuUnavailable(msg) from exc
        self._style = _questionary_style(self._questionary)

    def select(self, message: str, choices: Sequence[MenuChoice]) -> object | None:
        labels = [choice.label for choice in choices]
        answer = _ask_questionary(
            self._questionary.select,
            message,
            labels,
            qmark="",
            pointer=status_glyph("selected"),
            style=self._style,
            use_arrow_keys=True,
            use_jk_keys=False,
            use_search_filter=True,
            match_middle=True,
            ignore_case=True,
            show_selected=False,
            instruction=" ",
        )
        if answer is None:
            return None
        for choice in choices:
            if choice.label == answer:
                return choice.value
        return None

    def multi_select(self, message: str, choices: Sequence[MenuChoice]) -> list[object] | None:
        labels = [choice.label for choice in choices]
        answer = _ask_questionary(
            self._questionary.checkbox,
            message,
            labels,
            qmark="",
            pointer=status_glyph("selected"),
            style=self._style,
            use_search_filter=True,
            use_jk_keys=False,
            match_middle=True,
            ignore_case=True,
            instruction=" ",
        )
        if answer is None:
            return None
        selected_labels = {str(label) for label in cast(Sequence[object], answer)}
        return [choice.value for choice in choices if choice.label in selected_labels]

    def text(self, message: str, *, default: str = "") -> str | None:
        answer = _ask_questionary_prompt(
            self._questionary.text,
            message,
            default=default,
            qmark="",
            style=self._style,
            instruction=" ",
        )
        if answer is None:
            return None
        return str(answer).strip()

    def confirm(self, message: str, *, default: bool = False) -> bool | None:
        answer = _ask_questionary_prompt(
            self._questionary.confirm,
            message,
            default=default,
            qmark="",
            style=self._style,
            instruction=" ",
        )
        if answer is None:
            return None
        return bool(answer)


def _questionary_style(questionary_module: object) -> object | None:
    """Return an accessible questionary style for Atlas prompts."""

    style_factory = getattr(questionary_module, "Style", None)
    if style_factory is None:
        return None

    return cast(object, style_factory.from_dict(questionary_style_map()))


def can_auto_launch_menu(
    *,
    stdin: object | None = None,
    stdout: object | None = None,
    env: Mapping[str, str] | None = None,
) -> bool:
    """Return True when no-arg atlas should open the interactive menu."""

    return has_interactive_tty(stdin=stdin, stdout=stdout) and not is_automation_environment(env)


def has_interactive_tty(
    *,
    stdin: object | None = None,
    stdout: object | None = None,
) -> bool:
    """Return True when both input and output look like a real terminal."""

    input_stream = stdin or sys.stdin
    output_stream = stdout or sys.stdout
    input_tty = bool(getattr(input_stream, "isatty", lambda: False)())
    output_tty = bool(getattr(output_stream, "isatty", lambda: False)())
    return input_tty and output_tty


def is_automation_environment(env: Mapping[str, str] | None = None) -> bool:
    values = os.environ if env is None else env
    explicit_no_menu = values.get("ATLAS_NO_MENU", "").strip().lower()
    explicit_menu = values.get("ATLAS_MENU", "").strip().lower()
    if explicit_no_menu in {"1", "true", "yes", "on"}:
        return True
    if explicit_menu in {"0", "false", "no", "off"}:
        return True
    automation_keys = (
        "CI",
        "GITHUB_ACTIONS",
        "BUILDKITE",
        "TF_BUILD",
        "CODEBUILD_BUILD_ID",
        "JENKINS_URL",
    )
    return any(values.get(key) for key in automation_keys)


def run_interactive_menu(
    settings: AtlasSettings,
    actions: MenuActions,
    *,
    prompts: PromptUI | None = None,
    console: Console | None = None,
) -> None:
    """Run the keyboard-navigable atlas menu."""

    prompt_ui = prompts or QuestionaryPromptUI()
    output = ensure_atlas_theme(console) if console is not None else themed_console()
    with _menu_session(output):
        if _run_setup_gate_if_needed(settings, actions, prompt_ui, output) == FlowResult.quit:
            return
        while True:
            _print_launcher(output, settings)
            selected = cast(
                MainMenuChoice | None,
                prompt_ui.select("Choose workflow", _main_choices()),
            )
            if selected is None or selected == MainMenuChoice.quit:
                return
            result = _dispatch_main_choice(settings, actions, prompt_ui, output, selected)
            if result == FlowResult.quit:
                return


def build_video_options(
    settings: AtlasSettings,
    url: str,
    *,
    playlist: bool = False,
) -> VideoDownloadOptions:
    return VideoDownloadOptions(
        url=url,
        output_dir=settings.output_dir,
        archive=settings.archive,
        archive_file=settings.archive_file,
        use_aria2=False,
        download_engine=DownloadEngineChoice.native,
        connections=settings.aria2_connections,
        splits=settings.aria2_splits,
        chunk_size=settings.aria2_chunk_size,
        playlist=playlist,
        organize=OrganizeMode.playlist if playlist else OrganizeMode.channel_date,
        file_access_retries=settings.media_file_access_retries,
        concurrent_fragments=settings.media_concurrent_fragments,
        retry_sleep=settings.media_retry_sleep,
        skip_unavailable_fragments=settings.media_skip_unavailable_fragments,
        throttled_rate=settings.media_throttled_rate,
        http_chunk_size=settings.media_http_chunk_size,
        socket_timeout=settings.media_socket_timeout,
        source_address=settings.media_source_address,
        impersonate=settings.media_impersonate,
        extractor_args=settings.media_extractor_args,
        match_filters=settings.media_match_filters,
        break_match_filters=settings.media_break_match_filters,
        max_downloads=settings.media_max_downloads,
        break_on_existing=settings.media_break_on_existing,
        break_on_reject=settings.media_break_on_reject,
        break_per_input=settings.media_break_per_input,
        date=settings.media_date,
        date_before=settings.media_date_before,
        date_after=settings.media_date_after,
        min_filesize=settings.media_min_filesize,
        max_filesize=settings.media_max_filesize,
        reject_live=settings.media_reject_live,
        reject_upcoming=settings.media_reject_upcoming,
        live_from_start=settings.media_live_from_start,
        download_sections=settings.media_download_sections,
        sponsorblock_mark=settings.media_sponsorblock_mark,
        sponsorblock_remove=settings.media_sponsorblock_remove,
        sponsorblock_chapter_title=settings.media_sponsorblock_chapter_title,
        sponsorblock_api=settings.media_sponsorblock_api,
        write_info_json=settings.write_info_json,
        write_thumbnail=settings.write_thumbnail,
        embed_thumbnail=settings.embed_thumbnail,
        embed_metadata=settings.embed_metadata,
        container=settings.video_container,
        progress_mode=ProgressMode.auto,
    )


def build_audio_options(
    settings: AtlasSettings,
    url: str,
    *,
    playlist: bool = False,
) -> AudioDownloadOptions:
    return AudioDownloadOptions(
        url=url,
        output_dir=settings.output_dir,
        archive=settings.archive,
        archive_file=settings.archive_file,
        use_aria2=False,
        download_engine=DownloadEngineChoice.native,
        connections=settings.aria2_connections,
        splits=settings.aria2_splits,
        chunk_size=settings.aria2_chunk_size,
        playlist=playlist,
        organize=OrganizeMode.playlist if playlist else OrganizeMode.channel_date,
        file_access_retries=settings.media_file_access_retries,
        concurrent_fragments=settings.media_concurrent_fragments,
        retry_sleep=settings.media_retry_sleep,
        skip_unavailable_fragments=settings.media_skip_unavailable_fragments,
        throttled_rate=settings.media_throttled_rate,
        http_chunk_size=settings.media_http_chunk_size,
        socket_timeout=settings.media_socket_timeout,
        source_address=settings.media_source_address,
        impersonate=settings.media_impersonate,
        extractor_args=settings.media_extractor_args,
        match_filters=settings.media_match_filters,
        break_match_filters=settings.media_break_match_filters,
        max_downloads=settings.media_max_downloads,
        break_on_existing=settings.media_break_on_existing,
        break_on_reject=settings.media_break_on_reject,
        break_per_input=settings.media_break_per_input,
        date=settings.media_date,
        date_before=settings.media_date_before,
        date_after=settings.media_date_after,
        min_filesize=settings.media_min_filesize,
        max_filesize=settings.media_max_filesize,
        reject_live=settings.media_reject_live,
        reject_upcoming=settings.media_reject_upcoming,
        live_from_start=settings.media_live_from_start,
        download_sections=settings.media_download_sections,
        sponsorblock_mark=settings.media_sponsorblock_mark,
        sponsorblock_remove=settings.media_sponsorblock_remove,
        sponsorblock_chapter_title=settings.media_sponsorblock_chapter_title,
        sponsorblock_api=settings.media_sponsorblock_api,
        write_info_json=settings.write_info_json,
        write_thumbnail=settings.write_thumbnail,
        embed_thumbnail=settings.embed_thumbnail,
        embed_metadata=settings.embed_metadata,
        codec=settings.audio_codec,
        quality=settings.audio_quality,
        progress_mode=ProgressMode.auto,
    )


def build_file_options(settings: AtlasSettings, url: str) -> FileDownloadOptions:
    return FileDownloadOptions(
        url=url,
        output_dir=settings.output_dir,
        backend=settings.file_backend,
        connections=settings.aria2_connections,
        splits=settings.aria2_splits,
        chunk_size=settings.aria2_chunk_size,
        trust_server_names=settings.file_trust_server_names,
        content_disposition=settings.file_content_disposition,
        timestamping=settings.file_timestamping,
        use_server_timestamps=settings.file_use_server_timestamps,
        timeout=settings.file_timeout,
        lowest_speed_limit=settings.file_lowest_speed_limit,
        max_tries=settings.file_max_tries,
        retry_wait=settings.file_retry_wait,
        connect_timeout=settings.file_connect_timeout,
        file_allocation=settings.file_file_allocation,
        check_integrity=settings.file_check_integrity,
        remote_time=settings.file_remote_time,
        conditional_get=settings.file_conditional_get,
        http_accept_gzip=settings.file_http_accept_gzip,
        input_file=settings.file_input_file,
        save_session=settings.file_save_session,
        save_session_interval=settings.file_save_session_interval,
        metalink_preferred_protocol=settings.file_metalink_preferred_protocol,
        metalink_language=settings.file_metalink_language,
        metalink_os=settings.file_metalink_os,
        metalink_location=settings.file_metalink_location,
        metalink_base_uri=settings.file_metalink_base_uri,
        metalink_enable_unique_protocol=settings.file_metalink_enable_unique_protocol,
        server_stat_if=settings.file_server_stat_if,
        server_stat_of=settings.file_server_stat_of,
        server_stat_timeout=settings.file_server_stat_timeout,
        uri_selector=settings.file_uri_selector,
        progress_mode=ProgressMode.auto,
    )


def build_site_options(settings: AtlasSettings, url: str) -> SiteDownloadOptions:
    return SiteDownloadOptions(
        url=url,
        output_dir=settings.output_dir,
        backend=settings.site_backend,
        depth=settings.site_depth,
        page_requisites=settings.site_page_requisites,
        convert_links=settings.site_convert_links,
        span_hosts=settings.site_span_hosts,
        wait=settings.site_wait,
        accept=settings.site_accept,
        reject=settings.site_reject,
        robots=settings.site_robots,
        follow_sitemaps=settings.site_follow_sitemaps,
        no_parent=settings.site_no_parent,
        domains=settings.site_domains,
        exclude_domains=settings.site_exclude_domains,
        include_directories=settings.site_include_directories,
        exclude_directories=settings.site_exclude_directories,
        accept_regex=settings.site_accept_regex,
        reject_regex=settings.site_reject_regex,
        filter_mime_type=settings.site_filter_mime_type,
        ignore_case=settings.site_ignore_case,
        max_files=settings.site_max_files,
        max_total_size=settings.site_max_total_size,
        max_runtime=settings.site_max_runtime,
        max_threads=settings.site_max_threads,
        tries=settings.site_tries,
        waitretry=settings.site_waitretry,
        retry_on_http_error=settings.site_retry_on_http_error,
        max_redirect=settings.site_max_redirect,
        timeout=settings.site_timeout,
        dns_timeout=settings.site_dns_timeout,
        connect_timeout=settings.site_connect_timeout,
        read_timeout=settings.site_read_timeout,
        random_wait=settings.site_random_wait,
        timestamping=settings.site_timestamping,
        stats=settings.site_stats,
        progress_mode=ProgressMode.auto,
    )


def build_directory_options(settings: AtlasSettings, url: str) -> DirectoryMirrorOptions:
    return DirectoryMirrorOptions(
        url=url,
        output_dir=settings.output_dir,
        backend=settings.dir_backend,
        depth=settings.dir_depth,
        wait=settings.dir_wait,
        accept=settings.site_accept,
        reject=settings.site_reject,
        robots=settings.site_robots,
        no_parent=True,
        span_hosts=False,
        max_files=settings.site_max_files,
        max_total_size=settings.site_max_total_size,
        max_runtime=settings.site_max_runtime,
        max_threads=settings.site_max_threads,
        tries=settings.site_tries,
        waitretry=settings.site_waitretry,
        retry_on_http_error=settings.site_retry_on_http_error,
        max_redirect=settings.site_max_redirect,
        timeout=settings.site_timeout,
        dns_timeout=settings.site_dns_timeout,
        connect_timeout=settings.site_connect_timeout,
        read_timeout=settings.site_read_timeout,
        random_wait=settings.site_random_wait,
        timestamping=settings.dir_timestamping,
        user_agent=settings.dir_user_agent,
        if_modified_since=settings.dir_if_modified_since,
        stats=settings.site_stats,
        progress_mode=ProgressMode.auto,
    )


def _run_setup_gate_if_needed(
    settings: AtlasSettings,
    actions: MenuActions,
    prompts: PromptUI,
    console: Console,
) -> FlowResult | None:
    if not console.is_terminal:
        return None
    statuses = _runtime_tool_statuses()
    missing = [status for status in statuses if not status.installed]
    required_missing = [status for status in missing if status.tool.required]
    first_run = not config_path().exists()
    if not required_missing and not (first_run and missing):
        return None

    while True:
        _print_setup_gate(console, statuses)
        selected = cast(
            SetupGateChoice | None,
            prompts.select(
                "Setup",
                [
                    MenuChoice("Install tools", SetupGateChoice.install),
                    MenuChoice("Show install plan", SetupGateChoice.plan),
                    MenuChoice("Limited mode", SetupGateChoice.limited),
                    MenuChoice("Open Doctor", SetupGateChoice.doctor),
                    MenuChoice("Quit", SetupGateChoice.quit),
                ],
            ),
        )
        if selected is None or selected == SetupGateChoice.quit:
            return FlowResult.quit
        if selected == SetupGateChoice.limited:
            return None
        if selected == SetupGateChoice.install:
            actions.run_setup_install()
            return None
        if selected == SetupGateChoice.plan:
            actions.show_setup_plan()
        elif selected == SetupGateChoice.doctor:
            actions.run_doctor()
        after = _post_simple_action(prompts)
        if after == FlowResult.quit:
            return FlowResult.quit


def _runtime_tool_statuses() -> tuple[RuntimeToolStatus, ...]:
    return tuple(
        RuntimeToolStatus(tool=tool, installed=shutil.which(tool.executable) is not None)
        for tool in selected_tools(SetupMode.full)
    )


def _print_setup_gate(console: Console, statuses: Sequence[RuntimeToolStatus]) -> None:
    _refresh_menu_screen(console)
    console.print(Text("Setup", style=ATLAS_ACTIVE_STYLE))
    console.print("Atlas is installed. Some download tools are missing.")
    console.print()
    console.print(Text("Required", style=ATLAS_ACTIVE_STYLE))
    _print_runtime_tool_group(
        console,
        [status for status in statuses if status.tool.required],
        missing_style=ATLAS_WARNING_STYLE,
    )
    optional = [status for status in statuses if not status.tool.required]
    if optional:
        console.print()
        console.print(Text("Recommended", style=ATLAS_ACTIVE_STYLE))
        _print_runtime_tool_group(console, optional, missing_style=ATLAS_MUTED_STYLE)
    console.print()
    console.print(Text("Recommended action", style=ATLAS_ACTIVE_STYLE))
    console.print(
        "  Install full runtime: "
        + visual_join(("ffmpeg", "ffprobe", "aria2c", "wget2", "wget"))
    )
    console.print()


def _print_runtime_tool_group(
    console: Console,
    statuses: Sequence[RuntimeToolStatus],
    *,
    missing_style: str,
) -> None:
    table = Table.grid(padding=(0, 2))
    table.add_column(no_wrap=True)
    table.add_column(no_wrap=True)
    table.add_column(ratio=1)
    for status in statuses:
        if status.installed:
            glyph = status_glyph("success")
            style_name = ATLAS_ACTIVE_STYLE
            detail = status.tool.purpose
        else:
            glyph = status_glyph("warning") if status.tool.required else status_glyph("optional")
            style_name = missing_style
            detail = visual_join(("missing", status.tool.purpose))
        table.add_row(Text(glyph, style=style_name), status.tool.executable, detail)
    console.print(table)


def _print_launcher_header(console: Console, settings: AtlasSettings) -> None:
    console = ensure_atlas_theme(console)
    console.print(_launcher_header_panel(settings))


def _launcher_header_panel(settings: AtlasSettings | None = None) -> Panel:
    _ = settings
    body = Table.grid()
    body.add_column()
    body.add_row(_APP_TAGLINE)
    return Panel(
        body,
        title=Text(" atlas ", style=ATLAS_TITLE_STYLE),
        border_style=ATLAS_PANEL_STYLE,
        box=atlas_box(),
        expand=True,
    )


def _print_launcher(console: Console, settings: AtlasSettings) -> None:
    console = ensure_atlas_theme(console)
    _refresh_menu_screen(console)
    _print_launcher_header(console, settings)
    console.print()
    console.print(_menu_footer())


def _menu_footer(*, multi: bool = False, back: str = "quit") -> Text:
    _ = back
    move = "↑/↓" if visual_options().unicode else "up/down"
    footer = Text()
    footer.append(move, style=ATLAS_MUTED_STYLE)
    footer.append(" move   ")
    if multi:
        footer.append("space", style=ATLAS_MUTED_STYLE)
        footer.append(" select   ")
        footer.append("enter", style=ATLAS_MUTED_STYLE)
        footer.append(" continue   ")
    else:
        footer.append("enter", style=ATLAS_MUTED_STYLE)
        footer.append(" select   ")
    footer.append("type", style=ATLAS_MUTED_STYLE)
    footer.append(" filter   ")
    footer.append("ctrl-c", style=ATLAS_MUTED_STYLE)
    footer.append(" cancel")
    return footer


def _print_workflow_frame(
    console: Console,
    breadcrumb: str,
    *,
    subtitle: str | None = None,
) -> None:
    console = ensure_atlas_theme(console)
    _refresh_menu_screen(console)
    console.print(Text("atlas", style=ATLAS_TITLE_STYLE))
    console.print(Text(breadcrumb, style=ATLAS_ACTIVE_STYLE))
    if subtitle:
        console.print(Text(subtitle, style=ATLAS_MUTED_STYLE))
    console.print()


def _print_workflow_card(
    console: Console,
    title: str,
    rows: Sequence[tuple[str, str | Text]],
    *,
    style_name: str = ATLAS_PANEL_STYLE,
) -> None:
    console = ensure_atlas_theme(console)
    _refresh_menu_screen(console)
    _render_workflow_card(console, title, rows, style_name=style_name)


def _render_workflow_card(
    console: Console,
    title: str,
    rows: Sequence[tuple[str, str | Text]],
    *,
    style_name: str = ATLAS_PANEL_STYLE,
) -> None:
    table = Table.grid(padding=(0, 2))
    table.add_column(style=ATLAS_MUTED_STYLE, no_wrap=True)
    table.add_column(ratio=1)
    for label, value in rows:
        table.add_row(label, value)
    console.print(
        Panel(
            table,
            title=Text(title, style=ATLAS_TITLE_STYLE),
            title_align="left",
            border_style=style_name,
            box=atlas_box(),
            expand=True,
        )
    )
    console.print()


def _workflow_breadcrumb(kind: HubKind, *, stage: str | None = None) -> str:
    labels = {
        HubKind.auto: "Download",
        HubKind.video: _breadcrumb("Media", "Download video"),
        HubKind.audio: _breadcrumb("Media", "Extract audio"),
        HubKind.file: _breadcrumb("Files", "Download file"),
        HubKind.site: _breadcrumb("Files", "Mirror website"),
        HubKind.dir: _breadcrumb("Files", "Browse directory"),
        HubKind.manifest: _breadcrumb("Files", "Metalink manifest"),
    }
    breadcrumb = labels.get(kind, kind.value)
    return _breadcrumb(breadcrumb, stage) if stage else breadcrumb


def _breadcrumb(*parts: str | None) -> str:
    separator = " \u203a " if visual_options().unicode else " > "
    return separator.join(part for part in parts if part)


def _print_workflow_footer(console: Console, *, back: str = "quit") -> None:
    console = ensure_atlas_theme(console)
    console.print()
    console.print(_menu_footer(back=back))


def _print_plain_section(
    console: Console,
    title: str,
    lines: Sequence[str | Text],
    *,
    style_name: str = ATLAS_ACTIVE_STYLE,
) -> None:
    console = ensure_atlas_theme(console)
    console.print(Text(title, style=style_name))
    for line in lines:
        value = line if isinstance(line, Text) else Text.from_markup(line)
        text = Text("  ")
        text.append_text(value)
        console.print(text)
    console.print()


def _print_url_entry_screen(console: Console, kind: HubKind) -> None:
    _print_workflow_card(
        console,
        _workflow_breadcrumb(kind),
        (("Source", "waiting for input"),),
    )
    _print_workflow_footer(console)


def _print_playlist_url_entry_screen(console: Console) -> None:
    _print_workflow_card(
        console,
        _breadcrumb("Media", "Download playlist"),
        (("Source", "waiting for input"),),
    )
    _print_workflow_footer(console)


def _print_playlist_context_warning(console: Console, url: str) -> None:
    _print_workflow_frame(
        console,
        _workflow_breadcrumb(HubKind.video),
    )
    _print_plain_section(
        console,
        "Playlist detected",
        (
            escape(url),
            "Only this video will be downloaded. Use Download playlist to fetch all items.",
        ),
        style_name=ATLAS_WARNING_STYLE,
    )
    _print_workflow_footer(console)


def _print_explicit_playlist_prompt(console: Console, url: str) -> None:
    _print_workflow_frame(
        console,
        _breadcrumb("Media", "Playlist detected"),
    )
    _print_plain_section(
        console,
        "Playlist detected",
        (
            escape(url),
            "Choose playlist mode to fetch all items, or go back.",
        ),
        style_name=ATLAS_WARNING_STYLE,
    )
    _print_workflow_footer(console)


def _directory_footer(*, multi: bool = False) -> Text:
    return _menu_footer(multi=multi)


def _render_menu_context_card(
    console: Console,
    title: str,
    rows: Sequence[tuple[str, str]],
    *,
    style_name: str = ATLAS_PANEL_STYLE,
) -> None:
    console = ensure_atlas_theme(console)
    table = Table.grid(padding=(0, 2))
    table.add_column(style=ATLAS_MUTED_STYLE, no_wrap=True)
    table.add_column(ratio=1)
    for label, value in rows:
        table.add_row(label, value)
    console.print(
        Panel(
            table,
            title=Text(f" {title} ", style=ATLAS_TITLE_STYLE),
            border_style=style_name,
            box=atlas_box(),
            expand=True,
        )
    )
    console.print()


def _render_menu_section(
    console: Console,
    title: str,
    renderable: RenderableType,
) -> None:
    console = ensure_atlas_theme(console)
    console.print(Text(title, style=ATLAS_ACTIVE_STYLE))
    console.print(renderable)
    console.print()


def _menu_separator() -> str:
    return visual_separator()


def _print_screen_title(
    console: Console,
    title: str,
    subtitle: str | None = None,
    *,
    style_name: str = ATLAS_ACTIVE_STYLE,
) -> None:
    console = ensure_atlas_theme(console)
    console.print(Text(title, style=style_name))
    if subtitle:
        console.print(Text(subtitle, style=ATLAS_MUTED_STYLE))
    console.print()


def _print_fact_rows(console: Console, rows: Sequence[tuple[str, str]]) -> None:
    console = ensure_atlas_theme(console)
    table = Table.grid(padding=(0, 2))
    table.add_column(style=ATLAS_MUTED_STYLE, no_wrap=True)
    table.add_column(ratio=1)
    for label, value in rows:
        table.add_row(label, value)
    console.print(table)


def _compact_url_label(url: str) -> str:
    parsed = urlparse(url)
    if not parsed.netloc:
        return url
    label = parsed.netloc + (parsed.path or "/")
    if parsed.query:
        label = f"{label}?{parsed.query}"
    return label


def _main_choices() -> list[MenuChoice]:
    return [
        MenuChoice("Paste URL", MainMenuChoice.smart),
        MenuChoice("Media", MainMenuChoice.media),
        MenuChoice("Files", MainMenuChoice.files),
        MenuChoice("Batch", MainMenuChoice.batch),
        MenuChoice("Sessions", MainMenuChoice.sessions),
        MenuChoice("Tools", MainMenuChoice.tools),
        MenuChoice("Settings", MainMenuChoice.settings),
        MenuChoice("Quit", MainMenuChoice.quit),
    ]


def _media_choices() -> list[MenuChoice]:
    return [
        MenuChoice("Download video", MainMenuChoice.video),
        MenuChoice("Extract audio", MainMenuChoice.audio),
        MenuChoice("Download playlist", MainMenuChoice.playlist),
        MenuChoice("Show info", MainMenuChoice.info),
        MenuChoice("Show formats", MainMenuChoice.formats),
        MenuChoice("Back", FlowResult.back),
    ]


def _files_mirrors_choices() -> list[MenuChoice]:
    return [
        MenuChoice("Download file", MainMenuChoice.file),
        MenuChoice("Browse directory", MainMenuChoice.dir),
        MenuChoice("Mirror website", MainMenuChoice.site),
        MenuChoice("Back", FlowResult.back),
    ]


def _session_choices() -> list[MenuChoice]:
    return [
        MenuChoice("Resume session", MainMenuChoice.resume),
        MenuChoice("Retry failed", MainMenuChoice.retry),
        MenuChoice("Inspect session", MainMenuChoice.inspect),
        MenuChoice("Export URLs", MainMenuChoice.export_failed),
        MenuChoice("Back", FlowResult.back),
    ]


def _tool_choices() -> list[MenuChoice]:
    return [
        MenuChoice("Doctor", MainMenuChoice.doctor),
        MenuChoice("Setup tools", MainMenuChoice.setup),
        MenuChoice("Update Atlas", MainMenuChoice.update),
        MenuChoice("Advanced backend", MainMenuChoice.advanced),
        MenuChoice("Help", MainMenuChoice.shortcuts),
        MenuChoice("Back", FlowResult.back),
    ]


def _settings_choices() -> list[MenuChoice]:
    return [
        MenuChoice("Config", MainMenuChoice.config),
        MenuChoice("Back", FlowResult.back),
    ]


def _plan_choices(
    *,
    include_formats: bool = False,
    include_customize: bool = True,
) -> list[MenuChoice]:
    choices = [
        MenuChoice("Start", PlanMenuChoice.start),
        MenuChoice("Dry run", PlanMenuChoice.dry_run),
        MenuChoice("Back", PlanMenuChoice.back),
        MenuChoice("Quit", PlanMenuChoice.quit),
    ]
    if include_customize:
        choices.insert(1, MenuChoice("Customize", PlanMenuChoice.customize))
    if include_formats:
        choices.insert(2, MenuChoice("Choose exact format", PlanMenuChoice.formats))
    return choices


def _dispatch_main_choice(
    settings: AtlasSettings,
    actions: MenuActions,
    prompts: PromptUI,
    console: Console,
    selected: MainMenuChoice,
) -> FlowResult:
    if selected == MainMenuChoice.media:
        return _submenu_flow(settings, actions, prompts, console, "Media", _media_choices())
    if selected == MainMenuChoice.files:
        return _submenu_flow(
            settings,
            actions,
            prompts,
            console,
            "Files",
            _files_mirrors_choices(),
        )
    if selected == MainMenuChoice.sessions:
        return _submenu_flow(settings, actions, prompts, console, "Sessions", _session_choices())
    if selected == MainMenuChoice.tools:
        return _submenu_flow(settings, actions, prompts, console, "Tools", _tool_choices())
    if selected == MainMenuChoice.settings:
        return _submenu_flow(settings, actions, prompts, console, "Settings", _settings_choices())
    if selected == MainMenuChoice.smart:
        return _direct_flow(settings, actions, prompts, console, HubKind.auto)
    if selected == MainMenuChoice.video:
        return _media_flow(settings, actions, prompts, console, HubKind.video)
    if selected == MainMenuChoice.audio:
        return _media_flow(settings, actions, prompts, console, HubKind.audio)
    if selected == MainMenuChoice.file:
        return _direct_flow(settings, actions, prompts, console, HubKind.file)
    if selected == MainMenuChoice.site:
        return _direct_flow(settings, actions, prompts, console, HubKind.site)
    if selected == MainMenuChoice.dir:
        return _direct_flow(settings, actions, prompts, console, HubKind.dir)
    if selected == MainMenuChoice.playlist:
        return _playlist_flow(settings, actions, prompts, console)
    if selected == MainMenuChoice.info:
        return _info_flow(actions, prompts)
    if selected == MainMenuChoice.formats:
        return _formats_flow(actions, prompts)
    if selected == MainMenuChoice.batch:
        return _batch_flow(settings, actions, prompts, console)
    if selected == MainMenuChoice.resume:
        return _saved_batch_session_flow(actions, prompts, resume=True)
    if selected == MainMenuChoice.retry:
        return _saved_batch_session_flow(actions, prompts, resume=False)
    if selected == MainMenuChoice.inspect:
        return _saved_batch_session_flow(actions, prompts, resume=False, inspect=True)
    if selected == MainMenuChoice.export_failed:
        return _export_failed_session_flow(actions, prompts)
    if selected == MainMenuChoice.advanced:
        return _advanced_backend_flow(actions, prompts, console)
    if selected == MainMenuChoice.doctor:
        actions.run_doctor()
        return _post_simple_action(prompts)
    if selected == MainMenuChoice.setup:
        actions.run_setup()
        return _post_simple_action(prompts)
    if selected == MainMenuChoice.update:
        actions.run_update()
        return _post_simple_action(prompts)
    if selected == MainMenuChoice.config:
        return _config_flow(actions, prompts)
    if selected == MainMenuChoice.shortcuts:
        console.print(
            SmartSessionView(title="atlas", console=console).shortcut_help_overlay(
                _menu_shortcut_fields()
            )
        )
        return _post_simple_action(prompts)
    return FlowResult.quit


def _menu_shortcut_fields() -> tuple[ViewField, ...]:
    move = "↑/↓" if visual_options().unicode else "up/down"
    return (
        ViewField(move, "Move the highlighted choice"),
        ViewField("enter", "Choose the highlighted item"),
        ViewField("type", "Filter the current choice list"),
        ViewField("space", "Toggle an item in multi-select prompts"),
        ViewField("ctrl-c", "Cancel the current prompt and go back"),
    )


def _submenu_flow(
    settings: AtlasSettings,
    actions: MenuActions,
    prompts: PromptUI,
    console: Console,
    title: str,
    choices: Sequence[MenuChoice],
) -> FlowResult:
    while True:
        selected = prompts.select(title, choices)
        if selected is None or selected == FlowResult.back:
            return FlowResult.back
        result = _dispatch_main_choice(
            settings,
            actions,
            prompts,
            console,
            cast(MainMenuChoice, selected),
        )
        if result == FlowResult.quit:
            return result


def _media_flow(
    settings: AtlasSettings,
    actions: MenuActions,
    prompts: PromptUI,
    console: Console,
    kind: HubKind,
) -> FlowResult:
    while True:
        _print_url_entry_screen(console, kind)
        url = prompts.text("URL")
        if url is None:
            return FlowResult.back
        options: MenuDownloadOptions
        effective_kind = kind
        playlist = False
        if is_explicit_playlist_url(url):
            _print_explicit_playlist_prompt(console, url)
            selected = cast(
                HubKind | None,
                prompts.select(
                    "Playlist detected",
                    [
                        MenuChoice("Download playlist as video", HubKind.video),
                        MenuChoice("Download playlist as audio", HubKind.audio),
                        MenuChoice("Back", None),
                    ],
                ),
            )
            if selected is None:
                return FlowResult.back
            effective_kind = selected
            playlist = True
        elif is_watch_url_with_playlist_params(url):
            _print_playlist_context_warning(console, url)
        if effective_kind == HubKind.audio:
            options = build_audio_options(settings, url, playlist=playlist)
        else:
            options = build_video_options(settings, url, playlist=playlist)
        prepared = _prepare_media_options(
            settings,
            actions,
            prompts,
            console,
            options,
            effective_kind,
            playlist=playlist,
        )
        if prepared is None:
            return FlowResult.back
        options, effective_kind, media = prepared
        result = _plan_loop(actions, prompts, console, options, effective_kind, media=media)
        if result != CompletionChoice.another:
            return FlowResult.quit if result == CompletionChoice.quit else FlowResult.back


def _direct_flow(
    settings: AtlasSettings,
    actions: MenuActions,
    prompts: PromptUI,
    console: Console,
    kind: HubKind,
) -> FlowResult:
    while True:
        url = prompts.text("URL")
        if url is None:
            return FlowResult.back
        if kind in {HubKind.auto, HubKind.dir} and _url_should_scan_before_auto_plan(url):
            return _url_scan_action_flow(settings, actions, prompts, console, url)
        options: MenuDownloadOptions
        if kind == HubKind.site:
            options = build_site_options(settings, url)
        elif kind == HubKind.dir:
            options = build_directory_options(settings, url)
        else:
            options = build_file_options(settings, url)
        result = _plan_loop(actions, prompts, console, options, kind)
        if result != CompletionChoice.another:
            return FlowResult.quit if result == CompletionChoice.quit else FlowResult.back


def _playlist_flow(
    settings: AtlasSettings,
    actions: MenuActions,
    prompts: PromptUI,
    console: Console,
) -> FlowResult:
    while True:
        _print_playlist_url_entry_screen(console)
        url = prompts.text("Playlist URL")
        if url is None:
            return FlowResult.back
        kind = cast(
            BatchKind | None,
            prompts.select(
                "Download playlist as",
                [
                    MenuChoice("Video playlist", BatchKind.video),
                    MenuChoice("Audio playlist", BatchKind.audio),
                    MenuChoice("Back", None),
                ],
            ),
        )
        if kind is None:
            return FlowResult.back
        options: MenuDownloadOptions
        hub_kind: HubKind
        if kind == BatchKind.audio:
            options = build_audio_options(settings, url, playlist=True)
            hub_kind = HubKind.audio
        else:
            options = build_video_options(settings, url, playlist=True)
            hub_kind = HubKind.video
        prepared = _prepare_media_options(
            settings,
            actions,
            prompts,
            console,
            options,
            hub_kind,
            playlist=True,
        )
        if prepared is None:
            return FlowResult.back
        options, hub_kind, media = prepared
        result = _plan_loop(actions, prompts, console, options, hub_kind, media=media)
        if result != CompletionChoice.another:
            return FlowResult.quit if result == CompletionChoice.quit else FlowResult.back


def _prepare_media_options(
    settings: AtlasSettings,
    actions: MenuActions,
    prompts: PromptUI,
    console: Console,
    options: MenuDownloadOptions,
    kind: HubKind,
    *,
    playlist: bool,
) -> tuple[MenuDownloadOptions, HubKind, MediaInfo | None] | None:
    if kind not in {HubKind.video, HubKind.audio}:
        return options, kind, None
    try:
        media = _with_menu_status(
            console,
            "Inspecting media",
            lambda: actions.probe_media(options.url, playlist=playlist),
        )
    except AtlasError as exc:
        console.print(f"[{ATLAS_ERROR_STYLE}]Media probe failed[/{ATLAS_ERROR_STYLE}]")
        console.print(str(exc))
        return None
    resolver = MediaCapabilityResolver.from_info(media)
    if not resolver.catalog.formats:
        _print_media_empty_state(console, media, options.url, kind=kind)
        return options, kind, media
    _print_media_profile_context(
        console,
        media,
        resolver.catalog,
        url=options.url,
        kind=kind,
        playlist=playlist,
    )
    selected = _select_media_choice(prompts, resolver, kind)
    if selected is None:
        return None
    choice, selected_kind = selected
    if choice.requires_transcode and selected_kind != HubKind.audio:
        _print_media_choice_warnings(console, choice)
        confirmed = prompts.confirm("Continue with this profile?", default=False)
        if not confirmed:
            return None
    if selected_kind == HubKind.audio:
        audio_options = (
            options
            if isinstance(options, AudioDownloadOptions)
            else build_audio_options(settings, options.url, playlist=playlist)
        )
        return resolver.apply_audio_choice(audio_options, choice), HubKind.audio, media
    video_options = (
        options
        if isinstance(options, VideoDownloadOptions)
        else build_video_options(settings, options.url, playlist=playlist)
    )
    return resolver.apply_video_choice(video_options, choice), HubKind.video, media


def _select_media_choice(
    prompts: PromptUI,
    resolver: MediaCapabilityResolver,
    kind: HubKind,
) -> tuple[MediaChoice, HubKind] | None:
    profile_choices = (
        resolver.audio_profiles() if kind == HubKind.audio else resolver.video_profiles()
    )
    choices = [
        MenuChoice(format_choice_label(choice), choice.profile)
        for choice in profile_choices
        if choice.selectable
    ]
    if resolver.catalog.formats:
        choices.append(MenuChoice("Custom formats", MediaProfile.custom))
    choices.append(MenuChoice("Back", None))
    if len(choices) <= 1:
        return None
    selected = cast(MediaProfile | None, prompts.select("Choose profile", choices))
    if selected is None:
        return None
    if selected == MediaProfile.custom:
        return _select_exact_media_formats(prompts, resolver, kind)
    for choice in profile_choices:
        if choice.profile == selected:
            selected_kind = (
                HubKind.audio
                if selected in {MediaProfile.audio_best, MediaProfile.audio_mp3}
                else kind
            )
            return choice, selected_kind
    return None


def _select_exact_media_formats(
    prompts: PromptUI,
    resolver: MediaCapabilityResolver,
    kind: HubKind,
) -> tuple[MediaChoice, HubKind] | None:
    catalog = resolver.catalog
    if kind == HubKind.audio:
        audio_pool = list(catalog.audio_formats or catalog.combined_formats)
        if not audio_pool:
            return None
        selected_audio = prompts.select(
            "Audio format",
            [MenuChoice(format_format_row(fmt), fmt) for fmt in audio_pool]
            + [MenuChoice("Back", None)],
        )
        if not isinstance(selected_audio, FormatInfo):
            return None
        return resolver.choice_for_exact_audio(selected_audio), HubKind.audio

    video_pool = list(catalog.video_formats or catalog.combined_formats)
    if not video_pool:
        return None
    selected_video = prompts.select(
        "Video format",
        [MenuChoice(format_format_row(fmt), fmt) for fmt in video_pool]
        + [MenuChoice("Back", None)],
    )
    if not isinstance(selected_video, FormatInfo):
        return None
    audio: object | None = None
    if not _format_has_audio(selected_video) and catalog.audio_formats:
        audio = prompts.select(
            "Audio format",
            [MenuChoice("Auto best audio", catalog.audio_formats[0])]
            + [MenuChoice(format_format_row(fmt), fmt) for fmt in catalog.audio_formats]
            + [MenuChoice("Back", None)],
        )
        if audio is None:
            return None
    selected_audio = audio if isinstance(audio, FormatInfo) else None
    return resolver.choice_for_exact_formats(selected_video, selected_audio), HubKind.video


def _format_has_audio(fmt: object) -> bool:
    return bool(getattr(fmt, "acodec", None) and getattr(fmt, "acodec", None) != "none")


def _print_media_empty_state(
    console: Console,
    media: MediaInfo,
    url: str,
    *,
    kind: HubKind,
) -> None:
    _print_workflow_frame(
        console,
        _workflow_breadcrumb(kind),
    )
    _print_plain_section(
        console,
        "Source",
        (escape(url),),
    )
    _print_plain_section(
        console,
        "No compatible formats found",
        (
            escape(media.title or "unknown"),
            "Try Custom formats, Audio only, or update yt-dlp",
        ),
        style_name=ATLAS_WARNING_STYLE,
    )
    _print_workflow_footer(console)


def _print_media_profile_context(
    console: Console,
    media: MediaInfo,
    catalog: MediaCapabilityCatalog,
    *,
    url: str | None = None,
    kind: HubKind | None = None,
    playlist: bool = False,
) -> None:
    console = ensure_atlas_theme(console)
    active_kind = kind or HubKind.video
    card_rows: list[tuple[str, str | Text]] = []
    if url:
        card_rows.append(("Source", escape(url)))
    else:
        card_rows.extend(
            [
                ("Title", escape(media.title or "Untitled media")),
                ("Source", escape(_media_plan_source_label(media))),
            ]
        )
    _print_workflow_card(
        console,
        _workflow_breadcrumb(active_kind),
        tuple(card_rows),
    )
    if url and is_watch_url_with_playlist_params(url) and not playlist:
        _print_plain_section(
            console,
            "Playlist detected",
            (
                "Only this video will be downloaded. Use Download playlist to fetch all items.",
            ),
            style_name=ATLAS_WARNING_STYLE,
        )
    _print_plain_section(
        console,
        "Detected",
        (
            escape(media.title or "Untitled media"),
            escape(_media_source_label(media)),
            _media_format_count_label(catalog),
        ),
    )
    console.print(Text("Choose profile", style=ATLAS_ACTIVE_STYLE))
    _print_workflow_footer(console)


def _media_source_label(media: MediaInfo) -> str:
    source = media.extractor or "media"
    owner = media.uploader or media.channel
    duration = format_duration(media.duration) if media.duration else None
    return visual_join(part for part in (owner, source, duration) if part)


def _media_plan_source_label(media: MediaInfo | None) -> str:
    if media is None:
        return "media"
    source = media.extractor or "media"
    owner = media.uploader or media.channel
    return visual_join(part for part in (source, owner) if part)


def _media_format_count_label(catalog: MediaCapabilityCatalog) -> str:
    video_count = len(catalog.video_formats)
    audio_count = len(catalog.audio_formats)
    return visual_join(
        (
            f"{video_count} {_plural('video format', video_count)}",
            f"{audio_count} {_plural('audio format', audio_count)} available",
        )
    )


def _plural(label: str, count: int) -> str:
    return label if count == 1 else f"{label}s"


def _print_media_choice_warnings(console: Console, choice: MediaChoice) -> None:
    for warning in choice.warnings:
        console.print(f"[{ATLAS_WARNING_STYLE}]! {escape(warning)}[/{ATLAS_WARNING_STYLE}]")


def _plan_loop(
    actions: MenuActions,
    prompts: PromptUI,
    console: Console,
    options: MenuDownloadOptions,
    kind: HubKind,
    *,
    media: MediaInfo | None = None,
) -> CompletionChoice:
    current = options
    while True:
        try:
            plan = _build_plan_with_status(console, actions, current, kind)
        except AtlasError as exc:
            recovery = _prompt_plan_recovery(
                prompts,
                console,
                exc,
                stage="Planning interrupted",
            )
            if recovery == PlanRecoveryChoice.retry:
                continue
            if recovery == PlanRecoveryChoice.customize:
                previous = current
                current = _customize_options(prompts, current, console=console)
                _print_option_diff(console, previous, current)
                continue
            if recovery == PlanRecoveryChoice.doctor:
                actions.run_doctor()
                continue
            if recovery == PlanRecoveryChoice.quit:
                return CompletionChoice.quit
            return CompletionChoice.back
        active_kind = plan.route.kind if kind == HubKind.auto else kind
        if kind == HubKind.auto and type(plan.options) is not type(current):
            current = plan.options
        _print_menu_plan(console, plan, media=media)
        selected = cast(
            PlanMenuChoice | None,
            prompts.select(
                "Next",
                _plan_choices(include_formats=active_kind in {HubKind.audio, HubKind.video}),
            ),
        )
        if selected in {None, PlanMenuChoice.back}:
            return CompletionChoice.back
        if selected == PlanMenuChoice.quit:
            return CompletionChoice.quit
        if selected == PlanMenuChoice.customize:
            previous = current
            current = _customize_options(prompts, current, console=console)
            _print_option_diff(console, previous, current)
            continue
        if selected == PlanMenuChoice.formats:
            actions.run_formats(current.url)
            continue
        dry_run = selected == PlanMenuChoice.dry_run
        while True:
            try:
                execution_plan = plan
                if dry_run:
                    dry_options = current.model_copy(update={"dry_run": True})
                    execution_plan = actions.build_plan(dry_options, active_kind)
                saved_paths = actions.execute_plan(execution_plan)
            except AtlasError as exc:
                recovery = _prompt_plan_recovery(
                    prompts,
                    console,
                    exc,
                    stage="Dry run interrupted" if dry_run else "Download interrupted",
                )
                if recovery == PlanRecoveryChoice.retry:
                    continue
                if recovery == PlanRecoveryChoice.customize:
                    previous = current
                    current = _customize_options(prompts, current, console=console)
                    _print_option_diff(console, previous, current)
                elif recovery == PlanRecoveryChoice.doctor:
                    actions.run_doctor()
                elif recovery == PlanRecoveryChoice.quit:
                    return CompletionChoice.quit
                elif recovery == PlanRecoveryChoice.back:
                    return CompletionChoice.back
                break
            if dry_run:
                break
            completion = _completion_loop(
                prompts,
                saved_paths,
                console=console,
                plan=plan,
                media=media,
            )
            if completion in {CompletionChoice.reveal, CompletionChoice.open}:
                break
            return completion


def _prompt_plan_recovery(
    prompts: PromptUI,
    console: Console,
    error: AtlasError,
    *,
    stage: str,
) -> PlanRecoveryChoice:
    detail = redact_text(str(error)).strip() or "The operation did not complete."
    body = Table.grid(padding=(0, 2))
    body.add_column(style=ATLAS_MUTED_STYLE, no_wrap=True)
    body.add_column(ratio=1)
    body.add_row("Status", Text(stage, style=ATLAS_ERROR_STYLE))
    body.add_row("Reason", Text(detail))
    body.add_row("Your plan", Text("kept intact; choose how to continue"))
    console.print(
        Panel(
            body,
            title=Text(" Needs attention ", style=ATLAS_ERROR_STYLE),
            border_style=ATLAS_ERROR_STYLE,
            box=atlas_box(),
            padding=(0, 1),
            expand=False,
        )
    )
    selected = cast(
        PlanRecoveryChoice | None,
        prompts.select(
            "Continue",
            (
                MenuChoice("Try again", PlanRecoveryChoice.retry),
                MenuChoice("Customize plan", PlanRecoveryChoice.customize),
                MenuChoice("Run diagnostics", PlanRecoveryChoice.doctor),
                MenuChoice("Back to menu", PlanRecoveryChoice.back),
                MenuChoice("Quit", PlanRecoveryChoice.quit),
            ),
        ),
    )
    return selected or PlanRecoveryChoice.back


def _build_plan_with_status(
    console: Console,
    actions: MenuActions,
    options: MenuDownloadOptions,
    kind: HubKind,
) -> HubExecutionPlan:
    return _with_menu_status(console, "Planning", lambda: actions.build_plan(options, kind))


def _with_menu_status[T](console: Console, message: str, operation: Callable[[], T]) -> T:
    if not console.is_terminal or not visual_options().motion:
        return operation()
    with console.status(f"[{ATLAS_ACTIVE_STYLE}]{message}[/{ATLAS_ACTIVE_STYLE}]", spinner="dots"):
        return operation()


def _print_menu_plan(
    console: Console,
    plan: HubExecutionPlan,
    *,
    media: MediaInfo | None = None,
) -> None:
    """Render a compact interactive plan instead of a raw backend summary."""

    console = ensure_atlas_theme(console)
    if isinstance(plan.options, AudioDownloadOptions | VideoDownloadOptions):
        _print_media_menu_plan(console, plan, media=media)
        return
    _print_workflow_frame(console, _workflow_breadcrumb(plan.route.kind))
    for title, lines in _confirmation_sections(plan, media=media):
        _print_plain_section(console, title, lines)
    _print_workflow_footer(console)


def _print_media_menu_plan(
    console: Console,
    plan: HubExecutionPlan,
    *,
    media: MediaInfo | None,
) -> None:
    _print_workflow_card(
        console,
        _workflow_breadcrumb(plan.route.kind),
        tuple(_media_plan_card_rows(plan, media=media)),
    )
    _print_plain_section(console, "Options", tuple(_confirmation_option_lines(plan)))
    _print_plain_section(console, "Next", tuple(_confirmation_next_lines(plan)))
    _print_workflow_footer(console)


def _media_plan_card_rows(
    plan: HubExecutionPlan,
    *,
    media: MediaInfo | None,
) -> list[tuple[str, str | Text]]:
    options = plan.options
    output = plan.preview.output or plan.route.output_dir
    display_output = output.parent if output.suffix else output
    rows: list[tuple[str, str | Text]] = [
        ("Title", escape(media.title if media and media.title else "Untitled media")),
        ("Source", escape(_media_plan_source_label(media))),
    ]
    if isinstance(options, VideoDownloadOptions):
        container = _display_container(
            str(plan.preview.summary.get("container") or options.container.value)
        )
        rows.extend(
            [
                ("Quality", _video_plan_quality_label(plan, media=media)),
                ("Container", container),
                ("Output", Text.from_markup(_menu_path(display_output))),
            ]
        )
    elif isinstance(options, AudioDownloadOptions):
        rows.extend(
            [
                ("Quality", _audio_plan_quality_label(plan, media=media)),
                ("Output", Text.from_markup(_menu_path(display_output))),
            ]
        )
    return rows


def _confirmation_sections(
    plan: HubExecutionPlan,
    *,
    media: MediaInfo | None,
) -> tuple[tuple[str, tuple[str | Text, ...]], ...]:
    sections: list[tuple[str, tuple[str | Text, ...]]] = [
        ("Source", (escape(plan.route.url),)),
    ]
    if media is not None:
        sections.append(
            (
                "Detected",
                (
                    escape(media.title or "Untitled media"),
                    escape(_media_source_label(media)),
                    _detected_format_count_label(media, plan.route.kind),
                ),
            )
        )
    sections.append(("Output", tuple(_confirmation_output_lines(plan, media=media))))
    sections.append(("Options", tuple(_confirmation_option_lines(plan))))
    sections.append(("Next", tuple(_confirmation_next_lines(plan))))
    return tuple(sections)


def _confirmation_output_lines(
    plan: HubExecutionPlan,
    *,
    media: MediaInfo | None,
) -> list[str | Text]:
    output = plan.preview.output or plan.route.output_dir
    display_output = output.parent if output.suffix else output
    lines: list[str | Text] = [Text.from_markup(_menu_path(display_output))]
    profile = _selected_output_profile(plan, media=media)
    if profile:
        lines.append(profile)
    return lines


def _selected_output_profile(
    plan: HubExecutionPlan,
    *,
    media: MediaInfo | None,
) -> str:
    options = plan.options
    summary = plan.preview.summary
    if isinstance(options, AudioDownloadOptions):
        fmt = _selected_media_format(media, options.format)
        if fmt is None and options.codec == AudioCodec.best:
            return "Best audio"
        codec = _display_codec(fmt.acodec if fmt else options.codec.value)
        container = _display_container(fmt.ext if fmt and fmt.ext else _audio_container(options))
        return visual_join(part for part in (codec, container) if part)
    if isinstance(options, VideoDownloadOptions):
        selected = _selected_media_formats(media, options.format)
        video = next((fmt for fmt in selected if fmt.vcodec and fmt.vcodec != "none"), None)
        audio = next((fmt for fmt in selected if fmt.acodec and fmt.acodec != "none"), None)
        codec = _combined_media_codecs(
            _display_codec(video.vcodec if video else options.video_codec.value),
            _display_codec(audio.acodec if audio else None),
        )
        container = _display_container(str(summary.get("container") or options.container.value))
        resolution = video.resolution if video and video.resolution else options.resolution.value
        return visual_join(part for part in (resolution, codec, container) if part)
    if isinstance(options, FileDownloadOptions):
        probe = summary.get("probe")
        if isinstance(probe, Mapping):
            content_length = probe.get("content_length")
            if isinstance(content_length, int):
                return format_bytes(content_length)
    return ""


def _selected_media_format(media: MediaInfo | None, selector: str | None) -> FormatInfo | None:
    selected = _selected_media_formats(media, selector)
    return selected[0] if selected else None


def _selected_media_formats(media: MediaInfo | None, selector: str | None) -> list[FormatInfo]:
    if media is None or not selector:
        return []
    selected_ids = [part.strip() for part in selector.split("+") if part.strip()]
    if not selected_ids:
        return []
    selected: list[FormatInfo] = []
    for format_id in selected_ids:
        for fmt in media.formats:
            if fmt.format_id == format_id:
                selected.append(fmt)
                break
    return selected


def _video_plan_quality_label(
    plan: HubExecutionPlan,
    *,
    media: MediaInfo | None,
) -> str:
    options = plan.options
    if not isinstance(options, VideoDownloadOptions):
        return ""
    selected = _selected_media_formats(media, options.format)
    video = next((fmt for fmt in selected if fmt.vcodec and fmt.vcodec != "none"), None)
    audio = next((fmt for fmt in selected if fmt.acodec and fmt.acodec != "none"), None)
    resolution = video.resolution if video and video.resolution else options.resolution.value
    codecs = _combined_media_codecs(
        _display_codec(video.vcodec if video else options.video_codec.value),
        _display_codec(audio.acodec if audio else None),
    )
    details = visual_join(part for part in (resolution, codecs) if part)
    return visual_join(part for part in (_video_profile_label(options.quality), details) if part)


def _audio_plan_quality_label(
    plan: HubExecutionPlan,
    *,
    media: MediaInfo | None,
) -> str:
    profile = _selected_output_profile(plan, media=media)
    return visual_join(part for part in ("Audio only", profile) if part)


def _combined_media_codecs(video: str, audio: str) -> str:
    if video and audio:
        return f"{video} + {audio}"
    return video or audio


def _audio_container(options: AudioDownloadOptions) -> str:
    return "mp3" if options.codec == AudioCodec.mp3 else "audio"


def _display_codec(value: str | None) -> str:
    if not value or value == "none":
        return ""
    lowered = value.lower()
    if lowered == "best":
        return "Best audio"
    if "opus" in lowered:
        return "Opus"
    if "mp4a" in lowered or "aac" in lowered or lowered == "m4a":
        return "AAC"
    if "av01" in lowered or lowered == "av1":
        return "AV1"
    if "avc" in lowered or "h264" in lowered:
        return "H.264"
    if "vp9" in lowered:
        return "VP9"
    return value.upper() if len(value) <= 5 else value


def _display_container(value: str | None) -> str:
    if not value or value == "auto":
        return ""
    labels = {
        "audio": "Audio",
        "m4a": "M4A",
        "mkv": "MKV",
        "mp3": "MP3",
        "mp4": "MP4",
        "webm": "WebM",
    }
    return labels.get(value.lower(), value.upper())


def _confirmation_option_lines(plan: HubExecutionPlan) -> list[str]:
    options = plan.options
    if isinstance(options, AudioDownloadOptions | VideoDownloadOptions):
        artwork_label = "Artwork" if isinstance(options, AudioDownloadOptions) else "Thumbnail"
        return [
            _plain_option_row("Metadata", _on_off(options.embed_metadata)),
            _plain_option_row(artwork_label, _on_off(options.embed_thumbnail)),
            _plain_option_row("Archive", _on_off(options.archive)),
            _plain_option_row("Playlist", _playlist_confirmation_label(options)),
        ]
    if isinstance(options, FileDownloadOptions):
        return [
            _plain_option_row("Resume", _on_off(options.continue_download)),
            _plain_option_row("Overwrite", _on_off(options.overwrite)),
        ]
    if isinstance(options, DirectoryMirrorOptions | SiteDownloadOptions):
        return [
            _plain_option_row("Scope", "same host" if not options.span_hosts else "multi-host"),
            _plain_option_row("Depth", str(options.depth)),
            _plain_option_row("Resume", _on_off(options.continue_download)),
        ]
    return []


def _playlist_confirmation_label(options: AudioDownloadOptions | VideoDownloadOptions) -> str:
    if options.playlist:
        return "playlist"
    return "single item only"


def _on_off(value: bool) -> str:
    return "on" if value else "off"


def _plain_option_row(label: str, value: str) -> str:
    return f"{label:<10}{value}"


def _confirmation_next_lines(plan: HubExecutionPlan) -> list[str]:
    options = plan.options
    choices = ["Start", "Customize"]
    if isinstance(options, AudioDownloadOptions | VideoDownloadOptions):
        choices.append("Choose exact format")
    choices.extend(["Dry run", "Back", "Quit"])
    marker = status_glyph("selected")
    return [f"{marker} {choices[0]}", *(f"  {choice}" for choice in choices[1:])]


def _detected_format_count_label(media: MediaInfo, kind: HubKind) -> str:
    catalog = MediaCapabilityCatalog.from_media_info(media)
    audio = len(catalog.audio_formats)
    video = len(catalog.video_formats)
    labels: tuple[tuple[int, str], ...] = (
        (audio, "audio format"),
        (video, "video format"),
    )
    if kind == HubKind.video:
        labels = tuple(reversed(labels))
    return visual_join(f"{count} {_plural(label, count)}" for count, label in labels)


def _menu_plan_table(
    sections: Sequence[tuple[str, Sequence[tuple[str, str]]]],
) -> Table:
    table = Table.grid(padding=(0, 2))
    table.add_column(style=ATLAS_MUTED_STYLE, no_wrap=True)
    table.add_column(ratio=1)
    for index, (section, rows) in enumerate(sections):
        if index:
            table.add_row("", "")
        table.add_row(Text(section, style=ATLAS_ACTIVE_STYLE), "")
        for label, value in rows:
            table.add_row(label, value)
    return table


def _menu_plan_sections(
    plan: HubExecutionPlan,
) -> tuple[tuple[str, tuple[tuple[str, str], ...]], ...]:
    route = plan.route
    options = plan.options
    summary = plan.preview.summary
    sections: list[tuple[str, tuple[tuple[str, str], ...]]] = [
        (
            "Source",
            (
                ("URL", escape(route.url)),
                ("Mode", _menu_plan_mode(plan)),
                ("Backend", _menu_plan_backend(plan)),
            ),
        ),
        ("Output", tuple(_menu_plan_output_rows(plan))),
    ]

    if isinstance(options, AudioDownloadOptions | VideoDownloadOptions):
        sections.append(("Quality", tuple(_media_quality_rows(options, summary))))
        sections.append(("Extras", tuple(_media_extras_rows(options))))
    elif isinstance(options, DirectoryMirrorOptions):
        sections.append(("Mirror", tuple(_mirror_quality_rows(options, directory=True))))
        sections.append(("Extras", tuple(_mirror_extras_rows(options, directory=True))))
    elif isinstance(options, SiteDownloadOptions):
        sections.append(("Mirror", tuple(_mirror_quality_rows(options, directory=False))))
        sections.append(("Extras", tuple(_mirror_extras_rows(options, directory=False))))
    else:
        sections.append(("Transfer", tuple(_file_transfer_rows(options, summary))))
        sections.append(("Extras", tuple(_file_extras_rows(options))))

    safety_rows = list(_safety_rows(plan))
    scheduler = _menu_plan_scheduler(plan)
    if scheduler:
        safety_rows.append(("Scheduler", scheduler))
    safety_rows.append(("Next", _menu_plan_next_phases(plan)))
    sections.append(("Safety", tuple(safety_rows)))
    return tuple(sections)


def _menu_plan_output_rows(plan: HubExecutionPlan) -> list[tuple[str, str]]:
    output = plan.preview.output or plan.route.output_dir
    is_file = bool(output.suffix)
    rows = [("Folder", _menu_path(output.parent if is_file else output))]
    if is_file:
        rows.append(("Filename", escape(output.name)))
    return rows


def _media_quality_rows(
    options: AudioDownloadOptions | VideoDownloadOptions,
    summary: Mapping[str, object],
) -> list[tuple[str, str]]:
    if isinstance(options, AudioDownloadOptions):
        return [
            ("Profile", "Audio only" if options.codec != AudioCodec.mp3 else "MP3"),
            ("Format", escape(str(summary.get("format") or options.format or "best audio"))),
            ("Codec", options.codec.value),
            ("Quality", str(options.quality)),
        ]
    container = str(summary.get("container") or options.container.value)
    return [
        ("Profile", _video_profile_label(options.quality)),
        ("Format", escape(str(summary.get("format") or options.format or "best available"))),
        ("Container", container),
        ("Video", options.video_codec.value),
        ("Resolution", options.resolution.value),
    ]


def _media_extras_rows(
    options: AudioDownloadOptions | VideoDownloadOptions,
) -> list[tuple[str, str]]:
    rows = [
        ("Metadata", _enabled_label(options.embed_metadata)),
        ("Archive", _enabled_label(options.archive)),
    ]
    if isinstance(options, AudioDownloadOptions):
        rows.append(("Artwork", _enabled_label(options.embed_thumbnail)))
    else:
        rows.append(("Thumbnail", _enabled_label(options.embed_thumbnail)))
    if options.playlist:
        rows.append(("Playlist", "explicit playlist session"))
    fragments = options.concurrent_fragments
    if fragments:
        rows.append(("Fragments", str(fragments)))
    return rows


def _video_profile_label(quality: QualityIntent) -> str:
    return {
        QualityIntent.compatible: "Apple compatible",
        QualityIntent.balanced: "Balanced",
        QualityIntent.small: "Small file",
        QualityIntent.max: "Best quality",
    }.get(quality, quality.value)


def _file_transfer_rows(
    options: FileDownloadOptions,
    summary: Mapping[str, object],
) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    probe = summary.get("probe")
    if isinstance(probe, Mapping):
        content_type = probe.get("content_type")
        content_length = probe.get("content_length")
        supports_ranges = probe.get("supports_ranges")
        if content_type:
            rows.append(("Type", escape(str(content_type))))
        if isinstance(content_length, int):
            rows.append(("Size", format_bytes(content_length)))
        if supports_ranges is not None:
            rows.append(("Ranges", _enabled_label(bool(supports_ranges))))
    backend = str(summary.get("backend") or options.backend.value)
    if backend == "aria2":
        rows.append(("Connections", f"{options.splits} segments"))
    else:
        rows.append(("Mode", visual_join(("single file", "resumable"))))
    return rows


def _file_extras_rows(options: FileDownloadOptions) -> list[tuple[str, str]]:
    return [
        ("Resume", _enabled_label(options.continue_download)),
        ("Overwrite", _enabled_label(options.overwrite)),
    ]


def _mirror_quality_rows(
    options: SiteDownloadOptions,
    *,
    directory: bool,
) -> list[tuple[str, str]]:
    html = (
        "preserve pages"
        if directory
        else visual_join(
            (
                "convert links" if options.convert_links else "keep links",
                "page requisites" if options.page_requisites else "no page requisites",
            )
        )
    )
    rows = [
        (
            "Scope",
            visual_join(("same host", "no parent")) if options.no_parent else "same host",
        ),
        ("Depth", str(options.depth)),
        ("HTML", html),
        ("Network", _network_plan_label(options)),
    ]
    if options.domains:
        rows.append(("Domains", escape(options.domains)))
    return rows


def _mirror_extras_rows(
    options: SiteDownloadOptions,
    *,
    directory: bool,
) -> list[tuple[str, str]]:
    rows = [("Continue", _enabled_label(options.continue_download))]
    if directory:
        rows.append(("Timestamping", _enabled_label(options.timestamping)))
        if options.if_modified_since is not None:
            rows.append(
                (
                    "If-Modified-Since",
                    "enabled" if options.if_modified_since else "disabled",
                )
            )
    return rows


def _safety_rows(plan: HubExecutionPlan) -> list[tuple[str, str]]:
    safety = _menu_plan_safety(plan)
    rows = [("Policy", safety)] if safety else []
    options = plan.options
    if isinstance(options, AudioDownloadOptions | VideoDownloadOptions):
        if is_watch_url_with_playlist_params(options.url) and not options.playlist:
            rows.append(
                (
                    "Playlist",
                    "watch URL playlist/radio params kept single item",
                )
            )
        elif options.playlist:
            rows.append(("Playlist", "explicit playlist mode enabled"))
        else:
            rows.append(("Playlist", "single item by default"))
    return rows


def _menu_plan_mode(plan: HubExecutionPlan) -> str:
    route = plan.route
    options = plan.options
    if isinstance(options, AudioDownloadOptions):
        return "audio extraction" if options.playlist is False else "audio playlist"
    if isinstance(options, VideoDownloadOptions):
        return "video download" if options.playlist is False else "video playlist"
    if isinstance(options, DirectoryMirrorOptions):
        return "recursive directory mirror"
    if isinstance(options, SiteDownloadOptions):
        return "offline website mirror"
    if route.kind == HubKind.manifest:
        return "manifest-backed file download"
    return "direct file"


def _menu_plan_backend(plan: HubExecutionPlan) -> str:
    backend = plan.preview.summary.get("backend")
    if backend:
        reason = plan.preview.summary.get("backend_reason")
        return visual_join((str(backend), str(reason))) if reason else str(backend)
    return plan.route.engine.value


def _menu_path(path: Path) -> str:
    return f"[{ATLAS_PATH_STYLE}]{escape(str(path.expanduser()))}[/{ATLAS_PATH_STYLE}]"


def _add_media_plan_rows(
    table: Table,
    options: AudioDownloadOptions | VideoDownloadOptions,
    summary: Mapping[str, object],
) -> None:
    if isinstance(options, AudioDownloadOptions):
        table.add_row("Codec", visual_join((options.codec.value, f"quality {options.quality}")))
        table.add_row("Metadata", _enabled_label(options.embed_metadata))
        table.add_row("Artwork", _enabled_label(options.embed_thumbnail))
    else:
        table.add_row("Quality", visual_join((options.quality.value, options.container.value)))
        table.add_row("Video codec", options.video_codec.value)
        table.add_row("Metadata", _enabled_label(options.embed_metadata))
        table.add_row("Thumbnail", _enabled_label(options.embed_thumbnail))
    if options.playlist:
        table.add_row("Playlist", "explicit playlist session")
    fragments = summary.get("concurrent_fragments")
    if fragments:
        table.add_row("Fragments", str(fragments))
    table.add_row("Archive", _enabled_label(options.archive))


def _add_file_plan_rows(
    table: Table,
    options: FileDownloadOptions,
    summary: Mapping[str, object],
) -> None:
    probe = summary.get("probe")
    if isinstance(probe, Mapping):
        content_type = probe.get("content_type")
        content_length = probe.get("content_length")
        supports_ranges = probe.get("supports_ranges")
        if content_type:
            table.add_row("Type", escape(str(content_type)))
        if isinstance(content_length, int):
            table.add_row("Size", format_bytes(content_length))
        if supports_ranges is not None:
            table.add_row("Ranges", _enabled_label(bool(supports_ranges)))
    backend = str(summary.get("backend") or options.backend.value)
    if backend == "aria2":
        table.add_row("Connections", f"{options.splits} segments")
    else:
        table.add_row("Transfer", visual_join(("single file", "resumable")))
    table.add_row("Resume", _enabled_label(options.continue_download))
    table.add_row("Overwrite", _enabled_label(options.overwrite))


def _add_mirror_plan_rows(
    table: Table,
    options: SiteDownloadOptions,
    *,
    directory: bool,
) -> None:
    table.add_row(
        "Scope",
        visual_join(("same host", "no parent")) if options.no_parent else "same host",
    )
    table.add_row("Depth", str(options.depth))
    if options.domains:
        table.add_row("Domains", escape(options.domains))
    if directory:
        table.add_row("HTML", "preserve pages")
    else:
        html = "convert links" if options.convert_links else "keep links"
        assets = "page requisites" if options.page_requisites else "no page requisites"
        table.add_row("HTML", visual_join((html, assets)))
    table.add_row("Network", _network_plan_label(options))
    table.add_row("Continue", _enabled_label(options.continue_download))
    if directory:
        table.add_row("Timestamping", _enabled_label(options.timestamping))
        if options.if_modified_since is not None:
            label = "enabled" if options.if_modified_since else "disabled"
            table.add_row("If-Modified-Since", label)


def _network_plan_label(options: SiteDownloadOptions) -> str:
    parts = [
        f"wait {_seconds_label(options.wait)}",
        "random wait" if options.random_wait else "fixed wait",
        f"timeout {_seconds_label(options.timeout)}",
        f"tries {options.tries}",
    ]
    return visual_join(parts)


def _seconds_label(value: float | None) -> str:
    if value is None:
        return "default"
    return f"{value:g}s"


def _menu_plan_scheduler(plan: HubExecutionPlan) -> str | None:
    session = plan.preview.session
    if session is None:
        return None
    policy = session.scheduler_policy
    mode = policy.get("mode")
    strategy = policy.get("strategy")
    if mode and strategy:
        return visual_join((str(mode), str(strategy)))
    if mode:
        return str(mode)
    if strategy:
        return str(strategy)
    adaptive = plan.preview.summary.get("adaptive")
    if isinstance(adaptive, Mapping):
        queue = adaptive.get("queue_concurrency")
        segments = adaptive.get("per_file_segments")
        if queue and segments:
            return visual_join(("adaptive", f"{queue} jobs", f"{segments} segments"))
        return "adaptive"
    return None


def _menu_plan_safety(plan: HubExecutionPlan) -> str | None:
    if plan.route.safety:
        return "; ".join(plan.route.safety)
    options = plan.options
    if isinstance(options, DirectoryMirrorOptions | SiteDownloadOptions):
        notes = ["bounded recursion"]
        if options.no_parent:
            notes.append("no parent")
        if not options.span_hosts:
            notes.append("same host")
        return visual_join(notes)
    if isinstance(options, FileDownloadOptions):
        return visual_join(("single file", "no recursion"))
    if isinstance(options, AudioDownloadOptions | VideoDownloadOptions):
        return "archive on" if options.archive else "archive off"
    return None


def _menu_plan_next_phases(plan: HubExecutionPlan) -> str:
    options = plan.options
    if isinstance(options, AudioDownloadOptions):
        return "download -> extract audio -> metadata/artwork -> finalize"
    if isinstance(options, VideoDownloadOptions):
        return "download -> merge -> metadata/thumbnail -> finalize"
    if isinstance(options, DirectoryMirrorOptions | SiteDownloadOptions):
        return "scan -> mirror -> convert/preserve -> summarize"
    return "download -> verify -> finalize"


def _enabled_label(value: bool) -> str:
    return "enabled" if value else "disabled"


def _customize_options(
    prompts: PromptUI,
    options: MenuDownloadOptions,
    *,
    console: Console,
) -> MenuDownloadOptions:
    current = options
    while True:
        overlay = cast(str | None, prompts.select("Customize", _customize_choices(current)))
        if overlay is None or overlay == "back":
            return current
        candidate = _apply_customize_overlay(prompts, current, overlay)
        try:
            current = type(candidate).model_validate(candidate.model_dump(mode="python"))
        except ValidationError as exc:
            _print_customize_validation_error(console, exc)


def _print_customize_validation_error(console: Console, exc: ValidationError) -> None:
    errors = exc.errors()
    if not errors:
        console.print(Text("Not applied: Invalid option", style=ATLAS_ERROR_STYLE))
        return
    first = errors[0]
    location = ".".join(str(part).replace("_", " ") for part in first.get("loc", ()))
    message = str(first.get("msg") or "Invalid option").removeprefix("Value error, ")
    output = Text("Not applied: ", style=ATLAS_ERROR_STYLE)
    output.append(f"{location}: " if location else "")
    output.append(message)
    console.print(output)


def _print_option_diff(
    console: Console,
    before: MenuDownloadOptions,
    after: MenuDownloadOptions,
) -> None:
    fields = _option_diff_fields(before, after)
    if not fields:
        return
    view = SmartSessionView(title="atlas", console=console)
    console.print(
        view.customization_overlay(
            title="Changed Options",
            description="The next plan or dry-run will be rebuilt with these changes.",
            options=fields,
        )
    )


def _option_diff_fields(
    before: MenuDownloadOptions,
    after: MenuDownloadOptions,
    *,
    limit: int = 12,
) -> tuple[ViewField, ...]:
    return _mapping_diff_fields(
        before.model_dump(mode="json"),
        after.model_dump(mode="json"),
        limit=limit,
    )


def _print_mapping_diff(
    console: Console,
    *,
    title: str,
    before: Mapping[str, object],
    after: Mapping[str, object],
) -> None:
    fields = _mapping_diff_fields(before, after)
    if not fields:
        return
    view = SmartSessionView(title="atlas", console=console)
    console.print(
        view.customization_overlay(
            title=title,
            description="The next plan or dry-run will be rebuilt with these changes.",
            options=fields,
        )
    )


def _mapping_diff_fields(
    before_data: Mapping[str, object],
    after_data: Mapping[str, object],
    *,
    limit: int = 12,
) -> tuple[ViewField, ...]:
    fields: list[ViewField] = []
    for key in sorted(set(before_data) | set(after_data)):
        before_value = before_data.get(key)
        after_value = after_data.get(key)
        if before_value == after_value:
            continue
        fields.append(
            ViewField(
                _option_label(key),
                f"{_option_value_label(before_value)} -> {_option_value_label(after_value)}",
                "warning",
            )
        )
        if len(fields) >= limit:
            remaining = sum(
                1
                for remaining_key in sorted(set(before_data) | set(after_data))
                if remaining_key > key
                and before_data.get(remaining_key) != after_data.get(remaining_key)
            )
            if remaining:
                fields.append(ViewField("More", f"{remaining} additional change(s)", "muted"))
            break
    return tuple(fields)


def _option_label(key: str) -> str:
    return key.replace("_", " ").title()


def _option_value_label(value: object) -> str:
    if value is None:
        return "unset"
    if value is True:
        return "yes"
    if value is False:
        return "no"
    if isinstance(value, list):
        if not value:
            return "none"
        return _short_value(", ".join(str(item) for item in value))
    if isinstance(value, dict):
        if not value:
            return "none"
        return _short_value(", ".join(f"{key}={val}" for key, val in sorted(value.items())))
    return _short_value(str(value))


def _short_value(value: str, *, limit: int = 72) -> str:
    if len(value) <= limit:
        return value
    return f"{value[: limit - 1]}..."


def _customize_choices(options: MenuDownloadOptions) -> list[MenuChoice]:
    choices: list[MenuChoice] = []
    if isinstance(options, VideoDownloadOptions):
        choices.extend(
            [
                MenuChoice("Quality", "quality"),
                MenuChoice("Format", "format"),
                MenuChoice("Details", "video-details"),
                MenuChoice("yt-dlp format", "custom-format"),
                MenuChoice("Engine", "media-engine"),
                MenuChoice("Filters", "media-selection"),
                MenuChoice("Sections", "media-sections"),
                MenuChoice("Playlist", "playlist-range"),
                MenuChoice("Metadata", "metadata"),
                MenuChoice("Output", "output"),
                MenuChoice("Cookies", "cookies"),
                MenuChoice("Subtitles", "subtitles"),
            ]
        )
    elif isinstance(options, AudioDownloadOptions):
        choices.extend(
            [
                MenuChoice("Quality", "audio-quality"),
                MenuChoice("Format", "audio-format"),
                MenuChoice("yt-dlp format", "custom-format"),
                MenuChoice("Engine", "media-engine"),
                MenuChoice("Filters", "media-selection"),
                MenuChoice("Sections", "media-sections"),
                MenuChoice("Playlist", "playlist-range"),
                MenuChoice("Metadata", "metadata"),
                MenuChoice("Output", "output"),
                MenuChoice("Cookies", "cookies"),
                MenuChoice("Subtitles", "subtitles"),
            ]
        )
    elif isinstance(options, FileDownloadOptions):
        choices.extend(
            [
                MenuChoice("Backend", "backend"),
                MenuChoice("Output", "output"),
                MenuChoice("Format", "file-format"),
                MenuChoice("Transfer", "aria2-transfer"),
                MenuChoice("Sessions", "aria2-session"),
                MenuChoice("Metalink", "metalink"),
                MenuChoice("Server stats", "server-stats"),
                MenuChoice("HTTP", "http-policy"),
            ]
        )
    else:
        choices.extend(
            [
                MenuChoice("Backend", "backend"),
                MenuChoice("Output", "output"),
                MenuChoice("Basics", "site-format"),
                MenuChoice("Scope", "site-scope"),
                MenuChoice("Discovery", "site-discovery"),
                MenuChoice("Paths", "site-layout"),
                MenuChoice("Parsers", "site-parser"),
                MenuChoice("HTTP", "site-http"),
                MenuChoice("Cookies/auth", "site-cookies"),
                MenuChoice("TLS/OCSP", "site-tls"),
                MenuChoice("Signatures", "site-gpg"),
                MenuChoice("Network", "site-network"),
                MenuChoice("Archive", "site-archive"),
                MenuChoice("Bounds", "site-bounds"),
                MenuChoice("Adaptive", "site-adaptive"),
            ]
        )
    choices.append(MenuChoice("Back", "back"))
    return choices


def _apply_customize_overlay(
    prompts: PromptUI,
    options: MenuDownloadOptions,
    overlay: str,
) -> MenuDownloadOptions:
    if isinstance(options, VideoDownloadOptions):
        if overlay == "quality":
            quality = _select_enum(prompts, "Quality", QualityIntent, options.quality)
            resolution = _select_enum(prompts, "Resolution", ResolutionChoice, options.resolution)
            return options.model_copy(update={"quality": quality, "resolution": resolution})
        if overlay == "format":
            container = _select_enum(prompts, "Container", Container, options.container)
            codec = _select_enum(prompts, "Video codec", VideoCodecChoice, options.video_codec)
            return options.model_copy(update={"container": container, "video_codec": codec})
        if overlay == "video-details":
            hdr = _select_enum(prompts, "HDR", HdrChoice, options.hdr)
            fps = _select_enum(prompts, "FPS", FpsChoice, options.fps)
            return options.model_copy(update={"hdr": hdr, "fps": fps})
        if overlay == "custom-format":
            custom_format = _optional_text(prompts, "yt-dlp format", options.format)
            return options.model_copy(update={"format": custom_format})
        if overlay == "media-engine":
            return _media_engine_overlay(prompts, options)
        if overlay == "media-selection":
            return _media_selection_overlay(prompts, options)
        if overlay == "media-sections":
            return _media_sections_overlay(prompts, options)
        if overlay == "playlist-range":
            return _playlist_overlay(prompts, options)
        if overlay == "metadata":
            return _metadata_overlay(prompts, options)
        if overlay == "output":
            return _output_overlay(prompts, options)
        if overlay == "cookies":
            return _cookies_overlay(prompts, options)
        if overlay == "subtitles":
            return _subtitle_overlay(prompts, options)
        return options

    if isinstance(options, AudioDownloadOptions):
        if overlay in {"audio-quality", "audio-format"}:
            codec = _select_enum(prompts, "Codec", AudioCodec, options.codec)
            audio_quality = _int_prompt(
                prompts,
                "Audio quality 0-10",
                default=options.quality,
                minimum=0,
                maximum=10,
            )
            return options.model_copy(update={"codec": codec, "quality": audio_quality})
        if overlay == "custom-format":
            custom_format = _optional_text(prompts, "yt-dlp format", options.format)
            return options.model_copy(update={"format": custom_format})
        if overlay == "media-engine":
            return _media_engine_overlay(prompts, options)
        if overlay == "media-selection":
            return _media_selection_overlay(prompts, options)
        if overlay == "media-sections":
            return _media_sections_overlay(prompts, options)
        if overlay == "playlist-range":
            return _playlist_overlay(prompts, options)
        if overlay == "metadata":
            return _metadata_overlay(prompts, options)
        if overlay == "output":
            return _output_overlay(prompts, options)
        if overlay == "cookies":
            return _cookies_overlay(prompts, options)
        if overlay == "subtitles":
            return _subtitle_overlay(prompts, options)
        return options

    if isinstance(options, FileDownloadOptions):
        if overlay == "backend":
            backend = _select_enum(prompts, "Backend", FileBackendChoice, options.backend)
            return options.model_copy(update={"backend": backend})
        if overlay == "output":
            return _output_overlay(prompts, options)
        if overlay == "file-format":
            filename = _optional_text(prompts, "Filename override", options.filename)
            overwrite = prompts.confirm("Overwrite existing file?", default=options.overwrite)
            checksum = _optional_text(prompts, "Checksum sha256:<hex>", options.checksum)
            return options.model_copy(
                update={
                    "filename": filename,
                    "overwrite": options.overwrite if overwrite is None else overwrite,
                    "checksum": checksum,
                }
            )
        if overlay == "aria2-transfer":
            return _file_transfer_overlay(prompts, options)
        if overlay == "aria2-session":
            return _file_session_overlay(prompts, options)
        if overlay == "metalink":
            return _file_metalink_overlay(prompts, options)
        if overlay == "server-stats":
            return _file_server_stats_overlay(prompts, options)
        if overlay == "http-policy":
            return _file_http_policy_overlay(prompts, options)
        return options

    if overlay == "backend":
        return _site_backend_overlay(prompts, options)
    if overlay == "output":
        return _output_overlay(prompts, options)
    if overlay == "site-format":
        return _site_format_overlay(prompts, options)
    if overlay == "site-scope":
        return _site_scope_overlay(prompts, options)
    if overlay == "site-discovery":
        return _site_discovery_overlay(prompts, options)
    if overlay == "site-layout":
        return _site_layout_overlay(prompts, options)
    if overlay == "site-parser":
        return _site_parser_overlay(prompts, options)
    if overlay == "site-http":
        return _site_http_overlay(prompts, options)
    if overlay == "site-cookies":
        return _site_cookies_overlay(prompts, options)
    if overlay == "site-tls":
        return _site_tls_overlay(prompts, options)
    if overlay == "site-gpg":
        return _site_gpg_overlay(prompts, options)
    if overlay == "site-network":
        return _site_network_overlay(prompts, options)
    if overlay == "site-archive":
        return _site_archive_overlay(prompts, options)
    if overlay == "site-bounds":
        return _site_bounds_overlay(prompts, options)
    if overlay == "site-adaptive":
        return _site_adaptive_overlay(prompts, options)
    return options


def _media_engine_overlay(
    prompts: PromptUI,
    options: VideoDownloadOptions | AudioDownloadOptions,
) -> VideoDownloadOptions | AudioDownloadOptions:
    use_aria2 = prompts.confirm("Use aria2 for HTTP/HTTPS?", default=options.use_aria2)
    download_engine = _select_enum(
        prompts,
        "Engine",
        DownloadEngineChoice,
        options.download_engine,
    )
    connections = _int_prompt(
        prompts, "Aria2 connections", default=options.connections, minimum=1, maximum=64
    )
    splits = _int_prompt(prompts, "Aria2 splits", default=options.splits, minimum=1, maximum=64)
    chunk_size = (
        _optional_text(prompts, "Aria2 chunk size", options.chunk_size) or options.chunk_size
    )
    retries = _int_prompt(prompts, "Retries", default=options.retries, minimum=0, maximum=1000)
    fragment_retries = _int_prompt(
        prompts,
        "Fragment retries",
        default=options.fragment_retries,
        minimum=0,
        maximum=1000,
    )
    file_access_retries = _int_prompt(
        prompts,
        "File access retries",
        default=options.file_access_retries,
        minimum=0,
        maximum=1000,
    )
    concurrent_fragments = _int_prompt(
        prompts,
        "Concurrent fragments",
        default=options.concurrent_fragments,
        minimum=1,
        maximum=64,
    )
    retry_sleep = _list_prompt(prompts, "Retry sleep entries", options.retry_sleep)
    skip_unavailable = prompts.confirm(
        "Skip unavailable fragments?",
        default=options.skip_unavailable_fragments,
    )
    rate_limit = _optional_text(prompts, "Rate limit", options.rate_limit)
    throttled_rate = _optional_text(prompts, "Throttled rate", options.throttled_rate)
    http_chunk_size = _optional_text(prompts, "Native HTTP chunk size", options.http_chunk_size)
    socket_timeout = _optional_float_prompt(prompts, "Socket timeout", options.socket_timeout)
    source_address = _optional_text(prompts, "Source address", options.source_address)
    impersonate = _optional_text(prompts, "Impersonate target", options.impersonate)
    extractor_args = _list_prompt(
        prompts, "Extractor args (separate with |)", options.extractor_args
    )
    sleep = _optional_float_prompt(prompts, "Sleep before download", options.sleep)
    proxy = _optional_text(prompts, "Proxy", options.proxy)
    return options.model_copy(
        update={
            "use_aria2": options.use_aria2 if use_aria2 is None else use_aria2,
            "download_engine": download_engine,
            "connections": connections,
            "splits": splits,
            "chunk_size": chunk_size,
            "retries": retries,
            "fragment_retries": fragment_retries,
            "file_access_retries": file_access_retries,
            "concurrent_fragments": concurrent_fragments,
            "retry_sleep": retry_sleep,
            "skip_unavailable_fragments": (
                options.skip_unavailable_fragments if skip_unavailable is None else skip_unavailable
            ),
            "rate_limit": rate_limit,
            "throttled_rate": throttled_rate,
            "http_chunk_size": http_chunk_size,
            "socket_timeout": socket_timeout,
            "source_address": source_address,
            "impersonate": impersonate,
            "extractor_args": extractor_args,
            "sleep": sleep,
            "proxy": proxy,
        }
    )


def _media_selection_overlay(
    prompts: PromptUI,
    options: VideoDownloadOptions | AudioDownloadOptions,
) -> VideoDownloadOptions | AudioDownloadOptions:
    max_downloads = _optional_int_prompt(prompts, "Max downloads", options.max_downloads)
    break_on_existing = prompts.confirm(
        "Break on existing archive item?",
        default=options.break_on_existing,
    )
    break_on_reject = prompts.confirm("Break on rejected media?", default=options.break_on_reject)
    break_per_input = prompts.confirm(
        "Reset break counters per input?", default=options.break_per_input
    )
    reject_live = prompts.confirm("Reject active livestreams?", default=options.reject_live)
    reject_upcoming = prompts.confirm(
        "Reject upcoming livestreams?", default=options.reject_upcoming
    )
    live_from_start = prompts.confirm("Download live from start?", default=options.live_from_start)
    return options.model_copy(
        update={
            "match_filters": _list_prompt(
                prompts, "Match filters (separate with |)", options.match_filters
            ),
            "break_match_filters": _list_prompt(
                prompts,
                "Break match filters (separate with |)",
                options.break_match_filters,
            ),
            "max_downloads": max_downloads,
            "break_on_existing": (
                options.break_on_existing if break_on_existing is None else break_on_existing
            ),
            "break_on_reject": options.break_on_reject
            if break_on_reject is None
            else break_on_reject,
            "break_per_input": options.break_per_input
            if break_per_input is None
            else break_per_input,
            "date": _optional_text(prompts, "Exact upload date", options.date),
            "date_before": _optional_text(prompts, "Upload date before", options.date_before),
            "date_after": _optional_text(prompts, "Upload date after", options.date_after),
            "min_filesize": _optional_text(prompts, "Minimum filesize", options.min_filesize),
            "max_filesize": _optional_text(prompts, "Maximum filesize", options.max_filesize),
            "reject_live": options.reject_live if reject_live is None else reject_live,
            "reject_upcoming": options.reject_upcoming
            if reject_upcoming is None
            else reject_upcoming,
            "live_from_start": options.live_from_start
            if live_from_start is None
            else live_from_start,
        }
    )


def _media_sections_overlay(
    prompts: PromptUI,
    options: VideoDownloadOptions | AudioDownloadOptions,
) -> VideoDownloadOptions | AudioDownloadOptions:
    return options.model_copy(
        update={
            "download_sections": _list_prompt(
                prompts,
                "Download sections (separate with |)",
                options.download_sections,
            ),
            "sponsorblock_mark": _list_prompt(
                prompts,
                "SponsorBlock mark categories",
                options.sponsorblock_mark,
            ),
            "sponsorblock_remove": _list_prompt(
                prompts,
                "SponsorBlock remove categories",
                options.sponsorblock_remove,
            ),
            "sponsorblock_chapter_title": _optional_text(
                prompts,
                "SponsorBlock chapter title",
                options.sponsorblock_chapter_title,
            ),
            "sponsorblock_api": _optional_text(
                prompts,
                "SponsorBlock API",
                options.sponsorblock_api,
            ),
        }
    )


def _playlist_overlay(
    prompts: PromptUI,
    options: VideoDownloadOptions | AudioDownloadOptions,
) -> VideoDownloadOptions | AudioDownloadOptions:
    playlist = prompts.confirm("Allow playlist?", default=options.playlist)
    organize = _select_enum(prompts, "Organization", OrganizeMode, options.organize)
    playlist_items = options.playlist_items
    playlist_start = options.playlist_start
    playlist_end = options.playlist_end
    item_mode = cast(
        str | None,
        prompts.select(
            "Playlist items",
            [
                MenuChoice("All items", "all"),
                MenuChoice("Type range / start-end", "range"),
                MenuChoice("Choose item numbers", "selected"),
                MenuChoice("Keep current", "keep"),
            ],
        ),
    )
    if item_mode == "all":
        playlist_items = None
        playlist_start = None
        playlist_end = None
    elif item_mode == "range":
        playlist_items = _optional_text(prompts, "Playlist item range", playlist_items)
        playlist_start = _optional_int_prompt(prompts, "Playlist start", playlist_start)
        playlist_end = _optional_int_prompt(prompts, "Playlist end", playlist_end)
    elif item_mode == "selected":
        selected_items = prompts.multi_select(
            "Choose playlist items",
            _playlist_item_number_choices(),
        )
        if selected_items:
            playlist_items = ",".join(str(item) for item in selected_items)
            playlist_start = None
            playlist_end = None
    return options.model_copy(
        update={
            "playlist": options.playlist if playlist is None else playlist,
            "playlist_items": playlist_items,
            "playlist_start": playlist_start,
            "playlist_end": playlist_end,
            "organize": organize,
        }
    )


def _playlist_item_number_choices(*, limit: int = 50) -> list[MenuChoice]:
    return [MenuChoice(f"Item {index}", str(index)) for index in range(1, limit + 1)]


def _metadata_overlay(
    prompts: PromptUI,
    options: VideoDownloadOptions | AudioDownloadOptions,
) -> VideoDownloadOptions | AudioDownloadOptions:
    info_json = prompts.confirm("Write info JSON?", default=options.write_info_json)
    thumbnail = prompts.confirm("Write thumbnail?", default=options.write_thumbnail)
    embed_thumbnail = prompts.confirm("Embed thumbnail?", default=options.embed_thumbnail)
    metadata = prompts.confirm("Embed metadata?", default=options.embed_metadata)
    chapters = prompts.confirm("Preserve chapters?", default=options.chapters)
    split_chapters = prompts.confirm("Split chapters?", default=options.split_chapters)
    restrict_filenames = prompts.confirm("Restrict filenames?", default=options.restrict_filenames)
    overwrite = prompts.confirm("Overwrite existing media?", default=options.overwrite)
    continue_download = prompts.confirm(
        "Resume partial downloads?", default=options.continue_download
    )
    return options.model_copy(
        update={
            "write_info_json": options.write_info_json if info_json is None else info_json,
            "write_thumbnail": options.write_thumbnail if thumbnail is None else thumbnail,
            "embed_thumbnail": (
                options.embed_thumbnail if embed_thumbnail is None else embed_thumbnail
            ),
            "embed_metadata": options.embed_metadata if metadata is None else metadata,
            "chapters": options.chapters if chapters is None else chapters,
            "split_chapters": options.split_chapters if split_chapters is None else split_chapters,
            "restrict_filenames": (
                options.restrict_filenames if restrict_filenames is None else restrict_filenames
            ),
            "overwrite": options.overwrite if overwrite is None else overwrite,
            "continue_download": (
                options.continue_download if continue_download is None else continue_download
            ),
            "filename_template": _optional_text(
                prompts,
                "Filename template",
                options.filename_template,
            ),
        }
    )


def _subtitle_overlay(
    prompts: PromptUI,
    options: VideoDownloadOptions | AudioDownloadOptions,
) -> VideoDownloadOptions | AudioDownloadOptions:
    subtitle_mode = _select_enum(
        prompts,
        "Subtitles",
        SubtitleMode,
        options.subtitle_mode,
    )
    sub_lang = _optional_text(prompts, "Subtitle language", options.sub_lang)
    embed_subs = prompts.confirm("Embed subtitles?", default=options.embed_subs)
    return options.model_copy(
        update={
            "subtitle_mode": subtitle_mode,
            "sub_lang": sub_lang,
            "embed_subs": options.embed_subs if embed_subs is None else embed_subs,
        }
    )


def _file_transfer_overlay(prompts: PromptUI, options: FileDownloadOptions) -> FileDownloadOptions:
    continue_download = prompts.confirm(
        "Resume partial downloads?", default=options.continue_download
    )
    check_integrity = prompts.confirm("Check integrity?", default=options.check_integrity)
    remote_time = prompts.confirm("Use remote timestamp?", default=options.remote_time)
    conditional_get = prompts.confirm("Use conditional GET?", default=options.conditional_get)
    http_accept_gzip = prompts.confirm("Accept aria2 gzip?", default=options.http_accept_gzip)
    return options.model_copy(
        update={
            "connections": _int_prompt(
                prompts, "Connections", default=options.connections, minimum=1, maximum=64
            ),
            "splits": _int_prompt(prompts, "Splits", default=options.splits, minimum=1, maximum=64),
            "chunk_size": _optional_text(prompts, "Chunk size", options.chunk_size)
            or options.chunk_size,
            "timeout": _float_prompt(prompts, "Timeout", default=options.timeout, minimum=0),
            "connect_timeout": _optional_float_prompt(
                prompts,
                "Connect timeout",
                options.connect_timeout,
            ),
            "continue_download": (
                options.continue_download if continue_download is None else continue_download
            ),
            "rate_limit": _optional_text(prompts, "Rate limit", options.rate_limit),
            "lowest_speed_limit": _optional_text(
                prompts,
                "Lowest speed limit",
                options.lowest_speed_limit,
            ),
            "max_tries": _optional_int_prompt(prompts, "Max tries", options.max_tries),
            "retry_wait": _optional_float_prompt(prompts, "Retry wait", options.retry_wait),
            "file_allocation": _optional_text(prompts, "File allocation", options.file_allocation),
            "check_integrity": options.check_integrity
            if check_integrity is None
            else check_integrity,
            "remote_time": options.remote_time if remote_time is None else remote_time,
            "conditional_get": (
                options.conditional_get if conditional_get is None else conditional_get
            ),
            "http_accept_gzip": (
                options.http_accept_gzip if http_accept_gzip is None else http_accept_gzip
            ),
        }
    )


def _file_session_overlay(prompts: PromptUI, options: FileDownloadOptions) -> FileDownloadOptions:
    return options.model_copy(
        update={
            "input_file": _optional_path_prompt(prompts, "Input/session file", options.input_file),
            "save_session": _optional_path_prompt(
                prompts, "Save session file", options.save_session
            ),
            "save_session_interval": _optional_int_prompt(
                prompts,
                "Save session interval",
                options.save_session_interval,
            ),
        }
    )


def _file_metalink_overlay(prompts: PromptUI, options: FileDownloadOptions) -> FileDownloadOptions:
    metalink = prompts.confirm("Expand Metalink manifests?", default=options.metalink)
    force_metalink = prompts.confirm("Force Metalink mode?", default=options.force_metalink)
    unique_protocol = prompts.confirm(
        "Enable unique Metalink protocol?",
        default=bool(options.metalink_enable_unique_protocol),
    )
    return options.model_copy(
        update={
            "metalink": options.metalink if metalink is None else metalink,
            "force_metalink": options.force_metalink if force_metalink is None else force_metalink,
            "metalink_preferred_protocol": _select_optional_enum(
                prompts,
                "Preferred Metalink protocol",
                MetalinkPreferredProtocol,
                options.metalink_preferred_protocol,
            ),
            "metalink_language": _optional_text(
                prompts,
                "Metalink language",
                options.metalink_language,
            ),
            "metalink_os": _optional_text(prompts, "Metalink OS", options.metalink_os),
            "metalink_location": _optional_text(
                prompts,
                "Metalink location",
                options.metalink_location,
            ),
            "metalink_base_uri": _optional_text(
                prompts,
                "Metalink base URI",
                options.metalink_base_uri,
            ),
            "metalink_enable_unique_protocol": (
                options.metalink_enable_unique_protocol
                if unique_protocol is None
                else unique_protocol
            ),
        }
    )


def _file_server_stats_overlay(
    prompts: PromptUI,
    options: FileDownloadOptions,
) -> FileDownloadOptions:
    return options.model_copy(
        update={
            "server_stat_if": _optional_path_prompt(
                prompts,
                "Server stat input file",
                options.server_stat_if,
            ),
            "server_stat_of": _optional_path_prompt(
                prompts,
                "Server stat output file",
                options.server_stat_of,
            ),
            "server_stat_timeout": _optional_int_prompt(
                prompts,
                "Server stat timeout",
                options.server_stat_timeout,
            ),
            "uri_selector": _select_optional_enum(
                prompts,
                "URI selector",
                Aria2UriSelector,
                options.uri_selector,
            ),
        }
    )


def _file_http_policy_overlay(
    prompts: PromptUI,
    options: FileDownloadOptions,
) -> FileDownloadOptions:
    cache = prompts.confirm("Allow HTTP cache?", default=bool(options.cache))
    no_compression = prompts.confirm("Disable compression?", default=options.no_compression)
    check_certificate = prompts.confirm(
        "Check certificate?",
        default=True if options.check_certificate is None else options.check_certificate,
    )
    return options.model_copy(
        update={
            "user_agent": _optional_text(prompts, "User agent", options.user_agent),
            "headers": tuple(
                _list_prompt(prompts, "Headers (separate with |)", list(options.headers))
            ),
            "referer": _optional_text(prompts, "Referer", options.referer),
            "cache": options.cache if cache is None else cache,
            "compression": _optional_text(prompts, "Accept-Encoding", options.compression),
            "no_compression": options.no_compression if no_compression is None else no_compression,
            "method": _optional_text(prompts, "HTTP method", options.method) or options.method,
            "body_data": _optional_text(prompts, "Body data", options.body_data),
            "body_file": _optional_path_prompt(prompts, "Body file", options.body_file),
            "load_cookies": _optional_path_prompt(prompts, "Load cookies", options.load_cookies),
            "proxy": _optional_text(prompts, "Proxy", options.proxy),
            "http_user": _optional_text(prompts, "HTTP user", options.http_user),
            "http_password": _optional_text(prompts, "HTTP password", options.http_password),
            "check_certificate": (
                options.check_certificate if check_certificate is None else check_certificate
            ),
            "ca_certificate": _optional_path_prompt(
                prompts,
                "CA certificate",
                options.ca_certificate,
            ),
            "ca_directory": _optional_path_prompt(prompts, "CA directory", options.ca_directory),
            "certificate": _optional_path_prompt(
                prompts, "Client certificate", options.certificate
            ),
            "private_key": _optional_path_prompt(prompts, "Private key", options.private_key),
            "secure_protocol": _optional_text(prompts, "Secure protocol", options.secure_protocol),
        }
    )


def _site_format_overlay(prompts: PromptUI, options: SiteDownloadOptions) -> SiteDownloadOptions:
    depth = _int_prompt(
        prompts,
        "Mirror depth",
        default=options.depth,
        minimum=1,
        maximum=20,
    )
    keep_html = prompts.confirm("Keep HTML/index pages?", default=not _rejects_html(options.reject))
    assets = prompts.confirm("Fetch page assets?", default=options.page_requisites)
    convert_links = prompts.confirm("Convert links for offline use?", default=options.convert_links)
    wait = _optional_float_prompt(prompts, "Wait between requests", options.wait)
    spider = prompts.confirm("Spider/check only?", default=options.spider)
    return options.model_copy(
        update={
            "depth": depth,
            "reject": _set_reject_html(
                options.reject,
                keep=not _rejects_html(options.reject) if keep_html is None else keep_html,
            ),
            "page_requisites": options.page_requisites if assets is None else assets,
            "convert_links": options.convert_links if convert_links is None else convert_links,
            "wait": wait,
            "spider": options.spider if spider is None else spider,
        }
    )


def _site_scope_overlay(prompts: PromptUI, options: SiteDownloadOptions) -> SiteDownloadOptions:
    selected = cast(
        str | None,
        prompts.select(
            "Mirror scope",
            [
                MenuChoice("Keep current", "keep"),
                MenuChoice("Same host only", "same-host"),
                MenuChoice("Same domain + www", "same-domain-www"),
                MenuChoice("Include subdomains", "subdomains"),
                MenuChoice("Custom domains", "custom"),
            ],
        ),
    )
    update: dict[str, object] = {}
    if selected == "same-host":
        update.update({"span_hosts": False, "domains": None})
    elif selected == "same-domain-www":
        update.update({"span_hosts": True, "domains": _default_domains_for_url(options.url)})
    elif selected == "subdomains":
        update.update({"span_hosts": True, "domains": _seed_domain(options.url) or options.domains})
    elif selected == "custom":
        span_hosts = prompts.confirm("Span hosts?", default=options.span_hosts)
        update.update(
            {
                "span_hosts": options.span_hosts if span_hosts is None else span_hosts,
                "domains": _optional_text(prompts, "Domains", options.domains),
                "exclude_domains": _optional_text(
                    prompts,
                    "Exclude domains",
                    options.exclude_domains,
                ),
            }
        )
    no_parent = prompts.confirm("Stay below parent directory?", default=options.no_parent)
    if no_parent is not None:
        update["no_parent"] = no_parent
    return options.model_copy(update=update)


def _site_discovery_overlay(prompts: PromptUI, options: SiteDownloadOptions) -> SiteDownloadOptions:
    robots = prompts.confirm("Respect robots.txt?", default=options.robots)
    follow_sitemaps = prompts.confirm("Follow sitemaps?", default=options.follow_sitemaps)
    no_parent = prompts.confirm("Stay below parent directory?", default=options.no_parent)
    filter_urls = prompts.confirm("Apply filters to full URLs?", default=options.filter_urls)
    ignore_case = prompts.confirm("Ignore case in filters?", default=options.ignore_case)
    return options.model_copy(
        update={
            "accept": _optional_text(prompts, "Accept suffixes", options.accept),
            "reject": _optional_text(prompts, "Reject suffixes", options.reject),
            "robots": options.robots if robots is None else robots,
            "follow_sitemaps": (
                options.follow_sitemaps if follow_sitemaps is None else follow_sitemaps
            ),
            "no_parent": options.no_parent if no_parent is None else no_parent,
            "domains": _optional_text(prompts, "Domains", options.domains),
            "exclude_domains": _optional_text(prompts, "Exclude domains", options.exclude_domains),
            "include_directories": _optional_text(
                prompts,
                "Include directories",
                options.include_directories,
            ),
            "exclude_directories": _optional_text(
                prompts,
                "Exclude directories",
                options.exclude_directories,
            ),
            "accept_regex": _optional_text(prompts, "Accept regex", options.accept_regex),
            "reject_regex": _optional_text(prompts, "Reject regex", options.reject_regex),
            "filter_mime_type": _optional_text(
                prompts,
                "MIME type filter",
                options.filter_mime_type,
            ),
            "filter_urls": options.filter_urls if filter_urls is None else filter_urls,
            "ignore_case": options.ignore_case if ignore_case is None else ignore_case,
            "follow_tags": _optional_text(prompts, "Follow tag/attrs", options.follow_tags),
            "ignore_tags": _optional_text(prompts, "Ignore tag/attrs", options.ignore_tags),
        }
    )


def _site_layout_overlay(prompts: PromptUI, options: SiteDownloadOptions) -> SiteDownloadOptions:
    adjust_extension = prompts.confirm("Adjust extension?", default=options.adjust_extension)
    continue_download = prompts.confirm(
        "Continue partial downloads?",
        default=options.continue_download,
    )
    overwrite = prompts.confirm("Overwrite mirror files?", default=options.overwrite)
    convert_file_only = prompts.confirm(
        "Convert file part only?",
        default=options.convert_file_only,
    )
    cut_url_get_vars = prompts.confirm("Cut GET vars from URLs?", default=options.cut_url_get_vars)
    cut_file_get_vars = prompts.confirm(
        "Cut GET vars from filenames?",
        default=options.cut_file_get_vars,
    )
    keep_extension = prompts.confirm("Keep extension on clobber?", default=options.keep_extension)
    unlink = prompts.confirm("Unlink before clobber?", default=options.unlink)
    backup_converted = prompts.confirm("Backup converted files?", default=options.backup_converted)
    return options.model_copy(
        update={
            "directories": _optional_bool_prompt(
                prompts,
                "Preserve directories",
                options.directories,
            ),
            "host_directories": _optional_bool_prompt(
                prompts,
                "Include host directories",
                options.host_directories,
            ),
            "protocol_directories": _optional_bool_prompt(
                prompts,
                "Include protocol directories",
                options.protocol_directories,
            ),
            "cut_dirs": _optional_int_prompt(prompts, "Cut leading directories", options.cut_dirs),
            "default_page": _optional_text(prompts, "Default page name", options.default_page),
            "adjust_extension": (
                options.adjust_extension if adjust_extension is None else adjust_extension
            ),
            "continue_download": (
                options.continue_download if continue_download is None else continue_download
            ),
            "overwrite": options.overwrite if overwrite is None else overwrite,
            "convert_file_only": (
                options.convert_file_only if convert_file_only is None else convert_file_only
            ),
            "cut_url_get_vars": (
                options.cut_url_get_vars if cut_url_get_vars is None else cut_url_get_vars
            ),
            "cut_file_get_vars": (
                options.cut_file_get_vars if cut_file_get_vars is None else cut_file_get_vars
            ),
            "keep_extension": options.keep_extension if keep_extension is None else keep_extension,
            "unlink": options.unlink if unlink is None else unlink,
            "backups": _optional_int_prompt(prompts, "Backup count", options.backups),
            "backup_converted": (
                options.backup_converted if backup_converted is None else backup_converted
            ),
            "restrict_file_names": _optional_text(
                prompts,
                "Restrict file names",
                options.restrict_file_names,
            ),
            "download_attr": _select_optional_enum(
                prompts,
                "Download attribute mode",
                DownloadAttrMode,
                options.download_attr,
            ),
        }
    )


def _site_parser_overlay(prompts: PromptUI, options: SiteDownloadOptions) -> SiteDownloadOptions:
    input_file_only = prompts.confirm("Use only input file URLs?", default=options.input_file_only)
    force_html = prompts.confirm("Force HTML parser?", default=options.force_html)
    force_css = prompts.confirm("Force CSS parser?", default=options.force_css)
    force_sitemap = prompts.confirm("Force sitemap parser?", default=options.force_sitemap)
    force_atom = prompts.confirm("Force Atom parser?", default=options.force_atom)
    force_rss = prompts.confirm("Force RSS parser?", default=options.force_rss)
    force_metalink = prompts.confirm("Force Metalink parser?", default=options.force_metalink)
    return options.model_copy(
        update={
            "input_file": _optional_path_prompt(prompts, "Input file", options.input_file),
            "input_file_only": (
                options.input_file_only if input_file_only is None else input_file_only
            ),
            "base": _optional_text(prompts, "Base URL", options.base),
            "force_html": options.force_html if force_html is None else force_html,
            "force_css": options.force_css if force_css is None else force_css,
            "force_sitemap": options.force_sitemap if force_sitemap is None else force_sitemap,
            "force_atom": options.force_atom if force_atom is None else force_atom,
            "force_rss": options.force_rss if force_rss is None else force_rss,
            "force_metalink": options.force_metalink if force_metalink is None else force_metalink,
        }
    )


def _site_http_overlay(prompts: PromptUI, options: SiteDownloadOptions) -> SiteDownloadOptions:
    no_compression = prompts.confirm("Disable compression?", default=options.no_compression)
    content_on_error = prompts.confirm(
        "Save content on HTTP errors?",
        default=options.content_on_error,
    )
    save_headers = prompts.confirm("Save headers?", default=options.save_headers)
    server_response = prompts.confirm("Print server response?", default=options.server_response)
    ignore_length = prompts.confirm("Ignore content length?", default=options.ignore_length)
    return options.model_copy(
        update={
            "user_agent": _optional_text(prompts, "User agent", options.user_agent),
            "headers": tuple(
                _list_prompt(prompts, "Headers (separate with |)", list(options.headers))
            ),
            "referer": _optional_text(prompts, "Referer", options.referer),
            "cache": _optional_bool_prompt(prompts, "Allow HTTP cache", options.cache),
            "compression": _optional_text(prompts, "Accept-Encoding", options.compression),
            "no_compression": options.no_compression if no_compression is None else no_compression,
            "method": _optional_text(prompts, "HTTP method", options.method) or options.method,
            "body_data": _optional_text(prompts, "Body data", options.body_data),
            "body_file": _optional_path_prompt(prompts, "Body file", options.body_file),
            "post_data": _optional_text(prompts, "POST data", options.post_data),
            "post_file": _optional_path_prompt(prompts, "POST file", options.post_file),
            "content_on_error": (
                options.content_on_error if content_on_error is None else content_on_error
            ),
            "save_content_on": _optional_text(
                prompts,
                "Save content on statuses",
                options.save_content_on,
            ),
            "save_headers": options.save_headers if save_headers is None else save_headers,
            "server_response": (
                options.server_response if server_response is None else server_response
            ),
            "ignore_length": options.ignore_length if ignore_length is None else ignore_length,
            "quota": _optional_text(prompts, "Quota", options.quota),
            "limit_rate": _optional_text(prompts, "Limit rate", options.limit_rate),
            "start_pos": _optional_text(prompts, "Start position", options.start_pos),
        }
    )


def _site_cookies_overlay(prompts: PromptUI, options: SiteDownloadOptions) -> SiteDownloadOptions:
    keep_session_cookies = prompts.confirm(
        "Keep session cookies?",
        default=options.keep_session_cookies,
    )
    return options.model_copy(
        update={
            "cookies": _optional_bool_prompt(prompts, "Enable cookies", options.cookies),
            "browser_cookies": _site_browser_cookies_prompt(prompts, options.browser_cookies),
            "load_cookies": _optional_path_prompt(prompts, "Load cookies", options.load_cookies),
            "save_cookies": _optional_path_prompt(prompts, "Save cookies", options.save_cookies),
            "keep_session_cookies": (
                options.keep_session_cookies
                if keep_session_cookies is None
                else keep_session_cookies
            ),
            "cookie_suffixes": _optional_text(prompts, "Cookie suffixes", options.cookie_suffixes),
            "netrc": _optional_bool_prompt(prompts, "Use netrc", options.netrc),
            "netrc_file": _optional_path_prompt(prompts, "Netrc file", options.netrc_file),
            "proxy": _optional_bool_prompt(prompts, "Use proxy", options.proxy),
            "http_user": _optional_text(prompts, "HTTP user", options.http_user),
            "http_password": _optional_text(prompts, "HTTP password", options.http_password),
            "proxy_user": _optional_text(prompts, "Proxy user", options.proxy_user),
            "proxy_password": _optional_text(prompts, "Proxy password", options.proxy_password),
        }
    )


def _site_tls_overlay(prompts: PromptUI, options: SiteDownloadOptions) -> SiteDownloadOptions:
    https_only = prompts.confirm("HTTPS only?", default=options.https_only)
    http2_only = prompts.confirm("HTTP/2 only?", default=options.http2_only)
    return options.model_copy(
        update={
            "https_only": options.https_only if https_only is None else https_only,
            "https_enforce": _select_optional_enum(
                prompts,
                "HTTPS enforce",
                HttpsEnforceMode,
                options.https_enforce,
            ),
            "hsts": _optional_bool_prompt(prompts, "Use HSTS", options.hsts),
            "hsts_file": _optional_path_prompt(prompts, "HSTS file", options.hsts_file),
            "check_certificate": _optional_bool_prompt(
                prompts,
                "Check certificate",
                options.check_certificate,
            ),
            "check_hostname": _optional_bool_prompt(
                prompts,
                "Check certificate hostname",
                options.check_hostname,
            ),
            "ca_certificate": _optional_path_prompt(
                prompts,
                "CA certificate",
                options.ca_certificate,
            ),
            "ca_directory": _optional_path_prompt(prompts, "CA directory", options.ca_directory),
            "certificate": _optional_path_prompt(
                prompts,
                "Client certificate",
                options.certificate,
            ),
            "certificate_type": _select_optional_enum(
                prompts,
                "Certificate type",
                CertificateType,
                options.certificate_type,
            ),
            "private_key": _optional_path_prompt(prompts, "Private key", options.private_key),
            "private_key_type": _select_optional_enum(
                prompts,
                "Private key type",
                CertificateType,
                options.private_key_type,
            ),
            "crl_file": _optional_path_prompt(prompts, "CRL file", options.crl_file),
            "secure_protocol": _optional_text(prompts, "Secure protocol", options.secure_protocol),
            "ocsp": _optional_bool_prompt(prompts, "Use OCSP", options.ocsp),
            "ocsp_date": _optional_bool_prompt(prompts, "Check OCSP date", options.ocsp_date),
            "ocsp_file": _optional_path_prompt(prompts, "OCSP file", options.ocsp_file),
            "ocsp_nonce": _optional_bool_prompt(prompts, "Use OCSP nonce", options.ocsp_nonce),
            "ocsp_server": _optional_text(prompts, "OCSP server", options.ocsp_server),
            "ocsp_stapling": _optional_bool_prompt(
                prompts,
                "Use OCSP stapling",
                options.ocsp_stapling,
            ),
            "tls_false_start": _optional_bool_prompt(
                prompts,
                "Use TLS false start",
                options.tls_false_start,
            ),
            "tls_resume": _optional_bool_prompt(prompts, "Use TLS resume", options.tls_resume),
            "tls_session_file": _optional_path_prompt(
                prompts,
                "TLS session file",
                options.tls_session_file,
            ),
            "http2": _optional_bool_prompt(prompts, "Use HTTP/2", options.http2),
            "http2_only": options.http2_only if http2_only is None else http2_only,
            "http2_request_window": _optional_int_prompt(
                prompts,
                "HTTP/2 request window",
                options.http2_request_window,
            ),
        }
    )


def _site_gpg_overlay(prompts: PromptUI, options: SiteDownloadOptions) -> SiteDownloadOptions:
    verify_save_failed = prompts.confirm(
        "Save failed signature targets?",
        default=options.verify_save_failed,
    )
    return options.model_copy(
        update={
            "verify_sig": _select_optional_enum(
                prompts,
                "Verify signatures",
                VerifySigMode,
                options.verify_sig,
            ),
            "signature_extensions": _optional_text(
                prompts,
                "Signature extensions",
                options.signature_extensions,
            ),
            "gnupg_homedir": _optional_path_prompt(prompts, "GnuPG home", options.gnupg_homedir),
            "verify_save_failed": (
                options.verify_save_failed if verify_save_failed is None else verify_save_failed
            ),
        }
    )


def _site_network_overlay(prompts: PromptUI, options: SiteDownloadOptions) -> SiteDownloadOptions:
    retry_connrefused = prompts.confirm(
        "Retry connection refused?",
        default=options.retry_connrefused,
    )
    inet4_only = prompts.confirm("IPv4 only?", default=options.inet4_only)
    inet6_only = prompts.confirm("IPv6 only?", default=options.inet6_only)
    random_wait = prompts.confirm("Random wait?", default=options.random_wait)
    timestamping = prompts.confirm("Timestamping?", default=options.timestamping)
    return options.model_copy(
        update={
            "retry_connrefused": (
                options.retry_connrefused if retry_connrefused is None else retry_connrefused
            ),
            "inet4_only": options.inet4_only if inet4_only is None else inet4_only,
            "inet6_only": options.inet6_only if inet6_only is None else inet6_only,
            "bind_address": _optional_text(prompts, "Bind address", options.bind_address),
            "bind_interface": _optional_text(prompts, "Bind interface", options.bind_interface),
            "prefer_family": _select_optional_enum(
                prompts,
                "Prefer address family",
                PreferFamily,
                options.prefer_family,
            ),
            "dns_cache": _optional_bool_prompt(prompts, "DNS cache", options.dns_cache),
            "dns_cache_preload": _optional_path_prompt(
                prompts,
                "DNS cache preload file",
                options.dns_cache_preload,
            ),
            "tcp_fastopen": _optional_bool_prompt(prompts, "TCP Fast Open", options.tcp_fastopen),
            "max_threads": _int_prompt(
                prompts,
                "Max Wget2 threads",
                default=options.max_threads,
                minimum=1,
                maximum=100,
            ),
            "tries": _int_prompt(
                prompts,
                "Tries",
                default=options.tries,
                minimum=0,
                maximum=1000,
            ),
            "waitretry": _float_prompt(
                prompts,
                "Retry wait",
                default=options.waitretry,
                minimum=0,
            ),
            "retry_on_http_error": _optional_text(
                prompts,
                "Retry on HTTP errors",
                options.retry_on_http_error,
            ),
            "max_redirect": _int_prompt(
                prompts,
                "Max redirects",
                default=options.max_redirect,
                minimum=0,
                maximum=1000,
            ),
            "timeout": _optional_float_prompt(prompts, "Timeout", options.timeout),
            "dns_timeout": _optional_float_prompt(prompts, "DNS timeout", options.dns_timeout),
            "connect_timeout": _optional_float_prompt(
                prompts,
                "Connect timeout",
                options.connect_timeout,
            ),
            "read_timeout": _optional_float_prompt(prompts, "Read timeout", options.read_timeout),
            "random_wait": options.random_wait if random_wait is None else random_wait,
            "timestamping": options.timestamping if timestamping is None else timestamping,
        }
    )


def _site_archive_overlay(prompts: PromptUI, options: SiteDownloadOptions) -> SiteDownloadOptions:
    warc_cdx = prompts.confirm("Write WARC CDX?", default=options.warc_cdx)
    stats = prompts.confirm("Collect Wget2 stats?", default=options.stats)
    return options.model_copy(
        update={
            "warc_file": _optional_path_prompt(prompts, "WARC file", options.warc_file),
            "warc_compression": _optional_bool_prompt(
                prompts,
                "WARC compression",
                options.warc_compression,
            ),
            "warc_cdx": options.warc_cdx if warc_cdx is None else warc_cdx,
            "warc_max_size": _optional_text(prompts, "WARC max size", options.warc_max_size),
            "stats": options.stats if stats is None else stats,
        }
    )


def _site_bounds_overlay(prompts: PromptUI, options: SiteDownloadOptions) -> SiteDownloadOptions:
    return options.model_copy(
        update={
            "max_files": _optional_int_prompt(prompts, "Max files", options.max_files),
            "max_total_size": _optional_text(
                prompts,
                "Max total size",
                options.max_total_size,
            ),
            "max_runtime": _optional_float_prompt(
                prompts,
                "Max runtime seconds",
                options.max_runtime,
            ),
        }
    )


def _site_adaptive_overlay(prompts: PromptUI, options: SiteDownloadOptions) -> SiteDownloadOptions:
    adaptive = prompts.confirm("Use adaptive site scan?", default=options.adaptive)
    explain = prompts.confirm("Explain adaptive plan only?", default=options.explain)
    quiet = prompts.confirm("Quiet output?", default=options.quiet)
    json_output = prompts.confirm("JSON output?", default=options.json_output)
    verbose = prompts.confirm("Verbose errors?", default=options.verbose)
    return options.model_copy(
        update={
            "adaptive": options.adaptive if adaptive is None else adaptive,
            "max_concurrency": _optional_int_prompt(
                prompts,
                "Adaptive max concurrency",
                options.max_concurrency,
            ),
            "per_host_concurrency": _optional_int_prompt(
                prompts,
                "Adaptive per-host concurrency",
                options.per_host_concurrency,
            ),
            "politeness": _select_enum(
                prompts,
                "Adaptive politeness",
                AdaptivePoliteness,
                options.politeness,
            ),
            "explain": options.explain if explain is None else explain,
            "quiet": options.quiet if quiet is None else quiet,
            "json_output": options.json_output if json_output is None else json_output,
            "progress_mode": _select_enum(
                prompts,
                "Progress mode",
                ProgressMode,
                options.progress_mode,
            ),
            "verbose": options.verbose if verbose is None else verbose,
        }
    )


def _output_overlay(
    prompts: PromptUI,
    options: MenuDownloadOptions,
) -> MenuDownloadOptions:
    value = prompts.text("Output", default=str(options.output_dir))
    if value is None:
        return options
    return options.model_copy(update={"output_dir": Path(value).expanduser()})


def _cookies_overlay(
    prompts: PromptUI,
    options: VideoDownloadOptions | AudioDownloadOptions,
) -> VideoDownloadOptions | AudioDownloadOptions:
    selected = cast(
        str | None,
        prompts.select(
            "Cookies",
            [
                MenuChoice("None", "none"),
                MenuChoice("Safari", "safari"),
                MenuChoice("Chrome", "chrome"),
                MenuChoice("Firefox", "firefox"),
                MenuChoice("Brave", "brave"),
                MenuChoice("Cookies file", "file"),
                MenuChoice("Back", "back"),
            ],
        ),
    )
    if selected is None or selected == "back":
        return options
    if selected == "none":
        return options.model_copy(update={"browser_cookies": None, "cookies_file": None})
    if selected == "file":
        value = prompts.text("Cookies file", default=str(options.cookies_file or ""))
        if value is None:
            return options
        return options.model_copy(
            update={"browser_cookies": None, "cookies_file": Path(value).expanduser()}
        )
    return options.model_copy(update={"browser_cookies": selected, "cookies_file": None})


def _site_browser_cookies_prompt(prompts: PromptUI, current: str | None) -> str | None:
    selected = cast(
        str | None,
        prompts.select(
            "Browser cookies",
            [
                MenuChoice("Keep current", "keep"),
                MenuChoice("None", "none"),
                MenuChoice("Safari", "safari"),
                MenuChoice("Chrome", "chrome"),
                MenuChoice("Firefox", "firefox"),
                MenuChoice("Brave", "brave"),
                MenuChoice("Edge", "edge"),
                MenuChoice("Custom selector", "custom"),
            ],
        ),
    )
    if selected is None or selected == "keep":
        return current
    if selected == "none":
        return None
    if selected == "custom":
        return _optional_text(prompts, "Browser cookie selector", current)
    return selected


def _site_backend_overlay(
    prompts: PromptUI,
    options: SiteDownloadOptions,
) -> SiteDownloadOptions:
    backend = _select_enum(prompts, "Backend", SiteBackendChoice, options.backend)
    return options.model_copy(update={"backend": backend})


def _optional_text(prompts: PromptUI, message: str, current: str | None) -> str | None:
    value = prompts.text(message, default=current or "")
    if value is None:
        return current
    return value or None


def _list_prompt(prompts: PromptUI, message: str, current: list[str]) -> list[str]:
    value = prompts.text(message, default=" | ".join(current))
    if value is None:
        return current
    if not value.strip():
        return []
    return [item.strip() for item in value.split("|") if item.strip()]


def _rejects_html(reject: str | None) -> bool:
    values = {value.strip().lower().lstrip(".") for value in (reject or "").split(",")}
    return bool({"html", "htm"} & values)


def _set_reject_html(reject: str | None, *, keep: bool) -> str | None:
    values = [value.strip() for value in (reject or "").split(",") if value.strip()]
    filtered = [value for value in values if value.lower().lstrip(".") not in {"html", "htm"}]
    if keep:
        return ",".join(filtered) or None
    filtered.extend(suffix for suffix in ("html", "htm") if suffix not in filtered)
    return ",".join(filtered)


def _seed_domain(url: str) -> str | None:
    host = _host_for_menu(url)
    if not host:
        return None
    return host[4:] if host.startswith("www.") else host


def _optional_path_prompt(
    prompts: PromptUI,
    message: str,
    current: Path | None,
) -> Path | None:
    value = prompts.text(message, default=str(current or ""))
    if value is None:
        return current
    if not value:
        return None
    return Path(value).expanduser()


def _optional_bool_prompt(prompts: PromptUI, message: str, current: bool | None) -> bool | None:
    current_label = "unset" if current is None else ("enabled" if current else "disabled")
    selected = cast(
        str | None,
        prompts.select(
            f"{message} ({current_label})",
            [
                MenuChoice("Keep current", "keep"),
                MenuChoice("Unset/default", "unset"),
                MenuChoice("Enabled", "enabled"),
                MenuChoice("Disabled", "disabled"),
            ],
        ),
    )
    if selected is None or selected == "keep":
        return current
    if selected == "unset":
        return None
    return selected == "enabled"


def _optional_int_prompt(prompts: PromptUI, message: str, current: int | None) -> int | None:
    while True:
        value = prompts.text(message, default="" if current is None else str(current))
        if value is None:
            return current
        if not value:
            return None
        try:
            return int(value)
        except ValueError:
            continue


def _float_prompt(
    prompts: PromptUI,
    message: str,
    *,
    default: float,
    minimum: float,
) -> float:
    while True:
        value = prompts.text(message, default=f"{default:g}")
        if value is None:
            return default
        try:
            parsed = float(value)
        except ValueError:
            continue
        if parsed >= minimum:
            return parsed


def _optional_float_prompt(
    prompts: PromptUI,
    message: str,
    current: float | None,
) -> float | None:
    while True:
        value = prompts.text(message, default="" if current is None else f"{current:g}")
        if value is None:
            return current
        if not value:
            return None
        try:
            return float(value)
        except ValueError:
            continue


def _select_enum(
    prompts: PromptUI,
    message: str,
    enum_type: type[StrEnum],
    current: StrEnum,
) -> StrEnum:
    choices = [
        MenuChoice(f"{item.value}{'  current' if item == current else ''}", item)
        for item in enum_type
    ]
    selected = prompts.select(message, choices)
    return current if selected is None else cast(StrEnum, selected)


def _select_optional_enum(
    prompts: PromptUI,
    message: str,
    enum_type: type[StrEnum],
    current: StrEnum | None,
) -> StrEnum | None:
    choices = [MenuChoice("Unset", "__atlas_unset__")]
    choices.extend(
        MenuChoice(f"{item.value}{'  current' if item == current else ''}", item)
        for item in enum_type
    )
    selected = prompts.select(message, choices)
    if selected == "__atlas_unset__":
        return None
    if selected is None:
        return current
    return cast(StrEnum | None, selected)


def _int_prompt(
    prompts: PromptUI,
    message: str,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    while True:
        value = prompts.text(message, default=str(default))
        if value is None:
            return default
        try:
            parsed = int(value)
        except ValueError:
            continue
        if minimum <= parsed <= maximum:
            return parsed


def _completion_loop(
    prompts: PromptUI,
    saved_paths: Sequence[Path],
    *,
    console: Console | None = None,
    plan: HubExecutionPlan | None = None,
    media: MediaInfo | None = None,
) -> CompletionChoice:
    primary_path = _primary_saved_path(saved_paths, plan=plan)
    if console is not None:
        _print_completion_summary(console, saved_paths, primary_path, plan=plan, media=media)
    while True:
        choices: list[MenuChoice] = []
        if primary_path is not None:
            choices.extend(
                [
                    MenuChoice("Reveal in Finder", CompletionChoice.reveal),
                    MenuChoice("Open file", CompletionChoice.open),
                ]
            )
        choices.extend(
            [
                MenuChoice(_completion_another_label(plan), CompletionChoice.another),
                MenuChoice("Back to menu", CompletionChoice.back),
                MenuChoice("Quit", CompletionChoice.quit),
            ]
        )
        selected = cast(CompletionChoice | None, prompts.select("Next", choices))
        if selected is None:
            return CompletionChoice.back
        if selected == CompletionChoice.reveal:
            if primary_path is not None:
                _reveal_path(primary_path)
            continue
        if selected == CompletionChoice.open:
            if primary_path is not None:
                _open_path(primary_path)
            continue
        return selected


def _print_completion_summary(
    console: Console,
    saved_paths: Sequence[Path],
    primary_path: Path | None,
    *,
    plan: HubExecutionPlan | None,
    media: MediaInfo | None = None,
) -> None:
    console = ensure_atlas_theme(console)
    if primary_path is None:
        _print_workflow_card(
            console,
            "No New File",
            (("Output", "No saved path was reported by the downloader."),),
            style_name=ATLAS_WARNING_STYLE,
        )
        _print_workflow_footer(console)
        return
    _print_completion_saved_card(
        console,
        _completion_success_label(plan),
        primary_path,
    )
    details = _completion_detail_rows(saved_paths, primary_path, plan=plan, media=media)
    if details:
        _print_plain_section(
            console,
            "Details",
            tuple(_plain_option_row(label, value) for label, value in details),
        )
    _print_workflow_footer(console)


def _completion_success_label(plan: HubExecutionPlan | None) -> str:
    if plan is not None and isinstance(plan.options, AudioDownloadOptions):
        return "Audio Extracted"
    return "Download Complete"


def _print_completion_saved_card(console: Console, title: str, primary_path: Path) -> None:
    console = ensure_atlas_theme(console)
    _refresh_menu_screen(console)
    table = Table.grid(padding=(0, 2))
    table.add_column(style=ATLAS_MUTED_STYLE, no_wrap=True)
    table.add_column(ratio=1)
    table.add_row(
        f"{status_glyph('success')} Saved",
        Text.from_markup(_menu_path(primary_path.parent)),
    )
    filename = Text(" " * 12)
    filename.append(primary_path.name)
    console.print(
        Panel(
            Group(table, filename),
            title=Text(title, style=ATLAS_TITLE_STYLE),
            title_align="left",
            border_style=ATLAS_SUCCESS_STYLE,
            box=atlas_box(),
            expand=True,
        )
    )
    console.print()


def _completion_another_label(plan: HubExecutionPlan | None) -> str:
    if plan is not None and isinstance(plan.options, VideoDownloadOptions):
        return "Download another video"
    if plan is not None and isinstance(plan.options, AudioDownloadOptions):
        return "Extract another"
    return "Download another"


def _completion_detail_rows(
    saved_paths: Sequence[Path],
    primary_path: Path,
    *,
    plan: HubExecutionPlan | None,
    media: MediaInfo | None = None,
) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    if primary_path.exists():
        rows.append(("Size", format_bytes(primary_path.stat().st_size)))
    if plan is not None:
        options = plan.options
        if isinstance(options, VideoDownloadOptions):
            container = str(plan.preview.summary.get("container") or options.container.value)
            rows.extend(
                [
                    ("Format", _display_container(container)),
                    ("Video", _display_codec(options.video_codec.value)),
                    ("Metadata", "embedded" if options.embed_metadata else "off"),
                    ("Thumbnail", "embedded" if options.embed_thumbnail else "off"),
                    ("Archive", "updated" if options.archive else "off"),
                ]
            )
        elif isinstance(options, AudioDownloadOptions):
            rows.extend(
                [
                    ("Format", _selected_output_profile(plan, media=media)),
                    ("Metadata", "embedded" if options.embed_metadata else "off"),
                    ("Artwork", "embedded" if options.embed_thumbnail else "off"),
                    ("Archive", "updated" if options.archive else "off"),
                ]
            )
    return rows


def _primary_saved_path(
    saved_paths: Sequence[Path],
    *,
    plan: HubExecutionPlan | None = None,
) -> Path | None:
    if not saved_paths:
        return None
    paths = [Path(path) for path in saved_paths]
    non_temporary = [path for path in paths if not _looks_like_temporary_stream(path)]
    preferred_suffix = _preferred_final_suffix(plan)
    if preferred_suffix:
        for path in reversed(non_temporary):
            if path.suffix.lower() == preferred_suffix:
                return path
    if non_temporary:
        return non_temporary[-1]
    return paths[-1]


def _preferred_final_suffix(plan: HubExecutionPlan | None) -> str | None:
    if plan is None:
        return None
    options = plan.options
    if isinstance(options, VideoDownloadOptions):
        container = str(plan.preview.summary.get("container") or options.container.value)
        return None if container == "auto" else f".{container.lower()}"
    if isinstance(options, AudioDownloadOptions):
        codec = options.codec.value
        if codec == AudioCodec.best.value:
            return None
        return f".{codec.lower()}"
    output = plan.preview.output
    return output.suffix.lower() if output and output.suffix else None


def _looks_like_temporary_stream(path: Path) -> bool:
    return bool(_TEMP_STREAM_RE.search(path.name))


def _info_flow(actions: MenuActions, prompts: PromptUI) -> FlowResult:
    url = prompts.text("URL")
    if url is None:
        return FlowResult.back
    actions.run_info(url)
    return _post_simple_action(prompts)


def _formats_flow(actions: MenuActions, prompts: PromptUI) -> FlowResult:
    url = prompts.text("URL")
    if url is None:
        return FlowResult.back
    actions.run_formats(url)
    return _post_simple_action(prompts)


def _advanced_backend_flow(
    actions: MenuActions,
    prompts: PromptUI,
    console: Console,
) -> FlowResult:
    while True:
        tool = cast(
            BackendTool | None,
            prompts.select(
                "Backend",
                [
                    MenuChoice("yt-dlp", BackendTool.ytdlp),
                    MenuChoice("aria2c", BackendTool.aria2),
                    MenuChoice("wget2", BackendTool.wget2),
                    MenuChoice("wget", BackendTool.wget),
                    MenuChoice("Back", None),
                ],
            ),
        )
        if tool is None:
            return FlowResult.back
        args_text = prompts.text("Arguments", default="--help")
        if args_text is None:
            return FlowResult.back
        try:
            args = shlex.split(args_text)
        except ValueError as exc:
            console.print(
                f"[{ATLAS_ERROR_STYLE}]Could not parse arguments:[/{ATLAS_ERROR_STYLE}] "
                f"{escape(str(exc))}"
            )
            continue
        dry_run = prompts.confirm("Dry run?", default=True)
        actions.run_backend_tool(tool, args, dry_run=True if dry_run is None else dry_run)
        result = _post_simple_action(prompts)
        if result == FlowResult.quit:
            return result
        return FlowResult.back


def _batch_queue_overlay(
    prompts: PromptUI,
    *,
    kind: BatchKind,
    concurrency: int | None,
    allow_sites: bool,
    allow_dirs: bool,
    video_codec: VideoCodecChoice,
    audio_codec: AudioCodec,
    audio_quality: int,
    default_concurrency: int,
) -> tuple[BatchKind, int | None, bool, bool, VideoCodecChoice, AudioCodec, int]:
    current_kind = kind
    current_concurrency = concurrency
    current_allow_sites = allow_sites
    current_allow_dirs = allow_dirs
    current_video_codec = video_codec
    current_audio_codec = audio_codec
    current_audio_quality = audio_quality
    while True:
        selected = cast(
            str | None,
            prompts.select(
                "Batch queue",
                [
                    MenuChoice("Kind", "kind"),
                    MenuChoice("Concurrency", "concurrency"),
                    MenuChoice("Codecs", "codecs"),
                    MenuChoice("Sites", "sites"),
                    MenuChoice("Directories", "dirs"),
                    MenuChoice("Back", "back"),
                ],
            ),
        )
        if selected is None or selected == "back":
            return (
                current_kind,
                current_concurrency,
                current_allow_sites,
                current_allow_dirs,
                current_video_codec,
                current_audio_codec,
                current_audio_quality,
            )
        if selected == "kind":
            current_kind = cast(
                BatchKind,
                _select_enum(prompts, "Batch kind", BatchKind, current_kind),
            )
        elif selected == "concurrency":
            current_concurrency = _int_prompt(
                prompts,
                "Concurrency",
                default=current_concurrency or default_concurrency,
                minimum=1,
                maximum=16,
            )
        elif selected == "codecs":
            current_video_codec = cast(
                VideoCodecChoice,
                _select_enum(prompts, "Video codec", VideoCodecChoice, current_video_codec),
            )
            current_audio_codec = cast(
                AudioCodec,
                _select_enum(prompts, "Audio codec", AudioCodec, current_audio_codec),
            )
            current_audio_quality = _int_prompt(
                prompts,
                "Audio quality 0-10",
                default=current_audio_quality,
                minimum=0,
                maximum=10,
            )
        elif selected == "sites":
            answer = prompts.confirm("Allow website mirrors?", default=current_allow_sites)
            current_allow_sites = current_allow_sites if answer is None else answer
        elif selected == "dirs":
            answer = prompts.confirm("Allow directory mirrors?", default=current_allow_dirs)
            current_allow_dirs = current_allow_dirs if answer is None else answer


def _batch_flow(
    settings: AtlasSettings,
    actions: MenuActions,
    prompts: PromptUI,
    console: Console,
) -> FlowResult:
    while True:
        selected = cast(
            BatchSourceChoice | None,
            prompts.select(
                "Batch",
                [
                    MenuChoice("Paste URL and scan", BatchSourceChoice.url_scan),
                    MenuChoice("Use URL file", BatchSourceChoice.url_file),
                    MenuChoice("Paste multiple URLs", BatchSourceChoice.pasted_urls),
                    MenuChoice("Playlist as batch", BatchSourceChoice.playlist),
                    MenuChoice("Resume session", BatchSourceChoice.resume),
                    MenuChoice("Retry failed", BatchSourceChoice.retry),
                    MenuChoice("Inspect session", BatchSourceChoice.inspect),
                    MenuChoice("Export URLs", BatchSourceChoice.export_failed),
                    MenuChoice("Back", BatchSourceChoice.back),
                    MenuChoice("Quit", BatchSourceChoice.quit),
                ],
            ),
        )
        if selected in {None, BatchSourceChoice.back}:
            return FlowResult.back
        if selected == BatchSourceChoice.quit:
            return FlowResult.quit
        if selected == BatchSourceChoice.url_scan:
            result = _batch_url_scan_flow(settings, actions, prompts, console)
        elif selected == BatchSourceChoice.url_file:
            result = _batch_file_flow(settings, actions, prompts, console)
        elif selected == BatchSourceChoice.pasted_urls:
            result = _batch_pasted_urls_flow(settings, actions, prompts, console)
        elif selected == BatchSourceChoice.playlist:
            result = _batch_playlist_flow(settings, actions, prompts, console)
        elif selected == BatchSourceChoice.resume:
            result = _saved_batch_session_flow(actions, prompts, resume=True)
        elif selected == BatchSourceChoice.inspect:
            result = _saved_batch_session_flow(actions, prompts, resume=False, inspect=True)
        elif selected == BatchSourceChoice.export_failed:
            result = _export_failed_session_flow(actions, prompts)
        else:
            result = _saved_batch_session_flow(actions, prompts, resume=False)
        if result == FlowResult.quit:
            return FlowResult.quit
        return FlowResult.back


def _batch_file_flow(
    settings: AtlasSettings,
    actions: MenuActions,
    prompts: PromptUI,
    console: Console,
) -> FlowResult:
    while True:
        path_text = prompts.text("URL file")
        if path_text is None:
            return FlowResult.back
        result = _batch_file_plan_flow(
            settings,
            actions,
            prompts,
            console,
            Path(path_text),
        )
        if result != CompletionChoice.another:
            return FlowResult.quit if result == CompletionChoice.quit else FlowResult.back


def _batch_file_plan_flow(
    settings: AtlasSettings,
    actions: MenuActions,
    prompts: PromptUI,
    console: Console,
    file: Path,
    *,
    default_kind: BatchKind = BatchKind.auto,
    default_allow_sites: bool = False,
    default_allow_dirs: bool = False,
) -> CompletionChoice:
    kind = default_kind
    concurrency: int | None = settings.batch_concurrency
    allow_sites = default_allow_sites
    allow_dirs = default_allow_dirs
    video_codec = VideoCodecChoice.auto
    audio_codec = settings.audio_codec
    audio_quality = settings.audio_quality
    while True:
        _print_batch_plan(
            console,
            file,
            kind,
            concurrency,
            allow_sites,
            allow_dirs,
            video_codec,
            audio_codec,
            audio_quality,
        )
        selected = cast(PlanMenuChoice | None, prompts.select("Next", _plan_choices()))
        if selected in {None, PlanMenuChoice.back}:
            return CompletionChoice.back
        if selected == PlanMenuChoice.quit:
            return CompletionChoice.quit
        if selected == PlanMenuChoice.customize:
            previous = {
                "kind": kind.value,
                "concurrency": concurrency,
                "allow_sites": allow_sites,
                "allow_dirs": allow_dirs,
                "video_codec": video_codec.value,
                "audio_codec": audio_codec.value,
                "audio_quality": audio_quality,
            }
            (
                kind,
                concurrency,
                allow_sites,
                allow_dirs,
                video_codec,
                audio_codec,
                audio_quality,
            ) = _batch_queue_overlay(
                prompts,
                kind=kind,
                concurrency=concurrency,
                allow_sites=allow_sites,
                allow_dirs=allow_dirs,
                video_codec=video_codec,
                audio_codec=audio_codec,
                audio_quality=audio_quality,
                default_concurrency=settings.batch_concurrency,
            )
            _print_mapping_diff(
                console,
                title="Changed Batch Options",
                before=previous,
                after={
                    "kind": kind.value,
                    "concurrency": concurrency,
                    "allow_sites": allow_sites,
                    "allow_dirs": allow_dirs,
                    "video_codec": video_codec.value,
                    "audio_codec": audio_codec.value,
                    "audio_quality": audio_quality,
                },
            )
            continue
        actions.run_batch(
            file,
            kind=kind,
            concurrency=concurrency,
            allow_sites=allow_sites,
            allow_dirs=allow_dirs,
            video_codec=video_codec,
            audio_codec=audio_codec,
            audio_quality=audio_quality,
            dry_run=selected == PlanMenuChoice.dry_run,
        )
        if selected == PlanMenuChoice.dry_run:
            continue
        return _completion_loop(prompts, [])


def _batch_pasted_urls_flow(
    settings: AtlasSettings,
    actions: MenuActions,
    prompts: PromptUI,
    console: Console,
) -> FlowResult:
    value = prompts.text("Paste URLs")
    if value is None:
        return FlowResult.back
    urls = _parse_pasted_urls(value)
    if not urls:
        console.print(f"[{ATLAS_WARNING_STYLE}]No URLs found.[/{ATLAS_WARNING_STYLE}]")
        return FlowResult.back
    file = _write_menu_batch_file(settings.output_dir, urls)
    console.print(
        f"[{ATLAS_MUTED_STYLE}]Created temporary batch with {len(urls)} URL(s): "
        f"{escape(str(file))}[/{ATLAS_MUTED_STYLE}]"
    )
    result = _batch_file_plan_flow(settings, actions, prompts, console, file)
    return FlowResult.quit if result == CompletionChoice.quit else FlowResult.back


def _batch_playlist_flow(
    settings: AtlasSettings,
    actions: MenuActions,
    prompts: PromptUI,
    console: Console,
) -> FlowResult:
    url = prompts.text("Playlist URL")
    if url is None:
        return FlowResult.back
    selected = cast(
        HubKind | None,
        prompts.select(
            "Download playlist as",
            [
                MenuChoice("Video playlist", HubKind.video),
                MenuChoice("Audio playlist", HubKind.audio),
                MenuChoice("Back", None),
            ],
        ),
    )
    if selected is None:
        return FlowResult.back
    options: MenuDownloadOptions
    if selected == HubKind.audio:
        options = build_audio_options(settings, url, playlist=True)
    else:
        options = build_video_options(settings, url, playlist=True)
    result = _plan_loop(actions, prompts, console, options, selected)
    return FlowResult.quit if result == CompletionChoice.quit else FlowResult.back


def _saved_batch_session_flow(
    actions: MenuActions,
    prompts: PromptUI,
    *,
    resume: bool,
    inspect: bool = False,
) -> FlowResult:
    session_text = prompts.text("Session path or latest", default="latest")
    if session_text is None:
        return FlowResult.back
    selected = cast(
        PlanMenuChoice | None,
        prompts.select(
            "Next",
            [
                MenuChoice("Inspect", PlanMenuChoice.start),
                MenuChoice("Back", PlanMenuChoice.back),
                MenuChoice("Quit", PlanMenuChoice.quit),
            ]
            if inspect
            else _plan_choices(include_customize=False),
        ),
    )
    if selected in {None, PlanMenuChoice.back}:
        return FlowResult.back
    if selected == PlanMenuChoice.quit:
        return FlowResult.quit
    session = None if session_text.strip().lower() == "latest" else Path(session_text).expanduser()
    if inspect:
        actions.inspect_session(session)
        return _post_simple_action(prompts)
    dry_run = selected == PlanMenuChoice.dry_run
    if resume:
        actions.resume_session(session, dry_run=dry_run)
    else:
        actions.retry_failed_session(session, dry_run=dry_run)
    if dry_run:
        return FlowResult.back
    completion = _completion_loop(prompts, [])
    return FlowResult.quit if completion == CompletionChoice.quit else FlowResult.back


def _export_failed_session_flow(actions: MenuActions, prompts: PromptUI) -> FlowResult:
    session_text = prompts.text("Session path or latest", default="latest")
    if session_text is None:
        return FlowResult.back
    output_text = prompts.text("Output file (blank for stdout)", default="")
    if output_text is None:
        return FlowResult.back
    selected = cast(
        PlanMenuChoice | None,
        prompts.select(
            "Next",
            [
                MenuChoice("Export", PlanMenuChoice.start),
                MenuChoice("Back", PlanMenuChoice.back),
                MenuChoice("Quit", PlanMenuChoice.quit),
            ],
        ),
    )
    if selected in {None, PlanMenuChoice.back}:
        return FlowResult.back
    if selected == PlanMenuChoice.quit:
        return FlowResult.quit
    session = None if session_text.strip().lower() == "latest" else Path(session_text).expanduser()
    output = Path(output_text).expanduser() if output_text.strip() else None
    actions.export_failed_session(session, output=output)
    return _post_simple_action(prompts)


def _batch_url_scan_flow(
    settings: AtlasSettings,
    actions: MenuActions,
    prompts: PromptUI,
    console: Console,
) -> FlowResult:
    while True:
        url = prompts.text("Seed URL")
        if url is None:
            return FlowResult.back
        result = _url_scan_action_flow(settings, actions, prompts, console, url)
        return result


def _url_scan_action_flow(
    settings: AtlasSettings,
    actions: MenuActions,
    prompts: PromptUI,
    console: Console,
    url: str,
) -> FlowResult:
    while True:
        _print_detected_url_card(console, url)
        scan = _with_menu_status(console, "Scanning", partial(actions.scan_url, url))
        if _scan_failed(scan):
            failed_result = _scan_failed_flow(settings, actions, prompts, console, scan)
            if failed_result == FlowResult.retry:
                continue
            return failed_result
        if _scan_empty(scan):
            empty_result = _scan_empty_flow(settings, actions, prompts, console, scan)
            if empty_result == FlowResult.retry:
                continue
            return empty_result
        break
    directory_index = directory_index_from_work_item(scan)
    if _scan_looks_like_directory_index(scan, directory_index):
        return _directory_explorer_flow(settings, actions, prompts, console, scan, directory_index)
    _print_url_scan_summary(console, scan)
    selected = _select_batch_url_scan_action(prompts, scan)
    if selected is None or selected == BatchUrlScanChoice.back:
        return FlowResult.back
    if selected in {BatchUrlScanChoice.direct_links, BatchUrlScanChoice.selected_files}:
        urls = (
            _select_discovered_files(prompts, scan)
            if selected == BatchUrlScanChoice.selected_files
            else _downloadable_links_from_scan(scan)
        )
        if urls is None:
            return FlowResult.back
        if not urls:
            console.print(
                f"[{ATLAS_WARNING_STYLE}]No downloadable same-host links were found."
                f"[/{ATLAS_WARNING_STYLE}]"
            )
            return FlowResult.back
        batch_file = _write_menu_batch_file(settings.output_dir, urls)
        console.print(
            f"[{ATLAS_MUTED_STYLE}]Built downloadable queue with {len(urls)} URL(s): "
            f"{escape(str(batch_file))}[/{ATLAS_MUTED_STYLE}]"
        )
        batch_result = _batch_file_plan_flow(settings, actions, prompts, console, batch_file)
        if batch_result != CompletionChoice.another:
            return FlowResult.quit if batch_result == CompletionChoice.quit else FlowResult.back
        return FlowResult.back
    options, kind = _batch_url_scan_options(settings, prompts, scan, selected)
    if options is None:
        return FlowResult.back
    plan_result = _plan_loop(actions, prompts, console, options, kind)
    if plan_result != CompletionChoice.another:
        return FlowResult.quit if plan_result == CompletionChoice.quit else FlowResult.back
    return FlowResult.back


def _scan_failed(scan: WorkItem) -> bool:
    return scan.scan_status == ScanStatus.failed or (
        scan.error is not None
        and not scan.discovered_links
        and not scan.discovered_work_items
        and scan.scan_type == "failed scan"
    )


def _scan_empty(scan: WorkItem) -> bool:
    return scan.scan_status == ScanStatus.empty


def _scan_failed_flow(
    settings: AtlasSettings,
    actions: MenuActions,
    prompts: PromptUI,
    console: Console,
    scan: WorkItem,
) -> FlowResult:
    while True:
        _print_scan_failed(console, scan)
        selected = cast(
            ScanFailedChoice | None,
            prompts.select(
                "Scan failed",
                [
                    MenuChoice("Retry scan", ScanFailedChoice.retry),
                    MenuChoice("Doctor check", ScanFailedChoice.doctor),
                    MenuChoice("Backend fetch", ScanFailedChoice.backend_scan),
                    MenuChoice(
                        "Continue as mirror",
                        ScanFailedChoice.backend_mirror,
                    ),
                    MenuChoice("Error details", ScanFailedChoice.details),
                    MenuChoice("Back", ScanFailedChoice.back),
                ],
            ),
        )
        if selected in {None, ScanFailedChoice.back}:
            return FlowResult.back
        if selected in {ScanFailedChoice.retry, ScanFailedChoice.backend_scan}:
            return FlowResult.retry
        if selected == ScanFailedChoice.doctor:
            actions.run_doctor()
            continue
        if selected == ScanFailedChoice.details:
            _print_scan_error_details(console, scan)
            _view_only_back(prompts, message="Back")
            continue
        if selected == ScanFailedChoice.backend_mirror:
            options = _directory_scan_options(settings, scan.url, depth=settings.dir_depth)
            plan_result = _plan_loop(actions, prompts, console, options, HubKind.dir)
            return FlowResult.quit if plan_result == CompletionChoice.quit else FlowResult.back


def _scan_empty_flow(
    settings: AtlasSettings,
    actions: MenuActions,
    prompts: PromptUI,
    console: Console,
    scan: WorkItem,
) -> FlowResult:
    while True:
        _print_scan_empty(console, scan)
        selected = cast(
            ScanEmptyChoice | None,
            prompts.select(
                "No links found",
                [
                    MenuChoice("Retry scan", ScanEmptyChoice.retry),
                    MenuChoice("Treat as website", ScanEmptyChoice.website),
                    MenuChoice("This page only", ScanEmptyChoice.file),
                    MenuChoice("Back", ScanEmptyChoice.back),
                ],
            ),
        )
        if selected in {None, ScanEmptyChoice.back}:
            return FlowResult.back
        if selected == ScanEmptyChoice.retry:
            return FlowResult.retry
        if selected == ScanEmptyChoice.website:
            site_options = _offline_site_scan_options(settings, scan.url)
            plan_result = _plan_loop(actions, prompts, console, site_options, HubKind.site)
            return FlowResult.quit if plan_result == CompletionChoice.quit else FlowResult.back
        if selected == ScanEmptyChoice.file:
            file_options = build_file_options(settings, scan.final_url or scan.url)
            plan_result = _plan_loop(actions, prompts, console, file_options, HubKind.file)
            return FlowResult.quit if plan_result == CompletionChoice.quit else FlowResult.back


def _directory_explorer_flow(
    settings: AtlasSettings,
    actions: MenuActions,
    prompts: PromptUI,
    console: Console,
    scan: WorkItem,
    directory_index: DirectoryIndex,
) -> FlowResult:
    while True:
        _print_directory_explorer(console, scan, directory_index)
        selected = cast(
            DirectoryExplorerChoice | None,
            prompts.select(
                "What would you like to do?",
                _directory_explorer_choices(directory_index, status=scan.scan_status),
            ),
        )
        if selected in {None, DirectoryExplorerChoice.back}:
            return FlowResult.back
        if selected == DirectoryExplorerChoice.quit:
            return FlowResult.quit
        if selected == DirectoryExplorerChoice.tree:
            _print_directory_tree(console, directory_index)
            _view_only_back(prompts, message="Back")
            continue
        if selected == DirectoryExplorerChoice.offline_site:
            plan_result = _plan_loop(
                actions,
                prompts,
                console,
                _offline_site_scan_options(settings, scan.url),
                HubKind.site,
            )
            return FlowResult.quit if plan_result == CompletionChoice.quit else FlowResult.back
        if selected == DirectoryExplorerChoice.visible_files:
            return _directory_visible_files_flow(
                settings,
                actions,
                prompts,
                console,
                directory_index,
            )
        if selected == DirectoryExplorerChoice.everything:
            return _directory_deep_scan_flow(
                settings,
                actions,
                prompts,
                console,
                scan,
                [scan.final_url or scan.url],
            )
        if selected == DirectoryExplorerChoice.deep_scan:
            roots = [entry.url for entry in directory_index.folders] or [scan.final_url or scan.url]
            return _directory_deep_scan_flow(
                settings,
                actions,
                prompts,
                console,
                scan,
                roots,
            )
        if selected == DirectoryExplorerChoice.folder:
            folder = _select_directory_folder(prompts, console, directory_index)
            if folder is None:
                continue
            return _directory_deep_scan_flow(
                settings,
                actions,
                prompts,
                console,
                scan,
                [folder.url],
            )
        if selected == DirectoryExplorerChoice.folders:
            folders = _select_directory_folders(prompts, console, directory_index)
            if folders is None:
                continue
            if not folders:
                console.print(
                    f"[{ATLAS_WARNING_STYLE}]No folders selected.[/{ATLAS_WARNING_STYLE}]"
                )
                continue
            return _directory_deep_scan_flow(
                settings,
                actions,
                prompts,
                console,
                scan,
                [folder.url for folder in folders],
            )


def _scan_looks_like_directory_index(scan: WorkItem, directory_index: DirectoryIndex) -> bool:
    if not directory_index.folders:
        return False
    scan_type = (scan.scan_type or "").lower()
    if "directory" in scan_type or "index" in scan_type:
        return True
    return _url_should_scan_before_auto_plan(scan.final_url or scan.url)


def _directory_explorer_choices(
    directory_index: DirectoryIndex,
    *,
    status: ScanStatus,
) -> list[MenuChoice]:
    labels = {
        DirectoryExplorerChoice.everything: "Everything under this folder",
        DirectoryExplorerChoice.folder: "Choose one specific folder",
        DirectoryExplorerChoice.folders: "Choose multiple folders",
        DirectoryExplorerChoice.visible_files: "Only visible files at this level",
        DirectoryExplorerChoice.tree: "Browse full folder tree first",
        DirectoryExplorerChoice.deep_scan: "Deep scan selected folders first",
        DirectoryExplorerChoice.offline_site: "Treat as offline website instead",
        DirectoryExplorerChoice.back: "Back",
        DirectoryExplorerChoice.quit: "Quit",
    }
    return [
        MenuChoice(labels[action], action)
        for action in directory_explorer_actions(
            directory_index,
            status=status,
        )
    ]


def _print_directory_explorer(
    console: Console,
    scan: WorkItem,
    directory_index: DirectoryIndex,
) -> None:
    _refresh_menu_screen(console)
    visible = _menu_separator().join(
        (
            f"{len(directory_index.folders):,} folders",
            f"{len(directory_index.files):,} files",
        )
    )
    _render_menu_context_card(
        console,
        "Browse Directory",
        (
            ("Seed", escape(scan.final_url or scan.url)),
            ("Scope", visual_join(("same host", "no parent"))),
            ("Visible", visible),
            (
                "Estimated",
                f"~{format_bytes(scan.scan_estimated_bytes)}"
                if scan.scan_estimated_bytes is not None
                else "unknown",
            ),
        ),
    )
    folder_limit = _directory_folder_preview_limit(console, directory_index.folders)
    _print_directory_folder_preview(
        console,
        f"Folders ({len(directory_index.folders):,})",
        directory_index.folders,
        limit=folder_limit,
    )
    visible_files = tuple(
        entry for entry in directory_index.files if _directory_entry_is_downloadable(entry)
    )
    if visible_files:
        _print_directory_file_preview(
            console,
            f"Files at this level ({len(visible_files):,})",
            visible_files,
            limit=_DIRECTORY_FILE_PREVIEW_LIMIT,
        )
    if scan.scan_warnings:
        _print_directory_warnings(console, scan.scan_warnings)
    console.print(_directory_footer())


def _directory_folder_preview_limit(
    console: Console,
    entries: Sequence[DirectoryEntry],
) -> int:
    if not console.is_terminal:
        return len(entries)
    reserved = 18 + _DIRECTORY_FILE_PREVIEW_LIMIT
    available = max(4, console.size.height - reserved)
    return min(len(entries), available)


def _print_directory_folder_preview(
    console: Console,
    title: str,
    entries: Sequence[DirectoryEntry],
    *,
    limit: int,
) -> None:
    console = ensure_atlas_theme(console)
    if not entries:
        return
    table = Table.grid(padding=(0, 2), expand=True)
    table.add_column(ratio=1, overflow="ellipsis", no_wrap=True)
    table.add_column(style=ATLAS_MUTED_STYLE, no_wrap=True)
    for entry in entries[:limit]:
        table.add_row(
            _directory_entry_name(entry),
            _directory_entry_modified_label(entry),
        )
    remaining = len(entries) - limit
    if remaining > 0:
        note = Text(
            f"showing first {limit:,} of {len(entries):,} folders",
            style=ATLAS_MUTED_STYLE,
        )
        _render_menu_section(console, title, Group(table, note))
        return
    _render_menu_section(console, title, table)


def _print_directory_file_preview(
    console: Console,
    title: str,
    entries: Sequence[DirectoryEntry],
    *,
    limit: int,
) -> None:
    console = ensure_atlas_theme(console)
    if not entries:
        return
    table = Table.grid(expand=True)
    table.add_column(ratio=1, overflow="ellipsis", no_wrap=True)
    for entry in entries[:limit]:
        table.add_row(_directory_entry_name(entry))
    remaining = len(entries) - limit
    if remaining > 0:
        note = Text(
            f"showing first {limit:,} of {len(entries):,}; use / to filter",
            style=ATLAS_MUTED_STYLE,
        )
        _render_menu_section(console, title, Group(table, note))
        return
    _render_menu_section(console, title, table)


def _print_directory_warnings(console: Console, warnings: Sequence[str]) -> None:
    bullet = "•" if visual_options().unicode else "-"
    text = Text(style=ATLAS_WARNING_STYLE)
    for index, warning in enumerate(warnings):
        if index:
            text.append("\n")
        text.append(f"{bullet} {warning}")
    _render_menu_section(console, "Warnings", text)


def _print_directory_tree(console: Console, directory_index: DirectoryIndex) -> None:
    console = ensure_atlas_theme(console)
    _refresh_menu_screen(console)
    branch = "├──" if visual_options().unicode else "|--"
    last = "└──" if visual_options().unicode else "`--"
    lines = [directory_index.source_url.rstrip("/") + "/"]
    folders = directory_index.folders
    for index, entry in enumerate(folders):
        connector = last if index == len(folders) - 1 else branch
        lines.append(f"{connector} {_directory_entry_name(entry)}")
    if not folders:
        lines.append(f"{last} no visible folders")
    _print_screen_title(
        console,
        "Folder tree",
        escape(_compact_url_label(directory_index.source_url)),
    )
    console.print("\n".join(escape(line) for line in lines))
    console.print()
    console.print(_directory_footer())


def _select_directory_folder(
    prompts: PromptUI,
    console: Console,
    directory_index: DirectoryIndex,
) -> DirectoryEntry | None:
    _print_directory_picker_context(
        console,
        "Choose one specific folder",
        directory_index,
        "Select one visible folder to deep scan.",
        multi=False,
    )
    choices = [
        MenuChoice(_directory_entry_choice_label(entry), entry)
        for entry in directory_index.folders
    ]
    choices.append(MenuChoice("Back", None))
    return cast(DirectoryEntry | None, prompts.select("Folder", choices))


def _select_directory_folders(
    prompts: PromptUI,
    console: Console,
    directory_index: DirectoryIndex,
) -> list[DirectoryEntry] | None:
    _print_directory_picker_context(
        console,
        "Choose multiple folders",
        directory_index,
        "Select one or more visible folders to deep scan.",
        multi=True,
    )
    selected = prompts.multi_select(
        "Folders",
        [
            MenuChoice(_directory_entry_choice_label(entry), entry)
            for entry in directory_index.folders
        ],
    )
    if selected is None:
        return None
    return [cast(DirectoryEntry, entry) for entry in selected]


def _select_directory_files(
    prompts: PromptUI,
    console: Console,
    directory_index: DirectoryIndex,
    entries: Sequence[DirectoryEntry],
) -> list[DirectoryEntry] | None:
    _print_directory_picker_context(
        console,
        "Only visible files at this level",
        directory_index,
        "Search and select root-level files from this folder.",
        multi=True,
    )
    selected = prompts.multi_select(
        "Visible files",
        [MenuChoice(_directory_entry_choice_label(entry), entry) for entry in entries],
    )
    if selected is None:
        return None
    return [cast(DirectoryEntry, entry) for entry in selected]


def _print_directory_picker_context(
    console: Console,
    title: str,
    directory_index: DirectoryIndex,
    description: str,
    *,
    multi: bool,
) -> None:
    _refresh_menu_screen(console)
    _render_menu_context_card(
        console,
        title,
        (
            ("Source", escape(directory_index.source_url)),
            (
                "Visible",
                _menu_separator().join(
                    (
                        f"{len(directory_index.folders):,} folders",
                        f"{len(directory_index.files):,} files",
                    )
                ),
            ),
        ),
    )
    console.print(Text(description, style=ATLAS_MUTED_STYLE))
    console.print()
    console.print(_directory_footer(multi=multi))


def _directory_visible_files_flow(
    settings: AtlasSettings,
    actions: MenuActions,
    prompts: PromptUI,
    console: Console,
    directory_index: DirectoryIndex,
) -> FlowResult:
    entries = _downloadable_entries_from_directory_index(directory_index)
    if not entries:
        console.print(
            f"[{ATLAS_WARNING_STYLE}]No visible files were found.[/{ATLAS_WARNING_STYLE}]"
        )
        return FlowResult.back
    selected_entries = _select_directory_files(prompts, console, directory_index, entries)
    if selected_entries is None:
        return FlowResult.back
    if not selected_entries:
        console.print(
            f"[{ATLAS_WARNING_STYLE}]No visible files selected.[/{ATLAS_WARNING_STYLE}]"
        )
        return FlowResult.back
    urls = [entry.url for entry in selected_entries]
    batch_file = _write_menu_batch_file(settings.output_dir, urls)
    console.print(
        f"[{ATLAS_MUTED_STYLE}]Built exact visible-file queue with {len(urls)} URL(s): "
        f"{escape(str(batch_file))}[/{ATLAS_MUTED_STYLE}]"
    )
    result = _batch_file_plan_flow(
        settings,
        actions,
        prompts,
        console,
        batch_file,
        default_kind=BatchKind.file,
    )
    return FlowResult.quit if result == CompletionChoice.quit else FlowResult.back


def _directory_deep_scan_flow(
    settings: AtlasSettings,
    actions: MenuActions,
    prompts: PromptUI,
    console: Console,
    seed_scan: WorkItem,
    selected_roots: Sequence[str],
) -> FlowResult:
    scans = [
        _with_menu_status(console, "Scanning selected folder", partial(actions.scan_url, root))
        for root in selected_roots
    ]
    _print_deep_directory_scan_summary(console, seed_scan, selected_roots, scans)
    urls = _downloadable_links_from_scans(scans)
    if urls:
        batch_file = _write_menu_batch_file(settings.output_dir, urls)
        console.print(
            f"[{ATLAS_MUTED_STYLE}]Built exact scanned-file queue with {len(urls)} URL(s): "
            f"{escape(str(batch_file))}[/{ATLAS_MUTED_STYLE}]"
        )
        result = _batch_file_plan_flow(
            settings,
            actions,
            prompts,
            console,
            batch_file,
            default_kind=BatchKind.file,
        )
    else:
        batch_file = _write_menu_batch_file(settings.output_dir, selected_roots)
        console.print(
            f"[{ATLAS_MUTED_STYLE}]No direct files were visible yet; queued selected "
            "folder root(s): "
            f"{escape(str(batch_file))}[/{ATLAS_MUTED_STYLE}]"
        )
        result = _batch_file_plan_flow(
            settings,
            actions,
            prompts,
            console,
            batch_file,
            default_kind=BatchKind.dir,
            default_allow_dirs=True,
        )
    return FlowResult.quit if result == CompletionChoice.quit else FlowResult.back


def _print_deep_directory_scan_summary(
    console: Console,
    seed_scan: WorkItem,
    selected_roots: Sequence[str],
    scans: Sequence[WorkItem],
) -> None:
    total_counts = {
        "folders": 0,
        "files": 0,
        "html": 0,
        "media": 0,
        "external": 0,
    }
    estimated_size = 0
    estimated_known = False
    for scan in scans:
        counts = _scan_link_counts(scan)
        for key in total_counts:
            total_counts[key] += counts.get(key, 0)
        if scan.scan_estimated_bytes is not None:
            estimated_known = True
            estimated_size += scan.scan_estimated_bytes
    _print_screen_title(
        console,
        "Scan complete",
        f"Selected: {_selected_roots_label(selected_roots)}",
    )
    rows: list[tuple[str, str]] = [
        ("Source", escape(_compact_url_label(seed_scan.final_url or seed_scan.url))),
        ("Scope", visual_join(("same host", "no parent", "bounded scan"))),
        ("Folders", f"{total_counts['folders']:,}"),
        ("Files", f"{total_counts['files']:,}"),
        ("HTML", f"{total_counts['html']:,}"),
    ]
    if total_counts["media"]:
        rows.append(("Media", f"{total_counts['media']:,}"))
    if total_counts["external"]:
        rows.append(("Skipped", f"{total_counts['external']:,} external"))
    rows.append(
        (
            "Size",
            f"~{format_bytes(estimated_size)}" if estimated_known else "unknown",
        )
    )
    rows.append(("Plan", "exact-list adaptive batch"))
    _print_fact_rows(console, rows)


def _selected_roots_label(selected_roots: Sequence[str]) -> str:
    labels = [_downloadable_link_label(root).rstrip("/") + "/" for root in selected_roots[:3]]
    remaining = len(selected_roots) - len(labels)
    if remaining > 0:
        labels.append(f"+ {remaining} more")
    return escape(visual_join(labels))


def _downloadable_links_from_scans(scans: Sequence[WorkItem]) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    for scan in scans:
        for url in _downloadable_links_from_scan(scan):
            if url in seen:
                continue
            seen.add(url)
            urls.append(url)
    return urls


def _select_batch_url_scan_action(
    prompts: PromptUI,
    scan: WorkItem,
) -> BatchUrlScanChoice | None:
    return cast(
        BatchUrlScanChoice | None,
        prompts.select(
            "Actions",
            _batch_url_scan_choices(scan),
        ),
    )


def _batch_url_scan_options(
    settings: AtlasSettings,
    prompts: PromptUI,
    scan: WorkItem,
    selected: BatchUrlScanChoice,
) -> tuple[MenuDownloadOptions | None, HubKind]:
    if selected == BatchUrlScanChoice.file:
        return build_file_options(settings, scan.url), HubKind.file
    if selected == BatchUrlScanChoice.offline_site:
        return _offline_site_scan_options(settings, scan.url), HubKind.site
    if selected == BatchUrlScanChoice.folder:
        folder_url = _select_discovered_folder(prompts, scan)
        if folder_url is None:
            return None, HubKind.dir
        return _directory_scan_options(settings, folder_url, depth=settings.dir_depth), HubKind.dir
    return _directory_scan_options(settings, scan.url, depth=settings.dir_depth), HubKind.dir


def _batch_url_scan_choices(scan: WorkItem) -> list[MenuChoice]:
    choices = []
    if _downloadable_links_from_scan(scan):
        choices.extend(
            [
                MenuChoice(
                    "Download discovered files",
                    BatchUrlScanChoice.direct_links,
                ),
                MenuChoice("Choose discovered files", BatchUrlScanChoice.selected_files),
            ]
        )
    choices.extend(
        [
            MenuChoice("Offline website", BatchUrlScanChoice.offline_site),
            MenuChoice("Recursive mirror", BatchUrlScanChoice.recursive),
        ]
    )
    if _folder_choices_from_scan(scan):
        choices.append(MenuChoice("Choose folder", BatchUrlScanChoice.folder))
    choices.extend(
        [
            MenuChoice("This page only", BatchUrlScanChoice.file),
            MenuChoice("Back", BatchUrlScanChoice.back),
        ]
    )
    return choices


def _offline_site_scan_options(settings: AtlasSettings, url: str) -> SiteDownloadOptions:
    return build_site_options(settings, url).model_copy(
        update={
            "domains": _default_domains_for_url(url) or settings.site_domains,
            "page_requisites": True,
            "convert_links": True,
            "adjust_extension": True,
            "no_parent": True,
            "span_hosts": False,
            "wait": 0.5,
            "random_wait": True,
            "timeout": 60.0,
            "tries": 5,
            "continue_download": True,
            "adaptive": True,
        }
    )


def _directory_scan_options(
    settings: AtlasSettings,
    url: str,
    *,
    depth: int,
) -> DirectoryMirrorOptions:
    return build_directory_options(settings, url).model_copy(
        update={
            "domains": _default_domains_for_url(url) or settings.site_domains,
            "depth": depth,
            "no_parent": True,
            "span_hosts": False,
            "wait": 0.5,
            "random_wait": True,
            "timeout": 60.0,
            "tries": 5,
            "continue_download": True,
            "adaptive": True,
        }
    )


def _select_discovered_folder(prompts: PromptUI, scan: WorkItem) -> str | None:
    choices = _folder_choices_from_scan(scan)
    choices.extend(
        [
            MenuChoice("Custom folder or URL", "custom"),
            MenuChoice("Back", None),
        ]
    )
    selected = cast(str | None, prompts.select("Choose folder", choices))
    if selected is None:
        return None
    if selected == "custom":
        value = prompts.text("Folder path or URL")
        if value is None:
            return None
        return _folder_url(scan.url, value)
    return selected


def _folder_choices_from_scan(scan: WorkItem, *, limit: int = 20) -> list[MenuChoice]:
    seed_host = _host_for_menu(scan.url)
    seen: set[str] = set()
    choices: list[MenuChoice] = []
    for link in scan.discovered_links:
        parsed = urlparse(link)
        if seed_host and parsed.hostname and parsed.hostname.lower() != seed_host:
            continue
        path = parsed.path or "/"
        folder = path if path.endswith("/") else path.rsplit("/", 1)[0] + "/"
        if folder in {"", "/"} or folder in seen:
            continue
        seen.add(folder)
        choices.append(MenuChoice(folder, _folder_url(link, folder)))
        if len(choices) >= limit:
            break
    return choices


def _select_discovered_files(prompts: PromptUI, scan: WorkItem) -> list[str] | None:
    choices = _downloadable_link_choices_from_scan(scan)
    if not choices:
        return []
    selected = prompts.multi_select("Choose discovered files", choices)
    if selected is None:
        return None
    return [str(url) for url in selected]


def _downloadable_link_choices_from_scan(scan: WorkItem, *, limit: int = 100) -> list[MenuChoice]:
    return [
        MenuChoice(_downloadable_link_label(url), url)
        for url in _downloadable_links_from_scan(scan)[:limit]
    ]


def _downloadable_links_from_directory_index(directory_index: DirectoryIndex) -> list[str]:
    return [entry.url for entry in _downloadable_entries_from_directory_index(directory_index)]


def _downloadable_entries_from_directory_index(
    directory_index: DirectoryIndex,
) -> list[DirectoryEntry]:
    entries: list[DirectoryEntry] = []
    seen: set[str] = set()
    for entry in directory_index.files:
        if not _directory_entry_is_downloadable(entry):
            continue
        if entry.url in seen:
            continue
        seen.add(entry.url)
        entries.append(entry)
    return entries


def _directory_entry_is_downloadable(entry: DirectoryEntry) -> bool:
    return not entry.parent and entry.kind == "file" and _link_looks_downloadable(entry.url)


def _directory_entry_choice_label(entry: DirectoryEntry) -> str:
    details = [
        _directory_entry_modified_label(entry),
        _directory_entry_size_label(entry),
    ]
    suffix = visual_join(detail for detail in details if detail and detail != "-")
    name = _directory_entry_name(entry)
    return f"{name}  {suffix}" if suffix else name


def _directory_entry_name(entry: DirectoryEntry) -> str:
    name = entry.name
    if entry.kind == "directory" and not name.endswith("/"):
        return f"{name}/"
    return name


def _directory_entry_modified_label(entry: DirectoryEntry) -> str:
    if entry.last_modified is None:
        return "-"
    return entry.last_modified.strftime("%Y-%m-%d")


def _directory_entry_size_label(entry: DirectoryEntry) -> str:
    if entry.kind == "directory":
        return "-"
    if entry.visible_size is None:
        return "size unknown"
    return format_bytes(entry.visible_size)


def _downloadable_link_label(url: str) -> str:
    parsed = urlparse(url)
    path = parsed.path or "/"
    label = path if path != "/" else url
    if parsed.query:
        label = f"{label}?{parsed.query}"
    return label


def _downloadable_links_from_scan(scan: WorkItem) -> list[str]:
    seed_url = scan.final_url or scan.url
    seed_host = _host_for_menu(seed_url)
    urls: list[str] = []
    seen: set[str] = set()

    def append(url: str) -> None:
        absolute = urljoin(seed_url, url)
        if absolute in seen:
            return
        if not _is_same_host_url(absolute, seed_host):
            return
        if not _link_looks_downloadable(absolute):
            return
        seen.add(absolute)
        urls.append(absolute)

    for item in scan.discovered_work_items:
        if item.error or item.external_host or not item.same_host:
            continue
        if item.kind not in {HubKind.file, HubKind.audio, HubKind.video, HubKind.manifest}:
            continue
        append(item.url)
    if not scan.discovered_work_items:
        for link in scan.discovered_links:
            append(link)
    return urls


def _is_same_host_url(url: str, seed_host: str | None) -> bool:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return False
    if parsed.hostname is None:
        return False
    return not (seed_host and parsed.hostname.lower() != seed_host)


def _link_looks_downloadable(url: str) -> bool:
    path = urlparse(url).path.lower()
    if path.endswith("/"):
        return False
    leaf = path.rsplit("/", 1)[-1]
    if not leaf:
        return False
    if leaf.endswith((".html", ".htm", ".xhtml", ".shtml")):
        return False
    return "." in leaf


def _folder_url(seed_url: str, folder: str) -> str:
    if folder.startswith(("http://", "https://")):
        return folder
    return urljoin(seed_url, folder)


def _default_domains_for_url(url: str) -> str | None:
    host = _host_for_menu(url)
    if not host:
        return None
    if host.startswith("www."):
        bare = host[4:]
        return f"{bare},{host}"
    if _looks_like_ip_or_localhost(host):
        return host
    return f"{host},www.{host}"


def _host_for_menu(url: str) -> str | None:
    host = urlparse(url).hostname
    return host.lower() if host else None


def _looks_like_ip_or_localhost(host: str) -> bool:
    return host == "localhost" or all(part.isdigit() for part in host.split(".") if part)


def _print_detected_url_card(console: Console, url: str) -> None:
    _refresh_menu_screen(console)
    detected = _detected_url_shape(url)
    title = (
        "Detected open directory"
        if "directory" in detected or "index" in detected
        else "Detected URL"
    )
    context = _menu_separator().join(
        (
            escape(_compact_url_label(url)),
            "same host",
            "scan first" if _url_should_scan_before_auto_plan(url) else "plan first",
        )
    )
    _print_screen_title(console, title, context)


def _detected_url_shape(url: str) -> str:
    path = (urlparse(url).path or "/").lower()
    leaf = path.rstrip("/").rsplit("/", 1)[-1]
    if path.endswith("/") or leaf in {"", "index", "directory"}:
        return "directory-style page / open index"
    if leaf.endswith((".html", ".htm", ".xhtml", ".shtml")):
        return "HTML page / possible directory index"
    if "." in leaf:
        return "direct file candidate"
    return "web page / unknown content"


def _url_should_scan_before_auto_plan(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return False
    path = (parsed.path or "/").lower()
    leaf = path.rstrip("/").rsplit("/", 1)[-1]
    if path.endswith("/") or leaf in {"", "index", "directory"}:
        return True
    return leaf.endswith((".html", ".htm", ".xhtml", ".shtml"))


def _print_url_scan_summary(console: Console, scan: WorkItem) -> None:
    console = ensure_atlas_theme(console)
    _refresh_menu_screen(console)
    counts = _scan_link_counts(scan)
    title = (
        "No downloadable files found"
        if counts["files"] == 0 and counts["folders"] == 0
        else "Scan complete"
    )
    _print_screen_title(console, title, escape(_compact_url_label(scan.final_url or scan.url)))
    rows: list[tuple[str, str]] = []
    if scan.scan_type:
        rows.append(("Type", escape(scan.scan_type)))
    rows.extend(
        (
            ("Host", scan.host or _host_for_menu(scan.url) or "-"),
            ("Found", f"{counts['links']:,} links"),
            ("Files", f"{counts['files']:,}"),
            ("Folders", f"{counts['folders']:,}"),
            ("HTML", f"{counts['html']:,}"),
        )
    )
    if counts.get("media"):
        rows.append(("Media", f"{counts['media']:,}"))
    if counts["external"]:
        rows.append(("Skipped", f"{counts['external']:,} external"))
    if scan.scan_estimated_bytes is not None:
        rows.append(("Size", f"~{format_bytes(scan.scan_estimated_bytes)}"))
    if scan.scan_recommended_mode:
        rows.append(("Mode", escape(scan.scan_recommended_mode)))
    if scan.scan_recommended_strategy:
        rows.append(("Plan", escape(scan.scan_recommended_strategy)))
    if scan.scan_warnings:
        rows.append(("Warnings", escape(_short_value("; ".join(scan.scan_warnings), limit=120))))
    if scan.error:
        rows.append(("Note", escape(scan.error)))
    _print_fact_rows(console, rows)
    console.print()


def _print_scan_failed(console: Console, scan: WorkItem) -> None:
    console = ensure_atlas_theme(console)
    _refresh_menu_screen(console)
    table = Table.grid(padding=(0, 2))
    table.add_column(style=ATLAS_MUTED_STYLE, no_wrap=True)
    table.add_column()
    table.add_row("URL", escape(scan.url))
    table.add_row("Host", scan.host or _host_for_menu(scan.url) or "-")
    table.add_row("Reason", escape(_scan_failure_reason(scan)))
    console.print(
        Panel(
            table,
            title=Text("Scan failed", style=ATLAS_ERROR_STYLE),
            border_style=ATLAS_ERROR_STYLE,
            box=atlas_box(),
            expand=False,
        )
    )
    console.print(
        f"[{ATLAS_MUTED_STYLE}]Atlas could not fetch the directory index safely. "
        "No discovered-file actions are available until discovery succeeds."
        f"[/{ATLAS_MUTED_STYLE}]"
    )
    console.print()
    console.print(_directory_footer())


def _print_scan_empty(console: Console, scan: WorkItem) -> None:
    _refresh_menu_screen(console)
    _print_screen_title(
        console,
        "No links found",
        escape(_compact_url_label(scan.final_url or scan.url)),
        style_name=ATLAS_WARNING_STYLE,
    )
    rows: list[tuple[str, str]] = []
    counts = _scan_link_counts(scan)
    rows.append(("Links", f"{counts['links']:,}"))
    rows.append(("Files", f"{counts['files']:,}"))
    rows.append(("Folders", f"{counts['folders']:,}"))
    if scan.content_type:
        rows.append(("Content", escape(scan.content_type)))
    rows.append(("Reason", escape(_scan_failure_reason(scan))))
    _print_fact_rows(console, rows)
    console.print(
        f"[{ATLAS_MUTED_STYLE}]Atlas fetched the page, but discovery did not find links. "
        "Discovered-file actions are hidden for this state."
        f"[/{ATLAS_MUTED_STYLE}]"
    )
    console.print()
    console.print(_directory_footer())


def _print_scan_error_details(console: Console, scan: WorkItem) -> None:
    _refresh_menu_screen(console)
    rows: list[tuple[str, str]] = []
    if scan.error:
        rows.append(("Error", escape(scan.error)))
    for index, error in enumerate(scan.scan_errors, start=1):
        code = error.get("code", "unknown")
        message = error.get("message", "")
        url = error.get("url", scan.url)
        rows.append(
            (f"Error {index}", visual_join((escape(str(code)), escape(str(message)))))
        )
        rows.append(("URL", escape(str(url))))
    if not scan.error and not scan.scan_errors:
        rows.append(("Details", "No additional scanner details were recorded."))
    _print_screen_title(console, "Error details", style_name=ATLAS_ERROR_STYLE)
    _print_fact_rows(console, rows)
    console.print()
    console.print(_directory_footer())


def _scan_failure_reason(scan: WorkItem) -> str:
    if scan.scan_errors:
        message = scan.scan_errors[0].get("message")
        if message:
            return str(message)
        code = scan.scan_errors[0].get("code")
        if code:
            return str(code).replace("_", " ")
    return scan.error or "scan failed"


def _scan_link_counts(scan: WorkItem) -> dict[str, int]:
    if scan.scan_counts:
        return {
            "links": scan.scan_counts.get("links", len(scan.discovered_links)),
            "folders": scan.scan_counts.get("folders", 0),
            "html": scan.scan_counts.get("html", 0),
            "files": scan.scan_counts.get("files", 0),
            "media": scan.scan_counts.get("media", 0),
            "external": scan.scan_counts.get("external", 0),
        }
    seed_host = _host_for_menu(scan.url)
    folders: set[str] = set()
    html = 0
    files = 0
    media = 0
    external = 0
    for link in scan.discovered_links:
        parsed = urlparse(link)
        if seed_host and parsed.hostname and parsed.hostname.lower() != seed_host:
            external += 1
            continue
        path = parsed.path.lower()
        if path.endswith("/"):
            folders.add(path)
        elif path.endswith((".html", ".htm")):
            html += 1
        elif path.endswith(
            (".mp3", ".flac", ".ogg", ".opus", ".wav", ".m4a", ".mp4", ".mkv", ".webm")
        ):
            media += 1
            files += 1
        else:
            files += 1
            folder = parsed.path.rsplit("/", 1)[0] + "/"
            if folder != "/":
                folders.add(folder)
    return {
        "links": len(scan.discovered_links),
        "folders": len(folders),
        "html": html,
        "files": files,
        "media": media,
        "external": external,
    }


def _config_flow(actions: MenuActions, prompts: PromptUI) -> FlowResult:
    selected = cast(
        str | None,
        prompts.select(
            "Config",
            [
                MenuChoice("View settings", "show"),
                MenuChoice("Config file", "path"),
                MenuChoice("Open config file", "open"),
                MenuChoice("Back", "back"),
                MenuChoice("Quit", "quit"),
            ],
        ),
    )
    if selected is None or selected == "back":
        return FlowResult.back
    if selected == "quit":
        return FlowResult.quit
    if selected == "path":
        actions.show_config_path()
    elif selected == "open":
        actions.open_config_file()
    else:
        actions.show_config()
    return _post_simple_action(prompts)


def _post_simple_action(prompts: PromptUI) -> FlowResult:
    selected = cast(
        str | None,
        prompts.select(
            "Next",
            [
                MenuChoice("Menu", "back"),
                MenuChoice("Quit", "quit"),
            ],
        ),
    )
    return FlowResult.quit if selected == "quit" else FlowResult.back


def _view_only_back(prompts: PromptUI, *, message: str = "Back") -> None:
    prompts.select(message, [MenuChoice("Back", "back")])


def _parse_pasted_urls(value: str) -> list[str]:
    urls: list[str] = []
    for line in value.replace(",", "\n").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        try:
            parts = shlex.split(stripped)
        except ValueError:
            parts = stripped.split()
        for part in parts:
            if part.startswith(("http://", "https://")):
                urls.append(part)
    return urls


def _write_menu_batch_file(output_dir: Path, urls: Sequence[str]) -> Path:
    batch_dir = output_dir.expanduser() / ".atlas" / "menu"
    try:
        ensure_private_directory(batch_dir)
        path = batch_dir / f"pasted-urls-{uuid4().hex}.txt"
        write_private_text(path, "\n".join(urls) + "\n")
        return path
    except OSError as exc:
        raise AtlasError(f"Could not create private menu batch file: {exc}") from exc


def _print_batch_plan(
    console: Console,
    file: Path,
    kind: BatchKind,
    concurrency: int | None,
    allow_sites: bool,
    allow_dirs: bool,
    video_codec: VideoCodecChoice,
    audio_codec: AudioCodec,
    audio_quality: int,
) -> None:
    table = Table.grid(padding=(0, 2))
    table.add_column(style=ATLAS_MUTED_STYLE, no_wrap=True)
    table.add_column()
    source = f"[{ATLAS_PATH_STYLE}]{escape(str(file.expanduser()))}[/{ATLAS_PATH_STYLE}]"
    table.add_row("Source", source)
    table.add_row("Kind", kind.value)
    table.add_row("Mode", "adaptive queue" if kind == BatchKind.auto else "policy batch")
    table.add_row("Concurrency", str(concurrency or 1))
    table.add_row("Backends", visual_join(("yt-dlp", "aria2c", "native", "wget2")))
    table.add_row("Video codec", video_codec.value)
    table.add_row("Audio codec", audio_codec.value)
    table.add_row("Audio quality", str(audio_quality))
    table.add_row("Sites", "allowed" if allow_sites else "skipped unless explicit")
    table.add_row("Directories", "allowed" if allow_dirs else "skipped unless explicit")
    table.add_row("Scheduler", "queue concurrency + backend-specific per-item workers")
    console.print(
        Panel(
            table,
            title=Text("atlas Batch Plan", style=ATLAS_TITLE_STYLE),
            border_style=ATLAS_PANEL_STYLE,
            box=atlas_box(),
            expand=False,
        )
    )


def _reveal_path(path: Path) -> None:
    if shutil.which("open"):
        subprocess.run(["open", "-R", str(path)], check=False)


def _open_path(path: Path) -> None:
    if shutil.which("open"):
        subprocess.run(["open", str(path)], check=False)
